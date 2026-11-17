"""Real HTTP nəqliyyatı (`urllib`): yönləndirməyə əməl etmir, cavab ölçüsünü məhdudlaşdırır, vaxt aşımını bildirir. Əsas testlərdə yalnız loopback
üzərində istifadə olunur. Başlıqlar və URL xəta mətninə yazılmır."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request

from agents.providers import HttpRequest, HttpResponse, TransportError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return None


class UrllibTransport:
    def __init__(self, max_bytes: int = 1_000_000) -> None:
        self.max_bytes = max_bytes
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __call__(self, r: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(r.url, data=r.body, method=r.method, headers=dict(r.headers))
        try:
            with self._opener.open(req, timeout=r.timeout) as resp:
                return HttpResponse(resp.status, self._read(resp))
        except urllib.error.HTTPError as exc:  # 3xx/4xx/5xx: status qaytarılır (gövdə məhdud oxunur)
            try:
                body = self._read(exc)
            except TransportError:
                body = b""
            return HttpResponse(exc.code, body)
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout", timeout=True) from None
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                raise TransportError("timeout", timeout=True) from None
            raise TransportError("connection failed") from None
        except OSError:
            raise TransportError("connection failed") from None

    def _read(self, resp) -> bytes:  # noqa: ANN001
        data = resp.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise TransportError("response too large")
        return data
