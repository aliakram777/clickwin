"""Modeldən asılı olmayan LLM qatı: provider interfeysi, konfiqurasiya, limitlər, mock provider.

Prinsiplər:
- Model cavabı ETİBARSIZ giriş sayılır; bu modul yalnız mətn qaytarır, heç bir alət/broker/Gate/təsdiq/sandbox
  imkanı vermir və onlara import da etmir.
- API açarı yalnız mühit dəyişənindən (`load_config`) oxunur, `Secret` ilə bürünür, loqlara və xəta
  mətnlərinə düşmür (`redact`). Açar yoxdursa aydın `LLMUnavailable` qaytarılır.
- Limitlər: sorğu/cavab ölçüsü, timeout, token büdcəsi (yaddaşda) və istəyə görə xərc büdcəsi (`CostLedger`).
- Bu mərhələdə YALNIZ interfeys və `MockProvider` var; real OpenAI/Anthropic bağlantısı ayrıca mərhələdir.
  Mock cavab real model cavabı kimi təqdim olunmur (`is_mock`).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from safety.execution_context import in_broker
from safety.ledger import CostLedger

DEFAULT_LLM_LOG = Path("logs/llm.jsonl")
REAL_PROVIDER_NAMES = frozenset({"openai", "anthropic"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,31}$")


# --- xətalar ---

class LLMError(Exception):
    code = "llm_error"


class LLMUnavailable(LLMError):
    code = "unavailable"


class LLMBadRequest(LLMError):
    code = "bad_request"


class LLMTooLarge(LLMError):
    code = "too_large"


class LLMBudgetExceeded(LLMError):
    code = "budget_exceeded"


class LLMBlocked(LLMError):
    code = "blocked"


class LLMTimeout(LLMError):
    code = "timeout"


class LLMBadResponse(LLMError):
    code = "bad_response"


class LLMProviderError(LLMError):
    """Provayder xətası. `charged=False` — sorğunun haqqının tutulmadığı məlumdur (rezerv azad olunur);
    `None` — naməlum (rezerv qorunur)."""
    code = "provider_error"

    def __init__(self, message: str, charged: bool | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.charged = charged
        self.retryable = retryable


class ConfigError(ValueError):
    pass


# --- sirr və redaksiya ---

class Secret:
    """API açarı sarğısı: repr/str/format/pickle/copy ilə sızmır; yalnız `reveal()` ilə oxunur."""
    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("sirr boş olmayan sətir olmalıdır")
        object.__setattr__(self, "_v", value)

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"

    def __bool__(self) -> bool:
        return True

    def __reduce__(self) -> Any:
        raise TypeError("Secret serializasiya oluna bilməz")

    def __copy__(self) -> Any:
        raise TypeError("Secret kopyalana bilməz")

    def __deepcopy__(self, memo: Any) -> Any:
        raise TypeError("Secret kopyalana bilməz")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Secret dəyişdirilə bilməz")


_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"(?i)((?:x-)?api[_-]?key|authorization|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\",}]{4,}"),
]


def redact(text: str, secrets: Iterable[Secret] = ()) -> str:
    """Açar kimi görünən və ya konfiqurasiya edilmiş sirləri `***` ilə əvəz edir."""
    out = str(text)
    for s in secrets:
        v = s.reveal()
        if len(v) >= 4:
            out = out.replace(v, "***")
    out = _PATTERNS[0].sub("***", out)
    out = _PATTERNS[1].sub(r"\1***", out)
    out = _PATTERNS[2].sub(r"\1\2***", out)
    return out


# --- konfiqurasiya ---

def _num(name: str, v: Any, integer: bool = False, positive: bool = True) -> None:
    ok = not isinstance(v, bool) and isinstance(v, int if integer else (int, float)) and math.isfinite(v)
    if not ok or (v <= 0 if positive else v < 0):
        raise ConfigError(f"{name} {'müsbət' if positive else 'mənfi olmayan'} "
                          f"{'tam ədəd' if integer else 'rəqəm'} olmalıdır")


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "none"
    model: str = ""
    api_key: Secret | None = field(default=None, repr=False)
    timeout_s: float = 30.0
    max_output_tokens: int = 1024
    max_prompt_chars: int = 20000
    max_response_chars: int = 20000
    max_total_tokens: int = 100000
    price_in_per_1k: float = 0.0
    price_out_per_1k: float = 0.0
    cost_limit: float | None = None  # ledger varsa LLM xərci üçün agent limiti

    def __post_init__(self) -> None:
        _num("timeout_s", self.timeout_s)
        for n in ("max_output_tokens", "max_prompt_chars", "max_response_chars", "max_total_tokens"):
            _num(n, getattr(self, n), integer=True)
        _num("price_in_per_1k", self.price_in_per_1k, positive=False)
        _num("price_out_per_1k", self.price_out_per_1k, positive=False)
        if self.cost_limit is not None:
            _num("cost_limit", self.cost_limit, positive=False)

    def describe(self) -> dict[str, Any]:
        """Loqa yazıla bilən təhlükəsiz görünüş (açarsız)."""
        return {"provider": self.provider, "model": self.model, "key_configured": self.api_key is not None,
                "timeout_s": self.timeout_s, "max_output_tokens": self.max_output_tokens,
                "max_total_tokens": self.max_total_tokens}

    def __repr__(self) -> str:
        return f"LLMConfig({self.describe()})"

    __str__ = __repr__


_ENV_NUMS = {
    "NAYMERA_LLM_TIMEOUT_S": ("timeout_s", float),
    "NAYMERA_LLM_MAX_OUTPUT_TOKENS": ("max_output_tokens", int),
    "NAYMERA_LLM_MAX_PROMPT_CHARS": ("max_prompt_chars", int),
    "NAYMERA_LLM_MAX_RESPONSE_CHARS": ("max_response_chars", int),
    "NAYMERA_LLM_MAX_TOTAL_TOKENS": ("max_total_tokens", int),
}


def load_config(env: Mapping[str, str] | None = None) -> LLMConfig:
    """Konfiqurasiyanı mühit dəyişənlərindən oxuyur (yeganə yer). Açar yoxdursa `api_key=None`."""
    env = os.environ if env is None else env
    kw: dict[str, Any] = {
        "provider": (env.get("NAYMERA_LLM_PROVIDER") or "none").strip().lower() or "none",
        "model": (env.get("NAYMERA_LLM_MODEL") or "").strip(),
    }
    key = (env.get("NAYMERA_LLM_API_KEY") or "").strip()
    if key:
        kw["api_key"] = Secret(key)
    for var, (field_name, cast) in _ENV_NUMS.items():
        raw = env.get(var)
        if raw is None or not raw.strip():
            continue
        try:
            parsed = cast(raw.strip())
        except ValueError:
            raise ConfigError(f"{var} yanlışdır") from None
        _num(var, parsed, integer=cast is int)  # xəta mətnində dəyişən adı olur, dəyər yox
        kw[field_name] = parsed
    try:
        return LLMConfig(**kw)
    except ConfigError as exc:
        raise ConfigError(str(exc)) from None


# --- provider interfeysi ---

@dataclass(frozen=True)
class LLMRequest:
    system: str
    prompt: str
    max_output_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    finish_reason: str = "stop"


class LLMProvider(Protocol):
    name: str
    is_mock: bool

    def complete(self, request: LLMRequest) -> LLMResponse: ...


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3) if text else 0


class MockProvider:
    """Offline test provayderi: ssenari ilə deterministik cavab. Real model DEYİL."""
    name = "mock"
    is_mock = True

    def __init__(self, script: list | None = None, default: str | None = None, delay: float = 0.0,
                 input_tokens: int | None = None, output_tokens: int | None = None,
                 model: str = "mock-1") -> None:
        self.script = list(script or [])
        self.default = default
        self.delay = delay
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.delay:
            time.sleep(self.delay)
        if self.script:
            item = self.script.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise LLMProviderError("mock ssenarisi bitdi", charged=False)
        if isinstance(item, BaseException):
            raise item
        text = item(request) if callable(item) else item
        in_t = self.input_tokens if self.input_tokens is not None else estimate_tokens(request.system + request.prompt)
        out_t = self.output_tokens if self.output_tokens is not None else estimate_tokens(text)
        return LLMResponse(text, in_t, out_t, self.model)


@dataclass(frozen=True)
class _ProviderSpec:
    factory: Callable[[LLMConfig], Any]
    requires_key: bool


class ProviderRegistry:
    """Provider-lər adla dəyişdirilə bilər. Real provayderlər ayrıca mərhələdə qeydiyyata alınacaq."""

    def __init__(self) -> None:
        self._p: dict[str, _ProviderSpec] = {}

    def register(self, name: str, factory: Callable[[LLMConfig], Any], requires_key: bool = True) -> None:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError("provayder adı yanlışdır")
        if name in self._p:
            raise ValueError(f"provayder artıq qeydiyyatdadır: {name}")
        self._p[name] = _ProviderSpec(factory, requires_key)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._p))

    def get(self, name: str) -> _ProviderSpec | None:
        return self._p.get(name)


DEFAULT_REGISTRY = ProviderRegistry()
DEFAULT_REGISTRY.register("mock", lambda cfg: MockProvider(), requires_key=False)


def create_provider(config: LLMConfig, registry: ProviderRegistry | None = None) -> Any:
    registry = registry or DEFAULT_REGISTRY
    name = config.provider
    if not name or name == "none":
        raise LLMUnavailable("LLM provayderi seçilməyib (NAYMERA_LLM_PROVIDER)")
    spec = registry.get(name)
    if spec is None:
        if name in REAL_PROVIDER_NAMES:
            raise LLMUnavailable(f"real provayder bu mərhələdə həyata keçirilməyib ({name}); ayrıca mərhələdir")
        raise LLMUnavailable(f"naməlum provayder: {name}; mövcud: {list(registry.names())}")
    if spec.requires_key and config.api_key is None:
        raise LLMUnavailable(f"{name} üçün API açarı təyin edilməyib (NAYMERA_LLM_API_KEY)")
    provider = spec.factory(config)
    if not callable(getattr(provider, "complete", None)):
        raise LLMUnavailable(f"{name}: provayder `complete` metodu təqdim etmir")
    return provider


# --- agent xərc əhatəsi (F-3) ---

_SPEND_OP_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")


@dataclass(frozen=True)
class SpendScope:
    """Ödənişli LLM sorğusunun hansı agentə və əməliyyata aid olduğunu bildirir. `GuardedLLM` bu əhatə daxilində xərci Ledger-də AGENTİN adına
    rezerv edir, agentin büdcə limitini (və varsa ümumi limiti) AÇIQ çağırışdan əvvəl yoxlayır."""
    agent: str
    op_id: str
    agent_limit: float
    global_limit: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent, str) or not self.agent.strip() or not isinstance(self.op_id, str) or not _SPEND_OP_RE.match(self.op_id):
            raise ValueError("xərc əhatəsi: agent/op_id yanlışdır")
        for name, v in (("agent_limit", self.agent_limit), ("global_limit", self.global_limit)):
            if v is None and name == "global_limit":
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"xərc əhatəsi: {name} sonlu və mənfi olmayan rəqəm olmalıdır")


_SPEND_SCOPE: ContextVar[SpendScope | None] = ContextVar("naymera_spend_scope", default=None)


@contextmanager
def spend_scope(scope: SpendScope):
    token = _SPEND_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SPEND_SCOPE.reset(token)


# --- qoruyucu müştəri ---

@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    is_mock: bool
    cost: float = 0.0
    finish_reason: str = "stop"


def _valid_tokens(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, int) and v >= 0


class GuardedLLM:
    """Provider-i limitlərlə sarır: ölçü, timeout, token/xərc büdcəsi, stop bayrağı, redaksiya, audit (llm.jsonl).
    Model mətni qaytarılır, amma ona icra səlahiyyəti verilmir."""

    def __init__(self, provider: Any, config: LLMConfig, log_path: str | Path = DEFAULT_LLM_LOG,
                 stop_path: str | Path | None = None, ledger: CostLedger | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.provider = provider
        self.config = config
        self.log_path = Path(log_path)
        self.stop_path = Path(stop_path) if stop_path is not None else None
        self.ledger = ledger
        self.clock = clock
        self.tokens_used = 0
        self._reserved = 0
        self._lock = threading.Lock()

    @property
    def _secrets(self) -> list[Secret]:
        return [self.config.api_key] if self.config.api_key is not None else []

    def complete(self, prompt: str, system: str = "", max_output_tokens: int | None = None) -> LLMResult:
        cfg = self.config
        t0 = self.clock()
        name = getattr(self.provider, "name", "unknown")
        info: dict[str, Any] = {"provider": name, "model": cfg.model}

        def fail(status: str, exc: LLMError) -> LLMError:
            self._log(status, t0, info, error=redact(str(exc), self._secrets))
            return exc

        if self.stop_path is not None and self.stop_path.exists():
            raise fail("blocked", LLMBlocked("stop bayrağı aktivdir"))
        if not isinstance(prompt, str) or not isinstance(system, str):
            raise fail("rejected", LLMBadRequest("prompt və system mətn olmalıdır"))
        if not bool(getattr(self.provider, "is_mock", False)) and not in_broker():
            # A-8: ödənişli (real) provayder YALNIZ Effect Broker (stop/Gate/audit/Ledger) daxilində çağırılır; kənarda heç bir rezerv və ya provayder çağırışı olmur
            raise fail("blocked", LLMBlocked("ödənişli LLM çağırışı yalnız Effect Broker (Gate/Ledger) daxilində icazəlidir"))
        info["prompt_sha256"] = hashlib.sha256((system + "\x00" + prompt).encode("utf-8")).hexdigest()
        info["prompt_chars"] = len(system) + len(prompt)
        if len(system) + len(prompt) > cfg.max_prompt_chars:
            raise fail("rejected", LLMTooLarge(f"sorğu çox böyükdür: {len(system) + len(prompt)} > {cfg.max_prompt_chars}"))
        cap = cfg.max_output_tokens
        if max_output_tokens is not None:
            if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
                raise fail("rejected", LLMBadRequest("max_output_tokens müsbət tam ədəd olmalıdır"))
            cap = min(cap, max_output_tokens)
        est_in = estimate_tokens(system + prompt)
        reserve = est_in + cap

        with self._lock:  # token büdcəsi: icradan əvvəl rezerv
            if self.tokens_used + self._reserved + reserve > cfg.max_total_tokens:
                raise fail("budget_exceeded", LLMBudgetExceeded("token büdcəsi aşılır"))
            self._reserved += reserve

        op_id = ""
        scope = _SPEND_SCOPE.get()
        if scope is not None and self.ledger is None and not bool(getattr(self.provider, "is_mock", False)):
            self._release_tokens(reserve)  # agent əhatəsində real provayder: xərc kitabı olmadan agent büdcəsi yoxlana bilməz -> bağlıdır
            raise fail("budget_exceeded", LLMBudgetExceeded("agent xərc büdcəsi üçün xərc kitabı (ledger) tələb olunur"))
        if self.ledger is not None:  # xərc büdcəsi (davamlı)
            est_cost = est_in * cfg.price_in_per_1k / 1000 + cap * cfg.price_out_per_1k / 1000
            try:
                if scope is not None:  # agentə aid sorğu: Ledger-də AGENTİN adına, sabit op_id ilə (təkrar icra ikinci dəfə ödənilmir)
                    op_id = "llm:" + scope.op_id
                    res = self.ledger.reserve(op_id, scope.agent, est_cost, agent_limit=scope.agent_limit, global_limit=scope.global_limit,
                                              group_prefix="llm:", group_limit=cfg.cost_limit, description=f"llm:{name}")
                else:
                    op_id = "llm:" + uuid.uuid4().hex
                    res = self.ledger.reserve(op_id, "llm", est_cost, agent_limit=cfg.cost_limit,
                                              description=f"llm:{name}")
            except Exception:  # noqa: BLE001 — xərc kitabı əlçatmazdır: heç bir provayder çağırışı yoxdur (qapalı qalır); rezerv tranzaksiya ilə geri alınıb
                op_id = ""
                self._release_tokens(reserve)
                raise fail("budget_exceeded", LLMBudgetExceeded("xərc kitabı əlçatmazdır: sorğu bloklandı")) from None
            if res.status != "reserved":
                op_id = ""   # rezerv götürülməyib: sonrakı mərhələlər başqasının qeydinə toxunmur
                self._release_tokens(reserve)
                raise fail("budget_exceeded", LLMBudgetExceeded(res.reason or "xərc büdcəsi aşılır"))

        request = LLMRequest(system, prompt, cap, cfg.timeout_s)
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["resp"] = self.provider.complete(request)
            except BaseException as exc:  # noqa: BLE001 — nəticə kimi ötürülür
                box["exc"] = exc

        worker = threading.Thread(target=target, daemon=True)
        try:
            worker.start()
        except BaseException:   # provayder çağırılmadı (məs. thread açıla bilmədi): rezerv və token SIZMIR
            self._release_tokens(reserve)
            if op_id and self.ledger is not None:
                try:
                    self.ledger.release(op_id)
                except Exception:  # noqa: BLE001
                    self._ledger_unknown(op_id)
            raise
        worker.join(cfg.timeout_s)

        if worker.is_alive():  # abandon: sorğu haqqı tutula bilər -> xərc naməlum
            self._finish_tokens(reserve, reserve)
            self._ledger_unknown(op_id)
            raise fail("timeout", LLMTimeout(f"zaman limiti aşıldı ({cfg.timeout_s}s)"))

        if "exc" in box:
            exc = box["exc"]
            charged = getattr(exc, "charged", None)
            if charged is False:
                self._finish_tokens(reserve, 0)
                if op_id:
                    self.ledger.release(op_id)
            else:
                self._finish_tokens(reserve, reserve)
                self._ledger_unknown(op_id)
            msg = redact(f"{type(exc).__name__}: {exc}", self._secrets)
            self._log("provider_error", t0, info, error=msg)
            raise LLMProviderError(msg, charged=charged,
                                   retryable=bool(getattr(exc, "retryable", False))) from None

        resp = box.get("resp")
        usable = (isinstance(resp, LLMResponse) and isinstance(resp.text, str)
                  and _valid_tokens(resp.input_tokens) and _valid_tokens(resp.output_tokens))
        if not usable:
            self._finish_tokens(reserve, reserve)
            self._ledger_unknown(op_id)
            raise fail("bad_response", LLMBadResponse("provayder cavabı yanlışdır"))

        actual = resp.input_tokens + resp.output_tokens
        cost = resp.input_tokens * cfg.price_in_per_1k / 1000 + resp.output_tokens * cfg.price_out_per_1k / 1000
        self._finish_tokens(reserve, actual)
        if op_id:  # hesablaşma uğursuzdursa nəticə QAYTARILMIR: rezerv qorunur və sahibin uzlaşdırması üçün işarələnir
            try:
                settled = bool(self.ledger.commit(op_id, cost))
            except Exception:  # noqa: BLE001
                settled = False
            if not settled:
                try:
                    self._ledger_unknown(op_id)
                except Exception:  # noqa: BLE001
                    pass
                raise fail("settlement_failed", LLMBadResponse("xərc hesablaşması uğursuz oldu; rezerv qorunur (uzlaşdırma tələb olunur)"))
        info.update(input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, response_chars=len(resp.text))
        if resp.output_tokens > cap:
            raise fail("bad_response", LLMBadResponse("provayder çıxış token limitini aşdı"))
        if len(resp.text) > cfg.max_response_chars:
            raise fail("response_too_large",
                       LLMTooLarge(f"cavab çox böyükdür: {len(resp.text)} > {cfg.max_response_chars}"))
        self._log("ok", t0, info)
        return LLMResult(resp.text, resp.input_tokens, resp.output_tokens, resp.model or cfg.model, name,
                         bool(getattr(self.provider, "is_mock", False)), cost, resp.finish_reason)

    # --- daxili ---
    def _release_tokens(self, reserve: int) -> None:
        with self._lock:
            self._reserved -= reserve

    def _finish_tokens(self, reserve: int, actual: int) -> None:
        with self._lock:
            self._reserved -= reserve
            self.tokens_used += actual

    def _ledger_unknown(self, op_id: str) -> None:
        if op_id and self.ledger is not None:
            self.ledger.mark_unknown(op_id)  # avtomatik azad edilmir: əl ilə uzlaşdırma

    def _log(self, status: str, t0: float, info: Mapping[str, Any], error: str = "") -> None:
        """Yalnız metadata: sorğunun hash-i və ölçüləri; sorğu/cavab mətni və açar yazılmır."""
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": "llm_call",
               "status": status, "duration_s": round(self.clock() - t0, 4), **info}
        if error:
            row["error"] = error
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
