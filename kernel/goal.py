"""Goal müqaviləsi (Mərhələ 18.0): məqsəd, qəbul meyarları, büdcə referansı, status keçidləri. Məqsəd İCAZƏ VERMİR və heç nə icra etmir."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from kernel.common import (SCHEMA_VERSION, BudgetRef, ContractError, InvalidTransition, Origin, as_tuple, canonical_json, check_enum, check_id, check_keys,
                           check_slug, check_version, clean_text, ensure_utc, fmt_dt, parse_dt, parse_json_object, sha256_hex, utcnow)


class GoalStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.DRAFT: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLED}),
    GoalStatus.ACTIVE: frozenset({GoalStatus.BLOCKED, GoalStatus.PAUSED, GoalStatus.COMPLETED, GoalStatus.FAILED, GoalStatus.CANCELLED}),
    GoalStatus.BLOCKED: frozenset({GoalStatus.ACTIVE, GoalStatus.FAILED, GoalStatus.CANCELLED}),
    GoalStatus.PAUSED: frozenset({GoalStatus.ACTIVE, GoalStatus.CANCELLED}),
    GoalStatus.COMPLETED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.CANCELLED: frozenset(),
}
TERMINAL_GOAL_STATUSES = frozenset(s for s, nxt in GOAL_TRANSITIONS.items() if not nxt)
METHODS = ("manual", "automated", "evidence")


@dataclass(frozen=True)
class AcceptanceCriterion:
    """Qəbul meyarı: nəyin, hansı üsulla yoxlanacağı. Yoxlamanın özü bu müqavilədə İCRA OLUNMUR."""
    criterion_id: str
    text: str
    method: str = "manual"

    def __post_init__(self) -> None:
        check_slug("criterion_id", self.criterion_id)
        object.__setattr__(self, "text", clean_text("criterion.text", self.text, max_len=500, min_len=1, multiline=True))
        if self.method not in METHODS:
            raise ContractError(f"criterion.method: {', '.join(METHODS)} olmalıdır", "bad_enum")

    def to_dict(self) -> dict[str, Any]:
        return {"criterion_id": self.criterion_id, "text": self.text, "method": self.method}

    @classmethod
    def from_dict(cls, data: Any) -> "AcceptanceCriterion":
        check_keys("criterion", data, ("criterion_id", "text", "method"))
        return cls(data["criterion_id"], data["text"], data["method"])


def new_goal_id() -> str:
    return "goal_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class Goal:
    """Dəyişməz məqsəd. Status keçidləri `transition()` ilə (yeni nüsxə qaytarır). `origin` etibarsız mənşəni (LLM/agent/xarici) bildirir: mətn məlumatdır, təlimat deyil."""
    goal_id: str
    title: str
    origin: Origin
    created_at: datetime
    updated_at: datetime
    description: str = ""
    priority: Priority = Priority.NORMAL
    status: GoalStatus = GoalStatus.DRAFT
    parent_goal_id: str | None = None
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    budget_ref: BudgetRef | None = None
    schema_version: int = SCHEMA_VERSION

    GRANTS_EXECUTION = False        # məqsəd icra icazəsi VERMİR

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("goal: schema_version uyğun deyil", "bad_version")
        check_id("goal_id", self.goal_id, "goal")
        object.__setattr__(self, "title", clean_text("goal.title", self.title, max_len=200, min_len=1))
        object.__setattr__(self, "description", clean_text("goal.description", self.description, max_len=4000, multiline=True))
        object.__setattr__(self, "origin", check_enum("goal.origin", Origin, self.origin))
        object.__setattr__(self, "priority", check_enum("goal.priority", Priority, self.priority))
        object.__setattr__(self, "status", check_enum("goal.status", GoalStatus, self.status))
        object.__setattr__(self, "created_at", ensure_utc("goal.created_at", self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc("goal.updated_at", self.updated_at))
        if self.updated_at < self.created_at:
            raise ContractError("goal.updated_at created_at-dən əvvəl ola bilməz", "bad_time")
        if self.parent_goal_id is not None:
            check_id("parent_goal_id", self.parent_goal_id, "goal")
            if self.parent_goal_id == self.goal_id:
                raise ContractError("goal özünün valideyni ola bilməz", "bad_parent")
        criteria = as_tuple("acceptance_criteria", self.acceptance_criteria, max_items=20)
        if not all(isinstance(c, AcceptanceCriterion) for c in criteria):
            raise ContractError("acceptance_criteria: AcceptanceCriterion elementləri tələb olunur", "bad_type")
        if len({c.criterion_id for c in criteria}) != len(criteria):
            raise ContractError("acceptance_criteria: təkrar criterion_id", "duplicate")
        object.__setattr__(self, "acceptance_criteria", criteria)
        if self.budget_ref is not None and not isinstance(self.budget_ref, BudgetRef):
            raise ContractError("budget_ref: BudgetRef tələb olunur", "bad_type")

    @classmethod
    def new(cls, title: str, *, origin: Origin, description: str = "", priority: Priority = Priority.NORMAL, parent_goal_id: str | None = None,
            acceptance_criteria: tuple[AcceptanceCriterion, ...] = (), budget_ref: BudgetRef | None = None, goal_id: str | None = None,
            now: datetime | None = None) -> "Goal":
        t = ensure_utc("now", now) if now is not None else utcnow()
        return cls(goal_id or new_goal_id(), title, origin, t, t, description, priority, GoalStatus.DRAFT, parent_goal_id, tuple(acceptance_criteria), budget_ref)

    def transition(self, new_status: GoalStatus, *, at: datetime | None = None) -> "Goal":
        new_status = check_enum("status", GoalStatus, new_status)
        if new_status not in GOAL_TRANSITIONS[self.status]:
            raise InvalidTransition(f"goal: {self.status.value} -> {new_status.value} keçidi icazəli deyil")
        when = ensure_utc("at", at) if at is not None else utcnow()
        return replace(self, status=new_status, updated_at=max(when, self.updated_at))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_GOAL_STATUSES

    # --- serializasiya
    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "goal_id": self.goal_id, "title": self.title, "description": self.description, "origin": self.origin.value,
                "priority": self.priority.value, "status": self.status.value, "parent_goal_id": self.parent_goal_id,
                "acceptance_criteria": [c.to_dict() for c in self.acceptance_criteria], "budget_ref": self.budget_ref.to_dict() if self.budget_ref else None,
                "created_at": fmt_dt(self.created_at), "updated_at": fmt_dt(self.updated_at)}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.to_json())

    @classmethod
    def from_dict(cls, data: Any) -> "Goal":
        check_keys("goal", data, ("schema_version", "goal_id", "title", "origin", "created_at", "updated_at"),
                   ("description", "priority", "status", "parent_goal_id", "acceptance_criteria", "budget_ref"))
        d = check_version("goal", data)
        crit = as_tuple("acceptance_criteria", d.get("acceptance_criteria", []), max_items=20)
        budget = d.get("budget_ref")
        return cls(d["goal_id"], d["title"], check_enum("origin", Origin, d["origin"]), parse_dt("created_at", d["created_at"]), parse_dt("updated_at", d["updated_at"]),
                   d.get("description", ""), check_enum("priority", Priority, d.get("priority", "normal")), check_enum("status", GoalStatus, d.get("status", "draft")),
                   d.get("parent_goal_id"), tuple(AcceptanceCriterion.from_dict(c) for c in crit), BudgetRef.from_dict(budget) if budget is not None else None)

    @classmethod
    def from_json(cls, text: str) -> "Goal":
        return cls.from_dict(parse_json_object("goal", text))
