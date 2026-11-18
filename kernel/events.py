"""Event müqaviləsi (Mərhələ 18.0): dəyişməz, versiyalı, idempotent açarlı hadisə. Hadisə yalnız FAKTI qeyd edir (heç nə icra etmir/icazə vermir). Yük (`payload`) qeydə alınmış hadisə növünün
sxemi ilə ciddi yoxlanılır (naməlum/çatışmayan sahə, tip, seçim, sirr, ölçü). Mətn sahələri ETİBARSIZDIR (mənşə `origin`)."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from kernel.common import (SCHEMA_VERSION, ContractError, Origin, canonical_json, check_capability_id, check_enum, check_hex64, check_id, check_int, check_keys,
                           check_version, clean_text, ensure_utc, fmt_dt, parse_dt, parse_json_object, sha256_hex, utcnow)
from kernel.capability import Availability
from kernel.goal import GoalStatus
from kernel.plan import PlanStatus
from kernel.proposal import ApprovalDecision

_REF = re.compile(r"^[a-z0-9][a-z0-9_\-:.]{2,95}$")


class EntityKind(str, Enum):
    GOAL = "goal"
    PLAN = "plan"
    CAPABILITY = "capability"
    PROPOSAL = "proposal"


@dataclass(frozen=True)
class EntityRef:
    kind: EntityKind
    entity_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", check_enum("entity.kind", EntityKind, self.kind))
        if self.kind is EntityKind.CAPABILITY:
            check_capability_id("entity.entity_id", self.entity_id)
        else:
            check_id("entity.entity_id", self.entity_id, {"goal": "goal", "plan": "plan", "proposal": "prop"}[self.kind.value])

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "entity_id": self.entity_id}

    @classmethod
    def from_dict(cls, data: Any) -> "EntityRef":
        check_keys("entity", data, ("kind", "entity_id"))
        return cls(data["kind"], data["entity_id"])


@dataclass(frozen=True)
class PayloadField:
    kind: str                      # "str" | "int" | "hash"
    required: bool = True
    choices: tuple[str, ...] | None = None
    max_len: int = 300


def _statuses(enum_cls: type[Enum]) -> tuple[str, ...]:
    return tuple(e.value for e in enum_cls)


EVENT_SCHEMAS: dict[str, dict[str, PayloadField]] = {
    "goal.created": {"title": PayloadField("str", max_len=200)},
    "goal.status_changed": {"from_status": PayloadField("str", choices=_statuses(GoalStatus)), "to_status": PayloadField("str", choices=_statuses(GoalStatus)),
                            "reason": PayloadField("str", required=False)},
    "plan.created": {"goal_id": PayloadField("str", max_len=80), "version": PayloadField("int"), "step_count": PayloadField("int")},
    "plan.status_changed": {"from_status": PayloadField("str", choices=_statuses(PlanStatus)), "to_status": PayloadField("str", choices=_statuses(PlanStatus)),
                            "reason": PayloadField("str", required=False)},
    "capability.registered": {"name": PayloadField("str", max_len=120), "availability": PayloadField("str", choices=_statuses(Availability))},
    "capability.availability_changed": {"from_availability": PayloadField("str", choices=_statuses(Availability)),
                                        "to_availability": PayloadField("str", choices=_statuses(Availability)), "evidence_ref": PayloadField("str", required=False)},
    "proposal.submitted": {"content_hash": PayloadField("hash"), "risk_level": PayloadField("str", choices=("low", "medium", "high", "critical"))},
    "proposal.decision_recorded": {"content_hash": PayloadField("hash"), "decision": PayloadField("str", choices=_statuses(ApprovalDecision)),
                                   "approval_ref": PayloadField("str", required=False, max_len=96)},
}


def validate_payload(event_type: str, payload: Any) -> dict[str, Any]:
    """Yükü qeydə alınmış sxemlə yoxlayır; təmiz nüsxə qaytarır. Naməlum növ/sahib sahə rədd edilir (fail-closed)."""
    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None:
        raise ContractError("event_type qeydə alınmayıb", "unknown_event_type")
    if not isinstance(payload, Mapping):
        raise ContractError("payload: obyekt tələb olunur", "bad_type")
    check_keys("payload", payload, [k for k, f in schema.items() if f.required], [k for k, f in schema.items() if not f.required])
    out: dict[str, Any] = {}
    for key, spec in schema.items():
        if key not in payload:
            continue
        value = payload[key]
        if spec.kind == "int":
            out[key] = check_int(f"payload.{key}", value, lo=0, hi=1_000_000)
        elif spec.kind == "hash":
            out[key] = check_hex64(f"payload.{key}", value)
        else:
            text = clean_text(f"payload.{key}", value, max_len=spec.max_len, min_len=1)
            if spec.choices is not None and text not in spec.choices:
                raise ContractError(f"payload.{key}: icazəli dəyərlərdən biri tələb olunur", "bad_enum")
            out[key] = text
    return out


def new_event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:20]


def _ref(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _REF.match(value):
        raise ContractError(f"{name}: kiçik hərf/rəqəm/_-:. (3-96) tələb olunur", "bad_id")
    return value


@dataclass(frozen=True)
class Event:
    """`payload_json` kanonik JSON mətnidir (dəyişməzlik və hash üçün); `.payload` hər çağırışda təmiz kopya verir."""
    event_id: str
    event_type: str
    entity: EntityRef
    timestamp: datetime
    correlation_id: str
    op_id: str
    origin: Origin
    payload_json: str
    schema_version: int = SCHEMA_VERSION

    GRANTS_EXECUTION = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("event: schema_version uyğun deyil", "bad_version")
        check_id("event_id", self.event_id, "evt")
        if not isinstance(self.entity, EntityRef):
            raise ContractError("event.entity: EntityRef tələb olunur", "bad_type")
        if not isinstance(self.event_type, str) or self.event_type.split(".", 1)[0] != self.entity.kind.value:
            raise ContractError("event_type varlıq növünə uyğun deyil", "entity_mismatch")
        object.__setattr__(self, "timestamp", ensure_utc("event.timestamp", self.timestamp))
        _ref("correlation_id", self.correlation_id)
        _ref("op_id", self.op_id)
        object.__setattr__(self, "origin", check_enum("event.origin", Origin, self.origin))
        data = validate_payload(self.event_type, parse_json_object("payload", self.payload_json))
        object.__setattr__(self, "payload_json", canonical_json(data))

    @classmethod
    def create(cls, event_type: str, entity: EntityRef, payload: Mapping[str, Any], *, op_id: str, correlation_id: str, origin: Origin,
               timestamp: datetime | None = None, event_id: str | None = None) -> "Event":
        data = validate_payload(event_type, payload)
        return cls(event_id or new_event_id(), event_type, entity, ensure_utc("timestamp", timestamp) if timestamp is not None else utcnow(), correlation_id, op_id,
                   origin, canonical_json(data))

    @property
    def payload(self) -> dict[str, Any]:
        return parse_json_object("payload", self.payload_json)

    @property
    def content_hash(self) -> str:
        """İdempotentlik müqayisəsi: eyni `op_id` yalnız EYNİ məzmunla təkrarlana bilər (event_id/timestamp məzmuna daxil DEYİL)."""
        return sha256_hex(canonical_json({"event_type": self.event_type, "entity": self.entity.to_dict(), "correlation_id": self.correlation_id,
                                          "origin": self.origin.value, "payload": self.payload, "schema_version": self.schema_version}))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "event_id": self.event_id, "event_type": self.event_type, "entity": self.entity.to_dict(),
                "timestamp": fmt_dt(self.timestamp), "correlation_id": self.correlation_id, "op_id": self.op_id, "origin": self.origin.value, "payload": self.payload}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any) -> "Event":
        check_keys("event", data, ("schema_version", "event_id", "event_type", "entity", "timestamp", "correlation_id", "op_id", "origin", "payload"))
        d = check_version("event", data)
        return cls(d["event_id"], d["event_type"], EntityRef.from_dict(d["entity"]), parse_dt("timestamp", d["timestamp"]), d["correlation_id"], d["op_id"],
                   check_enum("origin", Origin, d["origin"]), canonical_json(validate_payload(d["event_type"], d["payload"])))

    @classmethod
    def from_json(cls, text: str) -> "Event":
        return cls.from_dict(parse_json_object("event", text))
