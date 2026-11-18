"""Mərhələ 18.2-B: Kernel Loop-un TƏK icra adapteri. Hər yan təsir Effect Broker → Gate → Ledger zəncirindən və mövcud `ApprovalClient`-dən keçir.

- `StepHandlerRegistry`: addım icraçıları YALNIZ etibarlı server kodu tərəfindən qeydə alınır (capability_id → handler; icazə yalnız read_files/write_files/run_tests; `seal()` sonrası dəyişmir).
  Bu mərhələdə HEÇ BİR istehsal handler-i qeydə alınmır: reyestr boşdur, deməli real heç nə icra olunmur (testlər fixture handler-ləri ilə).
- `BrokeredApprovals`: hər addım üçün `ApprovalClient.request/check` (təsdiq ETMƏK imkanı yoxdur). Təsdiq effektin kanonik hash-inə (agent, icazə, xərc=0, op_id, təsvir, plan/addım hash-ləri, cəhd) bağlıdır;
  plan/addım dəyişərsə hash dəyişir və təsdiq etibarsız olur. Defoltda yalnız `kind="human"` təsdiqi qəbul olunur (mock yalnız testdə açıq `accept_mock=True` ilə).
- `BrokeredStepExecutor`: təsdiq YALNIZ Broker-in stop/təkrar/Gate/ledger mərhələləri keçildikdən SONRA (icraçının daxilində, Broker kontekstində) `consume` olunur (bir-dəfəlik) və sonra handler çağırılır.
  Xərc 0-dır (ödənişli addım bu mərhələdə qadağandır). Handler istisnası: `HandlerFailed` ⇒ failed (yan təsir yoxdur); digər istisna ⇒ UNKNOWN (sahib həlli).
DÜRÜST HƏDD: Broker konteksti proses-daxili ContextVar-dır, OS sərhədi DEYİL; Broker xətasının növü `EffectResult.reason` prefiksindən oxunur (Broker dəyişdirilmir)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agents.spec import AgentSpec
from core.ports import ALLOWED_STEP_PERMISSIONS, ApprovalPort, ApprovalState, StepExecutor, StepOutcome, StepRequest
from kernel.common import check_capability_id
from kernel.plan import PlanStepSpec
from safety.approvals import ApprovalClient, effect_digest
from safety.broker import Effect, EffectBroker, ExecutionError


class HandlerFailed(ExecutionError):
    """Handler yan təsir yaratmadan uğursuz oldu (məs. giriş yoxlaması). Xərc məlumdur (0): Broker op_id-ni «naməlum» işarələmir. Digər istisnalar naməlum nəticə sayılır."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message, cost=0.0)


class ApprovalUnusable(ExecutionError):
    """Təsdiq istifadə oluna bilmədi (bitib/istifadə olunub/hash uyğun deyil): handler ÇAĞIRILMAYIB. Xərc məlumdur (0). op_id kitabda «istifadə olunmuş» qalır: yeni cəhd yeni op_id tələb edir."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message, cost=0.0)


@dataclass(frozen=True)
class HandlerContext:
    request: StepRequest


Handler = Callable[[HandlerContext], str]


class StepHandlerRegistry:
    def __init__(self) -> None:
        self._h: dict[str, tuple[Handler, str]] = {}
        self._sealed = False

    def register(self, capability_id: str, handler: Handler, *, permission: str = "read_files") -> None:
        if self._sealed:
            raise RuntimeError("reyestr möhürlənib")
        check_capability_id("capability_id", capability_id)
        if permission not in ALLOWED_STEP_PERMISSIONS:
            raise ValueError("handler icazəsi yalnız read_files/write_files/run_tests ola bilər (həssas icazə bu mərhələdə BAĞLIDIR)")
        if not callable(handler) or capability_id in self._h:
            raise ValueError("handler çağırıla bilən və capability_id unikal olmalıdır")
        self._h[capability_id] = (handler, permission)

    def seal(self) -> None:
        self._sealed = True

    def capabilities(self) -> set[str]:
        """Qeydə alınmış capability_id-lər (planlaşdırıcıya yalnız bunlar təklif olunur)."""
        return set(self._h)

    def permission(self, capability_id: str | None) -> str | None:
        item = self._h.get(capability_id) if isinstance(capability_id, str) else None
        return item[1] if item else None

    def handler(self, capability_id: str | None) -> Handler | None:
        item = self._h.get(capability_id) if isinstance(capability_id, str) else None
        return item[0] if item else None


class BrokeredApprovals(ApprovalPort):
    def __init__(self, client: ApprovalClient, spec: AgentSpec, registry: StepHandlerRegistry, *, accept_mock: bool = False) -> None:
        self._client, self._spec, self._reg, self._accept_mock = client, spec, registry, accept_mock

    def ensure(self, request: StepRequest) -> ApprovalState:
        perm = self._reg.permission(request.step.capability_id)
        if perm is None:
            return ApprovalState("unavailable")
        try:
            digest = effect_digest(self._spec.name, perm, 0.0, request.op_id, request.description, request.payload)
            ticket = self._client.request(self._spec.name, perm, 0.0, request.op_id, request.description, request.payload)
        except Exception:                                          # noqa: BLE001  təsdiq xidməti əlçatmaz: fail-closed
            return ApprovalState("unavailable")
        if ticket.status == "pending":
            return ApprovalState("pending", ticket.id)
        if ticket.status == "denied":
            return ApprovalState("denied", ticket.id)
        if ticket.status == "approved":
            if not (ticket.human_approved or self._accept_mock) or not self._client.check(ticket.id, digest.hash).ok:
                return ApprovalState("unavailable", ticket.id)
            return ApprovalState("approved", ticket.id)
        return ApprovalState("unavailable", ticket.id)


class BrokeredStepExecutor(StepExecutor):
    def __init__(self, broker: EffectBroker, spec: AgentSpec, registry: StepHandlerRegistry, approvals: ApprovalClient) -> None:
        self._broker, self._spec, self._reg, self._client = broker, spec, registry, approvals

    def supports(self, step: PlanStepSpec) -> tuple[bool, str]:
        perm = self._reg.permission(step.capability_id)
        if perm is None:
            return False, "no_handler"
        if perm not in self._spec.permissions:
            return False, "agent_lacks_permission"
        if set(step.required_permissions) - {perm}:
            return False, "permission_mismatch"
        return True, ""

    def execute(self, request: StepRequest, approval_ref: str) -> StepOutcome:
        perm, handler = self._reg.permission(request.step.capability_id), self._reg.handler(request.step.capability_id)
        if perm is None or handler is None:
            return StepOutcome("blocked", error="no_handler")
        digest = effect_digest(self._spec.name, perm, 0.0, request.op_id, request.description, request.payload)
        effect = Effect(self._spec, perm, 0.0, request.op_id, request.description, payload=request.payload)

        def run() -> Any:
            used = self._client.consume(approval_ref, digest.hash)          # Broker-in stop/təkrar/Gate/ledger mərhələlərindən SONRA, bir-dəfəlik
            if not used.ok:
                raise ApprovalUnusable(used.reason or "approval_unusable")
            return handler(HandlerContext(request))

        res = self._broker.run(effect, run)
        if res.status == "ok":
            return StepOutcome("succeeded", summary=str(res.output) if res.output is not None else "")
        if res.status == "duplicate":
            return StepOutcome("unknown", error="duplicate_op_id")           # əvvəlki cəhd yarımçıq qala bilərdi: sahib həll edir
        if res.status == "blocked":
            return StepOutcome("blocked", error=f"broker_blocked:{res.reason}"[:150])
        if res.reason.startswith("ApprovalUnusable:"):
            return StepOutcome("blocked", error="approval_unusable")
        if res.reason.startswith("HandlerFailed:"):
            return StepOutcome("failed", error="handler_failed")
        return StepOutcome("unknown", error="executor_error")                # naməlum yan təsir: fail-closed
