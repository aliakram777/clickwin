"""KernelLoop (Mərhələ 18.2-B): OBSERVE → ORIENT → DECIDE → ACT → VERIFY → RECORD. YALNIZ L0/L1.

- L0: təklif. Plan yaradır, yoxlayır və READY edir; HEÇ VAXT icra etmir (konstruktor L0-a icraçı/təsdiq portu verilməsinə İCAZƏ VERMİR).
- L1: hər addım üçün sahibin AYRI təsdiqi (`ApprovalPort`; plan/addım hash-inə və cəhd nömrəsinə bağlı, bir-dəfəlik). İcra yalnız `StepExecutor` portu ilə (istehsalda Broker→Gate→Ledger).
- STOP həmişə üstündür: hər tick-in əvvəlində, planlaşdırıcıya müraciətdən əvvəl, təsdiq sorğusundan əvvəl və icradan DƏRHAL əvvəl yoxlanılır; təsdiqlənmiş addım da STOP altında icra olunmur.
- Heç nə avtomatik "tamamlandı" sayılmır: məqsədi yalnız sahib tamamlayır; plan yalnız hər addım uğurlu VƏ yoxlanmış (`Verifier`) olduqda COMPLETED olur.
- Planlaşdırıcı və icraçı çıxışları ETİBARSIZDIR: plan kernel müqaviləsi + `PlanValidator` ilə doğrulanır, icraçı mətni yalnız qısa, sirrsiz xülasə kimi saxlanır və heç vaxt təlimat kimi ayrışdırılmır.
- Qəza: RUNNING addım lease bitəndə UNKNOWN olur (avtomatik təkrar YOXDUR; sahib həll edir). `Exception` atan icraçı ⇒ UNKNOWN (yan təsir istisna olunmur); `BaseException` yayılır, addım RUNNING qalır.
Bu modul `safety`/`runtime`/`api`/`agents`, şəbəkə, mühit dəyişəni, model və agent kodu icrasını import/istifadə ETMİR."""
from __future__ import annotations

import hashlib
from typing import Callable

from core.lease import LeaseKeeper
from core.ports import (ALLOWED_STEP_PERMISSIONS, ApprovalPort, Autonomy, Planner, PlanValidatorPort, StepExecutor, StepOutcome, StepRequest, StepState, TickResult, Verifier, step_hash)
from core.store import EngineGoalStore
from core.validator import PlanValidator
from kernel.common import ContractError
from kernel.eventlog import EventLog
from kernel.goal import Goal, GoalStatus
from kernel.plan import Plan, PlanStatus

MAX_RUN_TICKS = 1000
_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_OUTCOMES = {"succeeded": StepState.SUCCEEDED, "failed": StepState.FAILED, "blocked": StepState.BLOCKED, "unknown": StepState.UNKNOWN}


class KernelLoop:
    def __init__(self, store: EngineGoalStore, *, autonomy: Autonomy | str, stop: Callable[[], bool], planner: Planner | None = None, validator: PlanValidatorPort | None = None,
                 approvals: ApprovalPort | None = None, executor: StepExecutor | None = None, verifier: Verifier | None = None, event_log: EventLog | None = None,
                 lease_s: float = 300.0, max_replans: int = 2, observer: Callable[[TickResult], None] | None = None, heartbeat: bool = True, heartbeat_interval_s: float | None = None) -> None:
        self._level = Autonomy(autonomy)                         # L2/L3 ⇒ ValueError: mövcud deyil
        if not callable(stop):
            raise ValueError("stop çağırıla bilən olmalıdır (STOP bayrağı məcburidir)")
        if self._level is Autonomy.L0 and (executor is not None or approvals is not None or verifier is not None):
            raise ValueError("L0 yalnız təklifdir: icraçı/təsdiq/yoxlayıcı portu verilə bilməz")
        if self._level is Autonomy.L1 and (executor is None or approvals is None):
            raise ValueError("L1 üçün icraçı və təsdiq portu məcburidir")
        if isinstance(lease_s, bool) or not isinstance(lease_s, (int, float)) or not 1 <= lease_s <= 86400:
            raise ValueError("lease_s 1..86400 olmalıdır")
        self._store, self._stop, self._planner, self._approvals, self._executor, self._verifier = store, stop, planner, approvals, executor, verifier
        self._validator = validator or PlanValidator(supports=executor.supports if executor is not None else None)
        self._log, self._lease, self._max_replans = event_log, float(lease_s), max(0, min(int(max_replans), 10))
        self._planner_failures: dict[str, int] = {}
        self._heartbeat, self._hb_interval = bool(heartbeat), heartbeat_interval_s
        self._observer = observer                                   # ixtiyari: tərəqqi hadisəsi müşahidəçisi (məs. epizodik yaddaş); xətası döngüyə təsir etmir
        self._halted = False

    @property
    def autonomy(self) -> Autonomy:
        return self._level

    # ------------------------------------------------------------------ tick
    def tick(self) -> TickResult:
        if self._stop():                                          # STOP üstündür: heç bir vəziyyət dəyişməsi, heç bir port çağırışı yoxdur
            if not self._halted:
                self._store.record("halted_stop")
            self._halted = True
            return TickResult("halted_stop")
        self._halted = False
        self._store.recover_expired()
        waiting: TickResult | None = None
        try:
            for goal, version in self._ordered_goals():
                res = self._process(goal, version)
                if res.progress:
                    self._store.record("tick", goal.goal_id, res.plan_id, res.step_id, action=res.action, detail=res.detail)
                    if self._observer is not None:
                        try:
                            self._observer(res)
                        except Exception:                           # noqa: BLE001  müşahidəçi uğursuzluğu icranı pozmur
                            pass
                    return res
                waiting = waiting or res
            return waiting or TickResult("idle")
        finally:
            if self._log is not None:
                try:
                    self._store.flush_outbox(self._log)
                except Exception:                                 # noqa: BLE001  jurnal əlçatmaz: outbox qalır, növbəti tick yenidən cəhd edir
                    pass

    def run(self, max_ticks: int = 50) -> list[TickResult]:
        """Məhdud sayda tick (sonsuz dövr YOXDUR). Tərəqqi dayananda (gözləmə/boş/STOP) bitir."""
        out: list[TickResult] = []
        for _ in range(max(1, min(int(max_ticks), MAX_RUN_TICKS))):
            r = self.tick()
            out.append(r)
            if not r.progress:
                break
        return out

    def _ordered_goals(self) -> list[tuple[Goal, int]]:
        return sorted(self._store.list_goals(GoalStatus.ACTIVE, 500), key=lambda gv: (_PRIORITY_RANK.get(gv[0].priority.value, 9), gv[0].created_at, gv[0].goal_id))

    # ------------------------------------------------------------------ məqsəd
    def _process(self, goal: Goal, version: int) -> TickResult:
        gid = goal.goal_id
        plan = self._store.current_plan(gid)
        if plan is None:
            return self._propose(goal, version)
        steps = self._store.steps(plan.plan_id)
        if any(s.state is StepState.UNKNOWN for s in steps):
            return TickResult("needs_reconciliation", gid, plan.plan_id, detail="nəticəsi naməlum addım: sahib həlli tələb olunur")
        if plan.status is PlanStatus.DRAFT:
            return self._validate(goal, plan)
        if plan.status is PlanStatus.PAUSED:
            return TickResult("plan_paused", gid, plan.plan_id)
        if self._level is Autonomy.L0:
            return TickResult("awaiting_owner_review", gid, plan.plan_id, detail="L0: yalnız təklif; icra YOXDUR")
        return self._advance(goal, version, plan, steps)

    def _propose(self, goal: Goal, version: int) -> TickResult:
        gid = goal.goal_id
        history = self._store.plans_for_goal(gid)
        if any(p.status is PlanStatus.COMPLETED for p in history):
            return TickResult("awaiting_owner_acceptance", gid, detail="plan tamamlanıb; məqsədi yalnız sahib tamamlayır")
        if len([p for p in history if p.status in (PlanStatus.SUPERSEDED, PlanStatus.FAILED, PlanStatus.CANCELLED)]) > self._max_replans:
            return self._block_goal(goal, version, "replan_limit")
        if self._planner is None:
            return TickResult("needs_plan", gid)
        if self._stop():
            return TickResult("halted_stop", gid)
        try:
            plan = self._planner.propose(goal)
        except Exception:                                           # noqa: BLE001  planlaşdırıcı etibarsızdır
            return self._planner_failed(goal, version, "planner_error")
        if self._stop():                                            # planlaşdırıcı işləyərkən STOP gəlibsə nəticə SAXLANMIR
            return TickResult("halted_stop", gid)
        if plan is None:
            return TickResult("no_plan_proposed", gid)
        if not isinstance(plan, Plan) or plan.goal_id != gid or plan.status is not PlanStatus.DRAFT or plan.version != 1:
            return self._planner_failed(goal, version, "planner_returned_invalid_plan")
        self._store.add_plan(plan, f"plan.add:{plan.plan_id}")
        return TickResult("proposed_plan", gid, plan.plan_id, progress=True)

    def _planner_failed(self, goal: Goal, version: int, why: str) -> TickResult:
        n = self._planner_failures.get(goal.goal_id, 0) + 1
        self._planner_failures[goal.goal_id] = n
        self._store.record(why, goal.goal_id, count=n)
        if n > self._max_replans:
            return self._block_goal(goal, version, why)
        return TickResult(why, goal.goal_id, progress=True)

    def _validate(self, goal: Goal, plan: Plan) -> TickResult:
        if self._stop():
            return TickResult("halted_stop", goal.goal_id, plan.plan_id)
        res = self._validator.validate(goal, plan)
        if res.ok:
            self._store.transition_plan(plan.plan_id, PlanStatus.READY, f"plan.ready:{plan.plan_id}:v{plan.version}", plan.version, validated_hash=plan.content_hash)
            return TickResult("plan_ready", goal.goal_id, plan.plan_id, progress=True)
        self._store.transition_plan(plan.plan_id, PlanStatus.SUPERSEDED, f"plan.invalid:{plan.plan_id}:v{plan.version}", plan.version, reason=",".join(res.reasons)[:280])
        return TickResult("plan_invalid", goal.goal_id, plan.plan_id, detail=",".join(res.reasons)[:200], progress=True)

    def _block_goal(self, goal: Goal, version: int, reason: str) -> TickResult:
        self._store.transition_goal(goal.goal_id, GoalStatus.BLOCKED, f"goal.block:{goal.goal_id}:v{version}", version, reason=reason[:280])
        return TickResult("goal_blocked", goal.goal_id, detail=reason[:200], progress=True)

    # ------------------------------------------------------------------ L1: addım
    def _advance(self, goal: Goal, version: int, plan: Plan, steps: list) -> TickResult:
        gid, pid = goal.goal_id, plan.plan_id
        by_id = {s.step_id: s for s in steps}
        bad = next((s for s in steps if s.state in (StepState.FAILED, StepState.BLOCKED)), None)
        if bad is not None:
            return self._block_goal(goal, version, f"step_{bad.state.value}:{bad.step_id}")
        if all(s.state is StepState.SUCCEEDED for s in steps):
            if plan.status is PlanStatus.EXECUTING and self._verifier is not None and all(s.verified for s in steps):
                self._store.transition_plan(pid, PlanStatus.COMPLETED, f"plan.complete:{pid}:v{plan.version}", plan.version)
                return TickResult("plan_completed_awaiting_acceptance", gid, pid, progress=True)
            return TickResult("awaiting_verification_or_owner", gid, pid, detail="yoxlanmamış/yoxlayıcısız: yalnız sahib tamamlayır")
        step = next((s for s in plan.steps if by_id[s.step_id].state in (StepState.PENDING, StepState.AWAITING_APPROVAL)
                     and all(by_id[d].state is StepState.SUCCEEDED for d in s.depends_on)), None)
        if step is None:
            return TickResult("waiting", gid, pid)
        rec = by_id[step.step_id]
        ok, why = self._executor.supports(step)
        if not ok or set(step.required_permissions) - ALLOWED_STEP_PERMISSIONS or step.cost_estimate_azn > 0:
            self._store.block_step(pid, plan.version, step.step_id, (why or "step_not_executable")[:120])           # sahibdən İCRA OLUNA BİLMƏYƏN addım üçün təsdiq İSTƏNMİR
            return TickResult("step_blocked", gid, pid, step.step_id, detail=(why or "not_executable")[:100], progress=True)
        attempt = rec.attempt + 1
        op_id = "core." + hashlib.sha256(f"{pid}:{plan.version}:{step.step_id}:{attempt}".encode()).hexdigest()[:40]
        req = StepRequest(gid, pid, plan.version, plan.content_hash, step, step_hash(step), attempt, op_id)
        if self._stop():
            return TickResult("halted_stop", gid, pid, step.step_id)
        state = self._approvals.ensure(req)
        if state.status == "denied":
            self._store.block_step(pid, plan.version, step.step_id, "approval_denied")
            return TickResult("approval_denied", gid, pid, step.step_id, progress=True)
        if state.status != "approved":
            self._store.mark_awaiting(pid, plan.version, step.step_id)
            return TickResult("awaiting_approval" if state.status == "pending" else "approval_unavailable", gid, pid, step.step_id, progress=rec.state is StepState.PENDING and state.status == "pending")
        if self._stop():                                            # təsdiqlənmiş addım da STOP altında icra olunmur
            return TickResult("halted_stop", gid, pid, step.step_id)
        if plan.status is PlanStatus.READY:
            self._store.transition_plan(pid, PlanStatus.EXECUTING, f"plan.exec:{pid}:v{plan.version}", plan.version)
        if not self._store.begin_step(pid, plan.version, step.step_id, op_id, attempt, self._lease):
            return TickResult("step_race", gid, pid, step.step_id)
        return self._act(req, state.ref)

    def _act(self, req: StepRequest, approval_ref: str) -> TickResult:
        pid, ver, sid = req.plan_id, req.plan_version, req.step.step_id
        try:
            if self._heartbeat:
                with LeaseKeeper(self._store, pid, ver, sid, req.op_id, self._lease, self._hb_interval):               # uzun addım: lease yenilənir
                    outcome = self._executor.execute(req, approval_ref)
            else:
                outcome = self._executor.execute(req, approval_ref)
        except Exception:                                           # noqa: BLE001  yan təsir istisna olunmur ⇒ naməlum (fail-closed)
            self._store.finish_step(pid, ver, sid, req.op_id, StepState.UNKNOWN, "", "", False, "executor_exception")
            return TickResult("step_unknown", req.goal_id, pid, sid, detail="icraçı istisna atdı", progress=True)
        if not isinstance(outcome, StepOutcome) or outcome.status not in _OUTCOMES:
            self._store.finish_step(pid, ver, sid, req.op_id, StepState.UNKNOWN, "", "", False, "executor_bad_outcome")
            return TickResult("step_unknown", req.goal_id, pid, sid, detail="icraçı yanlış nəticə qaytardı", progress=True)
        state, verified, error = _OUTCOMES[outcome.status], False, outcome.error
        if state is StepState.SUCCEEDED and self._verifier is not None:
            try:
                vr = self._verifier.verify(req, outcome)
                verified = bool(vr.ok)
            except Exception:                                       # noqa: BLE001
                verified = False
            if not verified:
                state, error = StepState.FAILED, "verification_failed"
        self._store.finish_step(pid, ver, sid, req.op_id, state, outcome.summary, outcome.output_sha256, verified, error)
        return TickResult({StepState.SUCCEEDED: "step_succeeded", StepState.FAILED: "step_failed", StepState.BLOCKED: "step_blocked", StepState.UNKNOWN: "step_unknown"}[state],
                          req.goal_id, pid, sid, progress=True)


__all__ = ["KernelLoop", "ContractError"]
