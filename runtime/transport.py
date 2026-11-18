"""Real HTTP nəqliyyatı (`urllib`): yönləndirməyə əməl etmir, cavab ölçüsünü məhdudlaşdırır, vaxt aşımını bildirir. Əsas testlərdə yalnız loopback
üzərində istifadə olunur. Başlıqlar və URL xəta mətninə yazılmır."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request

from agents.providers import EndpointError, HttpRequest, HttpResponse, TransportError, register_endpoint_enforcing_transport, validate_endpoint


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return None


@register_endpoint_enforcing_transport
class UrllibTransport:
    """`kind` ("cloud" | "private") verilibsə HƏR sorğunun URL-i `validate_endpoint(url, kind)` ilə yoxlanılır və uyğun gəlməyən hədəfə qoşulmadan `TransportError` verilir: `private` nəqliyyat
    ictimai hosta, `cloud` nəqliyyat isə lokal/özəl hosta GEDƏ BİLMƏZ (fail-closed). `kind=None` yoxlamasızdır (yalnız loopback testləri üçün)."""

    def __init__(self, max_bytes: int = 1_000_000, kind: str | None = None) -> None:
        if kind not in (None, "cloud", "private"):
            raise ValueError("kind cloud|private olmalıdır")
        self.max_bytes, self.kind = max_bytes, kind
        self._opener = urllib.request.build_opener(_NoRedirect)

    def __call__(self, r: HttpRequest) -> HttpResponse:
        if self.kind is not None:
            try:
                validate_endpoint(r.url, self.kind)
            except EndpointError:
                raise TransportError("endpoint policy violation") from None
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
