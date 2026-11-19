"""Android <-> backend inteqrasiya testləri: HƏQİQİ HTTP (yalnız loopback, müvəqqəti port) üzərində, Android ApiClient-in
etdiyi eyni sorğu ardıcıllığı ilə. DİQQƏT: müştəri Python istinad müştərisidir (Kotlin kodu burada icra olunmur; build edilməyib)."""
import base64
import http.client
import json
import socket
import sqlite3
import threading
import time

import pytest

from api.auth import AuthService
from api.bootstrap import build_app
from api.server import check_bind, create_server
from tests.p256_tools import Keypair


def b64(b):
    return base64.b64encode(b).decode()


class Reference:
    """Android ApiClient davranışının istinad nüsxəsi (Authorization başlığı, JSON, 401 -> yeniləmə)."""

    def __init__(self, port, timeout=5):
        self.port, self.timeout = port, timeout
        self.kp = Keypair()  # Keystore simulyasiyası
        self.access = self.refresh = ""

    def request(self, method, path, body=None, token="own", raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        h = {"Content-Type": "application/json"}
        t = self.access if token == "own" else token
        if t:
            h["Authorization"] = f"Bearer {t}"
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None))
        try:
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            payload = r.read()
            return r.status, (json.loads(payload) if payload else None), dict(r.getheaders())
        finally:
            conn.close()

    def pair(self, code, name="Pixel 8"):
        st, body, _ = self.request("POST", "/v1/pair", {
            "code": code, "device_name": name, "public_key": b64(self.kp.spki()),
            "signature": b64(self.kp.sign(AuthService.pair_message(code)))}, token=None)
        if st == 200:
            self.access, self.refresh = body["access_token"], body["refresh_token"]
        return st, body

    def sign(self, ch, action, subject, phash, decision):
        return b64(self.kp.sign(AuthService.step_up_message(ch["challenge_id"], ch["nonce"], action, subject, phash, decision)))


@pytest.fixture
def live(tmp_path, monkeypatch):
    # Test iplik modeli: server ayrı iplikdə, sorğular ardıcıldır (paralel giriş yoxdur) -> SQLite iplik yoxlaması söndürülür.
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, **{**k, "check_same_thread": False}))
    svc = build_app(tmp_path)
    httpd = create_server(svc, "127.0.0.1", 0, request_timeout=1)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    client = Reference(port)
    yield svc, client, httpd
    httpd.shutdown()
    httpd.server_close()


def paired(live):
    svc, client, _ = live
    assert client.pair(svc.auth.create_pairing_code())[0] == 200
    return svc, client


def test_full_android_flow_over_real_http(live):
    svc, c = paired(live)
    assert c.request("GET", "/v1/me")[1]["owner_id"] == "owner"
    st, chat, hdr = c.request("POST", "/v1/chat/messages", {"text": "status"})
    assert st == 200 and chat["assistant_message"]["provenance"] == "rules" and hdr["Cache-Control"] == "no-store"
    cid = chat["conversation_id"]
    c.request("POST", "/v1/chat/messages", {"text": "kömək", "conversation_id": cid})
    hist = c.request("GET", "/v1/chat/conversations")[1]["conversations"]
    assert [x["id"] for x in hist] == [cid] and hist[0]["message_count"] == 4
    msgs = c.request("GET", f"/v1/chat/conversations/{cid}/messages")[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    caps = c.request("GET", "/v1/capabilities")[1]
    assert caps["backend_mode"] == "mock" and caps["agent_runner"] == {"connected": False} and caps["push"] == {"available": False}


def test_event_stream_and_step_up_approval_over_real_http(live):
    svc, c = paired(live)
    t = svc.approvals.client.request("fin_agent", "spend_money", 0.5, "op1", "ödəniş", {"x": 1})
    evs = c.request("GET", "/v1/events")[1]
    assert [e["type"] for e in evs["events"]] == ["approval_request"] and evs["events"][0]["payload"]["approval_id"] == t.id
    assert c.request("POST", "/v1/events/ack", {"seq": evs["next_cursor"]})[1]["last_ack"] == evs["next_cursor"]
    pending = c.request("GET", "/v1/approvals")[1]["approvals"]
    assert [a["id"] for a in pending] == [t.id]
    ch = c.request("POST", f"/v1/approvals/{t.id}/challenge", {"decision": "approve"})[1]
    good = c.sign(ch, "approval", t.id, ch["approval"]["payload_hash"], "approve")
    # səs/mətn təsdiq deyil; imzasız və ya başqa açarın imzası keçmir
    st, _, _ = c.request("POST", f"/v1/approvals/{t.id}/decision", {"decision": "approve", "challenge_id": ch["challenge_id"], "proof": good, "method": "voice"})
    assert st == 403
    st, _, _ = c.request("POST", f"/v1/approvals/{t.id}/decision", {"decision": "approve", "challenge_id": ch["challenge_id"], "proof": b64(Keypair().sign(b"x")), "method": "biometric"})
    assert st == 403 and svc.approvals.gateway.get(t.id).status == "pending"
    ch2 = c.request("POST", f"/v1/approvals/{t.id}/challenge", {"decision": "approve"})[1]
    st, body, _ = c.request("POST", f"/v1/approvals/{t.id}/decision", {
        "decision": "approve", "challenge_id": ch2["challenge_id"], "method": "biometric",
        "proof": c.sign(ch2, "approval", t.id, ch2["approval"]["payload_hash"], "approve")})
    assert (st, body) == (200, {"ok": True, "status": "approved"})
    tk = svc.approvals.gateway.get(t.id)
    assert (tk.status, tk.approver_kind, tk.channel) == ("approved", "human", "android")


def test_unauthorized_requests_are_rejected_over_real_http(live):
    svc, c = paired(live)
    protected = [(m, p) for m, p, a in svc.app.router.routes() if a == "session"]
    assert len(protected) >= 25
    for method, pattern in protected:
        path = pattern.replace("{id}", "x").replace("{key}", "x")
        for tok in (None, "nay_a_" + "x" * 43, "bozuq"):
            st, body, hdr = c.request(method, path, token=tok)
            assert st == 401 and body["error"]["code"] == "unauthorized" and "WWW" not in str(hdr), (method, path)
    assert c.request("GET", "/v1/me", headers={"Authorization": "Basic abc"}, token=None)[0] == 401
    # cihaz ləğv olunduqda token dərhal qüvvədən düşür
    svc.auth.revoke_device(svc.auth.list_devices()[0]["device_id"])
    assert c.request("GET", "/v1/me")[0] == 401


def test_refresh_rotation_and_reuse_detection_over_real_http(live):
    svc, c = paired(live)
    old = c.refresh
    st, new, _ = c.request("POST", "/v1/token/refresh", {"refresh_token": old}, token=None)
    assert st == 200 and new["refresh_token"] != old
    assert c.request("GET", "/v1/me", token=new["access_token"])[0] == 200
    assert c.request("POST", "/v1/token/refresh", {"refresh_token": old}, token=None)[0] == 401
    assert c.request("GET", "/v1/me", token=new["access_token"])[0] == 401  # oğurluq əlaməti: cihaz ləğv


def test_pairing_rejects_missing_key_wrong_proof_and_reused_codes(live):
    svc, c, _ = live
    code = svc.auth.create_pairing_code()
    body = {"code": code, "device_name": "P"}
    assert c.request("POST", "/v1/pair", body, token=None)[0] == 400                       # açıq açar yoxdur
    other = Keypair()
    body.update(public_key=b64(c.kp.spki()), signature=b64(other.sign(AuthService.pair_message(code))))
    assert c.request("POST", "/v1/pair", body, token=None)[0] == 400                       # sahiblik sübutu yoxdur
    assert c.pair(code)[0] == 200
    assert Reference(c.port).pair(code)[0] == 401                                          # kod təkrar işləmir


def test_transport_level_errors_are_clean_json_without_leaks(live):
    svc, c = paired(live)
    st, body, hdr = c.request("GET", "/v1/yoxdur")
    assert st == 404 and set(body["error"]) == {"code", "message", "request_id"}
    assert c.request("DELETE", "/v1/me")[0] == 405
    assert c.request("POST", "/v1/chat/messages", raw=b"{bad json")[0] == 400
    assert c.request("POST", "/v1/chat/messages", raw=b"x" * 70000)[0] in (413, 400)
    assert c.request("POST", "/v1/chat/messages", {"text": "a"}, headers={"Content-Type": "text/plain"})[0] == 415
    assert hdr["X-Content-Type-Options"] == "nosniff" and hdr["Server"] == "naymera"  # Python/WSGI sürümü sızmır


def test_server_does_not_log_tokens_or_request_lines(live, capfd):
    svc, c = paired(live)
    c.request("GET", "/v1/me")
    c.request("GET", "/v1/me?token=" + c.access)
    out = capfd.readouterr()
    assert c.access not in out.out + out.err and "GET /v1" not in out.out + out.err


def test_slow_or_stalled_client_does_not_block_the_server_for_long(live):
    svc, c = paired(live)
    s = socket.create_connection(("127.0.0.1", c.port), timeout=5)
    s.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n")  # başlıqlar tamamlanmır
    started = time.time()
    st = c.request("GET", "/v1/health", token=None)[0]
    s.close()
    assert st == 200 and time.time() - started < 5  # sorğu vaxt aşımı (1s) ilə sərbəst buraxılır


def test_connection_refused_after_the_server_stops(live):
    svc, c, httpd = live
    paired_ok = c.pair(svc.auth.create_pairing_code())[0] == 200
    httpd.shutdown()
    httpd.server_close()
    assert paired_ok
    with pytest.raises(OSError):
        c.request("GET", "/v1/health", token=None)


def test_server_refuses_open_binds_without_tls(tmp_path):
    svc = build_app(tmp_path)
    with pytest.raises(ValueError):
        create_server(svc, "0.0.0.0", 0)
    check_bind("127.0.0.1")
