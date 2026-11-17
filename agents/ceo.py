"""CEO agent: tapşırığı təhlil edir, şöbə agentlərinə bölür, nəticələri toplayıb yoxlayır.

CEO özü yan təsir yaratmır: hər icra Orchestrator -> safety/gate.py üzərindən keçir
(icazə, xərc limiti, təsdiq, audit, stop). LLM yoxdur: təhlil Planner interfeysidir.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from agents.orchestrator import ExecutionResult, Orchestrator, Task
from agents.verify import VerifyResult, log_error


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
    status: str  # ok | blocked | unrouted | error | failed_check | skipped
    agent: str = ""
    output: Any = None
    reason: str = ""


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
        return "\n".join(lines)


class CEO:
    def __init__(
        self,
        orchestrator: Orchestrator,
        planner: Planner,
        budget: float,
        checks: Mapping[str, Callable[[Any], bool]] | None = None,
    ) -> None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
                or not math.isfinite(budget) or budget < 0:
            raise ValueError("büdcə sonlu və mənfi olmayan rəqəm olmalıdır")
        self.orchestrator = orchestrator
        self.planner = planner
        self.budget = float(budget)
        self.checks = dict(checks or {})

    def run(self, goal: str) -> JobReport:
        steps = self.planner.plan(goal) if goal and goal.strip() else []
        if not steps:
            return JobReport(goal, "no_plan", reason="uyğun şöbə tapılmadı")
        total = sum(s.cost for s in steps)
        if total > self.budget + 1e-9:
            return JobReport(goal, "rejected",
                             reason=f"plan büdcəni aşır: {total:.4f} > {self.budget:.4f}")

        reports: list[StepReport] = []
        for step in steps:
            if self._stopped():
                reports.append(StepReport(step.department, "skipped", reason="stop bayrağı aktivdir"))
                continue
            reports.append(self._dispatch(goal, step))

        good = sum(r.status == "ok" for r in reports)
        status = "ok" if good == len(reports) else "partial" if good else "failed"
        return JobReport(goal, status, tuple(reports))

    def _dispatch(self, goal: str, step: Rule) -> StepReport:
        agents = self.orchestrator.registry.list(step.department)
        # Şöbə agentlərinin elan etdiyi girişlər məqsəd mətni ilə doldurulur.
        inputs = {k: goal for a in agents for k in a.spec.inputs}
        task = Task(step.department, inputs, step.permission, step.cost,
                    description=f"ceo:{step.department}:{goal[:60]}")
        result = self.orchestrator.execute(task)  # gate burada işləyir
        if result.status != "ok":
            return StepReport(step.department, result.status, result.agent, reason=result.reason)
        return self._verify(step, result)

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
