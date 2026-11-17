"""API-nin yığılması (bootstrap) və server qoruyucuları. Heç bir real soket açılmır."""
import io
import json

import pytest

from api.bootstrap import build_app
from api.http import wsgi_adapter
from api.server import DEFAULT_HOST, check_bind


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
    status, body = wsgi(svc.app, "POST", "/v1/pair", {"code": code, "device_name": "Pixel 8"})
    assert status == 200
    tok = body["access_token"]
    assert wsgi(svc.app, "GET", "/v1/me", token=tok)[0] == 200
    status, chat = wsgi(svc.app, "POST", "/v1/chat/messages", {"text": "kömək"}, token=tok)
    assert status == 200 and chat["assistant_message"]["provenance"] == "rules"
    assert wsgi(svc.app, "GET", "/v1/system/status", token=tok)[1]["llm_is_mock"] is True


def test_default_llm_is_mock_and_default_plans_are_rejected_safely(tmp_path):
    svc = build_app(tmp_path)
    code = svc.auth.create_pairing_code()
    tok = wsgi(svc.app, "POST", "/v1/pair", {"code": code, "device_name": "P"})[1]["access_token"]
    status, body = wsgi(svc.app, "POST", "/v1/jobs", {"goal": "hesabat"}, token=tok)
    assert status == 201 and body["job"]["is_mock"] is True and body["job"]["ok"] is False
    assert wsgi(svc.app, "GET", "/v1/company/agents", token=tok)[1] == {"agents": []}


def test_state_is_persistent_across_restarts(tmp_path):
    svc = build_app(tmp_path)
    code = svc.auth.create_pairing_code()
    tok = wsgi(svc.app, "POST", "/v1/pair", {"code": code, "device_name": "P"})[1]["access_token"]
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
