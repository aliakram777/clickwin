"""Hibrid AI beyni: Cloud və Local mühərrikləri eyni standart interfeysdə; AUTO / CLOUD / LOCAL; təhlükəsiz fallback.

Əsas qaydalar:
- Mühərrik yalnız MƏTN yaradır. İcra (pul, hesab, nəşr, alət) bu moduldan keçmir: təsdiq/Broker/Gate/CEO yolları dəyişməyib və burada import olunmur.
- Hər sorğu (`request_id`) ən çox BİR nəticə ilə bağlanır: təkrar eyni `request_id` saxlanmış nəticəni qaytarır (mühərrik yenidən çağırılmır);
  eyni anda gələn təkrar `in_progress` alır. Fallback yalnız əvvəlki mühərrik UĞURSUZ olduqda və ardıcıl (paralel deyil) edilir.
- CLOUD rejimi səssiz Local-a keçmir; LOCAL rejimi Cloud-a keçmir. AUTO sadə işləri Local-a, mürəkkəbləri Cloud-a üstünlük verir.
- Real mühərrik simulyasiyadan üstündür; mock/simulyasiya nəticəsi həmişə `is_mock=True` ilə qaytarılır. Qoşulmayan imkan `not_configured`.
- Ödənişli real Cloud `cloud_paid_use`, yaddaşın Cloud-a ötürülməsi `cloud_data_transfer` + məxfilik siyasəti, cihazda real model
  `local_resource_use` razılığı tələb edir. Sirr bənzər mətn Cloud-a göndərilmir. Mühərrik çıxışı ETİBARSIZ mətndir.
- Yaddaş yalnız oxunur (çağıran `MemoryRecord` siyahısı verir); burada yaddaşa yazma/silmə yoxdur.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from agents.llm import (LLMBlocked, LLMBudgetOverrun, LLMBudgetExceeded, LLMError, LLMResult, LLMTimeout, LLMUnavailable, estimate_tokens,
                        redact)
from agents.local_models import LocalModelSpec, ResourceGuard
from agents.memory import CATEGORIES, MemoryRecord, detect_secrets, untrusted_context
from safety.dbconn import connect

KINDS = ("chat", "plan", "summarize", "classify")
CONSENT_SCOPES = ("cloud_paid_use", "cloud_data_transfer", "local_model_download", "local_resource_use")
CLOUD_SHAREABLE_CATEGORIES = tuple(c for c in CATEGORIES if c not in ("approval", "result"))
MAX_PROMPT = 8000
MAX_OUTPUT = 4000
_RID = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")


class Mode(str, Enum):
    AUTO = "auto"
    CLOUD = "cloud"
    LOCAL = "local"


# --- tapşırıq təsnifatı ---

@dataclass(frozen=True)
class Task:
    kind: str
    complexity: str   # simple | complex
    language: str     # az | en | ru | tr


_COMPLEX_WORDS = ("planlaşdır", "təhlil et", "addım-addım", "step by step", "analyze", "analyse", "разбери", "анализ", "план",
                  "plan yap", "analiz et", "ətraflı", "in detail")
_EN_WORDS = {"hello", "how", "the", "you", "are", "what", "please", "is", "and", "can", "help", "hi"}
_TR_WORDS = ("merhaba", "nasılsın", "teşekkür", "evet", "hayır", "için", "değil", "lütfen")


def _language(text: str) -> str:
    low = text.casefold()
    if any("Ѐ" <= c <= "ӿ" for c in low):
        return "ru"
    if "ə" in low:
        return "az"
    if any(w in low for w in _TR_WORDS):
        return "tr"
    if set(re.findall(r"[a-z]+", low)) & _EN_WORDS:
        return "en"
    return "az"  # layihənin əsas dili


def classify_task(kind: str, text: str, language: str | None = None) -> Task:
    """Deterministik heuristika (semantik anlama deyil): plan həmişə mürəkkəbdir; uzun mətn və təhlil/planlaşdırma sözləri mürəkkəbdir."""
    low = text.casefold()
    complex_ = kind == "plan" or len(text) > 600 or any(w in low for w in _COMPLEX_WORDS)
    return Task(kind, "complex" if complex_ else "simple", language or _language(text))


# --- mühərrik interfeysi ---

@dataclass(frozen=True)
class EngineStatus:
    kind: str                # cloud | local
    name: str
    state: str               # ready | simulation | unavailable | not_configured
    is_mock: bool
    reason: str = ""
    model: str = ""
    supported_kinds: tuple[str, ...] = ()
    consent_needed: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.state in ("ready", "simulation")


@dataclass(frozen=True)
class EngineOutput:
    text: str
    model: str
    provider: str
    is_mock: bool
    cost: float = 0.0


class EngineError(Exception):
    fatal = False            # True: fallback yoxdur (məs. stop bayrağı)
    outcome_unknown = False  # True: sorğu işlənmiş ola bilər (vaxt aşımı)


class EngineUnavailable(EngineError):
    pass


class EngineTimeout(EngineError):
    outcome_unknown = True


class EngineBlocked(EngineError):
    fatal = True


class ConsentMissing(EngineError):
    pass


class Engine(Protocol):
    """Cloud və Local üçün ORTAQ interfeys."""
    kind: str
    name: str

    def status(self) -> EngineStatus: ...
    def supports(self, task: Task) -> tuple[bool, str]: ...
    def consent_scopes(self) -> tuple[str, ...]: ...
    def generate(self, prompt: str, system: str, request_id: str) -> EngineOutput: ...


# --- Cloud ---

class CloudEngine:
    """Provayderdən asılı olmayan Cloud adapteri: mövcud `GuardedLLM`-i sarır (açar yalnız serverin env-indədir, burada heç yerdə yoxdur)."""
    kind = "cloud"

    def __init__(self, client: Any, online: Callable[[], bool] | None = None, name: str = "cloud") -> None:
        self.client, self._online, self.name = client, online, name

    def _is_mock(self) -> bool:
        return True if self.client is None else bool(getattr(getattr(self.client, "provider", None), "is_mock", True))

    def status(self) -> EngineStatus:
        if self.client is None:
            return EngineStatus("cloud", self.name, "not_configured", True, "provayder seçilməyib")
        model = str(getattr(getattr(self.client, "provider", None), "model", ""))
        if self._online is not None and not self._online():
            return EngineStatus("cloud", self.name, "unavailable", self._is_mock(), "offline", model)
        if self._is_mock():
            return EngineStatus("cloud", self.name, "simulation", True, "simulyasiya: real model deyil", model)
        return EngineStatus("cloud", self.name, "ready", False, "", model, consent_needed=self.consent_scopes())

    def supports(self, task: Task) -> tuple[bool, str]:
        return True, ""

    def consent_scopes(self) -> tuple[str, ...]:
        return () if self.client is None or self._is_mock() else ("cloud_paid_use",)

    def generate(self, prompt: str, system: str, request_id: str) -> EngineOutput:
        if self.client is None:
            raise EngineUnavailable("not_configured")
        if self._online is not None and not self._online():
            raise EngineUnavailable("offline")
        try:
            res: LLMResult = self.client.complete(prompt, system=system)
        except LLMBlocked as exc:
            raise EngineBlocked(str(exc)) from None
        except LLMTimeout as exc:
            raise EngineTimeout(str(exc)) from None
        except LLMBudgetOverrun as exc:   # ödənilmiş, lakin tavanı/rezervi aşan cavab: "uğur" kimi qaytarılmır (kəsici açılıb)
            raise EngineUnavailable(f"budget_overrun: {exc}") from None
        except LLMBudgetExceeded as exc:
            raise EngineUnavailable(f"budget: {exc}") from None
        except LLMUnavailable as exc:
            raise EngineUnavailable(str(exc)) from None
        except LLMError as exc:
            raise EngineUnavailable(f"{exc.code}: {exc}") from None
        return EngineOutput(res.text, res.model, res.provider, bool(res.is_mock), float(res.cost))


# --- Local ---

class LocalRuntime(Protocol):
    name: str
    is_mock: bool
    languages: tuple[str, ...]

    def loaded(self) -> bool: ...
    def generate(self, prompt: str, system: str, max_tokens: int) -> str: ...


class MockLocalRuntime:
    """Real model DEYİL: interfeys müqaviləsini yoxlamaq üçün."""
    name = "mock-local"
    is_mock = True
    languages = ("az", "en", "ru", "tr")

    def __init__(self, loaded: bool = True, default: str = "lokal simulyasiya cavabı") -> None:
        self._loaded, self.default, self.calls = loaded, default, 0

    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def generate(self, prompt: str, system: str, max_tokens: int) -> str:
        self.calls += 1
        return self.default


class PersonalServerRuntime:
    """Şəxsi serverdəki yerli model üçün adapter. Şəbəkə kodu YOXDUR: nəqliyyat (`transport`) çağıran tərəfindən verilir; verilməyibsə
    `loaded()` False olur (NOT CONFIGURED). `is_mock=False` yalnız nəqliyyatın həqiqətən real serverə getdiyini çağıran açıq bildirdikdə."""

    def __init__(self, transport: Callable[[str, dict], dict] | None = None, name: str = "private-server", model: str = "",
                 is_mock: bool = True, languages: Sequence[str] = ("en",)) -> None:
        self.transport, self.name, self.model, self.is_mock = transport, name, model, is_mock
        self.languages = tuple(languages)

    def loaded(self) -> bool:
        return self.transport is not None

    def generate(self, prompt: str, system: str, max_tokens: int) -> str:
        if self.transport is None:
            raise EngineUnavailable("transport qoşulmayıb")
        try:
            out = self.transport("generate", {"prompt": prompt, "system": system, "max_tokens": max_tokens, "model": self.model})
        except TimeoutError:
            raise EngineTimeout("şəxsi server vaxt aşımı") from None
        except Exception:  # noqa: BLE001 — nəqliyyat xətası: daxili detal sızmır
            raise EngineUnavailable("şəxsi server əlçatmazdır") from None
        text = out.get("text") if isinstance(out, dict) else None
        if not isinstance(text, str):
            raise EngineUnavailable("şəxsi server yanlış cavab verdi")
        return text


class LocalEngine:
    kind = "local"

    def __init__(self, runtime: Any = None, spec: LocalModelSpec | None = None, guard: ResourceGuard | None = None,
                 consent_scope: str | None = "local_resource_use", name: str = "local") -> None:
        self.runtime, self.spec, self.guard, self.consent_scope, self.name = runtime, spec, guard, consent_scope, name

    def _mock(self) -> bool:
        return True if self.runtime is None else bool(getattr(self.runtime, "is_mock", True))

    def status(self) -> EngineStatus:
        model = self.spec.id if self.spec else str(getattr(self.runtime, "model", ""))
        kinds = self.spec.kinds if self.spec else ("chat", "classify", "summarize")
        if self.runtime is None:
            return EngineStatus("local", self.name, "not_configured", True, "model/runtime yoxdur", model, kinds)
        if not self.runtime.loaded():
            return EngineStatus("local", self.name, "not_configured", self._mock(), "model_not_loaded", model, kinds)
        if self._mock():
            return EngineStatus("local", self.name, "simulation", True, "simulyasiya: real model deyil", model, kinds)
        return EngineStatus("local", self.name, "ready", False, "", model, kinds, self.consent_scopes())

    def supports(self, task: Task) -> tuple[bool, str]:
        kinds = self.spec.kinds if self.spec else ("chat", "classify", "summarize")
        if task.kind not in kinds:
            return False, f"kind:{task.kind}"
        max_c = self.spec.max_complexity if self.spec else "simple"
        if task.complexity == "complex" and max_c != "complex":
            return False, "complexity:complex"
        langs = self.spec.languages if self.spec else tuple(getattr(self.runtime, "languages", ()))
        if task.language not in langs:
            return False, f"language:{task.language}"
        return True, ""

    def consent_scopes(self) -> tuple[str, ...]:
        return (self.consent_scope,) if (self.consent_scope and self.runtime is not None and not self._mock()) else ()

    def generate(self, prompt: str, system: str, request_id: str) -> EngineOutput:
        if self.runtime is None or not self.runtime.loaded():
            raise EngineUnavailable("model_not_loaded")
        if self.guard is not None:
            ok, why = self.guard.check_run(len(prompt) + len(system))
            if not ok:
                raise EngineUnavailable(f"resources_low:{why}")
        max_tokens = self.guard.limits.max_output_tokens if self.guard is not None else 256
        text = self.runtime.generate(prompt, system, max_tokens)
        return EngineOutput(text, self.status().model, self.name, self._mock())


# --- sağlamlıq izləmə (dövrə kəsici) ---

class HealthTracker:
    """Ardıcıl uğursuzluqdan sonra mühərriki müvəqqəti kənarlaşdırır; müddət bitəndə bir yoxlama sorğusuna icazə verir."""

    def __init__(self, clock: Callable[[], float] = time.time, threshold: int = 3, cooldown_s: float = 30.0) -> None:
        self.clock, self.threshold, self.cooldown_s = clock, threshold, cooldown_s
        self._fails: dict[str, int] = {}
        self._opened: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, kind: str) -> bool:
        with self._lock:
            return self._fails.get(kind, 0) >= self.threshold and self.clock() - self._opened.get(kind, 0.0) < self.cooldown_s

    def record(self, kind: str, ok: bool) -> None:
        with self._lock:
            if ok:
                self._fails[kind] = 0
            else:
                self._fails[kind] = self._fails.get(kind, 0) + 1
                if self._fails[kind] >= self.threshold:
                    self._opened[kind] = self.clock()


# --- davamlı vəziyyət: rejim, razılıq, məxfilik, sorğu kitabı ---

@dataclass(frozen=True)
class Privacy:
    cloud_may_receive_memory: bool = False
    cloud_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    state: str          # started | done | failed
    kind: str
    prompt_sha: str
    engine: str
    text: str
    provider: str
    model: str
    is_mock: bool
    mode: str
    tries: int


class AiStore:
    def __init__(self, db_path: str | Path, clock: Callable[[], float] = time.time, stale_s: float = 300.0) -> None:
        self.clock, self.stale_s = clock, stale_s
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._c = connect(str(db_path), isolation_level=None, timeout=30)
        self._c.row_factory = sqlite3.Row
        for stmt in (
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS consents (scope TEXT PRIMARY KEY, granted INTEGER NOT NULL, who TEXT NOT NULL, ts REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, who TEXT NOT NULL, what TEXT NOT NULL, detail TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, state TEXT NOT NULL, kind TEXT NOT NULL, prompt_sha TEXT NOT NULL, "
            "engine TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', "
            "is_mock INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT '', tries INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL)",
        ):
            self._c.execute(stmt)

    def close(self) -> None:
        with self._lock:
            self._c.close()

    def _log(self, who: str, what: str, detail: str) -> None:
        self._c.execute("INSERT INTO changes (ts, who, what, detail) VALUES (?, ?, ?, ?)", (self.clock(), who, what, detail))

    def changes(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return [(r["who"], r["what"], r["detail"]) for r in self._c.execute("SELECT who, what, detail FROM changes ORDER BY id")]

    # rejim
    def mode(self) -> Mode:
        with self._lock:
            row = self._c.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()
        return Mode(row["value"]) if row else Mode.AUTO

    def set_mode(self, mode: Any, who: str) -> None:
        m = Mode(mode)  # yanlış dəyərdə ValueError
        with self._lock:
            self._c.execute("INSERT INTO settings (key, value) VALUES ('mode', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (m.value,))
            self._log(who, "mode", m.value)

    # razılıq (defolt: yoxdur)
    def consent(self, scope: str) -> bool:
        with self._lock:
            row = self._c.execute("SELECT granted FROM consents WHERE scope = ?", (scope,)).fetchone()
        return bool(row["granted"]) if row else False

    def consents(self) -> dict[str, bool]:
        return {s: self.consent(s) for s in CONSENT_SCOPES}

    def set_consent(self, scope: str, granted: bool, who: str) -> None:
        if scope not in CONSENT_SCOPES:
            raise ValueError(f"naməlum razılıq sahəsi: {scope}")
        with self._lock:
            self._c.execute("INSERT INTO consents (scope, granted, who, ts) VALUES (?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET "
                            "granted = excluded.granted, who = excluded.who, ts = excluded.ts", (scope, int(bool(granted)), who, self.clock()))
            self._log(who, "consent", f"{scope}={bool(granted)}")

    # məxfilik
    def privacy(self) -> Privacy:
        with self._lock:
            row = self._c.execute("SELECT value FROM settings WHERE key = 'privacy'").fetchone()
        if not row:
            return Privacy()
        d = json.loads(row["value"])
        return Privacy(bool(d["cloud_may_receive_memory"]), tuple(d["cloud_categories"]))

    def set_privacy(self, cloud_may_receive_memory: bool, cloud_categories: Sequence[str], actor: str) -> None:
        cats = tuple(sorted(set(cloud_categories)))
        if not set(cats) <= set(CLOUD_SHAREABLE_CATEGORIES):
            raise ValueError(f"Cloud-a ötürülə bilməyən kateqoriya: {sorted(set(cats) - set(CLOUD_SHAREABLE_CATEGORIES))}")
        value = json.dumps({"cloud_may_receive_memory": bool(cloud_may_receive_memory) and bool(cats), "cloud_categories": list(cats)})
        with self._lock:
            self._c.execute("INSERT INTO settings (key, value) VALUES ('privacy', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (value,))
            self._log(actor, "privacy", value)

    # sorğu kitabı (təkrar icranın qarşısı)
    def _rec(self, row: sqlite3.Row | None) -> RequestRecord | None:
        if row is None:
            return None
        return RequestRecord(row["request_id"], row["state"], row["kind"], row["prompt_sha"], row["engine"], row["text"], row["provider"],
                             row["model"], bool(row["is_mock"]), row["mode"], row["tries"])

    def request(self, request_id: str) -> RequestRecord | None:
        with self._lock:
            return self._rec(self._c.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone())

    def begin(self, request_id: str, kind: str, prompt_sha: str, mode: str) -> tuple[str, RequestRecord]:
        """Atomik: new | retry | done | in_progress | mismatch."""
        now = self.clock()
        with self._lock:
            cur = self.request(request_id)
            if cur is None:
                self._c.execute("INSERT INTO requests (request_id, state, kind, prompt_sha, mode, created, updated) VALUES (?, 'started', ?, ?, ?, ?, ?)",
                                (request_id, kind, prompt_sha, mode, now, now))
                return "new", self.request(request_id)  # type: ignore[return-value]
            if cur.prompt_sha != prompt_sha or cur.kind != kind:
                return "mismatch", cur
            if cur.state == "done":
                return "done", cur
            updated = self._c.execute("SELECT updated FROM requests WHERE request_id = ?", (request_id,)).fetchone()["updated"]
            if cur.state == "started" and now - updated <= self.stale_s:
                return "in_progress", cur
            self._c.execute("UPDATE requests SET state = 'started', tries = tries + 1, mode = ?, updated = ? WHERE request_id = ?",
                            (mode, now, request_id))
            return "retry", self.request(request_id)  # type: ignore[return-value]

    def finish(self, request_id: str, engine: str, text: str, provider: str, model: str, is_mock: bool) -> bool:
        with self._lock:
            cur = self._c.execute("UPDATE requests SET state = 'done', engine = ?, text = ?, provider = ?, model = ?, is_mock = ?, updated = ? "
                                  "WHERE request_id = ? AND state = 'started'", (engine, text, provider, model, int(is_mock), self.clock(), request_id))
            return cur.rowcount == 1

    def fail(self, request_id: str) -> None:
        with self._lock:
            self._c.execute("UPDATE requests SET state = 'failed', updated = ? WHERE request_id = ? AND state = 'started'", (self.clock(), request_id))


# --- kontekst (yaddaş) ---

class ContextBuilder:
    """Yaddaş qeydlərini ETİBARSIZ məlumat bloku kimi bükür (mövcud `untrusted_context`). Yalnız oxu: qeydlər çağıran tərəfindən verilir."""

    def build(self, records: Sequence[MemoryRecord], per_record: int = 500, max_chars: int = 4000) -> str:
        return untrusted_context(list(records), per_record, max_chars)


# --- router ---

@dataclass(frozen=True)
class Attempt:
    engine: str
    outcome: str   # ok | unavailable | timeout_unknown | blocked | skipped_unavailable | skipped_unsupported | skipped_consent | skipped_egress | skipped_mock | skipped_budget
    detail: str = ""


@dataclass(frozen=True)
class RouteResult:
    status: str    # ok | in_progress | blocked | cloud_unavailable | local_unavailable | unsupported_locally | all_unavailable |
                   # consent_required | blocked_egress | bad_request
    request_id: str = ""
    text: str = ""
    engine: str = ""
    provider: str = ""
    model: str = ""
    is_mock: bool = False
    mode: str = ""
    reason: str = ""
    attempts: tuple[Attempt, ...] = ()
    consent_needed: tuple[str, ...] = ()
    privacy: dict = field(default_factory=dict)
    duplicate: bool = False


@dataclass(frozen=True)
class RouterStatus:
    mode: str
    active: str    # cloud | local | none
    cloud: EngineStatus
    local: EngineStatus


def _sanitize(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    t = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    return t.strip()[:MAX_OUTPUT]


def _short(text: Any) -> str:
    return redact(" ".join(str(text).split()))[:120]


class HybridRouter:
    def __init__(self, cloud: Engine, local: Engine, store: AiStore, *, clock: Callable[[], float] = time.time,
                 stop_path: str | Path | None = None, health: HealthTracker | None = None,
                 context: ContextBuilder | None = None) -> None:
        self.cloud, self.local, self.store, self.clock = cloud, local, store, clock
        self.stop_path = Path(stop_path) if stop_path is not None else None
        self.health = health or HealthTracker(clock)
        self.context = context or ContextBuilder()

    # --- status ---
    def _status(self, eng: Engine) -> EngineStatus:
        st = eng.status()
        if st.usable and self.health.blocked(eng.kind):
            return replace(st, state="unavailable", reason="circuit_open")
        return st

    def status(self) -> RouterStatus:
        mode, cloud, local = self.store.mode(), self._status(self.cloud), self._status(self.local)
        if mode is Mode.CLOUD:
            active = "cloud" if cloud.usable else "none"
        elif mode is Mode.LOCAL:
            active = "local" if local.usable else "none"
        else:
            active = "cloud" if cloud.usable else ("local" if local.usable else "none")
        return RouterStatus(mode.value, active, cloud, local)

    # --- sıralama ---
    def _order(self, mode: Mode, task: Task) -> list[Engine]:
        if mode is Mode.CLOUD:
            return [self.cloud]
        if mode is Mode.LOCAL:
            return [self.local]
        pref = [self.cloud, self.local] if task.complexity == "complex" else [self.local, self.cloud]
        # real mühərrik simulyasiyadan üstündür; bərabər olduqda tapşırığa görə üstünlük (sort sabitdir)
        return sorted(pref, key=lambda e: self._status(e).is_mock)

    def _compose(self, eng: Engine, prompt: str, context: Sequence[MemoryRecord]) -> tuple[str, dict]:
        info = {"context_records": 0, "context_sent_to_cloud": False, "context_withheld": False}
        if not context:
            return prompt, info
        records = list(context)
        if eng.kind == "cloud":
            pv = self.store.privacy()
            allowed = pv.cloud_may_receive_memory and self.store.consent("cloud_data_transfer")
            if not allowed:
                info["context_withheld"] = True
                return prompt, info
            records = [r for r in records if r.category in pv.cloud_categories]
            if not records:
                info["context_withheld"] = True
                return prompt, info
            info["context_sent_to_cloud"] = True
        block = self.context.build(records)
        info["context_records"] = len(records) if block else 0
        return (block + "\n\n" + prompt) if block else prompt, info

    # --- əsas giriş ---
    def route(self, request_id: str, kind: str, prompt: str, system: str = "", context: Sequence[MemoryRecord] = (),
              allow_mock: bool = True, allow_paid: bool = True) -> RouteResult:
        mode = self.store.mode()
        if not isinstance(request_id, str) or not _RID.match(request_id) or kind not in KINDS \
                or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT or not isinstance(system, str):
            return RouteResult("bad_request", str(request_id)[:80], mode=mode.value, reason="request_id/kind/prompt yanlışdır")
        if self.stop_path is not None and self.stop_path.exists():
            return RouteResult("blocked", request_id, mode=mode.value, reason="stop bayrağı aktivdir")
        sha = hashlib.sha256(f"{kind}\n{prompt}".encode("utf-8")).hexdigest()
        state, rec = self.store.begin(request_id, kind, sha, mode.value)
        if state == "mismatch":
            return RouteResult("bad_request", request_id, mode=mode.value, reason="request_id başqa məzmunla artıq istifadə olunub")
        if state == "done":
            return RouteResult("ok", request_id, rec.text, rec.engine, rec.provider, rec.model, rec.is_mock, rec.mode, duplicate=True)
        if state == "in_progress":
            return RouteResult("in_progress", request_id, mode=mode.value, reason="eyni sorğu artıq icra olunur")
        try:
            res = self._attempt(request_id, mode, classify_task(kind, prompt), prompt, system, context, allow_mock, allow_paid)
        except BaseException:
            self.store.fail(request_id)
            raise
        if res.status != "ok":
            self.store.fail(request_id)
        return res

    def _attempt(self, request_id: str, mode: Mode, task: Task, prompt: str, system: str, context: Sequence[MemoryRecord],
                 allow_mock: bool = True, allow_paid: bool = True) -> RouteResult:
        attempts: list[Attempt] = []
        consent_needed: list[str] = []
        egress_blocked = False
        privacy: dict = {"context_records": 0, "context_sent_to_cloud": False, "context_withheld": False}

        def add(eng: Engine, outcome: str, detail: str = "") -> None:
            attempts.append(Attempt(eng.kind, outcome, _short(detail)))

        for eng in self._order(mode, task):
            st = self._status(eng)
            if not st.usable:
                add(eng, "skipped_unavailable", st.reason or st.state)
                continue
            if st.is_mock and not allow_mock:
                add(eng, "skipped_mock", "simulyasiya mühərriki bu sorğu üçün icazəli deyil")
                continue
            if not allow_paid and "cloud_paid_use" in eng.consent_scopes():   # çağıranın ödənişli büdcəsi yoxdur: ödənişli mühərrik heç yoxlanmır
                add(eng, "skipped_budget", "ödənişli istifadə üçün büdcə yoxdur")
                continue
            ok, why = eng.supports(task)
            if not ok:
                add(eng, "skipped_unsupported", why)
                continue
            missing = [s for s in eng.consent_scopes() if not self.store.consent(s)]
            if missing:
                consent_needed.extend(m for m in missing if m not in consent_needed)
                add(eng, "skipped_consent", ",".join(missing))
                continue
            full, privacy = self._compose(eng, prompt, context)
            if eng.kind == "cloud" and (detect_secrets(full) or detect_secrets(system)):
                egress_blocked = True
                add(eng, "skipped_egress", "sirr bənzər məzmun Cloud-a göndərilmir")
                continue
            try:
                out = eng.generate(full, system, request_id)
            except EngineBlocked as exc:
                add(eng, "blocked", str(exc))
                return self._final("blocked", request_id, mode, attempts, consent_needed, privacy, "mühərrik bloklandı (stop/siyasət)")
            except ConsentMissing as exc:
                add(eng, "skipped_consent", str(exc))
                continue
            except EngineError as exc:
                self.health.record(eng.kind, False)
                add(eng, "timeout_unknown" if exc.outcome_unknown else "unavailable", str(exc))
                continue
            self.health.record(eng.kind, True)
            text = _sanitize(out.text)
            if not self.store.finish(request_id, eng.kind, text, out.provider, out.model, out.is_mock):
                return self._final("in_progress", request_id, mode, attempts, consent_needed, privacy, "sorğu artıq bağlanıb")
            add(eng, "ok")
            return RouteResult("ok", request_id, text, eng.kind, out.provider, out.model, bool(out.is_mock), mode.value, "",
                               tuple(attempts), (), privacy)
        return self._failure(request_id, mode, attempts, consent_needed, egress_blocked, privacy)

    def _final(self, status: str, request_id: str, mode: Mode, attempts: list[Attempt], consent: list[str], privacy: dict, reason: str) -> RouteResult:
        return RouteResult(status, request_id, "", "", "", "", False, mode.value, reason, tuple(attempts), tuple(consent), privacy)

    def _failure(self, request_id: str, mode: Mode, attempts: list[Attempt], consent: list[str], egress: bool, privacy: dict) -> RouteResult:
        reason = "; ".join(f"{a.engine}:{a.outcome}:{a.detail}" for a in attempts) or "mühərrik yoxdur"
        if consent:
            status = "consent_required"
        elif egress:
            status = "blocked_egress"
        elif any(a.outcome == "skipped_budget" for a in attempts) and not any(a.outcome in ("unavailable", "timeout_unknown") for a in attempts):
            status = "budget_exhausted"
        elif mode is Mode.CLOUD:
            status = "cloud_unavailable"
        elif mode is Mode.LOCAL:
            status = "unsupported_locally" if any(a.outcome == "skipped_unsupported" for a in attempts) else "local_unavailable"
        else:
            status = "all_unavailable"
        return self._final(status, request_id, mode, attempts, consent, privacy, reason)


# --- mövcud planlaşdırıcı üçün adapter ---

class RoutedLLM:
    """`LLMPlanner` və s. üçün `GuardedLLM.complete` uyğun adapter: sorğu router ilə yönləndirilir (plan = mürəkkəb: yalnız Cloud və ya
    Local-ın açıq dəstəklədiyi halda). Mühərrik yoxdursa `LLMUnavailable` atılır (planlaşdırıcı bunu 'plan yoxdur' kimi bildirir)."""

    def __init__(self, router: HybridRouter, kind: str = "plan") -> None:
        self.router, self.kind = router, kind

    def complete(self, prompt: str, system: str = "", max_output_tokens: int | None = None) -> LLMResult:
        res = self.router.route("llm-" + uuid.uuid4().hex, self.kind, prompt, system)
        if res.status != "ok":
            raise LLMUnavailable(f"{res.status}: {res.reason}")
        return LLMResult(res.text, estimate_tokens(prompt), estimate_tokens(res.text), res.model, res.provider, res.is_mock)
