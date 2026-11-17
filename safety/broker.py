"""Effect Broker: bütün yan təsirlərin yeganə yolu.

Hər effekt üçün sıra: stop bayrağı -> təkrar yoxlaması -> büdcə rezervi (atomik) -> Gate (icazə, təsdiq,
audit) -> icra -> hesablaşma. İcraçı (`executor`) yalnız bu sırada və yalnız broker daxilində çağırılır;
rədd olunan effektdə heç vaxt çağırılmır. Gate və Ledger dəyişdirilmir, olduğu kimi istifadə olunur.
"""
from __future__ import annotations

import functools
import math
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from agents.spec import AgentSpec
from safety.gate import DEFAULT_AUDIT_PATH, DEFAULT_STOP_PATH, Action, Approver, Gate
from safety.ledger import BudgetPolicy, CostLedger, log_denial

_ACTIVE: ContextVar[bool] = ContextVar("naymera_broker_active", default=False)


class BrokerRequired(RuntimeError):
    """Yan təsirli alət broker-dən kənarda çağırıldı."""


def in_broker() -> bool:
    return _ACTIVE.get()


def broker_only(fn: Callable) -> Callable:
    """Alətləri bəzəyin: yalnız `EffectBroker.run` daxilində çağırıla bilər."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _ACTIVE.get():
            raise BrokerRequired(f"{fn.__name__}: yalnız Effect Broker daxilində çağırıla bilər")
        return fn(*args, **kwargs)

    return wrapper


class ExecutionError(RuntimeError):
    """İcraçı xətası; real xərc məlumdursa `cost` ilə bildirilir."""

    def __init__(self, message: str, cost: float | None = None) -> None:
        super().__init__(message)
        self.cost = cost


class TransientError(ExecutionError):
    """Müvəqqəti xəta. Retry yalnız real xərc (`cost`) məlum olduqda mümkündür."""


@dataclass(frozen=True)
class Effect:
    spec: AgentSpec
    permission: str
    cost: float = 0.0
    op_id: str = ""  # verilibsə eyni op_id yalnız bir dəfə icra və hesablanır
    description: str = ""


@dataclass(frozen=True)
class EffectResult:
    status: str  # ok | blocked | duplicate | error
    reason: str = ""
    output: Any = None
    cost: float | None = None  # committed (faktiki) və ya reserved_unknown (qorunan rezerv)
    cost_status: str = ""  # committed | reserved_unknown
    transient: bool = False
    stage: str = ""  # error üçün: run | output
    op_id: str = ""


class EffectBroker:
    def __init__(
        self,
        audit_path: str | Path = DEFAULT_AUDIT_PATH,
        stop_path: str | Path = DEFAULT_STOP_PATH,
        approver: Approver | None = None,
        ledger: CostLedger | None = None,
        policy: BudgetPolicy | None = None,
        actual_cost: Callable[[Any], Any] | None = None,
    ) -> None:
        if policy is not None and ledger is None:
            raise ValueError("büdcə siyasəti xərc kitabı (ledger) tələb edir")
        self.audit_path = Path(audit_path)
        self.stop_path = Path(stop_path)
        self.approver = approver
        self.ledger = ledger
        self.policy = policy
        self.actual_cost = actual_cost
        self._spent: dict[str, float] = {}  # ledger yoxdursa yaddaşdakı sayğac
        self._seen: set[str] = set()  # ledger yoxdursa icra olunmuş açıq op_id-lər

    def global_room(self) -> float | None:
        """Ümumi limitdən qalan yer (limit/kitab yoxdursa None)."""
        if self.ledger is None or self.policy is None or self.policy.global_limit is None:
            return None
        return max(0.0, self.policy.global_limit - self.ledger.total())

    def run(
        self,
        effect: Effect,
        executor: Callable[[], Any],
        validate: Callable[[Any], str | None] | None = None,
    ) -> EffectResult:
        spec = effect.spec
        op_id = effect.op_id or uuid.uuid4().hex

        if self.stop_path.exists():  # 1. stop bayrağı: heç nə rezerv olunmur, qərar audit-ə yazılır
            reason = "stop bayrağı aktivdir"
            log_denial(self.audit_path, spec.name, effect.permission, effect.cost,
                       effect.description, reason, "stop")
            return EffectResult("blocked", reason, op_id=op_id)

        if self.ledger is not None:  # 2. təkrar
            if self.ledger.has(op_id):
                return EffectResult("duplicate", f"təkrar əməliyyat, artıq hesablanıb: {op_id}", op_id=op_id)
        elif effect.op_id and op_id in self._seen:
            return EffectResult("duplicate", f"təkrar əməliyyat: {op_id}", op_id=op_id)

        # Gate hər dəfə effektin cari spec-i ilə qurulur: yenilənmiş icazələr dərhal qüvvəyə minir.
        gate = Gate(spec, self.audit_path, self.stop_path, self.approver)
        reserved = False
        if self.ledger is None:
            gate.spent = self._spent.get(spec.name, 0.0)
        else:
            gate.spent = self.ledger.agent_total(spec.name)
            if _valid_cost(effect.cost):  # yanlış xərc rezerv edilmir: Gate bloklayıb audit yazır
                pol = self.policy or BudgetPolicy()
                res = self.ledger.reserve(  # 3. atomik büdcə rezervi
                    op_id, spec.name, effect.cost,
                    agent_limit=min(spec.cost_limit, pol.agent_limits.get(spec.name, spec.cost_limit)),
                    global_limit=pol.global_limit, task_limit=pol.task_limit,
                    description=effect.description)
                if res.status == "duplicate":
                    return EffectResult("duplicate", res.reason, op_id=op_id)
                if res.status == "denied":
                    log_denial(self.audit_path, spec.name, effect.permission, effect.cost,
                               effect.description, res.reason, "budget")
                    return EffectResult("blocked", res.reason, op_id=op_id)
                reserved = True

        try:  # 4. Gate: icazə, təsdiq, audit
            decision = gate.check(Action(effect.permission, effect.cost, effect.description))
        except BaseException:
            if reserved:
                self.ledger.release(op_id)
            raise
        if not decision.allowed:
            if reserved:
                self.ledger.release(op_id)
            return EffectResult("blocked", decision.reason, op_id=op_id)
        if self.ledger is None:
            self._spent[spec.name] = gate.spent
            if effect.op_id:
                self._seen.add(op_id)

        return self._execute(effect, op_id, reserved, executor, validate)  # 5. icra + 6. hesablaşma

    def _execute(self, effect: Effect, op_id: str, reserved: bool, executor: Callable[[], Any],
                 validate: Callable[[Any], str | None] | None) -> EffectResult:
        token = _ACTIVE.set(True)
        try:
            output = executor()
        except Exception as exc:  # noqa: BLE001 — icraçı xətası nəticə kimi qaytarılır
            failed = EffectResult(
                "error", f"{type(exc).__name__}: {exc}", stage="run", op_id=op_id,
                transient=isinstance(exc, (TransientError, TimeoutError, ConnectionError)))
            return self._settle(failed, op_id if reserved else "", effect.cost, getattr(exc, "cost", None))
        except BaseException:  # sistem səviyyəli kəsilmə: xərc naməlum, rezerv qorunur və işarələnir
            if reserved:
                self.ledger.mark_unknown(op_id)
            raise
        finally:
            _ACTIVE.reset(token)

        actual = self._reported_cost(output, effect.cost) if reserved else None
        try:
            error = validate(output) if validate is not None else None
        except Exception as exc:  # noqa: BLE001
            error = f"yoxlama xətası: {type(exc).__name__}: {exc}"
        if error:
            result = EffectResult("error", error, stage="output", op_id=op_id)
        else:
            result = EffectResult("ok", output=output, op_id=op_id)
        return self._settle(result, op_id if reserved else "", effect.cost, actual)

    def _reported_cost(self, output: Any, reserved: float) -> Any:
        if self.actual_cost is None:
            return reserved
        try:
            value = self.actual_cost(output)
        except Exception:  # noqa: BLE001 — xərc oxuna bilmədi: naməlum sayılır
            return None
        return reserved if value is None else value

    def _settle(self, result: EffectResult, op_id: str, reserved: float, actual: Any) -> EffectResult:
        """Faktiki xərc məlumdursa yazır; naməlumdursa rezervi qoruyub işarələyir (avtomatik azad edilmir)."""
        if not op_id or self.ledger is None:
            return result
        if _valid_cost(actual):
            self.ledger.commit(op_id, actual)
            return replace(result, cost=float(actual), cost_status="committed")
        self.ledger.mark_unknown(op_id)
        return replace(result, cost=float(reserved), cost_status="reserved_unknown")


def _valid_cost(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) \
        and math.isfinite(value) and value >= 0
