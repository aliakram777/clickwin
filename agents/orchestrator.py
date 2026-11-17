"""Tapşırığı şöbə agentinə yönləndirir. İcra yalnız safety/gate.py icazəsi ilə."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.registry import RegisteredAgent, Registry
from agents.verify import DEFAULT_ERRORS_PATH, Runner, VerifyResult, log_error
from safety.gate import DEFAULT_AUDIT_PATH, DEFAULT_STOP_PATH, Action, Approver, Gate
from safety.ledger import CostLedger


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


@dataclass
class Orchestrator:
    registry: Registry
    runner: Runner
    audit_path: str | Path = DEFAULT_AUDIT_PATH
    stop_path: str | Path = DEFAULT_STOP_PATH
    errors_path: str | Path = DEFAULT_ERRORS_PATH
    approver: Approver | None = None
    ledger: CostLedger | None = None  # verilibsə xərc davamlıdır və təkrar qorunur
    _spent: dict[str, float] = field(default_factory=dict, init=False, repr=False)

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
        gate.spent = (self.ledger.agent_total(spec.name) if self.ledger is not None
                      else self._spent.get(spec.name, 0.0))
        decision = gate.check(Action(task.permission, task.cost, task.description))
        if not decision.allowed:
            return ExecutionResult("blocked", spec.name, reason=decision.reason)
        if self.ledger is not None:
            if not self.ledger.record(op_id, spec.name, task.cost, task.description):
                return ExecutionResult("duplicate", spec.name, reason=f"təkrar əməliyyat: {op_id}")
        else:
            self._spent[spec.name] = gate.spent

        return self._run(spec, task)

    def _route(self, task: Task) -> RegisteredAgent | None:
        candidates = [
            a for a in self.registry.list(task.department)
            if all(k in task.input for k in a.spec.inputs)
        ]
        for agent in candidates:  # icazəsi olan agent üstündür
            if task.permission in agent.spec.permissions:
                return agent
        return candidates[0] if candidates else None

    def _run(self, spec, task: Task) -> ExecutionResult:
        try:
            output = self.runner(spec, dict(task.input))
        except Exception as exc:  # noqa: BLE001 — agent xətası jurnala düşməlidir
            return self._fail(spec.name, task, "run", f"{type(exc).__name__}: {exc}")
        missing = [k for k in spec.outputs if not isinstance(output, dict) or k not in output]
        if missing:
            return self._fail(spec.name, task, "output", f"elan olunan çıxış yoxdur: {missing}")
        return ExecutionResult("ok", spec.name, output)

    def _fail(self, agent: str, task: Task, stage: str, error: str) -> ExecutionResult:
        log_error(Path(self.errors_path), agent, task.description or task.department,
                  VerifyResult(False, stage, error))
        return ExecutionResult("error", agent, reason=error)
