"""M1: `tools/serve_core_dev.py` — yalnız 127.0.0.1, başlama/dayanma, endpoint smoke (həqiqi loopback HTTP; xarici şəbəkə YOXDUR), demo döngü + step-up ilə təsdiq."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import pathlib
import socket
import sys
import urllib.error
import urllib.request

import pytest

from api.auth import AuthService
from tests.p256_tools import Keypair

spec = importlib.util.spec_from_file_location("serve_core_dev", pathlib.Path(__file__).resolve().parent.parent / "tools" / "serve_core_dev.py")
scd = importlib.util.module_from_spec(spec)
sys.modules["serve_core_dev"] = scd
spec.loader.exec_module(scd)


def b64(b):
    return base64.b64encode(b).decode()


def h(t):
    return hashlib.sha256(t.encode()).hexdigest()


class Http:
    def __init__(self, base):
        self.base, self.token, self.kp = base, "", Keypair()

    def req(self, method, path, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if auth and self.token else {})})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def pair(self, code):
        st, body = self.req("POST", "/v1/pair", {"code": code, "device_name": "Test", "public_key": b64(self.kp.spki()), "signature": b64(self.kp.sign(AuthService.pair_message(code)))}, auth=False)
        assert st == 200, body
        self.token = body["access_token"]

    def sign(self, ch):
        return b64(self.kp.sign(AuthService.step_up_message(ch["challenge_id"], ch["nonce"], ch["action"], ch["subject"], ch["payload_hash"], ch["decision"])))


@pytest.fixture()
def server(tmp_path):
    s = scd.CoreDevServer(tmp_path / "data")
    s.start()
    yield s
    s.stop()


def paired(server):
    http = Http(server.url)
    http.pair(server.call(lambda dev: dev.services.auth.create_pairing_code()))
    return http


def test_binds_only_to_loopback_and_has_no_host_option_or_env_override(server, tmp_path, monkeypatch):
    host, port = scd.BIND_HOST, server.port
    assert host == "127.0.0.1" and port > 0 and server.url == f"http://127.0.0.1:{port}"
    with pytest.raises(SystemExit):
        scd.main(["--host", "0.0.0.0"])                                                      # host seçimi YOXDUR
    monkeypatch.setenv("NAYMERA_HOST", "0.0.0.0")
    monkeypatch.setenv("HOST", "0.0.0.0")
    s2 = scd.CoreDevServer(tmp_path / "d2")
    s2.start()
    try:
        assert s2.url.startswith("http://127.0.0.1:")                                       # mühit dəyişəni oxunmur
        probe = socket.create_connection(("127.0.0.1", s2.port), timeout=2)
        probe.close()
    finally:
        s2.stop()
    src = pathlib.Path(scd.__file__).read_text()
    assert 'add_argument("--host"' not in src and "os.environ" not in src and "getenv" not in src      # mənbədə host konfiqurasiyası yoxdur


def test_start_stop_and_port_release(tmp_path):
    s = scd.CoreDevServer(tmp_path / "d")
    port = s.start()
    assert Http(s.url).req("GET", "/v1/health", auth=False) == (200, {"status": "ok", "api_version": 1})
    s.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()                    # dayandıqdan sonra port bağlıdır
    with pytest.raises(RuntimeError):
        s.call(lambda dev: 1)
    s2 = scd.CoreDevServer(tmp_path / "d")                                                   # eyni məlumat qovluğu ilə yenidən başlama
    s2.start()
    s2.stop()


def test_failed_startup_is_reported_not_hung(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        s = scd.CoreDevServer(tmp_path / "d", port=blocker.getsockname()[1])
        with pytest.raises(OSError):
            s.start(timeout=5)                                                               # port məşğuldur: xəta qaytarılır, asılmır
    finally:
        blocker.close()


def test_authentication_is_unchanged_every_core_route_needs_a_session(server):
    http = Http(server.url)
    for method, path in (("GET", "/v1/core/status"), ("GET", "/v1/core/goals"), ("POST", "/v1/core/goals"), ("GET", "/v1/core/steps/awaiting"), ("GET", "/v1/core/journal")):
        assert http.req(method, path, {} if method == "POST" else None, auth=False)[0] == 401
    assert http.req("GET", "/v1/core/status", auth=True)[0] == 401                          # token yoxdur
    http.pair(server.call(lambda dev: dev.services.auth.create_pairing_code()))
    st, body = http.req("GET", "/v1/core/status")
    assert st == 200 and body["autonomy"] == "L1" and body["executes_without_owner_approval"] is False
    http2 = Http(server.url)
    code = server.call(lambda dev: dev.services.auth.create_pairing_code())
    assert http2.req("POST", "/v1/pair", {"code": code, "device_name": "X", "public_key": b64(http2.kp.spki()), "signature": b64(Keypair().sign(AuthService.pair_message(code)))}, auth=False)[0] == 400   # saxta sahiblik sübutu


def test_demo_flow_create_activate_plan_approve_execute_and_stop_wins(server, tmp_path):
    http = paired(server)
    st, body = http.req("POST", "/v1/core/goals", {"title": "demo hesabat", "client_op": "demo-op-0001"})
    gid = body["goal"]["id"]
    assert st == 201 and body["goal"]["status"] == "draft"
    ch = http.req("POST", f"/v1/core/goals/{gid}/challenge", {"to_status": "active"})[1]
    assert (ch["action"], ch["subject"], ch["payload_hash"], ch["decision"]) == ("core_goal", gid, h("active:1"), "active")
    assert http.req("POST", f"/v1/core/goals/{gid}/transition", {"to_status": "active", "challenge_id": ch["challenge_id"], "proof": http.sign(ch), "method": "biometric"})[0] == 200
    import time
    for _ in range(100):                                                                      # döngü ipdə boş vaxtda plan çıxarır
        st, body = http.req("GET", "/v1/core/steps/awaiting")
        if body["steps"]:
            break
        time.sleep(0.05)
    step = body["steps"][0]
    assert step["capability_id"] == "memory.recall" and server.call(lambda dev: dev.memory.stats()).get("episodic", 0) >= 1
    approvals = http.req("GET", "/v1/approvals")[1]["approvals"]
    assert [a["description"] for a in approvals] == [step["approval_description"]]
    aid = approvals[0]["id"]
    c2 = http.req("POST", f"/v1/approvals/{aid}/challenge", {"decision": "approve"})[1]
    proof = b64(http.kp.sign(AuthService.step_up_message(c2["challenge_id"], c2["nonce"], "approval", aid, c2["approval"]["payload_hash"], "approve")))
    assert http.req("POST", f"/v1/approvals/{aid}/decision", {"decision": "approve", "challenge_id": c2["challenge_id"], "proof": proof, "method": "biometric"})[0] == 200
    for _ in range(100):
        detail = http.req("GET", f"/v1/core/goals/{gid}")[1]
        if detail["plan"]["steps"][0]["state"] == "succeeded":
            break
        time.sleep(0.05)
    assert detail["plan"]["steps"][0]["state"] == "succeeded" and detail["goal"]["status"] == "active"
    # STOP üstündür: API ilə dayandır -> növbəti addımın təsdiqi olsa belə icra olunmur
    assert http.req("POST", "/v1/system/stop", {})[0] == 200
    time.sleep(0.4)
    state_before = http.req("GET", f"/v1/core/goals/{gid}")[1]["plan"]["steps"]
    aid2 = None
    for _ in range(40):
        pending = http.req("GET", "/v1/approvals")[1]["approvals"]
        if pending:
            aid2 = pending[0]["id"]
            break
        time.sleep(0.05)
    assert http.req("GET", "/v1/core/status")[1]["stop_active"] is True
    assert [s["state"] for s in http.req("GET", f"/v1/core/goals/{gid}")[1]["plan"]["steps"]] == [s["state"] for s in state_before]


def test_server_state_is_confined_to_the_data_dir_and_dirs_are_private(server, tmp_path):
    import os
    import stat
    assert stat.S_IMODE(os.stat(tmp_path / "data").st_mode) == 0o700 and (tmp_path / "data" / "core.db").exists()
