"""API nəqliyyat qatı: marşrutlaşdırma, ciddi JSON, ölçü limiti, təhlükəsizlik başlıqları, WSGI adapteri.

Defoltlar təhlükəsizdir: CORS yoxdur, cavablar keş olunmur, daxili xətalar sızmır (yalnız `request_id`), yalnız
`application/json`, təkrar açar/NaN/dərin JSON rədd edilir. Burada şəbəkə soketi yoxdur — server `api/server.py`-dədir.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import parse_qsl

MAX_BODY = 65536
_PATH_OK = re.compile(r"^/[A-Za-z0-9_\-:./]{0,255}$")
_PARAM = r"(?P<{name}>[A-Za-z0-9_\-:.]{{1,64}})"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    remote: str = ""
    json: Any = None


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Ctx:
    request_id: str
    session: Any = None
    remote: str = ""


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    handler: Callable[[Request, dict[str, str], Ctx], Response]
    auth: str
    regex: re.Pattern


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Callable, auth: str = "session") -> None:
        if auth not in ("none", "session"):
            raise ValueError("auth none|session olmalıdır")
        regex = re.compile("^" + re.sub(r"\{([a-z_]+)\}", lambda m: _PARAM.format(name=m.group(1)),
                                         re.escape(pattern).replace(r"\{", "{").replace(r"\}", "}")) + "$")
        self._routes.append(Route(method, pattern, handler, auth, regex))

    def routes(self) -> list[tuple[str, str, str]]:
        return [(r.method, r.pattern, r.auth) for r in self._routes]

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, str], list[str]]:
        allowed: list[str] = []
        for r in self._routes:
            m = r.regex.match(path)
            if m is None:
                continue
            if r.method == method:
                return r, m.groupdict(), []
            allowed.append(r.method)
        return None, {}, sorted(set(allowed))


def _reject_constant(name: str) -> Any:
    raise ValueError(f"yolverilməz JSON sabiti: {name}")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"təkrar açar: {k}")
        out[k] = v
    return out


class App:
    def __init__(self, router: Router, *, max_body: int = MAX_BODY, https: bool = False,
                 authenticate: Callable[[str, Request], Any] | None = None,
                 on_error: Callable[[str, str], None] | None = None) -> None:
        self.router, self.max_body, self.https = router, max_body, https
        self._authenticate, self._on_error = authenticate, on_error

    # --- ictimai ---
    def handle(self, req: Request) -> Response:
        rid = uuid.uuid4().hex[:16]
        try:
            resp = self._dispatch(req, rid)
        except ApiError as exc:
            resp = self.error(exc.status, exc.code, exc.message, rid)
        except Exception as exc:  # noqa: BLE001 — daxili xəta müştəriyə sızmır
            if self._on_error is not None:
                try:
                    self._on_error(rid, type(exc).__name__)
                except Exception:  # noqa: BLE001
                    pass
            resp = self.error(500, "internal_error", "Daxili xəta baş verdi.", rid)
        return self._finish(resp, rid)

    def error(self, status: int, code: str, message: str, rid: str | None = None) -> Response:
        rid = rid or uuid.uuid4().hex[:16]
        return self._finish(Response(status, {"error": {"code": code, "message": message, "request_id": rid}}), rid)

    # --- daxili ---
    def _finish(self, resp: Response, rid: str) -> Response:
        h = dict(resp.headers)
        h.update({"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
                  "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'",
                  "Referrer-Policy": "no-referrer", "X-Request-Id": rid})
        if self.https:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return Response(resp.status, resp.body, h)

    def _dispatch(self, req: Request, rid: str) -> Response:
        path = req.path
        if not isinstance(path, str) or not _PATH_OK.match(path) or "//" in path or "/../" in path \
                or "/./" in path or path.endswith(("/..", "/.")):
            raise ApiError(400, "bad_path", "Yanlış yol.")
        route, params, allowed = self.router.match(req.method, path)
        if route is None:
            if allowed:
                resp = Response(405, {"error": {"code": "method_not_allowed", "message": "Metod dəstəklənmir.",
                                                "request_id": rid}}, {"Allow": ", ".join(allowed)})
                return resp
            raise ApiError(404, "not_found", "Tapılmadı.")
        if len(req.body) > self.max_body:
            raise ApiError(413, "too_large", "Sorğu çox böyükdür.")
        parsed: Any = None
        if req.method in ("POST", "PUT", "PATCH"):
            parsed = self._parse_json(req)
        session = None
        if route.auth == "session":
            if self._authenticate is None:
                raise ApiError(401, "unauthorized", "Autentifikasiya tələb olunur.")
            m = re.fullmatch(r"Bearer (\S{1,200})", req.headers.get("authorization", ""))
            if m is None:
                raise ApiError(401, "unauthorized", "Autentifikasiya tələb olunur.")
            session = self._authenticate(m.group(1), req)
        resp = route.handler(replace(req, json=parsed), params, Ctx(rid, session, req.remote))
        if not isinstance(resp, Response):
            raise TypeError("handler Response qaytarmalıdır")
        return resp

    def _parse_json(self, req: Request) -> dict:
        if not req.body:
            return {}
        if not req.headers.get("content-type", "").lower().startswith("application/json"):
            raise ApiError(415, "unsupported_media_type", "Yalnız application/json qəbul olunur.")
        try:
            data = json.loads(req.body.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_dupes)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError(400, "bad_json", "JSON yanlışdır.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "JSON obyekt olmalıdır.")
        return data


def wsgi_adapter(app: App) -> Callable:
    """Standart WSGI tətbiqi. Gövdə `CONTENT_LENGTH` yoxlanmamış oxunmur (böyük sorğu oxunmadan rədd edilir)."""

    def application(environ: dict, start_response: Callable) -> list[bytes]:
        def reply(resp: Response) -> list[bytes]:
            out = json.dumps(resp.body, ensure_ascii=False).encode("utf-8")
            headers = list(resp.headers.items()) + [("Content-Length", str(len(out))), ("Server", "naymera")]  # sürüm sızmır
            start_response(f"{resp.status} {HTTPStatus(resp.status).phrase}", headers)
            return [out]

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return reply(app.error(400, "bad_length", "Content-Length yanlışdır."))
        if length < 0:
            return reply(app.error(400, "bad_length", "Content-Length yanlışdır."))
        if length > app.max_body:
            return reply(app.error(413, "too_large", "Sorğu çox böyükdür."))
        body = environ["wsgi.input"].read(length) if length else b""
        try:
            pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True, max_num_fields=50,
                              strict_parsing=False)
        except ValueError:
            return reply(app.error(400, "bad_query", "Sorğu sətri yanlışdır."))
        query = dict(pairs)
        if len(query) != len(pairs):
            return reply(app.error(400, "bad_query", "Təkrar sorğu parametri."))
        headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = environ["CONTENT_TYPE"]
        req = Request(environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/"), query, headers, body,
                      environ.get("REMOTE_ADDR", ""))
        return reply(app.handle(req))

    return application
