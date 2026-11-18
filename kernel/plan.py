"""Plan müqaviləsi (Mərhələ 18.0): sıralı addımlar, asılılıqlar, gözlənilən nəticə, yoxlama meyarları, versiya və status. Plan İCAZƏ VERMİR: addımın tələb etdiyi icazələr
yalnız TƏSVİRDİR; həssas icazə (`SENSITIVE_PERMISSIONS`) sahib təsdiqi tələb edir və bu qeyd edilməsə plan rədd edilir. İcra yalnız Broker/Gate/Ledger/təsdiq yolundadır."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from agents.spec import KNOWN_PERMISSIONS, SENSITIVE_PERMISSIONS
from kernel.common import (SCHEMA_VERSION, ContractError, InvalidTransition, Origin, as_tuple, canonical_json, check_capability_id, check_enum, check_id, check_int, check_keys, check_money,
                           check_slug, check_version, clean_text, ensure_utc, fmt_dt, parse_dt, parse_json_object, sha256_hex, utcnow)


class PlanStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"            # plan tamamdır (sahib təsdiqi DEYİL)
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


PLAN_TRANSITIONS: dict[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.DRAFT: frozenset({PlanStatus.READY, PlanStatus.CANCELLED, PlanStatus.SUPERSEDED}),
    PlanStatus.READY: frozenset({PlanStatus.EXECUTING, PlanStatus.SUPERSEDED, PlanStatus.CANCELLED}),
    PlanStatus.EXECUTING: frozenset({PlanStatus.PAUSED, PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}),
    PlanStatus.PAUSED: frozenset({PlanStatus.EXECUTING, PlanStatus.CANCELLED}),
    PlanStatus.COMPLETED: frozenset(),
    PlanStatus.FAILED: frozenset(),
    PlanStatus.CANCELLED: frozenset(),
    PlanStatus.SUPERSEDED: frozenset(),
}
MAX_STEPS = 64


def new_plan_id() -> str:
    return "plan_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class PlanStepSpec:
    step_id: str
    title: str
    expected_outcome: str
    verification_criteria: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    capability_id: str | None = None
    required_permissions: tuple[str, ...] = ()
    requires_owner_approval: bool = False
    cost_estimate_azn: float = 0.0

    def __post_init__(self) -> None:
        check_slug("step_id", self.step_id)
        object.__setattr__(self, "title", clean_text("step.title", self.title, max_len=200, min_len=1))
        object.__setattr__(self, "expected_outcome", clean_text("step.expected_outcome", self.expected_outcome, max_len=1000, min_len=1, multiline=True))
        vc = as_tuple("step.verification_criteria", self.verification_criteria, max_items=10)
        if not vc:
            raise ContractError("step.verification_criteria: ən azı bir yoxlama meyarı tələb olunur", "missing_verification")
        object.__setattr__(self, "verification_criteria", tuple(clean_text("step.verification_criterion", c, max_len=500, min_len=1, multiline=True) for c in vc))
        deps = as_tuple("step.depends_on", self.depends_on, max_items=MAX_STEPS)
        for d in deps:
            check_slug("depends_on", d)
        if len(set(deps)) != len(deps) or self.step_id in deps:
            raise ContractError("step.depends_on: təkrar və ya özünə asılılıq", "bad_dependency")
        object.__setattr__(self, "depends_on", deps)
        if self.capability_id is not None:
            check_capability_id("step.capability_id", self.capability_id)
        perms = as_tuple("step.required_permissions", self.required_permissions, max_items=8)
        if not all(isinstance(p, str) for p in perms) or set(perms) - KNOWN_PERMISSIONS or len(set(perms)) != len(perms):
            raise ContractError("step.required_permissions: naməlum və ya təkrar icazə", "bad_permission")
        object.__setattr__(self, "required_permissions", perms)
        if not isinstance(self.requires_owner_approval, bool):
            raise ContractError("step.requires_owner_approval: bool tələb olunur", "bad_type")
        if set(perms) & SENSITIVE_PERMISSIONS and not self.requires_owner_approval:       # həssas icazə təsdiqsiz təsvir oluna BİLMƏZ
            raise ContractError("step: həssas icazə (pul/xarici hesab/nəşr) sahib təsdiqi tələb edir", "owner_approval_required")
        object.__setattr__(self, "cost_estimate_azn", check_money("step.cost_estimate_azn", self.cost_estimate_azn))

    def to_dict(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "title": self.title, "expected_outcome": self.expected_outcome, "verification_criteria": list(self.verification_criteria),
                "depends_on": list(self.depends_on), "capability_id": self.capability_id, "required_permissions": list(self.required_permissions),
                "requires_owner_approval": self.requires_owner_approval, "cost_estimate_azn": self.cost_estimate_azn}

    @classmethod
    def from_dict(cls, data: Any) -> "PlanStepSpec":
        check_keys("step", data, ("step_id", "title", "expected_outcome", "verification_criteria"),
                   ("depends_on", "capability_id", "required_permissions", "requires_owner_approval", "cost_estimate_azn"))
        return cls(data["step_id"], data["title"], data["expected_outcome"], tuple(as_tuple("verification_criteria", data["verification_criteria"], max_items=10)),
                   tuple(as_tuple("depends_on", data.get("depends_on", []), max_items=MAX_STEPS)), data.get("capability_id"),
                   tuple(as_tuple("required_permissions", data.get("required_permissions", []), max_items=8)), data.get("requires_owner_approval", False),
                   data.get("cost_estimate_azn", 0.0))


def _validate_steps(steps: tuple[PlanStepSpec, ...]) -> None:
    """Sıralı addımlar: ID-lər unikal; hər asılılıq YALNIZ əvvəlki addımlara işarə edir (beləliklə dövr mümkün deyil və sıra topoloji sıradır)."""
    if not steps or len(steps) > MAX_STEPS:
        raise ContractError(f"plan: 1..{MAX_STEPS} addım tələb olunur", "bad_steps")
    seen: set[str] = set()
    for s in steps:
        if not isinstance(s, PlanStepSpec):
            raise ContractError("plan.steps: PlanStepSpec tələb olunur", "bad_type")
        if s.step_id in seen:
            raise ContractError("plan: təkrar step_id", "duplicate")
        missing = [d for d in s.depends_on if d not in seen]
        if missing:
            raise ContractError("plan: asılılıq yalnız əvvəlki addımlara işarə edə bilər (naməlum/irəlidəki/dövri asılılıq)", "bad_dependency")
        seen.add(s.step_id)


@dataclass(frozen=True)
class Plan:
    plan_id: str
    goal_id: str
    steps: tuple[PlanStepSpec, ...]
    origin: Origin
    created_at: datetime
    updated_at: datetime
    version: int = 1
    status: PlanStatus = PlanStatus.DRAFT
    schema_version: int = SCHEMA_VERSION

    GRANTS_EXECUTION = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("plan: schema_version uyğun deyil", "bad_version")
        check_id("plan_id", self.plan_id, "plan")
        check_id("goal_id", self.goal_id, "goal")
        steps = as_tuple("plan.steps", self.steps, max_items=MAX_STEPS)
        _validate_steps(steps)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "origin", check_enum("plan.origin", Origin, self.origin))
        object.__setattr__(self, "status", check_enum("plan.status", PlanStatus, self.status))
        object.__setattr__(self, "version", check_int("plan.version", self.version, lo=1, hi=10_000))
        object.__setattr__(self, "created_at", ensure_utc("plan.created_at", self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc("plan.updated_at", self.updated_at))
        if self.updated_at < self.created_at:
            raise ContractError("plan.updated_at created_at-dən əvvəl ola bilməz", "bad_time")

    @classmethod
    def new(cls, goal_id: str, steps: tuple[PlanStepSpec, ...], *, origin: Origin, plan_id: str | None = None, now: datetime | None = None) -> "Plan":
        t = ensure_utc("now", now) if now is not None else utcnow()
        return cls(plan_id or new_plan_id(), goal_id, tuple(steps), origin, t, t)

    def transition(self, new_status: PlanStatus, *, at: datetime | None = None) -> "Plan":
        new_status = check_enum("status", PlanStatus, new_status)
        if new_status not in PLAN_TRANSITIONS[self.status]:
            raise InvalidTransition(f"plan: {self.status.value} -> {new_status.value} keçidi icazəli deyil")
        when = ensure_utc("at", at) if at is not None else utcnow()
        return replace(self, status=new_status, updated_at=max(when, self.updated_at))

    def revise(self, steps: tuple[PlanStepSpec, ...], *, at: datetime | None = None) -> "Plan":
        """Yeni versiya (version+1, DRAFT). Yalnız DRAFT/READY plan yenilənə bilər (icra olunan plan dəyişdirilmir)."""
        if self.status not in (PlanStatus.DRAFT, PlanStatus.READY):
            raise InvalidTransition("plan: yalnız draft/ready plan yenilənə bilər")
        when = ensure_utc("at", at) if at is not None else utcnow()
        return replace(self, steps=tuple(steps), version=self.version + 1, status=PlanStatus.DRAFT, updated_at=max(when, self.updated_at))

    @property
    def total_cost_estimate_azn(self) -> float:
        return float(sum(s.cost_estimate_azn for s in self.steps))

    @property
    def needs_owner_approval(self) -> bool:
        return any(s.requires_owner_approval for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "plan_id": self.plan_id, "goal_id": self.goal_id, "version": self.version, "status": self.status.value,
                "origin": self.origin.value, "steps": [s.to_dict() for s in self.steps], "created_at": fmt_dt(self.created_at), "updated_at": fmt_dt(self.updated_at)}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json({"goal_id": self.goal_id, "version": self.version, "steps": [s.to_dict() for s in self.steps]}))

    @classmethod
    def from_dict(cls, data: Any) -> "Plan":
        check_keys("plan", data, ("schema_version", "plan_id", "goal_id", "steps", "origin", "created_at", "updated_at"), ("version", "status"))
        d = check_version("plan", data)
        steps = tuple(PlanStepSpec.from_dict(s) for s in as_tuple("steps", d["steps"], max_items=MAX_STEPS))
        return cls(d["plan_id"], d["goal_id"], steps, check_enum("origin", Origin, d["origin"]), parse_dt("created_at", d["created_at"]),
                   parse_dt("updated_at", d["updated_at"]), d.get("version", 1), check_enum("status", PlanStatus, d.get("status", "draft")))

    @classmethod
    def from_json(cls, text: str) -> "Plan":
        return cls.from_dict(parse_json_object("plan", text))
