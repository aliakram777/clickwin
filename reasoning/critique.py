"""Deterministik tənqidçi (Mərhələ 18.4): planı icradan əvvəl qaydalarla yoxlayır (model YOXDUR). `blocking` problem ⇒ `GuardedPlanner` planı geri qaytarmır.
Qaydalar: capability yoxdur/əlçatmaz, yoxlama meyarı maşınla yoxlanmır (xəbərdarlıq: avtomatik «verified» olmayacaq), təkrar başlıq, çox uzun zəncir."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from kernel.goal import Goal
from kernel.plan import Plan
from reasoning.verify import is_machine_checkable


@dataclass(frozen=True)
class Issue:
    code: str
    step_id: str
    blocking: bool


class Critic:
    def __init__(self, available_capabilities: Callable[[], set[str]], max_chain: int = 6) -> None:
        self._caps, self._max_chain = available_capabilities, max_chain

    def review(self, plan: Plan) -> list[Issue]:
        caps, issues = set(self._caps()), []
        titles: set[str] = set()
        depth: dict[str, int] = {}
        for s in plan.steps:
            if s.capability_id is None or s.capability_id not in caps:
                issues.append(Issue("capability_unavailable", s.step_id, True))
            if not all(is_machine_checkable(c) for c in s.verification_criteria):
                issues.append(Issue("criteria_not_machine_checkable", s.step_id, False))
            if s.title.casefold() in titles:
                issues.append(Issue("duplicate_title", s.step_id, False))
            titles.add(s.title.casefold())
            depth[s.step_id] = 1 + max((depth[d] for d in s.depends_on), default=0)
            if depth[s.step_id] > self._max_chain:
                issues.append(Issue("chain_too_long", s.step_id, True))
        return issues


class GuardedPlanner:
    """Planlaşdırıcını tənqidçi ilə sarır: bloklayan problem varsa `None` (plan əvvəlcə düzəldilməlidir)."""

    def __init__(self, inner, critic: Critic) -> None:
        self._inner, self._critic = inner, critic
        self.last_issues: list[Issue] = []

    def propose(self, goal: Goal) -> Plan | None:
        plan = self._inner.propose(goal)
        self.last_issues = self._critic.review(plan) if plan is not None else []
        return None if plan is None or any(i.blocking for i in self.last_issues) else plan
