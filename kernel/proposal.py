"""ImprovementProposal və OwnerDecision müqavilələri (Mərhələ 18.0).

Təklifin MƏZMUNU (`content_hash`) sahib qərarından AYRIDIR: sahib qərarı (`OwnerDecision`) yalnız konkret `content_hash`-ə bağlanır; məzmun dəyişərsə hash dəyişir və əvvəlki qərar
etibarsız olur (`revise()` təsdiqi sıfırlayır). Təsdiq statusu qərar obyekti olmadan APPROVED/REJECTED ola bilməz. HEÇ BİR təklif/qərar icra, merge, deploy və ya xərc İCAZƏSİ VERMİR
(`GRANTS_EXECUTION=False`): qərar yalnız "təklifin məzmununa baxıldı" deməkdir. Qərarın həqiqiliyi (cihaz imzası/step-up) bu müqavilənin deyil, sahib qapısının məsuliyyətidir;
`approval_ref` yalnız həmin sübuta formal istinaddır."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any

from kernel.common import (SCHEMA_VERSION, ContractError, Evidence, InvalidTransition, Origin, RiskLevel, as_tuple, canonical_json, check_enum, check_hex64, check_id,
                           check_keys, check_version, clean_text, ensure_utc, fmt_dt, parse_dt, parse_json_object, risk_at_least, sha256_hex, utcnow)

PROTECTED_PREFIXES = ("safety/", "runtime/", ".github/", "agents/factory_auth.py", "agents/agent_factory.py", "agents/company.py", "api/auth.py", "api/owner_gate.py",
                      "api/bootstrap.py", "tests/conftest.py", "tools/run_privileged_isolation_tests.sh", "kernel/")
_COMPONENT = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./\-]{0,119}$")
_ACTOR = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_APPROVAL_REF = re.compile(r"^[A-Za-z0-9_.:\-]{8,96}$")


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


PROPOSAL_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.DRAFT: frozenset({ProposalStatus.SUBMITTED, ProposalStatus.WITHDRAWN}),
    ProposalStatus.SUBMITTED: frozenset({ProposalStatus.WITHDRAWN, ProposalStatus.SUPERSEDED}),
    ProposalStatus.WITHDRAWN: frozenset(),
    ProposalStatus.SUPERSEDED: frozenset(),
}


def new_proposal_id() -> str:
    return "prop_" + uuid.uuid4().hex[:16]


def check_component(value: Any) -> str:
    if not isinstance(value, str) or not _COMPONENT.match(value) or any(seg in ("", ".", "..") for seg in value.rstrip("/").split("/")):
        raise ContractError("affected_components: nisbi, '..'-siz yol/identifikator tələb olunur", "bad_component")
    return value


def touches_protected(components: tuple[str, ...]) -> bool:
    return any(c == p or c.startswith(p) for c in components for p in PROTECTED_PREFIXES)


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    summary: str
    mitigations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", check_enum("risk.level", RiskLevel, self.level))
        object.__setattr__(self, "summary", clean_text("risk.summary", self.summary, max_len=1000, min_len=1, multiline=True))
        object.__setattr__(self, "mitigations", tuple(clean_text("risk.mitigation", m, max_len=500, min_len=1, multiline=True)
                                                      for m in as_tuple("risk.mitigations", self.mitigations, max_items=10)))

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level.value, "summary": self.summary, "mitigations": list(self.mitigations)}

    @classmethod
    def from_dict(cls, data: Any) -> "RiskAssessment":
        check_keys("risk", data, ("level", "summary"), ("mitigations",))
        return cls(check_enum("risk.level", RiskLevel, data["level"]), data["summary"], tuple(as_tuple("mitigations", data.get("mitigations", []), max_items=10)))


@dataclass(frozen=True)
class OwnerDecision:
    """Sahib qərarı: təklifin konkret məzmun hash-inə bağlı QEYD. İcazə vermir; həqiqilik sahib qapısında yoxlanır (approval_ref istinaddır)."""
    proposal_id: str
    content_hash: str
    decision: ApprovalDecision
    decided_by: str
    decided_at: datetime
    approval_ref: str = ""
    note: str = ""
    schema_version: int = SCHEMA_VERSION

    GRANTS_EXECUTION = False
    SCOPE = "proposal_content_only"       # qərar yalnız bu məzmuna aiddir: merge/deploy/xərc/icra təsdiqi DEYİL

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("owner_decision: schema_version uyğun deyil", "bad_version")
        check_id("proposal_id", self.proposal_id, "prop")
        check_hex64("content_hash", self.content_hash)
        object.__setattr__(self, "decision", check_enum("decision", ApprovalDecision, self.decision))
        if not isinstance(self.decided_by, str) or not _ACTOR.match(self.decided_by):
            raise ContractError("decided_by: sahib identifikatoru tələb olunur", "bad_actor")
        object.__setattr__(self, "decided_at", ensure_utc("decided_at", self.decided_at))
        if self.decision is ApprovalDecision.APPROVE and not _APPROVAL_REF.match(self.approval_ref or ""):
            raise ContractError("approve qərarı üçün approval_ref (step-up/təsdiq istinadı) tələb olunur", "missing_approval_ref")
        if self.approval_ref and not _APPROVAL_REF.match(self.approval_ref):
            raise ContractError("approval_ref formatı yanlışdır", "bad_approval_ref")
        object.__setattr__(self, "note", clean_text("decision.note", self.note, max_len=500, multiline=True))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "proposal_id": self.proposal_id, "content_hash": self.content_hash, "decision": self.decision.value,
                "decided_by": self.decided_by, "decided_at": fmt_dt(self.decided_at), "approval_ref": self.approval_ref, "note": self.note}

    @classmethod
    def from_dict(cls, data: Any) -> "OwnerDecision":
        check_keys("owner_decision", data, ("schema_version", "proposal_id", "content_hash", "decision", "decided_by", "decided_at"), ("approval_ref", "note"))
        d = check_version("owner_decision", data)
        return cls(d["proposal_id"], d["content_hash"], check_enum("decision", ApprovalDecision, d["decision"]), d["decided_by"], parse_dt("decided_at", d["decided_at"]),
                   d.get("approval_ref", ""), d.get("note", ""))


@dataclass(frozen=True)
class ImprovementProposal:
    proposal_id: str
    title: str
    gap: str
    improvement: str
    risk: RiskAssessment
    origin: Origin
    created_at: datetime
    updated_at: datetime
    evidence: tuple[Evidence, ...] = ()
    affected_components: tuple[str, ...] = ()
    test_criteria: tuple[str, ...] = ()
    status: ProposalStatus = ProposalStatus.DRAFT
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    decision: OwnerDecision | None = None
    schema_version: int = SCHEMA_VERSION

    GRANTS_EXECUTION = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("proposal: schema_version uyğun deyil", "bad_version")
        check_id("proposal_id", self.proposal_id, "prop")
        object.__setattr__(self, "title", clean_text("proposal.title", self.title, max_len=200, min_len=1))
        object.__setattr__(self, "gap", clean_text("proposal.gap", self.gap, max_len=3000, min_len=1, multiline=True))
        object.__setattr__(self, "improvement", clean_text("proposal.improvement", self.improvement, max_len=4000, min_len=1, multiline=True))
        if not isinstance(self.risk, RiskAssessment):
            raise ContractError("proposal.risk: RiskAssessment tələb olunur", "bad_type")
        object.__setattr__(self, "origin", check_enum("proposal.origin", Origin, self.origin))
        object.__setattr__(self, "status", check_enum("proposal.status", ProposalStatus, self.status))
        object.__setattr__(self, "approval_status", check_enum("proposal.approval_status", ApprovalStatus, self.approval_status))
        object.__setattr__(self, "created_at", ensure_utc("proposal.created_at", self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc("proposal.updated_at", self.updated_at))
        if self.updated_at < self.created_at:
            raise ContractError("proposal.updated_at created_at-dən əvvəl ola bilməz", "bad_time")
        ev = as_tuple("proposal.evidence", self.evidence, max_items=30)
        if not all(isinstance(e, Evidence) for e in ev):
            raise ContractError("proposal.evidence: Evidence elementləri tələb olunur", "bad_type")
        object.__setattr__(self, "evidence", ev)
        comps = tuple(check_component(c) for c in as_tuple("affected_components", self.affected_components, max_items=30))
        if len(set(comps)) != len(comps):
            raise ContractError("affected_components: təkrar komponent", "duplicate")
        object.__setattr__(self, "affected_components", comps)
        object.__setattr__(self, "test_criteria", tuple(clean_text("proposal.test_criterion", t, max_len=500, min_len=1, multiline=True)
                                                       for t in as_tuple("test_criteria", self.test_criteria, max_items=20)))
        if self.status is not ProposalStatus.DRAFT and not (ev and comps and self.test_criteria):
            raise ContractError("draft olmayan təklif sübut, təsirlənən komponent və test meyarı tələb edir", "incomplete_proposal")
        if touches_protected(comps) and not risk_at_least(self.risk.level, RiskLevel.HIGH):
            raise ContractError("qorunan komponentə (safety/runtime/auth/CI/kernel) toxunan təklifin riski ən azı high olmalıdır", "risk_too_low")
        # sahib təsdiqi AYRI və hash-ə bağlıdır
        if self.approval_status is ApprovalStatus.PENDING:
            if self.decision is not None:
                raise ContractError("pending təklifdə qərar ola bilməz", "bad_decision")
        else:
            d = self.decision
            expected = ApprovalDecision.APPROVE if self.approval_status is ApprovalStatus.APPROVED else ApprovalDecision.REJECT
            if (not isinstance(d, OwnerDecision) or d.proposal_id != self.proposal_id or d.content_hash != self.content_hash or d.decision is not expected
                    or self.status is ProposalStatus.DRAFT):
                raise ContractError("approved/rejected status yalnız draft olmayan təklif üçün və həmin məzmun hash-inə bağlı sahib qərarı ilə mümkündür", "approval_without_decision")

    @classmethod
    def new(cls, title: str, gap: str, improvement: str, risk: RiskAssessment, *, origin: Origin, evidence: tuple[Evidence, ...] = (),
            affected_components: tuple[str, ...] = (), test_criteria: tuple[str, ...] = (), proposal_id: str | None = None, now: datetime | None = None) -> "ImprovementProposal":
        t = ensure_utc("now", now) if now is not None else utcnow()
        return cls(proposal_id or new_proposal_id(), title, gap, improvement, risk, origin, t, t, tuple(evidence), tuple(affected_components), tuple(test_criteria))

    # --- məzmun hash-i (status, təsdiq, vaxt DAXİL DEYİL)
    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json({"title": self.title, "gap": self.gap, "improvement": self.improvement, "risk": self.risk.to_dict(),
                                          "evidence": [e.to_dict() for e in self.evidence], "affected_components": list(self.affected_components),
                                          "test_criteria": list(self.test_criteria)}))

    @property
    def touches_protected_components(self) -> bool:
        return touches_protected(self.affected_components)

    # --- keçidlər (hamısı yeni nüsxə qaytarır)
    def transition(self, new_status: ProposalStatus, *, at: datetime | None = None) -> "ImprovementProposal":
        new_status = check_enum("status", ProposalStatus, new_status)
        if new_status not in PROPOSAL_TRANSITIONS[self.status]:
            raise InvalidTransition(f"proposal: {self.status.value} -> {new_status.value} keçidi icazəli deyil")
        when = ensure_utc("at", at) if at is not None else utcnow()
        return replace(self, status=new_status, updated_at=max(when, self.updated_at))

    def submit(self, *, at: datetime | None = None) -> "ImprovementProposal":
        return self.transition(ProposalStatus.SUBMITTED, at=at)

    def with_decision(self, decision: OwnerDecision, *, at: datetime | None = None) -> "ImprovementProposal":
        """Sahib qərarını qeyd edir. Yalnız SUBMITTED + PENDING təklif, eyni proposal_id və HAZIRKI `content_hash` ilə. Qərar təkrar dəyişdirilə bilməz."""
        if not isinstance(decision, OwnerDecision):
            raise ContractError("OwnerDecision tələb olunur", "bad_type")
        if self.status is not ProposalStatus.SUBMITTED or self.approval_status is not ApprovalStatus.PENDING:
            raise InvalidTransition("proposal: qərar yalnız submitted və hələ qərarsız təklifə qeyd olunur")
        if decision.proposal_id != self.proposal_id:
            raise ContractError("qərar başqa təklifə aiddir", "decision_mismatch")
        if decision.content_hash != self.content_hash:
            raise ContractError("qərar təklifin cari məzmununa bağlı deyil (məzmun dəyişib)", "stale_decision")
        status = ApprovalStatus.APPROVED if decision.decision is ApprovalDecision.APPROVE else ApprovalStatus.REJECTED
        when = ensure_utc("at", at) if at is not None else utcnow()
        return replace(self, approval_status=status, decision=decision, updated_at=max(when, self.updated_at))

    def revise(self, *, at: datetime | None = None, **changes: Any) -> "ImprovementProposal":
        """Məzmunu dəyişir: yeni hash, status DRAFT, təsdiq SIFIRLANIR (əvvəlki qərar yeni məzmuna keçmir)."""
        allowed = {"title", "gap", "improvement", "risk", "evidence", "affected_components", "test_criteria"}
        if set(changes) - allowed:
            raise ContractError("revise: yalnız məzmun sahələri dəyişdirilə bilər", "bad_field")
        if self.status in (ProposalStatus.WITHDRAWN, ProposalStatus.SUPERSEDED):
            raise InvalidTransition("proposal: bağlanmış təklif yenilənə bilməz")
        when = ensure_utc("at", at) if at is not None else utcnow()
        return replace(self, status=ProposalStatus.DRAFT, approval_status=ApprovalStatus.PENDING, decision=None, updated_at=max(when, self.updated_at), **changes)

    # --- serializasiya
    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "proposal_id": self.proposal_id, "title": self.title, "gap": self.gap, "improvement": self.improvement,
                "risk": self.risk.to_dict(), "origin": self.origin.value, "evidence": [e.to_dict() for e in self.evidence],
                "affected_components": list(self.affected_components), "test_criteria": list(self.test_criteria), "status": self.status.value,
                "approval_status": self.approval_status.value, "decision": self.decision.to_dict() if self.decision else None, "content_hash": self.content_hash,
                "created_at": fmt_dt(self.created_at), "updated_at": fmt_dt(self.updated_at)}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Any) -> "ImprovementProposal":
        check_keys("proposal", data, ("schema_version", "proposal_id", "title", "gap", "improvement", "risk", "origin", "created_at", "updated_at"),
                   ("evidence", "affected_components", "test_criteria", "status", "approval_status", "decision", "content_hash"))
        d = check_version("proposal", data)
        dec = d.get("decision")
        obj = cls(d["proposal_id"], d["title"], d["gap"], d["improvement"], RiskAssessment.from_dict(d["risk"]), check_enum("origin", Origin, d["origin"]),
                  parse_dt("created_at", d["created_at"]), parse_dt("updated_at", d["updated_at"]),
                  tuple(Evidence.from_dict(e) for e in as_tuple("evidence", d.get("evidence", []), max_items=30)),
                  tuple(as_tuple("affected_components", d.get("affected_components", []), max_items=30)), tuple(as_tuple("test_criteria", d.get("test_criteria", []), max_items=20)),
                  check_enum("status", ProposalStatus, d.get("status", "draft")), check_enum("approval_status", ApprovalStatus, d.get("approval_status", "pending")),
                  OwnerDecision.from_dict(dec) if dec is not None else None)
        if "content_hash" in d and d["content_hash"] != obj.content_hash:           # saxlanmış hash ilə həqiqi məzmun uyğun deyilsə (dəyişdirilmiş sənəd) rədd
            raise ContractError("content_hash məzmuna uyğun deyil (sənəd dəyişdirilib)", "hash_mismatch")
        return obj

    @classmethod
    def from_json(cls, text: str) -> "ImprovementProposal":
        return cls.from_dict(parse_json_object("proposal", text))
