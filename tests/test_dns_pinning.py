"""DNS çözümü + IP pinləmə: hostname-ə əsaslanan yoxlama YETƏRLİ DEYİL (`.lan` ictimai IP-yə yönələ bilər, DNS qoşulma anında dəyişə bilər).

Nəqliyyat (`runtime/transport.UrllibTransport(kind=...)`) HƏR sorğuda hostname-in BÜTÜN A/AAAA nəticələrini yoxlayır, YALNIZ yoxlanmış IP literalına qoşulur (ikinci DNS sorğusu yoxdur),
TLS-də sertifikatı və SNI-ni ORİJİNAL hostname ilə yoxlayır və 3xx yönləndirmələri izləmir. Testlər həqiqi soketlərdən (loopback HTTP/HTTPS serverləri) və DNS təqlidindən istifadə edir."""
from __future__ import annotations

import ast
import datetime
import ipaddress
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agents.llm import LLMProviderError, LLMRequest
from agents.providers import (EndpointError, HttpRequest, OpenAICompatibleProvider, TransportError, check_resolved_addresses, ip_allowed)
from runtime import transport as transport_mod
from runtime.env import build_engines
from runtime.transport import UrllibTransport

ROOT = Path(__file__).resolve().parent.parent
PUBLIC4, PUBLIC6 = "93.184.216.34", "2606:4700:4700::1111"
PRIVATE_OK = ["127.0.0.1", "127.5.5.5", "10.1.2.3", "172.16.0.9", "172.31.255.255", "192.168.1.20", "::1", "fd00::1", "fc00::5", "::ffff:127.0.0.1", "::ffff:192.168.0.4"]
PRIVATE_BAD = ["8.8.8.8", PUBLIC4, PUBLIC6, "169.254.169.254", "169.254.1.1", "fe80::1", "ff02::1", "224.0.0.1", "0.0.0.0", "::", "100.64.0.1", "172.32.0.1", "192.0.2.1",
               "255.255.255.255", "::ffff:8.8.8.8", "::ffff:169.254.169.254", "2001:db8::1", "64:ff9b::808:808"]
CLOUD_OK = ["8.8.8.8", PUBLIC4, PUBLIC6, "::ffff:8.8.8.8"]
CLOUD_BAD = ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "fe80::1", "::1", "fd00::1", "ff02::1", "224.0.0.1", "0.0.0.0", "::", "100.64.0.1", "::ffff:127.0.0.1"]


class ScriptedDNS:
    """DNS təqlidi: hər çağırışda sıradakı cavabı qaytarır (rebinding simulyasiyası). `calls` çağırış sayını/adları saxlayır."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, host, port):
        self.calls.append((host, port))
        i = min(len(self.calls) - 1, len(self.answers) - 1)
        a = self.answers[i]
        if isinstance(a, Exception):
            raise a
        return list(a)


class Recorder:
    """Soket açmayan `connector`: hansı IP-yə qoşulmaq istədiyini qeyd edir."""

    def __init__(self):
        self.connects = []

    def __call__(self, ip, port, timeout):
        self.connects.append((ip, port))
        raise ConnectionRefusedError()


def req(url, method="POST", body=b"{}"):
    return HttpRequest(method, url, {"Content-Type": "application/json"}, body, 3.0)


# ============================================================================ siyasət cədvəli (IPv4 və IPv6)

@pytest.mark.parametrize("ip", PRIVATE_OK)
def test_private_kind_accepts_only_loopback_and_private_addresses(ip):
    assert ip_allowed(ipaddress.ip_address(ip), "private") is True


@pytest.mark.parametrize("ip", PRIVATE_BAD)
def test_private_kind_rejects_public_link_local_multicast_unspecified_cgnat_and_reserved(ip):
    assert ip_allowed(ipaddress.ip_address(ip), "private") is False


@pytest.mark.parametrize("ip", CLOUD_OK)
def test_cloud_kind_accepts_only_global_unicast(ip):
    assert ip_allowed(ipaddress.ip_address(ip), "cloud") is True


@pytest.mark.parametrize("ip", CLOUD_BAD)
def test_cloud_kind_rejects_private_loopback_link_local_multicast_unspecified_and_cgnat(ip):
    assert ip_allowed(ipaddress.ip_address(ip), "cloud") is False


def test_every_resolved_address_must_pass_and_empty_or_garbage_fails_closed():
    assert check_resolved_addresses("nas.lan", ["192.168.1.5", "fd00::5"], "private") == ["192.168.1.5", "fd00::5"]
    for answers in (["192.168.1.5", PUBLIC4], [PUBLIC4, "192.168.1.5"], ["fd00::5", PUBLIC6], ["::1", "8.8.8.8", "127.0.0.1"], [], ["not-an-ip"], ["fe80::1%eth0"]):
        with pytest.raises(EndpointError):
            check_resolved_addresses("nas.lan", answers, "private")
    with pytest.raises(EndpointError):
        check_resolved_addresses("api.example.com", [PUBLIC4, "10.0.0.5"], "cloud")


# ============================================================================ nəqliyyat: DNS çözümü + pin (soketsiz)

def pinned(kind, *answers, **kw):
    dns, rec = ScriptedDNS(*answers), Recorder()
    return UrllibTransport(kind=kind, resolver=dns, connector=rec, **kw), dns, rec


def test_a_lan_name_pointing_at_a_public_ip_is_rejected_before_any_connection():
    t, dns, rec = pinned("private", [PUBLIC4])
    with pytest.raises(TransportError, match="endpoint policy"):
        t(req("http://nas.lan:11434/v1/chat/completions"))
    assert rec.connects == [] and dns.calls == [("nas.lan", 11434)]


@pytest.mark.parametrize("answers", [[PUBLIC4, "192.168.1.5"], ["192.168.1.5", PUBLIC4], [PUBLIC6, "fd00::1"], ["fd00::1", PUBLIC6], ["192.168.1.5", PUBLIC6],
                                     ["127.0.0.1", "169.254.169.254"], ["10.0.0.4", "fe80::1"], ["::1", "ff02::1"]])
def test_mixed_private_and_unsafe_answers_fail_closed_in_any_order(answers):
    t, dns, rec = pinned("private", answers)
    with pytest.raises(TransportError):
        t(req("http://box.lan:8000/x"))
    assert rec.connects == []


@pytest.mark.parametrize("answers,expected", [(["192.168.1.20"], "192.168.1.20"), (["10.0.0.7", "10.0.0.8"], "10.0.0.7"), (["fd00::7"], "fd00::7"), (["::1"], "::1"),
                                              (["127.0.0.1", "::1"], "127.0.0.1")])
def test_all_private_ipv4_and_ipv6_answers_are_accepted_and_the_first_validated_ip_is_pinned(answers, expected):
    t, dns, rec = pinned("private", answers)
    with pytest.raises(TransportError, match="connection failed"):                      # Recorder bağlantını rədd edir (soket yoxdur)
        t(req("http://llama.lan:8080/v1/chat/completions"))
    assert rec.connects[0] == (expected, 8080) and len(dns.calls) == 1


@pytest.mark.parametrize("bad", ["169.254.169.254", "0.0.0.0", "224.0.0.1", "fe80::1", "ff02::1", "::", "100.64.0.1"])
def test_link_local_multicast_unspecified_and_cgnat_are_rejected_for_local_runtimes(bad):
    t, dns, rec = pinned("private", [bad])
    with pytest.raises(TransportError, match="endpoint policy"):
        t(req("http://srv.local:8000/x"))
    assert rec.connects == []


def test_dns_answer_changing_between_validation_and_connection_cannot_redirect_the_connection():
    t, dns, rec = pinned("private", ["192.168.1.20"], [PUBLIC4])                        # 1-ci cavab özəl, 2-ci (rebinding) ictimai
    with pytest.raises(TransportError):
        t(req("http://rebind.lan:9000/x"))
    assert dns.calls == [("rebind.lan", 9000)]                                            # YALNIZ bir DNS sorğusu: bağlantı üçün ikinci çözüm YOXDUR
    assert rec.connects == [("192.168.1.20", 9000)]                                       # qoşulma yoxlanmış IP-yə, ictimai IP-yə YOX


def test_dns_rebinding_across_requests_is_caught_because_every_request_is_re_resolved_and_re_validated():
    t, dns, rec = pinned("private", ["127.0.0.1"], [PUBLIC4], ["127.0.0.1"])
    with pytest.raises(TransportError, match="connection failed"):
        t(req("http://ollama.local:11434/a"))
    with pytest.raises(TransportError, match="endpoint policy"):                          # 2-ci sorğu: ad artıq ictimai IP-yə çözülür
        t(req("http://ollama.local:11434/b"))
    with pytest.raises(TransportError, match="connection failed"):
        t(req("http://ollama.local:11434/c"))
    assert rec.connects == [("127.0.0.1", 11434), ("127.0.0.1", 11434)] and len(dns.calls) == 3


@pytest.mark.parametrize("answers", [[], OSError("nxdomain"), [""]])
def test_resolution_failures_fail_closed_without_connecting(answers):
    t, dns, rec = pinned("private", answers)
    with pytest.raises(TransportError):
        t(req("http://gone.lan:8000/x"))
    assert rec.connects == []


@pytest.mark.parametrize("bad", ["10.0.0.5", "127.0.0.1", "192.168.1.1", "169.254.169.254", "fe80::1", "::1", "fd00::1", "224.0.0.1", "0.0.0.0", "100.64.0.1"])
def test_cloud_endpoint_resolving_to_a_private_loopback_or_link_local_address_is_rejected(bad):
    t, dns, rec = pinned("cloud", [bad])
    with pytest.raises(TransportError, match="endpoint policy"):
        t(req("https://api.example.com/v1/chat/completions"))
    assert rec.connects == []


@pytest.mark.parametrize("answers", [[PUBLIC4, "10.0.0.5"], ["10.0.0.5", PUBLIC4], [PUBLIC6, "::1"], [PUBLIC4, PUBLIC6, "169.254.169.254"]])
def test_cloud_endpoint_with_mixed_public_and_private_answers_fails_closed(answers):
    t, dns, rec = pinned("cloud", answers)
    with pytest.raises(TransportError, match="endpoint policy"):
        t(req("https://api.example.com/v1/chat/completions"))
    assert rec.connects == []


@pytest.mark.parametrize("answers,first", [([PUBLIC4], PUBLIC4), ([PUBLIC6], PUBLIC6), ([PUBLIC6, PUBLIC4], PUBLIC6)])
def test_cloud_endpoint_resolving_to_public_ipv4_and_ipv6_is_pinned_to_a_validated_address(answers, first):
    t, dns, rec = pinned("cloud", answers)
    with pytest.raises(TransportError):
        t(req("https://api.example.com/v1/chat/completions"))
    assert rec.connects[0] == (first, 443) and len(dns.calls) == 1


def test_cloud_dns_rebinding_to_a_private_address_is_rejected_on_the_next_request():
    t, dns, rec = pinned("cloud", [PUBLIC4], ["10.0.0.5"])
    with pytest.raises(TransportError, match="connection failed"):
        t(req("https://api.example.com/v1/chat/completions"))
    with pytest.raises(TransportError, match="endpoint policy"):
        t(req("https://api.example.com/v1/chat/completions"))
    assert rec.connects == [(PUBLIC4, 443)]


def test_the_default_connector_rechecks_the_actual_peer_address(monkeypatch):
    class FakeSock:
        closed = False

        def getpeername(self):
            return ("203.0.113.9", 80)                                                    # faktiki qarşı tərəf yoxlanmış IP-dən FƏRQLİDİR

        def close(self):
            self.closed = True
    sock = FakeSock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: sock)
    connect = transport_mod._pinned_connector("private")
    with pytest.raises(TransportError, match="endpoint policy"):
        connect("192.168.1.20", 80, 1.0)
    assert sock.closed is True


# ============================================================================ həqiqi soketlər: loopback serverlər

class _Handler(BaseHTTPRequestHandler):
    seen: list = []
    mode = "ok"

    def log_message(self, *a):  # noqa: ANN002
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        type(self).seen.append((self.path, self.headers.get("Host"), self.client_address[0]))
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/secret")
            self.end_headers()
            return
        body = json.dumps({"choices": [{"message": {"content": "lokal cavab"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}, "model": "m"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    _Handler.seen, _Handler.mode = [], "ok"
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_a_real_loopback_server_behind_a_private_name_works_and_dns_is_consulted_exactly_once(server):
    dns = ScriptedDNS(["127.0.0.1"])
    prov = OpenAICompatibleProvider(f"http://ollama.local:{server}", "small", None, UrllibTransport(kind="private", resolver=dns), kind="private")
    res = prov.complete(LLMRequest("", "salam", 8, 3.0))
    assert res.text == "lokal cavab" and len(dns.calls) == 1
    assert _Handler.seen[0][1] == f"ollama.local:{server}" and _Handler.seen[0][2] == "127.0.0.1"          # Host başlığı ORİJİNAL ad


def test_a_real_loopback_server_addressed_by_ip_literal_and_localhost_works_with_the_system_resolver(server):
    for host in ("127.0.0.1", "localhost"):
        prov = OpenAICompatibleProvider(f"http://{host}:{server}", "small", None, UrllibTransport(kind="private"), kind="private")
        assert prov.complete(LLMRequest("", "q", 8, 3.0)).text == "lokal cavab"
    assert len(_Handler.seen) == 2


def test_a_name_resolving_to_loopback_and_a_public_ip_never_reaches_the_server(server):
    dns = ScriptedDNS(["127.0.0.1", "8.8.8.8"])
    t = UrllibTransport(kind="private", resolver=dns)
    with pytest.raises(TransportError, match="endpoint policy"):
        t(req(f"http://mixed.lan:{server}/v1/chat/completions"))
    assert _Handler.seen == []


def test_the_pinned_connection_cannot_be_diverted_by_a_second_dns_answer(server):
    dns = ScriptedDNS(["127.0.0.1"], [PUBLIC4])                                          # rebinding: 2-ci cavab ictimai olardı
    t = UrllibTransport(kind="private", resolver=dns)
    resp = t(req(f"http://rebind.lan:{server}/v1/chat/completions"))
    assert resp.status == 200 and len(dns.calls) == 1 and len(_Handler.seen) == 1


def test_ipv6_loopback_server_works_when_the_host_supports_it():
    try:
        srv = HTTPServer.__new__(HTTPServer)
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("bu mühitdə IPv6 loopback (::1) yoxdur: IPv6 siyasəti yuxarıdakı cədvəl/pin testləri ilə yoxlanılır")

    class V6Server(HTTPServer):
        address_family = socket.AF_INET6
    _Handler.seen, _Handler.mode = [], "ok"
    srv = V6Server(("::1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        dns = ScriptedDNS(["::1"])
        t = UrllibTransport(kind="private", resolver=dns)
        assert t(req(f"http://v6.local:{port}/v1/chat/completions")).status == 200
        assert _Handler.seen[0][2] == "::1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_redirects_are_never_followed_and_the_provider_treats_3xx_as_an_error(server):
    _Handler.mode = "redirect"                                                           # 302 -> http://127.0.0.1:1/secret
    t = UrllibTransport(kind="private", resolver=ScriptedDNS(["127.0.0.1"]))
    resp = t(HttpRequest("POST", f"http://redir.lan:{server}/v1/chat/completions", {}, b"{}", 3.0))
    assert resp.status == 302 and len(_Handler.seen) == 1                                 # olduğu kimi qaytarıldı, İKİNCİ bağlantı YOX
    prov = OpenAICompatibleProvider(f"http://localhost:{server}", "m", None, UrllibTransport(kind="private"), kind="private")
    with pytest.raises(LLMProviderError):
        prov.complete(LLMRequest("", "q", 8, 3.0))
    assert len(_Handler.seen) == 2


# ============================================================================ TLS: sertifikat + SNI orijinal hostname ilə

def make_pki(tmp_path, dns_name):
    def key():
        return ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False).sign(ca_key, hashes.SHA256()))
    srv_key = key()
    srv = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])).issuer_name(ca_name)
           .public_key(srv_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
           .not_valid_after(now + datetime.timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
           .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False,
                                        key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
           .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
           .add_extension(x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False)
           .sign(ca_key, hashes.SHA256()))
    ca_pem, cert_pem, key_pem = tmp_path / "ca.pem", tmp_path / "srv.pem", tmp_path / "srv.key"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(srv.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(srv_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_pem, cert_pem, key_pem


@pytest.fixture
def tls_server(tmp_path):
    ca_pem, cert_pem, key_pem = make_pki(tmp_path, "llm.example.test")
    sni = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_pem, key_pem)
    ctx.sni_callback = lambda sock, name, _ctx: sni.append(name)
    _Handler.seen, _Handler.mode = [], "ok"
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield types_ns(port=srv.server_address[1], ca=ca_pem, sni=sni)
    srv.shutdown()
    srv.server_close()


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def trusting(ca_pem):
    return ssl.create_default_context(cafile=str(ca_pem))


def test_tls_verifies_the_certificate_against_the_original_hostname_and_sends_that_hostname_as_sni(tls_server):
    t = UrllibTransport(kind="cloud", resolver=ScriptedDNS([PUBLIC4]), ssl_context=trusting(tls_server.ca),
                        connector=lambda ip, port, timeout: socket.create_connection(("127.0.0.1", tls_server.port), timeout))
    resp = t(HttpRequest("POST", "https://llm.example.test/v1/chat/completions", {}, b"{}", 5.0))
    assert resp.status == 200 and tls_server.sni == ["llm.example.test"]                  # SNI = orijinal hostname (IP deyil)
    assert _Handler.seen[0][1] == "llm.example.test"


def test_cloud_transport_does_not_follow_a_redirect_to_a_private_target_over_tls(tls_server):
    _Handler.mode = "redirect"                                                           # 302 -> http://127.0.0.1:1/secret (özəl/loopback hədəf)
    t = UrllibTransport(kind="cloud", resolver=ScriptedDNS([PUBLIC4]), ssl_context=trusting(tls_server.ca),
                        connector=lambda ip, port, timeout: socket.create_connection(("127.0.0.1", tls_server.port), timeout))
    resp = t(HttpRequest("POST", "https://llm.example.test/v1/chat/completions", {}, b"{}", 5.0))
    assert resp.status == 302 and len(_Handler.seen) == 1                                 # yönləndirmə izlənmədi: endpoint siyasəti keçilə bilməz


def test_a_certificate_for_another_name_is_rejected_even_when_the_pinned_ip_is_correct(tls_server):
    t = UrllibTransport(kind="cloud", resolver=ScriptedDNS([PUBLIC4]), ssl_context=trusting(tls_server.ca),
                        connector=lambda ip, port, timeout: socket.create_connection(("127.0.0.1", tls_server.port), timeout))
    with pytest.raises(TransportError, match="tls verification failed"):
        t(HttpRequest("POST", "https://other.example.test/v1/chat/completions", {}, b"{}", 5.0))
    assert _Handler.seen == []


def test_an_untrusted_certificate_chain_is_rejected_with_the_default_context(tls_server):
    t = UrllibTransport(kind="cloud", resolver=ScriptedDNS([PUBLIC4]),
                        connector=lambda ip, port, timeout: socket.create_connection(("127.0.0.1", tls_server.port), timeout))      # sistem CA-ları: test CA-sı yoxdur
    with pytest.raises(TransportError, match="tls verification failed"):
        t(HttpRequest("POST", "https://llm.example.test/v1/chat/completions", {}, b"{}", 5.0))
    assert _Handler.seen == []


def test_private_https_servers_get_the_same_certificate_and_sni_checks(tls_server):
    # sertifikat adı ictimai-görünüşlüdür (llm.example.test): `private` nəqliyyat onu ad səviyyəsində rədd edir; seam (kind=None) ilə yalnız TLS/SNI davranışı yoxlanılır
    strict = UrllibTransport(kind="private", resolver=ScriptedDNS(["127.0.0.1"]), ssl_context=trusting(tls_server.ca))
    with pytest.raises(TransportError, match="endpoint policy"):
        strict(HttpRequest("POST", f"https://llm.example.test:{tls_server.port}/v1/chat/completions", {}, b"{}", 5.0))
    tls_only = UrllibTransport(kind=None, resolver=ScriptedDNS(["127.0.0.1"]), ssl_context=trusting(tls_server.ca))
    assert tls_only(HttpRequest("POST", f"https://llm.example.test:{tls_server.port}/v1/chat/completions", {}, b"{}", 5.0)).status == 200
    assert tls_server.sni[-1] == "llm.example.test"
    wrong = UrllibTransport(kind=None, resolver=ScriptedDNS(["127.0.0.1"]), ssl_context=trusting(tls_server.ca))
    with pytest.raises(TransportError, match="tls verification failed"):
        wrong(HttpRequest("POST", f"https://not-the-cert-name.test:{tls_server.port}/x", {}, b"{}", 5.0))


# ============================================================================ istehsal yığılması

def test_production_transports_are_built_without_test_seams_and_with_the_pinning_policy(tmp_path):
    eng = build_engines({"NAYMERA_LOCAL_AI_URL": "http://127.0.0.1:11434", "NAYMERA_LOCAL_AI_MODEL": "m"}, log_dir=tmp_path, stop_path=tmp_path / "STOP")
    t = eng.local.runtime.provider._transport
    assert type(t) is UrllibTransport and t.kind == "private"
    assert t._resolver is transport_mod._system_resolver and t._ssl.verify_mode == ssl.CERT_REQUIRED and t._ssl.check_hostname is True
    offenders = []
    for d in ("runtime", "tools", "api", "agents", "safety"):
        for p in (ROOT / d).rglob("*.py"):
            if p.relative_to(ROOT).as_posix() == "runtime/transport.py":
                continue
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "UrllibTransport":
                    if any(k.arg in ("resolver", "connector", "ssl_context") for k in node.keywords):
                        offenders.append(p.relative_to(ROOT).as_posix())
    assert offenders == [], offenders


def test_a_lan_name_resolving_to_a_public_ip_never_opens_a_socket_through_the_production_local_engine(tmp_path, monkeypatch):
    connects = []
    monkeypatch.setattr(transport_mod, "_system_resolver", lambda host, port: [PUBLIC4])           # DNS: .lan -> ictimai IP
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connects.append(a) or (_ for _ in ()).throw(AssertionError("qoşulma olmamalıdır")))
    eng = build_engines({"NAYMERA_LOCAL_AI_URL": "http://nas.lan:11434", "NAYMERA_LOCAL_AI_MODEL": "m"}, log_dir=tmp_path, stop_path=tmp_path / "STOP")
    assert eng.local.status().state == "ready"                                           # ad səviyyəsində yoxlama keçir...
    from agents.hybrid import EngineUnavailable
    with pytest.raises(EngineUnavailable):                                               # ...lakin DNS səviyyəsində bağlantı rədd edilir
        eng.local.generate("salam", "", "rid-dns")
    assert connects == []
