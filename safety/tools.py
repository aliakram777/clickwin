"""Tool Runtime: alətlər yalnız qeydiyyatlı allowlist ilə, hər çağırış Effect Broker-dən keçməklə.

DÜRÜST SƏRHƏDLƏR (saxta təminat yoxdur):
- Bu modul OS səviyyəli sandbox DEYİL. In-process icra yalnız host tərəfindən qeydiyyata alınmış etibarlı
  "pure" və fayl alətləri üçündür; fayl alətləri workspace-ə tətbiq səviyyəsində bağlanır (safety/workspace.py).
- In-process tətbiq olunan: çıxış ölçüsü, fayl ölçüsü/kvota, yol sərhədi, KOOPERATİV zaman limiti (handler
  `ctx.check_deadline()` çağırmalıdır). Tətbiq OLUNMAYAN: yaddaş limiti, sərt zaman limiti, şəbəkə/proses
  izolyasiyası. Bunlar yalnız OS sandbox adapterində mümkündür.
- Shell/şəbəkə/sistem/env növlü alətlər heç vaxt in-process işləmir: yalnız `SandboxAdapter` ilə, adapter
  müstəqil sınaq (probe) nəticələri ilə hamısını "blocked" göstərdikdə və tələb olunan məhdudiyyətləri elan
  etdikdə. Adapter yoxdursa/təsdiqlənmirsə icra DENY. Bu repoda real OS sandbox adapteri YOXDUR; yalnız
  interfeys və `NullSandbox` var. Adapterin öz attestasiyası etibara əsaslanır: real adapter üçün müstəqil
  yoxlama (məs. konteyner/seccomp testləri) ayrıca işdir.
"""
from __future__ import annotations

import functools
import json
import math
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from agents.spec import KNOWN_PERMISSIONS, AgentSpec
from safety.broker import (
    BrokerRequired, Effect, EffectBroker, EffectResult, ExecutionError, TransientError, broker_only,
)
from safety.ledger import log_denial

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")
KINDS = frozenset({"pure", "fs_read", "fs_write", "exec", "network", "system", "env"})
IN_PROCESS_KINDS = frozenset({"pure", "fs_read", "fs_write"})
SANDBOX_ONLY_KINDS = KINDS - IN_PROCESS_KINDS
PARAM_TYPES = (str, int, float, bool)

ALL_ENFORCEMENTS = frozenset({"timeout", "memory", "network", "filesystem", "output", "env", "processes",
                              "privilege", "network_allowlist"})
REQUIRED_PROBES = ("fs_escape", "network", "env_secrets", "shell_escape", "memory", "timeout")
_BASE = frozenset({"timeout", "memory", "filesystem", "output", "env"})
REQUIRED_ENFORCEMENT: dict[str, frozenset[str]] = {
    "pure": _BASE, "fs_read": _BASE, "fs_write": _BASE,
    "exec": _BASE | {"network", "processes"},
    "network": _BASE | {"network_allowlist"},
    "system": _BASE | {"network", "processes", "privilege"},
    "env": _BASE | {"network"},
}

_ACTIVE_TOOL: ContextVar[str] = ContextVar("naymera_active_tool", default="")


# --- xətalar ---

class ToolError(ExecutionError):
    """Alət xətası. Real xərc məlumdur (`cost`), təkrar cəhd üçün namizəd deyil."""


class ToolTimeout(TransientError):
    """Kooperativ zaman limiti aşıldı."""


class ToolDenied(ValueError):
    """Alət çağırışı icradan əvvəl rədd edildi (yol, argument və s.)."""


# --- limitlər və siyasət ---

def _pos(name: str, v: Any) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} müsbət sonlu rəqəm olmalıdır")


@dataclass(frozen=True)
class ResourceLimits:
    timeout_s: float = 10.0
    memory_mb: int = 256  # yalnız OS sandbox tətbiq edə bilər; in-process tətbiq OLUNMUR
    max_output_bytes: int = 65536
    max_arg_bytes: int = 65536

    def __post_init__(self) -> None:
        for n in ("timeout_s", "memory_mb", "max_output_bytes", "max_arg_bytes"):
            _pos(n, getattr(self, n))


@dataclass(frozen=True)
class RuntimePolicy:
    """İcazə verilən alət növləri. Defolt: yalnız in-process etibarlı növlər; exec/network/system/env BAĞLI."""
    allow_kinds: frozenset[str] = IN_PROCESS_KINDS

    def __post_init__(self) -> None:
        if not isinstance(self.allow_kinds, frozenset) or not self.allow_kinds <= KINDS:
            raise ValueError(f"allow_kinds yalnız bunlardan ibarət ola bilər: {sorted(KINDS)}")


@dataclass(frozen=True)
class ToolContext:
    limits: ResourceLimits
    cost: float
    clock: Callable[[], float]
    deadline: float

    def check_deadline(self) -> None:
        if self.clock() > self.deadline:
            raise ToolTimeout("zaman limiti aşıldı", cost=self.cost)


# --- alət təsviri və reyestr ---

@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str
    permission: str
    params: Mapping[str, type]
    required: tuple[str, ...] = ()
    handler: Callable[..., Any] | None = None
    sandboxed: bool = False
    cost: float = 0.0
    precheck: Callable[[Mapping[str, Any]], None] | None = None
    description: str = ""


@dataclass(frozen=True)
class ToolInfo:
    """Alətin açıq təsviri (handler olmadan)."""
    name: str
    kind: str
    permission: str
    params: Mapping[str, type]
    required: tuple[str, ...]
    sandboxed: bool
    cost: float
    description: str


def tool_handler(name: str) -> Callable[[Callable], Callable]:
    """In-process handler-i bəzəyir: yalnız broker daxilində VƏ yalnız bu alət aktiv olanda işləyir."""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            if _ACTIVE_TOOL.get() != name:
                raise BrokerRequired(f"{name}: handler yalnız öz alət çağırışı daxilində işləyə bilər")
            return fn(*args, **kwargs)

        return broker_only(inner)

    return deco


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._frozen = False

    def register(self, tool: ToolSpec) -> None:
        if self._frozen:
            raise ValueError("reyestr bağlanıb: yeni alət əlavə etmək olmaz")
        if not isinstance(tool.name, str) or not NAME_RE.match(tool.name) or len(tool.name) > 40:
            raise ValueError(f"alət adı yanlışdır: {tool.name!r}")
        if tool.name in self._tools:
            raise ValueError(f"alət artıq qeydiyyatdadır: {tool.name}")
        if tool.kind not in KINDS:
            raise ValueError(f"naməlum alət növü: {tool.kind}")
        if tool.permission not in KNOWN_PERMISSIONS:
            raise ValueError(f"naməlum icazə: {tool.permission}")
        for k, t in tool.params.items():
            if not isinstance(k, str) or t not in PARAM_TYPES:
                raise ValueError(f"parametr tipi dəstəklənmir: {k}")
        if not set(tool.required) <= set(tool.params):
            raise ValueError("required parametrləri params-da olmalıdır")
        if isinstance(tool.cost, bool) or not isinstance(tool.cost, (int, float)) \
                or not math.isfinite(tool.cost) or tool.cost < 0:
            raise ValueError("alət xərci sonlu və mənfi olmayan rəqəm olmalıdır")
        if tool.sandboxed or tool.kind in SANDBOX_ONLY_KINDS:
            if not tool.sandboxed or tool.handler is not None:
                raise ValueError(f"{tool.kind} növü yalnız sandbox adapteri ilə işləyə bilər "
                                 "(sandboxed=True, in-process handler olmadan)")
        elif not callable(tool.handler):
            raise ValueError("in-process alət üçün handler tələb olunur")
        self._tools[tool.name] = tool

    def freeze(self) -> None:
        self._frozen = True

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def describe(self, name: str) -> ToolInfo | None:
        t = self._tools.get(name) if isinstance(name, str) else None
        if t is None:
            return None
        return ToolInfo(t.name, t.kind, t.permission, dict(t.params), t.required, t.sandboxed, t.cost,
                        t.description)


# --- sandbox adapter müqaviləsi ---

@dataclass(frozen=True)
class SandboxAttestation:
    name: str
    isolation: str  # "os" | "none"
    enforces: frozenset[str]
    probes: Mapping[str, str]  # REQUIRED_PROBES -> "blocked" | "allowed" | "error"


@dataclass(frozen=True)
class SandboxRequest:
    tool: str
    args: Mapping[str, Any]
    limits: ResourceLimits
    workspace_root: str
    env: Mapping[str, str] = field(default_factory=dict)  # həmişə boş: sirr ötürülmür


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    output: Any = None
    error: str = ""


class SandboxAdapter(Protocol):
    """Real OS sandbox adapterləri bunu həyata keçirir. `attest` müstəqil sınaqların (fayl qaçışı, şəbəkə,
    env sirləri, shell qaçışı, yaddaş, zaman) nəticəsini qaytarır; `run` alətin YALNIZ izolə mühitdə icrasıdır."""

    def attest(self) -> SandboxAttestation: ...

    def run(self, request: SandboxRequest) -> SandboxResult: ...


class NullSandbox:
    """Sandbox yoxdur: attestasiya həmişə uğursuzdur, icra olunmur."""

    def attest(self) -> SandboxAttestation:
        return SandboxAttestation("null", "none", frozenset(), {})

    def run(self, request: SandboxRequest) -> SandboxResult:
        return SandboxResult(False, error="sandbox yoxdur")


def verify_sandbox(adapter: Any, needed: frozenset[str] = frozenset()) -> tuple[bool, str]:
    """Fail-closed: istənilən xəta, naməlum cavab və ya çatışmayan sübut => (False, səbəb)."""
    try:
        att = adapter.attest()
    except Exception as exc:  # noqa: BLE001
        return False, f"sandbox attestasiyası uğursuz: {type(exc).__name__}"
    if not isinstance(att, SandboxAttestation):
        return False, "sandbox attestasiyası yanlışdır"
    if att.isolation != "os":
        return False, "sandbox OS səviyyəli izolyasiya təmin etmir"
    bad = [p for p in REQUIRED_PROBES if att.probes.get(p) != "blocked"]
    if bad:
        return False, f"sandbox sınaqları keçməyib: {bad}"
    missing = sorted(needed - set(att.enforces))
    if missing:
        return False, f"sandbox tələb olunan məhdudiyyətləri tətbiq etmir: {missing}"
    return True, "sandbox təsdiqlənib"


# --- icra ---

@dataclass(frozen=True)
class ToolResult:
    status: str  # ok | blocked | error | duplicate | pending_approval
    output: Any = None
    reason: str = ""
    stage: str = ""  # tool (broker-dən əvvəl rədd) | broker | ""
    approval_id: str = ""
    cost: float | None = None
    cost_status: str = ""
    transient: bool = False


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        broker: EffectBroker,
        policy: RuntimePolicy | None = None,
        limits: ResourceLimits | None = None,
        sandbox: SandboxAdapter | None = None,
        workspace: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry ToolRegistry olmalıdır")
        if not isinstance(broker, EffectBroker):
            raise TypeError("alət icrası Effect Broker tələb edir")
        registry.freeze()  # icra başlayandan sonra yeni alət əlavə olunmur
        self.registry = registry
        self.broker = broker
        self.policy = policy or RuntimePolicy()
        self.limits = limits or ResourceLimits()
        self.sandbox = sandbox if sandbox is not None else NullSandbox()
        self.workspace = workspace
        self.clock = clock

    # --- ictimai ---
    def call(self, spec: AgentSpec, tool: str, args: Mapping[str, Any], op_id: str = "",
             approval_id: str = "") -> ToolResult:
        t = self.registry._tools.get(tool) if isinstance(tool, str) else None
        if t is None:
            return self._deny(spec, "unknown_tool", "naməlum alət (allowlist-də yoxdur)", tool)
        if t.name not in spec.tools:
            return self._deny(spec, t.permission, "alət agentin alət siyahısında deyil", tool, t.cost)
        if t.kind not in self.policy.allow_kinds:
            return self._deny(spec, t.permission, f"alət növü bağlıdır: {t.kind}", tool, t.cost)
        err = self._check_args(t, args)
        if err:
            return self._deny(spec, t.permission, err, tool, t.cost)
        if t.precheck is not None:
            try:
                t.precheck(args)
            except ToolDenied as exc:
                return self._deny(spec, t.permission, str(exc), tool, t.cost)
            except Exception as exc:  # noqa: BLE001 — yoxlama xətası: DENY
                return self._deny(spec, t.permission, f"yoxlama xətası: {type(exc).__name__}", tool, t.cost)
        if t.sandboxed:
            if self.workspace is None:
                return self._deny(spec, t.permission, "sandbox alətləri üçün workspace tələb olunur", tool, t.cost)
            ok, why = verify_sandbox(self.sandbox, REQUIRED_ENFORCEMENT[t.kind])
            if not ok:
                return self._deny(spec, t.permission, why, tool, t.cost)

        effect = Effect(spec, t.permission, t.cost, op_id, f"tool:{t.name}",
                        payload={"tool": t.name, "args": dict(args)}, approval_id=approval_id)
        res = self.broker.run(effect, lambda: self._execute(t, dict(args)), validate=self._validate_output)
        return ToolResult(res.status, res.output, res.reason, "broker" if res.status != "ok" else "",
                          res.approval_id, res.cost, res.cost_status, res.transient)

    def security_posture(self) -> dict[str, Any]:
        ok, why = verify_sandbox(self.sandbox, _BASE)  # ümumi baza məhdudiyyətləri də elan olunmalıdır
        return {
            "os_sandbox_verified": ok,
            "reason": why,
            "in_process_enforced": ["output_size", "file_size", "workspace_quota", "path_confinement",
                                    "cooperative_timeout", "allowlist", "broker"],
            "not_enforced_in_process": ["memory", "hard_timeout", "network", "process_isolation",
                                        "env_secrets_for_arbitrary_code"],
        }

    # --- daxili ---
    def _deny(self, spec: AgentSpec, permission: str, reason: str, tool: Any, cost: float = 0.0) -> ToolResult:
        log_denial(self.broker.audit_path, spec.name, permission, cost, f"tool:{tool}", reason, "tool")
        return ToolResult("blocked", reason=reason, stage="tool")

    def _check_args(self, t: ToolSpec, args: Any) -> str | None:
        if not isinstance(args, Mapping):
            return "arqumentlər xəritə olmalıdır"
        unknown = sorted(set(args) - set(t.params))
        if unknown:
            return f"naməlum arqument: {unknown}"
        missing = [k for k in t.required if k not in args]
        if missing:
            return f"çatışmayan arqument: {missing}"
        for k, v in args.items():
            if not _type_ok(t.params[k], v):
                return f"arqument tipi yanlışdır: {k}"
        try:
            size = len(json.dumps(dict(args), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            return "arqumentlər JSON-a çevrilə bilmir"
        if size > self.limits.max_arg_bytes:
            return f"arqumentlər çox böyükdür: {size} > {self.limits.max_arg_bytes}"
        return None

    def _execute(self, t: ToolSpec, args: dict[str, Any]) -> Any:
        token = _ACTIVE_TOOL.set(t.name)
        try:
            if t.sandboxed:
                res = self.sandbox.run(SandboxRequest(t.name, args, self.limits,
                                                       str(self.workspace.root), {}))
                if not isinstance(res, SandboxResult) or not res.ok:
                    raise ToolError((getattr(res, "error", "") or "sandbox xətası"), cost=t.cost)
                return res.output
            ctx = ToolContext(self.limits, t.cost, self.clock, self.clock() + self.limits.timeout_s)
            return t.handler(ctx, **args)
        finally:
            _ACTIVE_TOOL.reset(token)

    def _validate_output(self, output: Any) -> str | None:
        try:
            size = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            return "çıxış JSON deyil"
        if size > self.limits.max_output_bytes:
            return f"çıxış ölçü limitini aşır: {size} > {self.limits.max_output_bytes}"
        return None


def _type_ok(expected: type, v: Any) -> bool:
    if expected is bool:
        return isinstance(v, bool)
    if isinstance(v, bool):
        return False
    if expected is int:
        return isinstance(v, int)
    if expected is float:
        return isinstance(v, (int, float)) and math.isfinite(v)
    return isinstance(v, str)
