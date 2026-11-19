"""API-nin yığılması (bootstrap) və server qoruyucuları. Heç bir real soket açılmır."""
import base64
import io
import json

import pytest

from api.bootstrap import build_app
from api.auth import AuthService
from api.http import wsgi_adapter
from api.server import DEFAULT_HOST, check_bind
from tests.p256_tools import Keypair


def pair_body(code, name="P"):
    kp = Keypair()
    b64 = lambda b: base64.b64encode(b).decode()
    return {"code": code, "device_name": name, "public_key": b64(kp.spki()),
            "signature": b64(kp.sign(AuthService.pair_message(code)))}


def wsgi(app, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else b""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "", "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data), "REMOTE_ADDR": "127.0.0.1"}
    if token:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    seen = {}
    out = b"".join(wsgi_adapter(app)(environ, lambda s, h: seen.update(status=s, headers=dict(h))))
    return int(seen["status"].split()[0]), json.loads(out)


def test_default_stack_boots_pairs_and_answers_over_wsgi(tmp_path):
    svc = build_app(tmp_path)
    assert wsgi(svc.app, "GET", "/v1/health") == (200, {"status": "ok", "api_version": 1})
    code = svc.auth.create_pairing_code()
    status, body = wsgi(svc.app, "POST", "/v1/pair", pair_body(code, "Pixel 8"))
    assert status == 200
    tok = body["access_token"]
    assert wsgi(svc.app, "GET", "/v1/me", token=tok)[0] == 200
    status, chat = wsgi(svc.app, "POST", "/v1/chat/messages", {"text": "kömək"}, token=tok)
    assert status == 200 and chat["assistant_message"]["provenance"] == "rules"
    assert wsgi(svc.app, "GET", "/v1/system/status", token=tok)[1]["llm_is_mock"] is True


def test_default_llm_is_mock_and_default_plans_are_rejected_safely(tmp_path):
    svc = build_app(tmp_path)
    code = svc.auth.create_pairing_code()
    tok = wsgi(svc.app, "POST", "/v1/pair", pair_body(code, "P"))[1]["access_token"]
    status, body = wsgi(svc.app, "POST", "/v1/jobs", {"goal": "hesabat"}, token=tok)
    assert status == 201 and body["job"]["is_mock"] is True and body["job"]["ok"] is False
    assert wsgi(svc.app, "GET", "/v1/company/agents", token=tok)[1] == {"agents": []}


def test_state_is_persistent_across_restarts(tmp_path):
    svc = build_app(tmp_path)
    code = svc.auth.create_pairing_code()
    tok = wsgi(svc.app, "POST", "/v1/pair", pair_body(code, "P"))[1]["access_token"]
    cid = wsgi(svc.app, "POST", "/v1/chat/messages", {"text": "salam"}, token=tok)[1]["conversation_id"]
    again = build_app(tmp_path)  # proses yenidən başladı
    assert wsgi(again.app, "GET", "/v1/me", token=tok)[0] == 200
    msgs = wsgi(again.app, "GET", f"/v1/chat/conversations/{cid}/messages", token=tok)[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_server_binds_to_loopback_by_default_and_refuses_open_binds_without_tls():
    assert DEFAULT_HOST == "127.0.0.1"
    check_bind("127.0.0.1")
    check_bind("localhost")
    for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
        with pytest.raises(ValueError):
            check_bind(host)
    check_bind("0.0.0.0", tls_cert="/x/cert.pem", tls_key="/x/key.pem")
    with pytest.raises(ValueError):
        check_bind("0.0.0.0", tls_cert="/x/cert.pem")


def test_default_stack_has_a_hybrid_router_with_honest_engine_states(tmp_path):
    svc = build_app(tmp_path)
    code = svc.auth.create_pairing_code()
    tok = wsgi(svc.app, "POST", "/v1/pair", pair_body(code))[1]["access_token"]
    st = wsgi(svc.app, "GET", "/v1/ai/status", token=tok)[1]
    assert st["mode"] == "auto"
    assert st["cloud"]["state"] == "simulation" and st["cloud"]["is_mock"] is True
    assert st["local"]["state"] == "not_configured"
    assert all(v is False for v in st["consents"].values())
    assert st["voice"]["tts"]["local"] == "simulation" and st["voice"]["stt"]["local"] == "not_configured"
    chat = wsgi(svc.app, "POST", "/v1/chat/messages", {"text": "salam"}, token=tok)[1]["assistant_message"]
    assert chat["provenance"] == "rules" and chat["engine"] == "" and "simulyasiya" in chat["notice"].lower()   # simulyasiya real AI kimi göstərilmir
    assert chat["notice_code"] == "simulation_only"
    assert wsgi(svc.app, "GET", "/v1/system/status", token=tok)[1]["llm_is_mock"] is True
    assert wsgi(svc.app, "GET", "/v1/capabilities", token=tok)[1]["backend_mode"] == "mock"


def test_ai_settings_persist_across_restarts(tmp_path):
    svc = build_app(tmp_path)
    tok = wsgi(svc.app, "POST", "/v1/pair", pair_body(svc.auth.create_pairing_code()))[1]["access_token"]
    assert wsgi(svc.app, "POST", "/v1/ai/mode", {"mode": "local"}, token=tok)[0] == 200
    again = build_app(tmp_path)
    assert wsgi(again.app, "GET", "/v1/ai/status", token=tok)[1]["mode"] == "local"


def test_build_app_accepts_externally_built_engines_and_reports_their_honest_state(tmp_path):
    from agents.hybrid import CloudEngine, LocalEngine
    svc = build_app(tmp_path, cloud_engine=CloudEngine(None), local_engine=LocalEngine(None))
    tok = wsgi(svc.app, "POST", "/v1/pair", pair_body(svc.auth.create_pairing_code()))[1]["access_token"]
    st = wsgi(svc.app, "GET", "/v1/ai/status", token=tok)[1]
    assert st["cloud"]["state"] == "not_configured" and st["local"]["state"] == "not_configured" and st["active"] == "none"
    assert wsgi(svc.app, "GET", "/v1/system/status", token=tok)[1]["llm_is_mock"] is True
