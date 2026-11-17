"""API nəqliyyat qatı: marşrutlaşdırma, JSON, ölçü limiti, təhlükəsizlik başlıqları, WSGI adapteri.
Heç bir real şəbəkə soketi yoxdur: sorğular birbaşa `handle`/WSGI environ ilə verilir."""
import io
import json

import pytest

from api.http import ApiError, App, Request, Response, Router, wsgi_adapter


def make_app(**kw):
    router = Router()

    def echo(req, params, ctx):
        return Response(200, {"echo": req.json, "params": params})

    def boom(req, params, ctx):
        raise RuntimeError("daxili sirr: sk-test-FAKEKEY1234567890abcdef")

    def deny(req, params, ctx):
        raise ApiError(403, "forbidden", "icazə yoxdur")

    router.add("POST", "/v1/echo/{item}", echo, auth="none")
    router.add("GET", "/v1/boom", boom, auth="none")
    router.add("GET", "/v1/deny", deny, auth="none")
    router.add("GET", "/v1/ping", lambda r, p, c: Response(200, {"ok": True}), auth="none")
    return App(router, **kw)


def call(app, method="GET", path="/v1/ping", body=None, headers=None, query=None, raw=None):
    h = {"content-type": "application/json"} if body is not None or raw is not None else {}
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    return app.handle(Request(method, path, query or {}, h, data, "127.0.0.1"))


def test_routing_with_path_params_and_json_body():
    r = call(make_app(), "POST", "/v1/echo/abc123", {"x": [1, 2]})
    assert r.status == 200 and r.body == {"echo": {"x": [1, 2]}, "params": {"item": "abc123"}}


def test_unknown_route_and_wrong_method_are_clean_errors():
    app = make_app()
    nf = call(app, "GET", "/v1/yoxdur")
    assert nf.status == 404 and nf.body["error"]["code"] == "not_found"
    bad = call(app, "DELETE", "/v1/ping")
    assert bad.status == 405 and "GET" in bad.headers["Allow"]


@pytest.mark.parametrize("path", ["/v1/echo/../../etc", "/v1/echo/a b", "/v1/echo/" + "x" * 300, "//v1/ping",
                                  "/v1/ping/", "/v1/echo/a%2e%2e", "v1/ping"])
def test_malformed_paths_are_rejected(path):
    assert call(make_app(), "POST", path, {"a": 1}).status in (400, 404, 405)


def test_json_validation_and_size_limit():
    app = make_app(max_body=100)
    assert call(app, "POST", "/v1/echo/a", raw=b"{bad json").status == 400
    assert call(app, "POST", "/v1/echo/a", raw=b"[1,2]").status == 400                 # obyekt deyil
    assert call(app, "POST", "/v1/echo/a", raw=b'{"a":"' + b"x" * 200 + b'"}').status == 413
    assert call(app, "POST", "/v1/echo/a", raw=b'{"a":NaN}').status == 400
    assert call(app, "POST", "/v1/echo/a", raw=b'{"a":1,"a":2}').status == 400         # təkrar açar
    assert call(app, "POST", "/v1/echo/a", {"a": 1}, headers={"content-type": "text/plain"}).status == 415
    assert call(app, "POST", "/v1/echo/a", raw=b"\xff\xfe").status == 400
    deep = b"[" * 200 + b"]" * 200
    assert call(make_app(), "POST", "/v1/echo/a", raw=deep).status == 400  # dərin/obyekt olmayan JSON


def test_errors_never_leak_internals():
    r = call(make_app(), "GET", "/v1/boom")
    assert r.status == 500 and r.body["error"]["code"] == "internal_error"
    blob = json.dumps(r.body)
    assert "sk-test" not in blob and "RuntimeError" not in blob and "Traceback" not in blob
    assert r.body["error"]["request_id"] == r.headers["X-Request-Id"]
    d = call(make_app(), "GET", "/v1/deny")
    assert d.status == 403 and d.body["error"] == {"code": "forbidden", "message": "icazə yoxdur",
                                                    "request_id": d.headers["X-Request-Id"]}


def test_security_headers_on_every_response():
    app = make_app(https=True)
    for r in (call(app), call(app, "GET", "/v1/yoxdur"), call(app, "GET", "/v1/boom")):
        h = r.headers
        assert h["Cache-Control"] == "no-store" and h["X-Content-Type-Options"] == "nosniff"
        assert h["Content-Security-Policy"] == "default-src 'none'" and h["Referrer-Policy"] == "no-referrer"
        assert h["Content-Type"].startswith("application/json") and "Strict-Transport-Security" in h
        assert "Server" not in h and "Access-Control-Allow-Origin" not in h  # CORS defoltda bağlıdır
    assert "Strict-Transport-Security" not in call(make_app()).headers


def test_request_ids_are_unique_and_json_is_utf8():
    app = make_app()
    ids = {call(app).headers["X-Request-Id"] for _ in range(5)}
    assert len(ids) == 5
    r = call(app, "POST", "/v1/echo/a", {"t": "Azərbaycan dili"})
    assert r.body["echo"]["t"] == "Azərbaycan dili"


def test_route_table_is_introspectable():
    routes = make_app().router.routes()
    assert ("GET", "/v1/ping", "none") in routes and ("POST", "/v1/echo/{item}", "none") in routes


def run_wsgi(app, method, path, body=b"", ctype="application/json", extra=None):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "a=1&b=2", "CONTENT_TYPE": ctype,
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "REMOTE_ADDR": "127.0.0.1"}
    environ.update(extra or {})
    seen = {}

    def start(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    out = b"".join(wsgi_adapter(app)(environ, start))
    return seen["status"], seen["headers"], json.loads(out)


def test_wsgi_adapter_roundtrip_and_oversized_body_is_refused_before_reading():
    status, headers, body = run_wsgi(make_app(), "POST", "/v1/echo/x", b'{"k": 1}')
    assert status.startswith("200") and body["echo"] == {"k": 1}
    small = make_app(max_body=10)
    status, _, body = run_wsgi(small, "POST", "/v1/echo/x", b'{"k": "' + b"x" * 50 + b'"}')
    assert status.startswith("413")
    bad_len = {"CONTENT_LENGTH": "abc"}
    assert run_wsgi(make_app(), "POST", "/v1/echo/x", b"{}", extra=bad_len)[0].startswith("400")
    neg = {"CONTENT_LENGTH": "-5"}
    assert run_wsgi(make_app(), "POST", "/v1/echo/x", b"{}", extra=neg)[0].startswith("400")


def test_query_string_is_parsed_without_duplicates_ambiguity():
    seen = {}
    router = Router()
    router.add("GET", "/v1/q", lambda r, p, c: seen.update(q=r.query) or Response(200, {}), auth="none")
    app = App(router)
    run_wsgi(app, "GET", "/v1/q")
    assert seen["q"] == {"a": "1", "b": "2"}
