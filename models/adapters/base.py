"""HTTP+JSON adapterlər üçün ortaq əsas (Mərhələ 18.1-B): endpoint siyasəti, nəqliyyat xətaları, ölçü limiti, təhlükəsiz JSON ayrışdırma, normallaşdırma.

Xəta semantikası (GuardedLLM ilə uyğun): `charged=False` yalnız haqqın tutulmadığı BƏLLİ olduqda (qoşulma xətası, 401/403/429/4xx); cavab alındıqdan sonrakı pozulmuşluq və
5xx/timeout üçün `charged=None` (naməlum: rezerv qorunur). Sirr heç vaxt xəta mətninə yazılmır."""
from __future__ import annotations

import json
import re
from typing import Any

from agents.llm import LLMBadResponse, LLMProviderError, LLMRequest, LLMResponse, Secret, estimate_tokens
from agents.providers import EndpointError, HttpRequest, HttpResponse, ProviderTimeout, Transport, TransportError, validate_endpoint

MAX_RESPONSE_BYTES = 1_000_000
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}$")


def check_model_name(model: Any) -> str:
    if not isinstance(model, str) or not _MODEL_NAME.match(model):
        raise ValueError("model adı yanlışdır (hərf/rəqəm/._- , ən çox 80)")
    return model


def clean_response_model(value: Any, fallback: str) -> str:
    """Cavabdakı model adı etibarsızdır: yalnız təhlükəsiz simvollar və ≤80; əks halda sorğu modeli."""
    return value if isinstance(value, str) and _MODEL_NAME.match(value) else fallback


def tokens(usage: Any, key: str, fallback: int) -> int:
    v = usage.get(key) if isinstance(usage, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else fallback


def safe_text(value: Any) -> str:
    if not isinstance(value, str):
        raise LLMBadResponse("provayder cavabı mətn deyil")
    try:
        value.encode("utf-8")                                # tək surrogate və s. aşağı axına buraxılmır
    except UnicodeEncodeError:
        raise LLMBadResponse("provayder cavabı yanlış Unicode ehtiva edir") from None
    return value


def parse_json_body(resp: HttpResponse, limit: int = MAX_RESPONSE_BYTES) -> dict:
    body = resp.body
    if not isinstance(body, (bytes, bytearray)):
        raise LLMBadResponse("provayder cavabı baytlar deyil")
    if len(body) > limit:
        raise LLMBadResponse("provayder cavabı çox böyükdür")
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise LLMBadResponse("provayder cavabı yanlış formatdadır") from None
    if not isinstance(data, dict):
        raise LLMBadResponse("provayder cavabı obyekt deyil")
    return data


def raise_for_status(status: Any) -> None:
    if isinstance(status, bool) or not isinstance(status, int):
        raise LLMBadResponse("provayder cavabının statusu yanlışdır")
    if status == 200:
        return
    if status in (401, 403):
        raise LLMProviderError(f"provayder HTTP {status}: etibarnamə rədd edildi", charged=False, retryable=False)
    if status == 429:
        raise LLMProviderError("provayder HTTP 429: sürət limiti", charged=False, retryable=True)
    if status >= 500:
        raise LLMProviderError(f"provayder HTTP {status}: server xətası", charged=None, retryable=True)
    raise LLMProviderError(f"provayder HTTP {status}", charged=False, retryable=False)


class HttpJsonProvider:
    """Alt siniflər `_url`, `_headers`, `_body`, `_parse` təyin edir. Növ həmişə `cloud` (HTTPS + ictimai DNS adı); lokal üçün `local.py`/OpenAI-uyğun private endpoint."""
    is_mock = False
    kind = "cloud"
    name = "http_json"
    DEFAULT_BASE = ""

    def __init__(self, model: str, api_key: Secret, transport: Transport, *, base_url: str | None = None) -> None:
        if not isinstance(api_key, Secret):
            raise ValueError("API açarı Secret olmalıdır")
        if not callable(transport):
            raise ValueError("nəqliyyat çağırıla bilən olmalıdır")
        self.model = check_model_name(model)
        self.base_url = validate_endpoint(base_url or self.DEFAULT_BASE, "cloud")
        self._key, self._transport = api_key, transport

    def describe(self) -> dict[str, Any]:                      # diaqnostika: etibar mənbəyi DEYİL
        return {"name": self.name, "kind": self.kind, "base_url": self.base_url, "model": self.model, "key_configured": True}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.describe()})"

    __str__ = __repr__

    # --- alt siniflər
    def _url(self) -> str: raise NotImplementedError
    def _headers(self, key: str) -> dict[str, str]: raise NotImplementedError
    def _body(self, request: LLMRequest) -> dict[str, Any]: raise NotImplementedError
    def _parse(self, data: dict, request: LLMRequest) -> LLMResponse: raise NotImplementedError

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:                                                   # hər sorğuda yenidən yoxla (atribut dəyişdirilsə belə siyasət pozulmur): FAIL-CLOSED
            validate_endpoint(self.base_url, "cloud")
        except EndpointError:
            raise LLMProviderError("endpoint siyasəti pozulub: sorğu göndərilmədi", charged=False) from None
        body = json.dumps(self._body(request)).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "naymera", **self._headers(self._key.reveal())}
        try:
            resp = self._transport(HttpRequest("POST", self._url(), headers, body, float(request.timeout_s)))
        except TransportError as exc:
            if exc.timeout:
                raise ProviderTimeout("provayder vaxt aşımı", charged=None, retryable=True) from None
            raise LLMProviderError("provayderə qoşulmaq mümkün olmadı", charged=False, retryable=True) from None
        if not isinstance(resp, HttpResponse):
            raise LLMBadResponse("nəqliyyat HttpResponse qaytarmadı")
        raise_for_status(resp.status)
        return self._parse(parse_json_body(resp), request)

    def _estimate(self, request: LLMRequest, text: str) -> tuple[int, int]:
        return estimate_tokens(request.system + request.prompt), estimate_tokens(text)
