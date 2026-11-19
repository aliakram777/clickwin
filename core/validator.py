"""Plan doğrulayıcısı (Mərhələ 18.2-B). Planlaşdırıcının təklifi ETİBARSIZDIR: READY olmazdan əvvəl bu qaydalardan keçməlidir. READY sahib təsdiqi DEYİL (L1-də hər addım ayrıca təsdiqlənir).

Qaydalar (fail-closed): hər addımın icra oluna bilən `capability_id`-si olmalıdır (icraçı `supports` deyir); həssas icazələr (spend_money/external_account/publish) və ödənişli addımlar
(`cost_estimate_azn` > 0; ledger vahidi) bu mərhələdə QADAĞANDIR; addım sayı məhduddur. Kernel `Plan` müqaviləsi artıq asılılıq/yoxlama meyarı/həssas icazə⇒təsdiq qaydalarını məcbur edir."""
from __future__ import annotations

from typing import Callable

from core.ports import ALLOWED_STEP_PERMISSIONS, ValidationResult
from kernel.goal import Goal, GoalStatus
from kernel.plan import Plan, PlanStepSpec

MAX_STEPS = 32


class PlanValidator:
    def __init__(self, supports: Callable[[PlanStepSpec], tuple[bool, str]] | None = None, max_steps: int = MAX_STEPS) -> None:
        self._supports, self._max = supports, max(1, min(int(max_steps), 64))

    def validate(self, goal: Goal, plan: Plan) -> ValidationResult:
        why: list[str] = []
        if goal.status is not GoalStatus.ACTIVE:
            why.append("goal_not_active")
        if plan.goal_id != goal.goal_id:
            why.append("goal_mismatch")
        if not plan.steps:
            why.append("no_steps")
        if len(plan.steps) > self._max:
            why.append("too_many_steps")
        for s in plan.steps:
            if set(s.required_permissions) - ALLOWED_STEP_PERMISSIONS:
                why.append(f"sensitive_permission:{s.step_id}")
            if s.cost_estimate_azn > 0:
                why.append(f"paid_step_not_supported:{s.step_id}")
            if s.capability_id is None:
                why.append(f"no_capability:{s.step_id}")
            elif self._supports is not None:
                ok, reason = self._supports(s)
                if not ok:
                    why.append(f"unsupported:{s.step_id}:{(reason or 'no_handler')[:40]}")
        return ValidationResult(not why, tuple(why))
