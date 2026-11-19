"""CEO agent: tapşırığı təhlil edir, şöbə agentlərinə bölür, nəticələri toplayıb yoxlayır.

CEO özü yan təsir yaratmır: hər icra Orchestrator -> safety/gate.py üzərindən keçir
(icazə, xərc limiti, təsdiq, audit, stop). LLM yoxdur: təhlil Planner interfeysidir.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from agents.orchestrator import ExecutionResult, Orchestrator, Task
from agents.verify import VerifyResult, log_error
from agents.workflow import PlanError, PlanStep, execution_order
from safety.ledger import log_budget_denial


@dataclass(frozen=True)
class Rule:
    """Plan addımı: açar söz tapılanda `department`-ə `permission` ilə tapşırıq verilir."""
    keyword: str
    department: str
    permission: str
    cost: float = 0.0


class Planner(Protocol):
    def plan(self, goal: str) -> list[Rule]: ...


class RulePlanner:
    """Deterministik təhlilçi (LLM əvəzi): məqsəd mətnində açar sözləri axtarır."""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self.rules = list(rules)

    def plan(self, goal: str) -> list[Rule]:
        text = goal.casefold()
        return [r for r in self.rules if r.keyword.casefold() in text]


@dataclass(frozen=True)
class StepReport:
    department: str
    status: str  # ok | blocked | unrouted | error | duplicate | failed_check | skipped
    agent: str = ""
    output: Any = None
    reason: str = ""
    step_id: str = ""
    attempts: int = 1
    op_id: str = ""  # son cəhdin op_id-si
    needs_reconciliation: bool = False  # xərc naməlum: rezerv əl ilə uzlaşdırılmalıdır
    approval_id: str = ""  # pending_approval: sahibin təsdiqini gözləyən sorğu
    restored: bool = False  # saxlanmış vəziyyətdən bərpa olunub (bu icrada yenidən işləməyib)


@dataclass(frozen=True)
class ResumeState:
    """Yarımçıq workflow-un davamı: `completed` (uğurlu) və `terminal` (icra olunmuş uğursuz) addımlar yenidən
    işləmir; `attempts` — addımın davam edəcəyi cəhd nömrəsi; `blocked` — əl ilə uzlaşdırmadan əvvəl işləməməli."""
    completed: Mapping[str, "StepReport"] = field(default_factory=dict)
    terminal: Mapping[str, "StepReport"] = field(default_factory=dict)
    attempts: Mapping[str, int] = field(default_factory=dict)
    blocked: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JobReport:
    goal: str
    status: str  # ok | partial | failed | no_plan | rejected
    steps: tuple[StepReport, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def failures(self) -> list[StepReport]:
        return [s for s in self.steps if s.status != "ok"]

    @property
    def needs_reconciliation(self) -> list[StepReport]:
        return [s for s in self.steps if s.needs_reconciliation]

    @property
    def pending_approvals(self) -> list[StepReport]:
        return [s for s in self.steps if s.status == "pending_approval"]

    @property
    def outputs(self) -> dict[str, Any]:
        return {s.department: s.output for s in self.steps if s.status == "ok"}

    def summary(self) -> str:
        head = f"{self.status}: {len(self.steps) - len(self.failures)}/{len(self.steps)} addım uğurlu"
        if self.reason:
            head += f" ({self.reason})"
        lines = [head]
        lines += [
            f"- {s.department}/{s.agent or '?'}: {s.status} — {s.reason}" for s in self.failures
        ]
        lines += [f"! əl ilə uzlaşdırma lazımdır: {s.op_id} (xərc naməlum, rezerv saxlanılır)"
                  for s in self.needs_reconciliation]
        lines += [f"? sahibin təsdiqi gözlənilir: {s.approval_id} ({s.department})"
                  for s in self.pending_approvals]
        return "\n".join(lines)


MAX_RETRIES = 2  # sərt yuxarı hədd


class CEO:
    def __init__(
        self,
        orchestrator: Orchestrator,
        planner: Planner,
        budget: float,
        checks: Mapping[str, Callable[[Any], bool]] | None = None,
        max_retries: int = 2,
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) \
                or not 0 <= max_retries <= MAX_RETRIES:
            raise ValueError(f"max_retries 0..{MAX_RETRIES} arasında tam ədəd olmalıdır")
        self.max_retries = max_retries
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
                or not math.isfinite(budget) or budget < 0:
            raise ValueError("büdcə sonlu və mənfi olmayan rəqəm olmalıdır")
        self.orchestrator = orchestrator
        self.planner = planner
        self.budget = float(budget)
        self.checks = dict(checks or {})

    def run(self, goal: str, job_id: str | None = None) -> JobReport:
        steps = self.planner.plan(goal) if goal and goal.strip() else []
        if not steps:
            return JobReport(goal, "no_plan", reason="uyğun şöbə tapılmadı")
        rejected = self._preflight(goal, steps)
        if rejected is not None:
            return rejected

        reports: list[StepReport] = []
        for i, step in enumerate(steps):
            if self._stopped():
                reports.append(StepReport(step.department, "skipped", reason="stop bayrağı aktivdir"))
                continue
            op_id = f"{job_id}:{i}:{step.department}" if job_id else ""
            reports.append(self._dispatch(goal, step, op_id))

        good = sum(r.status == "ok" for r in reports)
        status = "ok" if good == len(reports) else "partial" if good else "failed"
        return JobReport(goal, status, tuple(reports))

    def _dispatch(self, goal: str, step: Rule, op_id: str = "") -> StepReport:
        task = self._task(goal, step, op_id, f"ceo:{step.department}:{goal[:60]}")
        result = self.orchestrator.execute(task)  # gate burada işləyir
        if result.status != "ok":
            return StepReport(step.department, result.status, result.agent, reason=result.reason,
                              approval_id=result.approval_id)
        return self._verify(step, result)

    def _task(self, goal: str, step: Rule | PlanStep, op_id: str, description: str) -> Task:
        agents = self.orchestrator.registry.list(step.department)
        # Şöbə agentlərinin elan etdiyi girişlər məqsəd mətni ilə doldurulur.
        inputs = {k: goal for a in agents for k in a.spec.inputs}
        return Task(step.department, inputs, step.permission, step.cost,
                    description=description, op_id=op_id)

    def _preflight(self, goal: str, steps: Sequence[Rule | PlanStep]) -> JobReport | None:
        """İcradan əvvəl büdcə yoxlaması; rədd varsa heç bir addım icra olunmur."""
        total = sum(s.cost for s in steps)
        if total > self.budget + 1e-9:
            return JobReport(goal, "rejected",
                             reason=f"plan büdcəni aşır: {total:.4f} > {self.budget:.4f}")
        orch = self.orchestrator
        reason = None
        pol = orch.policy
        if pol is not None and pol.task_limit is not None:
            over = [s.department for s in steps if s.cost > pol.task_limit + 1e-9]
            if over:
                reason = f"tapşırıq limiti aşılır ({pol.task_limit:.4f}): {over}"
        room = orch.global_room()
        if reason is None and room is not None and total > room + 1e-9:
            reason = f"ümumi xərc limiti aşılır: plan {total:.4f} > qalan {room:.4f}"
        if reason is None:
            return None
        log_budget_denial(orch.audit_path, "ceo", "plan", total, f"ceo:{goal[:60]}", reason)
        return JobReport(goal, "rejected", reason=reason)

    def run_workflow(self, goal: str, steps: Sequence[PlanStep], job_id: str | None = None, *,
                     resume: ResumeState | None = None,
                     on_step: Callable[[PlanStep, StepReport], None] | None = None) -> JobReport:
        """Asılılıq qrafı üzrə icra: asılılıq əvvəl; uğursuz addımın asılıları dayandırılır;
        yalnız müvəqqəti və xərci məlum xətalar `max_retries`-ə qədər təkrar olunur.
        `resume`: saxlanmış vəziyyətdən davam (tamamlanmış addımlar yenidən işləmir); `on_step`: hər icra
        olunmuş/keçilmiş addımdan sonra çağırılır (davamlı saxlama üçün)."""
        try:
            ordered = execution_order(steps)
        except PlanError as exc:
            return JobReport(goal, "rejected", reason=str(exc))
        if resume is not None and not job_id:
            raise ValueError("davam etdirmə üçün job_id tələb olunur")
        fixed: dict[str, StepReport] = {} if resume is None else {**resume.completed, **resume.terminal}
        rejected = self._preflight(goal, [s for s in ordered if s.id not in fixed])
        if rejected is not None:
            return rejected
        job_id = job_id or uuid.uuid4().hex
        reports: dict[str, StepReport] = {}
        for step in ordered:
            if step.id in fixed:
                reports[step.id] = replace(fixed[step.id], restored=True)
                continue
            bad = next((d for d in step.depends_on if reports[d].status != "ok"), None)
            if self._stopped():
                reports[step.id] = StepReport(step.department, "skipped", step_id=step.id,
                                              reason="stop bayrağı aktivdir")
            elif bad is not None:
                reports[step.id] = StepReport(step.department, "skipped", step_id=step.id,
                                              reason=f"asılılıq uğursuz: {bad}")
            elif resume is not None and step.id in resume.blocked:
                reports[step.id] = StepReport(step.department, "skipped", step_id=step.id,
                                              reason=resume.blocked[step.id], needs_reconciliation=True)
            else:
                start = resume.attempts.get(step.id, 0) if resume is not None else 0
                reports[step.id] = self._run_step(goal, step, job_id, start)
            if on_step is not None:
                on_step(step, reports[step.id])
        final = tuple(reports[s.id] for s in ordered)
        good = sum(r.status == "ok" for r in final)
        status = "ok" if good == len(final) else "partial" if good else "failed"
        return JobReport(goal, status, final)

    def run_proposal(self, goal: str, proposal: Any, job_id: str | None = None) -> JobReport:
        """LLM/xarici təklifi icra edir: təklif etibarsız/mövcud deyilsə heç nə icra olunmur; etibarlıdırsa
        yalnız mövcud deterministik yolla (`run_workflow`: büdcə, Orchestrator, Broker, Gate, təsdiq)."""
        if not getattr(proposal, "ok", False):
            return JobReport(goal, "no_plan", reason=str(getattr(proposal, "reason", "") or "təklif rədd edildi"))
        return self.run_workflow(goal, proposal.steps, job_id)

    def _run_step(self, goal: str, step: PlanStep, job_id: str, start_attempt: int = 0) -> StepReport:
        if start_attempt > self.max_retries:  # cəhd limiti davam etdirmələr arasında da qüvvədədir
            return StepReport(step.department, "error", reason="retry limiti bitib", step_id=step.id,
                              attempts=start_attempt, op_id=f"{job_id}:{step.id}:a{start_attempt - 1}")
        for attempt in range(start_attempt, self.max_retries + 1):
            op_id = f"{job_id}:{step.id}:a{attempt}"  # hər cəhd üçün unikal
            task = self._task(goal, step, op_id, f"ceo:{step.id}#a{attempt}:{goal[:40]}")
            result = self.orchestrator.execute(task)  # gate + büdcə burada işləyir
            unknown = result.cost_status == "reserved_unknown"
            if result.status == "ok":
                return replace(self._verify(step, result), step_id=step.id, attempts=attempt + 1,
                               op_id=op_id, needs_reconciliation=unknown)
            # retry: yalnız müvəqqəti xəta VƏ xərc məlum (naməlum xərcdə avtomatik retry yoxdur)
            if result.status == "error" and result.transient and not unknown \
                    and result.cost_status == "committed":
                continue
            break
        reason = result.reason + (" — naməlum xərc: əl ilə uzlaşdırma lazımdır" if unknown else "")
        return StepReport(step.department, result.status, result.agent, reason=reason,
                          step_id=step.id, attempts=attempt + 1, op_id=op_id,
                          needs_reconciliation=unknown, approval_id=result.approval_id)

    def _verify(self, step: Rule, result: ExecutionResult) -> StepReport:
        check = self.checks.get(step.department)
        if check is not None:
            try:
                passed = bool(check(result.output))
                error = "çıxış gözlənilənə uyğun deyil"
            except Exception as exc:  # noqa: BLE001
                passed, error = False, f"yoxlama xətası: {type(exc).__name__}: {exc}"
            if not passed:
                log_error(Path(self.orchestrator.errors_path), result.agent, f"ceo:{step.department}",
                          VerifyResult(False, "check", error))
                return StepReport(step.department, "failed_check", result.agent, reason=error)
        return StepReport(step.department, "ok", result.agent, result.output)

    def _stopped(self) -> bool:
        return Path(self.orchestrator.stop_path).exists()
