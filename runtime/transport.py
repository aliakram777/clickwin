"""Real HTTP nəqliyyatı: DNS ÇÖZÜMÜ + IP PİNLƏMƏ, yönləndirmə YOX, cavab ölçüsü limiti, vaxt aşımı. Başlıqlar və URL xəta mətninə yazılmır.

`kind` ("cloud" | "private") verilibsə HƏR sorğuda: (1) endpoint adı `validate_endpoint` ilə yoxlanılır; (2) hostname-in BÜTÜN A/AAAA nəticələri çözülür və `check_resolved_addresses` ilə yoxlanılır
(`private`: yalnız loopback/RFC1918/ULA; `cloud`: yalnız qlobal ictimai ünvan; qarışıq nəticə = rədd); (3) bağlantı YALNIZ yoxlanmış IP LİTERALINA qurulur (ikinci DNS sorğusu YOXDUR: rebinding/TOCTOU bağlanır)
və qoşulduqdan sonra `getpeername()` yenidən yoxlanılır; (4) HTTPS-də sertifikat + hostname yoxlaması və SNI ORİJİNAL hostname ilə aparılır (IP-yə deyil); (5) 3xx cavablar İZLƏNMİR (status olduğu kimi qaytarılır).
`kind=None` yoxlamasızdır (yalnız loopback testləri üçün). `resolver`/`connector`/`ssl_context` parametrləri TEST seam-idir: istehsal kodu (`runtime/env.py`) onları ötürmür (AST testi).
DÜRÜST QEYD: bu proses-daxili nəqliyyat siyasətidir; DNS cavabı sorğu anında yoxlanılır (DNSSEC YOX); yoxlanmış IP-dən sonrakı şəbəkə marşrutu operatordadır."""
from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse
from typing import Callable, Sequence

from agents.providers import (EndpointError, HttpRequest, HttpResponse, TransportError, check_resolved_addresses, ip_allowed,
                              register_endpoint_enforcing_transport, validate_endpoint)
import ipaddress

Resolver = Callable[[str, int], Sequence[str]]
Connector = Callable[[str, int, float], socket.socket]


def _system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)           # A + AAAA (hamısı)
    return [str(i[4][0]) for i in infos]


def _pinned_connector(kind: str | None) -> Connector:
    def connect(ip: str, port: int, timeout: float) -> socket.socket:
        sock = socket.create_connection((ip, port), timeout)                   # IP LİTERALI: DNS sorğusu yoxdur
        if kind is not None:                                                    # faktiki qarşı tərəf yoxlanmış IP ilə EYNİ olmalıdır (və siyasətə uyğun)
            peer = sock.getpeername()[0].split("%", 1)[0]
            if ipaddress.ip_address(peer) != ipaddress.ip_address(ip) or not ip_allowed(ipaddress.ip_address(peer), kind):
                sock.close()
                raise TransportError("endpoint policy violation")
        return sock
    return connect


@register_endpoint_enforcing_transport
class UrllibTransport:
    """(Ad geriyə uyğunluq üçün saxlanılıb.) Bax: modul sənədi."""

    def __init__(self, max_bytes: int = 1_000_000, kind: str | None = None, *, resolver: Resolver | None = None, connector: Connector | None = None,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        if kind not in (None, "cloud", "private"):
            raise ValueError("kind cloud|private olmalıdır")
        self.max_bytes, self.kind = max_bytes, kind
        self._resolver = resolver or _system_resolver
        self._connect = connector or _pinned_connector(kind)
        self._ssl = ssl_context if ssl_context is not None else ssl.create_default_context()      # sertifikat + hostname yoxlaması AÇIQDIR

    def __call__(self, r: HttpRequest) -> HttpResponse:
        if self.kind is not None:
            try:
                validate_endpoint(r.url, self.kind)
            except EndpointError:
                raise TransportError("endpoint policy violation") from None
        u = urllib.parse.urlsplit(r.url)
        host, tls = u.hostname or "", u.scheme == "https"
        port = u.port or (443 if tls else 80)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        addresses = self._addresses(host, port)
        sock = self._open(addresses, port, float(r.timeout))
        try:
            if tls:
                sock = self._ssl.wrap_socket(sock, server_hostname=host)        # SNI + sertifikat hostname-i ORİJİNAL ad (IP deyil)
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=r.timeout, context=self._ssl)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=r.timeout)
            conn.sock = sock
            sock.settimeout(r.timeout)
            conn.request(r.method, path, body=r.body, headers=dict(r.headers))
            resp = conn.getresponse()
            body = self._read(resp)
            return HttpResponse(resp.status, body)                              # 3xx də daxil: yönləndirmə izlənmir
        except TransportError:
            raise
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout", timeout=True) from None
        except ssl.SSLError:
            raise TransportError("tls verification failed") from None
        except (http.client.HTTPException, OSError):
            raise TransportError("connection failed") from None
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _addresses(self, host: str, port: int) -> list[str]:
        try:
            resolved = list(self._resolver(host, port))
        except (OSError, EndpointError):
            raise TransportError("name resolution failed") from None
        if self.kind is None:
            return [str(a) for a in resolved] or _fail()
        try:
            return check_resolved_addresses(host, resolved, self.kind)          # BÜTÜN nəticələr: biri icazəsizsə rədd
        except EndpointError:
            raise TransportError("endpoint policy violation") from None

    def _open(self, addresses: list[str], port: int, timeout: float) -> socket.socket:
        last: Exception | None = None
        for ip in addresses:                                                    # hamısı yoxlanıb; birincidən başlayaraq
            try:
                return self._connect(ip, port, timeout)
            except TransportError:
                raise
            except (socket.timeout, TimeoutError) as exc:
                last = exc
            except OSError as exc:
                last = exc
        if isinstance(last, (socket.timeout, TimeoutError)):
            raise TransportError("timeout", timeout=True) from None
        raise TransportError("connection failed") from None

    def _read(self, resp: http.client.HTTPResponse) -> bytes:
        data = resp.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise TransportError("response too large")
        return data


def _fail() -> list[str]:
    raise TransportError("name resolution failed")
