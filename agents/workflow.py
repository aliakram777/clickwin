"""Tapşırıq asılılıq qrafı (DAG): doğrulama və icra ardıcıllığı."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class PlanError(ValueError):
    """Plan yanlışdır (boş, təkrar id, olmayan asılılıq, dövr)."""


@dataclass(frozen=True)
class PlanStep:
    id: str
    department: str
    permission: str
    cost: float = 0.0
    depends_on: tuple[str, ...] = ()
    description: str = ""


def validate_plan(steps: Sequence[PlanStep]) -> None:
    if not steps:
        raise PlanError("plan boşdur")
    seen: set[str] = set()
    for s in steps:
        if not isinstance(s.id, str) or not s.id.strip():
            raise PlanError("addım id-si boş ola bilməz")
        if not isinstance(s.department, str) or not s.department.strip():
            raise PlanError(f"{s.id}: şöbə boş ola bilməz")
        if not isinstance(s.depends_on, (tuple, list)) or not all(
                isinstance(d, str) and d for d in s.depends_on):
            raise PlanError(f"{s.id}: depends_on sətirlər siyahısı olmalıdır")
        if s.id in seen:
            raise PlanError(f"təkrar addım id-si: {s.id}")
        seen.add(s.id)
    for s in steps:
        for d in s.depends_on:
            if d == s.id:
                raise PlanError(f"{s.id} özünə asılıdır")
            if d not in seen:
                raise PlanError(f"{s.id}: olmayan asılılıq: {d}")
    _order(steps)  # dövr varsa PlanError


def execution_order(steps: Sequence[PlanStep]) -> list[PlanStep]:
    """Asılılıqlar əvvəl gələn sıra; asılı olmayanlar elan sırasını saxlayır."""
    validate_plan(steps)
    return _order(steps)


def _order(steps: Sequence[PlanStep]) -> list[PlanStep]:
    done: set[str] = set()
    remaining = list(steps)
    ordered: list[PlanStep] = []
    while remaining:
        nxt = next((s for s in remaining if all(d in done for d in s.depends_on)), None)
        if nxt is None:
            raise PlanError("asılılıq dövrü: " + ", ".join(s.id for s in remaining))
        ordered.append(nxt)
        done.add(nxt.id)
        remaining.remove(nxt)
    return ordered
