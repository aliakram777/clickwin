"""Real AI runtime: OpenAI-uyğun (chat completions) provayder adapteri. Şəbəkə kodu BURADA YOXDUR: nəqliyyat (`Transport`) inyeksiya olunur
(real nəqliyyat `runtime/transport.py`-dədir; testlər fake nəqliyyat və ya loopback istifadə edir).

- Açar yalnız `Secret` kimi daxil olur (mühit dəyişənindən `runtime/env.py` oxuyur), heç vaxt repr/xəta/loqa düşmür.
- Cloud endpoint: yalnız HTTPS və DNS adı (IP-literal/localhost/özəl şəbəkə qadağandır). Şəxsi (lokal) server: loopback/özəl şəbəkə (düz HTTP ola bilər)
  və ya TLS ilə ictimai ad.
- Cavab etibarsız mətndir; səhvlərdə cavab gövdəsi və başlıqlar xəta mətninə yazılmır.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from agents.hybrid import EngineTimeout, EngineUnavailable
from agents.llm import (LLMBadResponse, LLMProviderError, LLMRequest, LLMResponse, Secret, estimate_tokens)


class TransportError(Exception):
    def __init__(self, message: str, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict
    body: bytes
    timeout: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]


_ENFORCING_TRANSPORTS: set[type] = set()      # yalnız `runtime/` nəqliyyat sinifləri (idxal vaxtı) özünü qeyd edir: hər sorğuda `validate_endpoint(url, self.kind)` tətbiq edənlər


def register_endpoint_enforcing_transport(cls: type) -> type:
    """Sinif dekoratoru: `cls` nümunələrinin `kind` atributu ("cloud"/"private") hər sorğuda endpoint siyasəti kimi tətbiq edilir (`runtime/transport.py`)."""
    _ENFORCING_TRANSPORTS.add(cls)
    return cls


def enforces_endpoint_kind(transport: Any, kind: str) -> bool:
    """`transport` TAM tipi qeydli endpoint-tətbiq edən nəqliyyatdır və `kind` üçün qurulubdur (ixtiyari callable/alt sinif/ördək tipi → False: fail-closed)."""
    return type(transport) in _ENFORCING_TRANSPORTS and getattr(transport, "kind", None) == kind


class EndpointError(ValueError):
    pass


_URL = re.compile(r"^(?P<scheme>https?)://(?P<auth>[^/?#]+)(?P<path>/[^?#]*)?(?P<rest>[?#].*)?$", re.I)
_HOST = re.compile(r"^(?:\[(?P<v6>[0-9a-fA-F:]+)\]|(?P<name>[A-Za-z0-9.\-]+))(?::(?P<port>\d{1,5}))?$")
_PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa")


def validate_endpoint(url: Any, kind: str) -> str:
    """Endpoint-i yoxlayır və normallaşdırılmış baza URL-ni qaytarır (sonda `/` yoxdur)."""
    if kind not in ("cloud", "private"):
        raise ValueError("kind cloud|private olmalıdır")
    if not isinstance(url, str) or not url or len(url) > 300 or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        raise EndpointError("URL yanlışdır")
    m = _URL.match(url)
    if m is None or m.group("rest"):
        raise EndpointError("URL yanlışdır (sorğu sətri/fraqment qadağandır)")
    scheme, auth, path = m.group("scheme").lower(), m.group("auth"), (m.group("path") or "").rstrip("/")
    if "@" in auth:
        raise EndpointError("URL-də istifadəçi məlumatı olmamalıdır")
    hm = _HOST.match(auth)
    if hm is None:
        raise EndpointError("host yanlışdır")
    host = (hm.group("v6") or hm.group("name")).lower()
    port = int(hm.group("port")) if hm.group("port") else None
    if port is not None and not 1 <= port <= 65535:
        raise EndpointError("port yanlışdır")
    if any(seg in ("..", ".") for seg in path.split("/")):
        raise EndpointError("yol yanlışdır")
    try:
        ip: ipaddress._BaseAddress | None = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if kind == "cloud":
        if scheme != "https":
            raise EndpointError("Cloud endpoint yalnız HTTPS ola bilər")
        if ip is not None or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES) or "." not in host:
            raise EndpointError("Cloud endpoint ictimai DNS adı olmalıdır")
        if port not in (None, 443):
            raise EndpointError("Cloud endpoint üçün qeyri-standart port")
    else:
        # `private` YALNIZ həqiqətən lokal/özəl hədəfdir: loopback, özəl şəbəkə IP-si, `localhost`, özəl şəkilçili ad (.local/.lan/.internal/.home.arpa) və ya tək etiketli ad (docker/LAN: `ollama`).
        # İctimai DNS adı/IP (HTTPS ilə də) ödənişli uzaq servis ola bilər və LokalEngine kimi (ledger-siz, razılıqsız) QƏBUL EDİLMİR: bunun üçün GuardedLLM + paylaşılan CostLedger (kind="cloud") tələb olunur.
        local_net = (ip is not None and (ip.is_loopback or (ip.is_private and not ip.is_link_local))) \
            or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES) or (ip is None and "." not in host)
        if not local_net:
            raise EndpointError("lokal endpoint yalnız loopback/özəl şəbəkə/lokal ad ola bilər; ictimai (ödənişli ola bilən) host üçün Cloud provayder + GuardedLLM + paylaşılan CostLedger istifadə edin")
    return f"{scheme}://{auth}{path}"


_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def ip_allowed(ip: ipaddress._BaseAddress, kind: str) -> bool:
    """Çözülmüş (həqiqi bağlantı) ünvan siyasəti. IPv4-ə xəritələnmiş IPv6 (`::ffff:a.b.c.d`) əvvəlcə açılır.
    - `private` (lokal): YALNIZ loopback (127/8, ::1) və RFC1918/ULA (10/8, 172.16/12, 192.168/16, fc00::/7). İctimai, link-local (169.254/16, fe80::/10), multicast, unspecified (0.0.0.0, ::),
      CGNAT (100.64/10), sənədləşmə/rezerv aralıqları və qalan HƏR ŞEY rədd edilir.
    - `cloud`: YALNIZ qlobal marşrutlanan ictimai unicast ünvan (`is_global`); loopback/özəl/link-local/multicast/unspecified/CGNAT rədd edilir."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if kind == "private":
        return ip.is_loopback or any(ip.version == n.version and ip in n for n in _PRIVATE_NETWORKS)
    if kind == "cloud":
        return ip.is_global and not ip.is_multicast
    raise ValueError("kind cloud|private olmalıdır")


def check_resolved_addresses(host: str, addresses: Sequence[str], kind: str) -> list[str]:
    """Hostname-in BÜTÜN A/AAAA nəticələri yoxlanılır: boşdursa, oxunmursa və ya İSTƏNİLƏN biri siyasətə uyğun deyilsə (həm özəl, həm ictimai qaytaran ad daxil) `EndpointError` (FAIL-CLOSED).
    Yoxlanmış ünvanlar qaytarılır: nəqliyyat YALNIZ bunlardan birinə (ikinci DNS sorğusu olmadan) qoşulur (DNS rebinding/TOCTOU yoxdur)."""
    out: list[str] = []
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            raise EndpointError("host ünvanı oxunmadı (fail-closed)") from None
        if not ip_allowed(ip, kind):
            raise EndpointError(f"host '{host[:60]}' siyasətə uyğun olmayan ünvana çözülür ({kind} endpoint üçün icazəsiz IP)")
        if str(ip) not in out:
            out.append(str(ip))
    if not out:
        raise EndpointError(f"host '{host[:60]}' heç bir ünvana çözülmədi")
    return out


class ProviderTimeout(LLMProviderError):
    pass


class OpenAICompatibleProvider:
    """`LLMProvider` interfeysi: OpenAI-uyğun `/v1/chat/completions`. Real provayder (`is_mock=False`); qoşulma/razılıq/büdcə qapıları
    üst qatdadır (`CloudEngine`, `GuardedLLM`, `runtime/env.py`)."""
    is_mock = False

    def __init__(self, base_url: str, model: str, api_key: Secret | None, transport: Transport, *, kind: str = "cloud",
                 name: str = "openai_compat") -> None:
        self.base_url = validate_endpoint(base_url, kind)
        if kind == "cloud" and api_key is None:
            raise ValueError("Cloud provayder üçün API açarı tələb olunur")
        if not isinstance(model, str) or not model.strip() or len(model) > 80:
            raise ValueError("model adı yanlışdır")
        self.model, self.kind, self.name = model, kind, name
        self._key, self._transport = api_key, transport

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "base_url": self.base_url, "model": self.model, "key_configured": self._key is not None}

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider({self.describe()})"

    __str__ = __repr__

    def _url(self) -> str:
        return self.base_url + ("/chat/completions" if self.base_url.endswith("/v1") else "/v1/chat/completions")

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = ([{"role": "system", "content": request.system}] if request.system else []) + [{"role": "user", "content": request.prompt}]
        body = json.dumps({"model": self.model, "messages": messages, "max_tokens": request.max_output_tokens, "temperature": 0,
                           "stream": False}).encode("utf-8")
        try:        # hər sorğuda yenidən yoxla (atribut dəyişdirilsə belə lokal provayder ictimai hosta getmir): FAIL-CLOSED
            validate_endpoint(self.base_url, self.kind)
        except EndpointError:
            raise LLMProviderError("endpoint siyasəti pozulub: sorğu göndərilmədi", charged=False) from None
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "naymera"}
        if self._key is not None:
            headers["Authorization"] = "Bearer " + self._key.reveal()
        try:
            resp = self._transport(HttpRequest("POST", self._url(), headers, body, float(request.timeout_s)))
        except TransportError as exc:
            if exc.timeout:
                raise ProviderTimeout("provayder vaxt aşımı", charged=None, retryable=True) from None
            raise LLMProviderError("provayderə qoşulmaq mümkün olmadı", charged=False, retryable=True) from None
        return self._parse(resp, request)

    @staticmethod
    def _parse(resp: HttpResponse, request: LLMRequest) -> LLMResponse:
        status = resp.status
        if status in (401, 403):
            raise LLMProviderError(f"provayder HTTP {status}: etibarnamə rədd edildi", charged=False, retryable=False)
        if status == 429:
            raise LLMProviderError("provayder HTTP 429: sürət limiti", charged=False, retryable=True)
        if status >= 500:
            raise LLMProviderError(f"provayder HTTP {status}: server xətası", charged=None, retryable=True)
        if status != 200:
            raise LLMProviderError(f"provayder HTTP {status}", charged=False, retryable=False)
        try:
            data = json.loads(resp.body.decode("utf-8"))
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError):
            raise LLMBadResponse("provayder cavabı yanlış formatdadır") from None
        if not isinstance(text, str):
            raise LLMBadResponse("provayder cavabı mətn deyil")
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}

        def tokens(key: str, fallback: int) -> int:
            v = usage.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else fallback

        model = data.get("model") if isinstance(data.get("model"), str) else ""
        finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else "stop"
        return LLMResponse(text, tokens("prompt_tokens", estimate_tokens(request.system + request.prompt)),
                           tokens("completion_tokens", estimate_tokens(text)), (model or "")[:80], finish)


class OpenAICompatLocalRuntime:
    """`LocalRuntime` interfeysi: şəxsi/lokal OpenAI-uyğun server (llama.cpp, Ollama və s.) üçün. Real model deyil, real SERVER bağlantısıdır:
    server modelini yükləməyibsə sorğu uğursuz olur və mühərrik `unavailable` hesab olunur."""
    is_mock = False

    def __init__(self, provider: OpenAICompatibleProvider, languages: Sequence[str] = ("en",), name: str = "private-server") -> None:
        if provider.kind != "private":
            raise ValueError("lokal runtime yalnız şəxsi endpoint ilə işləyir")
        validate_endpoint(provider.base_url, "private")      # lokal runtime yalnız həqiqətən lokal/özəl endpoint-lə (ictimai/ödənişli uzaq servis YOX)
        self.provider, self.languages, self.name = provider, tuple(languages), name
        self.model = provider.model

    def loaded(self) -> bool:
        return True

    def generate(self, prompt: str, system: str, max_tokens: int) -> str:
        try:
            return self.provider.complete(LLMRequest(system, prompt, max_tokens, 30.0)).text
        except ProviderTimeout:
            raise EngineTimeout("şəxsi server vaxt aşımı") from None
        except (LLMProviderError, LLMBadResponse):
            raise EngineUnavailable("şəxsi server cavab vermədi") from None
