"""NAYMERA CORE portları (Mərhələ 18.2-B): kernel döngüsünün asılı olduğu dar interfeyslər və dəyişməz məlumat tipləri. Bu modul HEÇ NƏ İCRA ETMİR.

Avtonomluq: yalnız L0 (təklif) və L1 (hər addım üçün sahib təsdiqi). L2/L3 tipdə MÖVCUD DEYİL (konstruktor `Autonomy("L2")` ValueError verir).
Etibar: planlaşdırıcı və icraçı çıxışları ETİBARSIZDIR (məlumat; təlimat deyil). İcra yalnız `StepExecutor` portu ilə; onun yeganə təqdim olunan nəticəsi `runtime/brokered_core.py`-dir
(Effect Broker -> Gate -> Ledger + `ApprovalClient`)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from kernel.goal import Goal
from kernel.plan import Plan, PlanStepSpec


class Autonomy(str, Enum):
    L0 = "L0"          # yalnız təklif: plan yaradır, yoxlayır, READY edir; HEÇ VAXT icra etmir
    L1 = "L1"          # hər addım üçün sahibin ayrıca (hash-ə bağlı, bir-dəfəlik) təsdiqi
    # L2/L3 BİLƏRƏKDƏN YOXDUR


class Actor(str, Enum):
    OWNER = "owner"
    ENGINE = "engine"


class StepState(str, Enum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"            # icra olunmayıb (stop/Gate/handler yoxdur/rədd)
    UNKNOWN = "unknown"            # nəticəsi məlum deyil (qəza/təkrar op_id): YALNIZ sahib həll edir; avtomatik təkrar YOXDUR


TERMINAL_STEP_STATES = frozenset({StepState.SUCCEEDED})
ALLOWED_STEP_PERMISSIONS = frozenset({"read_files", "write_files", "run_tests"})        # həssas icazələr (pul/hesab/nəşr) bu mərhələdə BAĞLIDIR


@dataclass(frozen=True)
class StepRecord:
    plan_id: str
    plan_version: int
    step_id: str
    state: StepState
    attempt: int
    op_id: str
    lease_until: float | None
    summary: str
    output_sha256: str
    verified: bool
    error: str
    updated_at: str


@dataclass(frozen=True)
class StepRequest:
    """Bir addımın icra sorğusu. `payload` təsdiq hash-inə daxildir: plan/addım dəyişərsə təsdiq etibarsız olur."""
    goal_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    step: PlanStepSpec
    step_hash: str
    attempt: int
    op_id: str

    @property
    def payload(self) -> dict[str, Any]:
        return {"goal_id": self.goal_id, "plan_id": self.plan_id, "plan_version": self.plan_version, "plan_hash": self.plan_hash, "step_id": self.step.step_id,
                "step_hash": self.step_hash, "capability_id": self.step.capability_id, "attempt": self.attempt}

    @property
    def description(self) -> str:
        return f"core:{self.plan_id}:v{self.plan_version}:{self.step.step_id}"[:200]


def step_hash(step: PlanStepSpec) -> str:
    return hashlib.sha256(json.dumps(step.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StepOutcome:
    status: str                    # succeeded | failed | blocked | unknown
    summary: str = ""              # ETİBARSIZ mətn: saxlanmazdan əvvəl sirr yoxlanır və qısaldılır
    error: str = ""
    output_sha256: str = ""


@dataclass(frozen=True)
class ApprovalState:
    status: str                    # pending | approved | denied | unavailable
    ref: str = ""


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    evidence: str = ""


@dataclass(frozen=True)
class TickResult:
    action: str
    goal_id: str = ""
    plan_id: str = ""
    step_id: str = ""
    detail: str = ""
    progress: bool = False         # True: vəziyyət dəyişdi (plan/addım/keçid); False: gözləmə/boş/dayandırılıb


class Planner(Protocol):
    def propose(self, goal: Goal) -> Plan | None: ...                     # ETİBARSIZ təklif; kernel Plan müqaviləsi ilə doğrulanır


class PlanValidatorPort(Protocol):
    def validate(self, goal: Goal, plan: Plan) -> ValidationResult: ...


class StepExecutor(Protocol):
    def supports(self, step: PlanStepSpec) -> tuple[bool, str]: ...      # təsdiq SORUŞULMAZDAN əvvəl: icra oluna bilməyən addım üçün sahibdən təsdiq istənmir
    def execute(self, request: StepRequest, approval_ref: str) -> StepOutcome: ...


class ApprovalPort(Protocol):
    def ensure(self, request: StepRequest) -> ApprovalState: ...         # sorğu yaradır/oxuyur; təsdiq ETMƏK imkanı YOXDUR


class Verifier(Protocol):
    def verify(self, request: StepRequest, outcome: StepOutcome) -> VerifyResult: ...


class FileStop:
    """STOP bayrağı: fayl mövcuddursa dayanmalıdır (Broker/Gate ilə eyni mexanizm)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def __call__(self) -> bool:
        return self._path.exists()
