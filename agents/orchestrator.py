"""Tapşırığı şöbə agentinə yönləndirir. İcra yalnız safety/gate.py icazəsi ilə."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from agents.registry import RegisteredAgent, Registry
from agents.verify import DEFAULT_ERRORS_PATH, Runner, VerifyResult, log_error
from safety.gate import DEFAULT_AUDIT_PATH, DEFAULT_STOP_PATH, Action, Approver, Gate
from safety.ledger import BudgetPolicy, CostLedger, log_budget_denial


@dataclass(frozen=True)
class Task:
    department: str
    input: dict
    permission: str
    cost: float = 0.0
    description: str = ""
    op_id: str = ""  # verilibsə: eyni op_id yalnız bir dəfə icra və hesablanır


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # ok | blocked | unrouted | error | duplicate
    agent: str = ""
    output: Any = None
    reason: str = ""
    cost: float | None = None  # ledger varsa: yazılan (committed) və ya qorunan (reserved) xərc
    cost_status: str = ""  # committed | reserved_unknown
    transient: bool = False  # müvəqqəti xəta (retry üçün namizəd)


class ExecutionError(RuntimeError):
    """Runner xətası; real xərc məlumdursa `cost` ilə bildirilir."""

    def __init__(self, message: str, cost: float | None = None) -> None:
        super().__init__(message)
        self.cost = cost


class TransientError(ExecutionError):
    """Müvəqqəti runner xətası. Retry yalnız real xərc (`cost`) məlum olduqda mümkündür."""


@dataclass
class Orchestrator:
    registry: Registry
    runner: Runner
    audit_path: str | Path = DEFAULT_AUDIT_PATH
    stop_path: str | Path = DEFAULT_STOP_PATH
    errors_path: str | Path = DEFAULT_ERRORS_PATH
    approver: Approver | None = None
    ledger: CostLedger | None = None  # verilibsə xərc davamlıdır və təkrar qorunur
    policy: BudgetPolicy | None = None  # ümumi/tapşırıq/agent limitləri (ledger tələb edir)
    actual_cost: Callable[[Any], Any] | None = None  # çıxışdan faktiki xərc; None -> rezerv
    _spent: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.policy is not None and self.ledger is None:
            raise ValueError("büdcə siyasəti xərc kitabı (ledger) tələb edir")

    def global_room(self) -> float | None:
        """Ümumi limitdən qalan yer (limit/kitab yoxdursa None)."""
        if self.ledger is None or self.policy is None or self.policy.global_limit is None:
            return None
        return max(0.0, self.policy.global_limit - self.ledger.total())

    def execute(self, task: Task) -> ExecutionResult:
        if not isinstance(task.department, str) or not task.department:
            return ExecutionResult("blocked", reason="tapşırıqda şöbə yoxdur")
        agent = self._route(task)
        if agent is None:
            return ExecutionResult("unrouted", reason=f"uyğun agent yoxdur: {task.department}")
        spec = agent.spec
        op_id = task.op_id or uuid.uuid4().hex
        if self.ledger is not None and self.ledger.has(op_id):
            return ExecutionResult("duplicate", spec.name, reason=f"təkrar əməliyyat, artıq hesablanıb: {op_id}")

        # Gate hər dəfə reyestrdəki cari təsvirlə qurulur: yenilənmiş icazələr dərhal qüvvəyə minir.
        gate = Gate(spec, self.audit_path, self.stop_path, self.approver)
        reserved = False
        if self.ledger is None:
            gate.spent = self._spent.get(spec.name, 0.0)
        else:
            gate.spent = self.ledger.agent_total(spec.name)
            if _valid_cost(task.cost):  # yanlış xərc rezerv edilmir: gate onu bloklayıb audit yazır
                pol = self.policy or BudgetPolicy()
                res = self.ledger.reserve(
                    op_id, spec.name, task.cost,
                    agent_limit=min(spec.cost_limit, pol.agent_limits.get(spec.name, spec.cost_limit)),
                    global_limit=pol.global_limit, task_limit=pol.task_limit,
                    description=task.description)
                if res.status == "duplicate":
                    return ExecutionResult("duplicate", spec.name, reason=res.reason)
                if res.status == "denied":
                    log_budget_denial(self.audit_path, spec.name, task.permission, task.cost,
                                      task.description, res.reason)
                    return ExecutionResult("blocked", spec.name, reason=res.reason)
                reserved = True

        try:
            decision = gate.check(Action(task.permission, task.cost, task.description))
        except BaseException:
            if reserved:
                self.ledger.release(op_id)
            raise
        if not decision.allowed:
            if reserved:
                self.ledger.release(op_id)
            return ExecutionResult("blocked", spec.name, reason=decision.reason)
        if self.ledger is None:
            self._spent[spec.name] = gate.spent

        return self._run(spec, task, op_id if reserved else "")

    def _route(self, task: Task) -> RegisteredAgent | None:
        candidates = [
            a for a in self.registry.list(task.department)
            if all(k in task.input for k in a.spec.inputs)
        ]
        for agent in candidates:  # icazəsi olan agent üstündür
            if task.permission in agent.spec.permissions:
                return agent
        return candidates[0] if candidates else None

    def _run(self, spec, task: Task, op_id: str = "") -> ExecutionResult:
        try:
            output = self.runner(spec, dict(task.input))
        except Exception as exc:  # noqa: BLE001 — agent xətası jurnala düşməlidir
            result = replace(self._fail(spec.name, task, "run", f"{type(exc).__name__}: {exc}"),
                             transient=isinstance(exc, (TransientError, TimeoutError, ConnectionError)))
            return self._settle(result, op_id, task.cost, getattr(exc, "cost", None))
        actual = self._reported_cost(output, task.cost) if op_id else None
        missing = [k for k in spec.outputs if not isinstance(output, dict) or k not in output]
        if missing:
            result = self._fail(spec.name, task, "output", f"elan olunan çıxış yoxdur: {missing}")
            return self._settle(result, op_id, task.cost, actual)
        return self._settle(ExecutionResult("ok", spec.name, output), op_id, task.cost, actual)

    def _reported_cost(self, output: Any, reserved: float) -> Any:
        if self.actual_cost is None:
            return reserved
        try:
            value = self.actual_cost(output)
        except Exception:  # noqa: BLE001 — xərc oxuna bilmədi: naməlum sayılır
            return None
        return reserved if value is None else value

    def _settle(self, result: ExecutionResult, op_id: str, reserved: float, actual: Any) -> ExecutionResult:
        """Faktiki xərc məlumdursa yazır; naməlumdursa rezervi qoruyub işarələyir."""
        if not op_id or self.ledger is None:
            return result
        if _valid_cost(actual):
            self.ledger.commit(op_id, actual)
            return replace(result, cost=float(actual), cost_status="committed")
        self.ledger.mark_unknown(op_id)
        return replace(result, cost=float(reserved), cost_status="reserved_unknown")

    def _fail(self, agent: str, task: Task, stage: str, error: str) -> ExecutionResult:
        log_error(Path(self.errors_path), agent, task.description or task.department,
                  VerifyResult(False, stage, error))
        return ExecutionResult("error", agent, reason=error)


def _valid_cost(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) \
        and math.isfinite(value) and value >= 0
