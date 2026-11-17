"""Tapşırığı şöbə agentinə yönləndirir. İcra yalnız safety/broker.py (Effect Broker) üzərindən."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agents.registry import RegisteredAgent, Registry
from agents.verify import DEFAULT_ERRORS_PATH, Runner, VerifyResult, log_error
from safety.approvals import ApprovalClient
from safety.broker import Effect, EffectBroker, EffectResult, ExecutionError, TransientError
from safety.gate import DEFAULT_AUDIT_PATH, DEFAULT_STOP_PATH, Approver
from safety.ledger import BudgetPolicy, CostLedger

__all__ = ["Task", "ExecutionResult", "ExecutionError", "TransientError", "Orchestrator"]


@dataclass(frozen=True)
class Task:
    department: str
    input: dict
    permission: str
    cost: float = 0.0
    description: str = ""
    op_id: str = ""  # verilibsə: eyni op_id yalnız bir dəfə icra və hesablanır
    approval_id: str = ""  # sahib təsdiqlədikdən sonra eyni tapşırığı göndərərkən (istəyə bağlı)


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # ok | blocked | unrouted | error | duplicate | pending_approval
    agent: str = ""
    output: Any = None
    reason: str = ""
    cost: float | None = None  # ledger varsa: yazılan (committed) və ya qorunan (reserved) xərc
    cost_status: str = ""  # committed | reserved_unknown
    transient: bool = False  # müvəqqəti xəta (retry üçün namizəd)
    approval_id: str = ""  # pending_approval: sahibin qərar verəcəyi sorğu


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
    approvals: ApprovalClient | None = None  # həssas effektlər üçün təsdiq (approver ilə birlikdə yox)
    broker: EffectBroker | None = None  # verilməsə yuxarıdakı parametrlərlə qurulur

    def __post_init__(self) -> None:
        if self.broker is None:
            self.broker = EffectBroker(self.audit_path, self.stop_path, self.approver, self.ledger,
                                       self.policy, self.actual_cost, self.approvals)
        # Orkestrator və CEO broker-in cari parametrlərini görür (paylaşılan broker halında da).
        b = self.broker
        self.audit_path, self.stop_path, self.approver = b.audit_path, b.stop_path, b.approver
        self.ledger, self.policy, self.actual_cost = b.ledger, b.policy, b.actual_cost
        self.approvals = b.approvals

    def global_room(self) -> float | None:
        """Ümumi limitdən qalan yer (limit/kitab yoxdursa None)."""
        return self.broker.global_room()

    def execute(self, task: Task) -> ExecutionResult:
        if not isinstance(task.department, str) or not task.department:
            return ExecutionResult("blocked", reason="tapşırıqda şöbə yoxdur")
        agent = self._route(task)
        if agent is None:
            return ExecutionResult("unrouted", reason=f"uyğun agent yoxdur: {task.department}")
        spec = agent.spec
        effect = Effect(spec, task.permission, task.cost, task.op_id, task.description,
                        payload=task.input, approval_id=task.approval_id)
        # Runner yalnız bu lambda ilə, broker-in icra mərhələsində çağırılır.
        res = self.broker.run(
            effect,
            lambda: self.runner(spec, dict(task.input)),
            validate=lambda out: _missing_outputs(spec.outputs, out),
        )
        return self._to_result(spec.name, task, res)

    def _route(self, task: Task) -> RegisteredAgent | None:
        candidates = [
            a for a in self.registry.list(task.department)
            if all(k in task.input for k in a.spec.inputs)
        ]
        for agent in candidates:  # icazəsi olan agent üstündür
            if task.permission in agent.spec.permissions:
                return agent
        return candidates[0] if candidates else None

    def _to_result(self, agent: str, task: Task, res: EffectResult) -> ExecutionResult:
        if res.status == "error":
            log_error(Path(self.errors_path), agent, task.description or task.department,
                      VerifyResult(False, res.stage, res.reason))
            return ExecutionResult("error", agent, reason=res.reason, cost=res.cost,
                                   cost_status=res.cost_status, transient=res.transient)
        if res.status == "ok":
            return ExecutionResult("ok", agent, res.output, cost=res.cost, cost_status=res.cost_status)
        return ExecutionResult(res.status, agent, reason=res.reason, approval_id=res.approval_id)


def _missing_outputs(outputs: list[str], output: Any) -> str | None:
    missing = [k for k in outputs if not isinstance(output, dict) or k not in output]
    return f"elan olunan çıxış yoxdur: {missing}" if missing else None
