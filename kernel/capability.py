"""Capability müqaviləsi (Mərhələ 18.0): NAYMERA-nın nəyi bacardığının TƏSVİRİ (giriş/çıxış müqaviləsi, tələb olunan icazələr, risk, əlçatanlıq, sübut). Qabiliyyət İCAZƏ VERMİR və İCRA ETMİR:
`permissions_required` yalnız tələbdir (səlahiyyət Gate/təsdiqdədir). Dürüstlük qaydası: `AVAILABLE` yalnız sübutla `VERIFIED` qabiliyyət üçündür; simulyasiya/qurulmamış real kimi göstərilmir."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from agents.spec import KNOWN_PERMISSIONS, SENSITIVE_PERMISSIONS
from kernel.common import (SCHEMA_VERSION, ContractError, Evidence, InvalidTransition, Origin, RiskLevel, as_tuple, canonical_json, check_capability_id, check_enum,
                           check_keys, check_slug, check_version, clean_text, ensure_utc, fmt_dt, parse_dt, parse_json_object, risk_at_least, sha256_hex, utcnow)


class Availability(str, Enum):
    AVAILABLE = "available"
    SIMULATION = "simulation"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


AVAILABILITY_TRANSITIONS: dict[Availability, frozenset[Availability]] = {
    Availability.NOT_CONFIGURED: frozenset({Availability.SIMULATION, Availability.AVAILABLE, Availability.DISABLED}),
    Availability.SIMULATION: frozenset({Availability.NOT_CONFIGURED, Availability.AVAILABLE, Availability.DISABLED}),
    Availability.AVAILABLE: frozenset({Availability.NOT_CONFIGURED, Availability.DISABLED}),
    Availability.DISABLED: frozenset({Availability.NOT_CONFIGURED, Availability.SIMULATION}),
}
FIELD_TYPES = ("string", "integer", "number", "boolean", "json")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        check_slug("field.name", self.name)
        if self.type not in FIELD_TYPES:
            raise ContractError(f"field.type: {', '.join(FIELD_TYPES)} olmalıdır", "bad_enum")
        if not isinstance(self.required, bool):
            raise ContractError("field.required: bool tələb olunur", "bad_type")
        object.__setattr__(self, "description", clean_text("field.description", self.description, max_len=300))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "required": self.required, "description": self.description}

    @classmethod
    def from_dict(cls, data: Any) -> "FieldSpec":
        check_keys("field", data, ("name", "type"), ("required", "description"))
        return cls(data["name"], data["type"], data.get("required", True), data.get("description", ""))


def _fields(name: str, value: Any) -> tuple[FieldSpec, ...]:
    items = as_tuple(name, value, max_items=32)
    if not all(isinstance(f, FieldSpec) for f in items) or len({f.name for f in items}) != len(items):
        raise ContractError(f"{name}: FieldSpec elementləri və unikal adlar tələb olunur", "bad_fields")
    return items


@dataclass(frozen=True)
class Capability:
    capability_id: str
    name: str
    description: str
    origin: Origin
    risk: RiskLevel
    registered_at: datetime
    updated_at: datetime
    inputs: tuple[FieldSpec, ...] = ()
    outputs: tuple[FieldSpec, ...] = ()
    permissions_required: tuple[str, ...] = ()
    availability: Availability = Availability.NOT_CONFIGURED
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    evidence: tuple[Evidence, ...] = ()
    schema_version: int = SCHEMA_VERSION

    GRANTS_EXECUTION = False          # qabiliyyət təsviri icra icazəsi VERMİR

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("capability: schema_version uyğun deyil", "bad_version")
        check_capability_id("capability_id", self.capability_id)
        object.__setattr__(self, "name", clean_text("capability.name", self.name, max_len=120, min_len=1))
        object.__setattr__(self, "description", clean_text("capability.description", self.description, max_len=2000, multiline=True))
        object.__setattr__(self, "origin", check_enum("capability.origin", Origin, self.origin))
        object.__setattr__(self, "risk", check_enum("capability.risk", RiskLevel, self.risk))
        object.__setattr__(self, "availability", check_enum("capability.availability", Availability, self.availability))
        object.__setattr__(self, "verification_status", check_enum("capability.verification_status", VerificationStatus, self.verification_status))
        object.__setattr__(self, "registered_at", ensure_utc("capability.registered_at", self.registered_at))
        object.__setattr__(self, "updated_at", ensure_utc("capability.updated_at", self.updated_at))
        if self.updated_at < self.registered_at:
            raise ContractError("capability.updated_at registered_at-dən əvvəl ola bilməz", "bad_time")
        object.__setattr__(self, "inputs", _fields("inputs", self.inputs))
        object.__setattr__(self, "outputs", _fields("outputs", self.outputs))
        perms = as_tuple("permissions_required", self.permissions_required, max_items=8)
        if not all(isinstance(p, str) for p in perms) or set(perms) - KNOWN_PERMISSIONS or len(set(perms)) != len(perms):
            raise ContractError("permissions_required: naməlum və ya təkrar icazə", "bad_permission")
        object.__setattr__(self, "permissions_required", perms)
        ev = as_tuple("evidence", self.evidence, max_items=50)
        if not all(isinstance(e, Evidence) for e in ev):
            raise ContractError("evidence: Evidence elementləri tələb olunur", "bad_type")
        object.__setattr__(self, "evidence", ev)
        if set(perms) & SENSITIVE_PERMISSIONS and not risk_at_least(self.risk, RiskLevel.HIGH):
            raise ContractError("həssas icazə tələb edən qabiliyyətin riski ən azı high olmalıdır", "risk_too_low")
        if self.verification_status is VerificationStatus.VERIFIED and not ev:
            raise ContractError("verified qabiliyyət üçün ən azı bir sübut tələb olunur", "missing_evidence")
        if self.availability is Availability.AVAILABLE and self.verification_status is not VerificationStatus.VERIFIED:
            raise ContractError("available qabiliyyət sübutla verified olmalıdır (simulyasiya/yoxlanmamış real kimi göstərilmir)", "unverified_available")

    @classmethod
    def new(cls, capability_id: str, name: str, description: str, *, origin: Origin, risk: RiskLevel, inputs: tuple[FieldSpec, ...] = (),
            outputs: tuple[FieldSpec, ...] = (), permissions_required: tuple[str, ...] = (), availability: Availability = Availability.NOT_CONFIGURED,
            now: datetime | None = None) -> "Capability":
        t = ensure_utc("now", now) if now is not None else utcnow()
        return cls(capability_id, name, description, origin, risk, t, t, tuple(inputs), tuple(outputs), tuple(permissions_required), availability)

    def with_availability(self, new_availability: Availability, *, evidence: tuple[Evidence, ...] = (), at: datetime | None = None) -> "Capability":
        """Əlçatanlıq keçidi. `AVAILABLE`-a keçid yeni sübut (VERIFIED) tələb edir; başqa halda sübut/verifikasiya saxlanır."""
        new_availability = check_enum("availability", Availability, new_availability)
        if new_availability not in AVAILABILITY_TRANSITIONS[self.availability]:
            raise InvalidTransition(f"capability: {self.availability.value} -> {new_availability.value} keçidi icazəli deyil")
        when = ensure_utc("at", at) if at is not None else utcnow()
        merged = self.evidence + tuple(evidence)
        status = self.verification_status
        if new_availability is Availability.AVAILABLE:
            if not evidence:
                raise ContractError("available üçün yeni sübut tələb olunur", "missing_evidence")
            status = VerificationStatus.VERIFIED
        elif new_availability in (Availability.NOT_CONFIGURED, Availability.DISABLED):
            status = VerificationStatus.UNVERIFIED if new_availability is Availability.NOT_CONFIGURED else status
        return replace(self, availability=new_availability, verification_status=status, evidence=merged, updated_at=max(when, self.updated_at))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "capability_id": self.capability_id, "name": self.name, "description": self.description, "origin": self.origin.value,
                "risk": self.risk.value, "inputs": [f.to_dict() for f in self.inputs], "outputs": [f.to_dict() for f in self.outputs],
                "permissions_required": list(self.permissions_required), "availability": self.availability.value,
                "verification_status": self.verification_status.value, "evidence": [e.to_dict() for e in self.evidence],
                "registered_at": fmt_dt(self.registered_at), "updated_at": fmt_dt(self.updated_at)}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.to_json())

    @classmethod
    def from_dict(cls, data: Any) -> "Capability":
        check_keys("capability", data, ("schema_version", "capability_id", "name", "description", "origin", "risk", "registered_at", "updated_at"),
                   ("inputs", "outputs", "permissions_required", "availability", "verification_status", "evidence"))
        d = check_version("capability", data)
        return cls(d["capability_id"], d["name"], d["description"], check_enum("origin", Origin, d["origin"]), check_enum("risk", RiskLevel, d["risk"]),
                   parse_dt("registered_at", d["registered_at"]), parse_dt("updated_at", d["updated_at"]),
                   tuple(FieldSpec.from_dict(f) for f in as_tuple("inputs", d.get("inputs", []), max_items=32)),
                   tuple(FieldSpec.from_dict(f) for f in as_tuple("outputs", d.get("outputs", []), max_items=32)),
                   tuple(as_tuple("permissions_required", d.get("permissions_required", []), max_items=8)),
                   check_enum("availability", Availability, d.get("availability", "not_configured")),
                   check_enum("verification_status", VerificationStatus, d.get("verification_status", "unverified")),
                   tuple(Evidence.from_dict(e) for e in as_tuple("evidence", d.get("evidence", []), max_items=50)))

    @classmethod
    def from_json(cls, text: str) -> "Capability":
        return cls.from_dict(parse_json_object("capability", text))
