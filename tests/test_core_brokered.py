"""Mərhələ 18.2-B: Kernel Loop + `runtime/brokered_core.py` — Effect Broker → Gate → Ledger + mövcud ApprovalService ilə inteqrasiya. Real heç nə icra olunmur (fixture handler-lər)."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from agents.spec import AgentSpec
from core import FileStop, KernelLoop, StepOutcome, StepRequest, StepState, build_store, step_hash
from kernel import Goal, GoalStatus, Origin, Plan, PlanStepSpec
from runtime.brokered_core import (ApprovalUnusable, BrokeredApprovals, BrokeredStepExecutor, HandlerContext, HandlerFailed, StepHandlerRegistry)
from safety.approvals import ApprovalService, Principal
from safety.broker import EffectBroker
from safety.dbconn import connect
from safety.execution_context import in_broker
from safety.ledger import CostLedger

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
HUMAN = Principal("owner", "human", "web")
MOCK = Principal("owner", "mock", "test")


def step(sid="s1", cap="note.record", **kw) -> PlanStepSpec:
    base = dict(step_id=sid, title="Addım", expected_outcome="Nəticə", verification_criteria=("yoxla",), capability_id=cap)
    base.update(kw)
    return PlanStepSpec(**base)


class Planner:
    def __init__(self, *steps):
        self.steps = steps or (step(),)

    def propose(self, goal):
        return Plan.new(goal.goal_id, tuple(self.steps), origin=Origin.LLM, now=T0)


class BW:
    def __init__(self, tmp_path, *, allow_mock=False, accept_mock=False, perms=("read_files",), steps=None, handler=None, register=True):
        self.tmp = tmp_path
        self.ledger = CostLedger(tmp_path / "ledger.db", global_limit=5.0)
        self.approvals = ApprovalService(tmp_path / "approvals.db", tmp_path / "approval_audit.jsonl", owners={"owner"}, ttl_seconds=600, allow_mock=allow_mock)
        self.broker = EffectBroker(tmp_path / "audit.jsonl", tmp_path / "STOP", ledger=self.ledger)
        self.spec = AgentSpec(name="naymera_core", purpose="p", tools=[], permissions=list(perms), inputs=["x"], outputs=["y"], cost_limit=0.0)
        self.calls: list[tuple[str, bool]] = []
        self.registry = StepHandlerRegistry()
        if register:
            self.registry.register("note.record", handler or self._handler)
            self.registry.register("other.cap", self._handler)
        self.registry.seal()
        self.executor = BrokeredStepExecutor(self.broker, self.spec, self.registry, self.approvals.client)
        self.port = BrokeredApprovals(self.approvals.client, self.spec, self.registry, accept_mock=accept_mock)
        self.conn = connect(":memory:")
        self.faces = build_store(self.conn)
        self.loop = KernelLoop(self.faces.engine, autonomy="L1", stop=FileStop(tmp_path / "STOP"), planner=Planner(*(steps or ())), approvals=self.port, executor=self.executor, lease_s=60)

    def _handler(self, ctx: HandlerContext) -> str:
        self.calls.append((ctx.request.step.step_id, in_broker()))
        return f"yazıldı:{ctx.request.step.step_id}"

    def goal(self) -> Goal:
        g = Goal.new("Məqsəd", origin=Origin.OWNER, now=T0)
        self.faces.owner.create_goal(g, f"goal.create:{g.goal_id}")
        self.faces.owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, f"goal.act:{g.goal_id}", 1)
        return g

    def tick_until(self, action, n=20):
        for _ in range(n):
            r = self.loop.tick()
            if r.action == action:
                return r
        raise AssertionError(f"{action} alınmadı; son: {r.action}")

    def approve_pending(self, principal=HUMAN):
        out = []
        for t in self.approvals.gateway.pending():
            out.append(self.approvals.gateway.approve(t.id, principal))
        return out

    def request_for(self, plan: Plan, step_id: str, attempt: int = 1) -> StepRequest:
        s = next(x for x in plan.steps if x.step_id == step_id)
        import hashlib
        op = "core." + hashlib.sha256(f"{plan.plan_id}:{plan.version}:{s.step_id}:{attempt}".encode()).hexdigest()[:40]
        return StepRequest(plan.goal_id, plan.plan_id, plan.version, plan.content_hash, s, step_hash(s), attempt, op)


@pytest.fixture()
def bw(tmp_path):
    return BW(tmp_path, steps=(step("a"), step("b", depends_on=("a",))))


# ------------------------------------------------------------------ əsas axın
def test_end_to_end_each_step_needs_its_own_human_approval_and_runs_inside_the_broker(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    assert bw.calls == []                                                                  # təsdiqdən əvvəl heç nə
    (t,) = bw.approvals.gateway.pending()
    plan = bw.faces.engine.current_plan(g.goal_id)
    assert t.payload["plan_hash"] == plan.content_hash and t.payload["step_hash"] == step_hash(plan.steps[0]) and t.payload["attempt"] == 1 and t.agent == "naymera_core"
    assert bw.approve_pending()[0].ok
    assert bw.loop.tick().action == "step_succeeded" and bw.calls == [("a", True)]         # handler Broker kontekstində çağırıldı
    assert bw.approvals.reader.get(t.id).status == "consumed"                              # bir-dəfəlik təsdiq istifadə olundu
    assert bw.loop.tick().action == "awaiting_approval" and bw.calls == [("a", True)]      # 2-ci addım ÜÇÜN AYRI təsdiq
    bw.approve_pending()
    assert bw.loop.tick().action == "step_succeeded" and [c[0] for c in bw.calls] == ["a", "b"]
    ops = [e.op_id for e in bw.ledger.entries()]
    assert len(ops) == 2 and all(o.startswith("core.") for o in ops) and bw.ledger.total() == 0.0      # hər icra ledger-də, xərc 0
    audit = [json.loads(x) for x in (bw.tmp / "audit.jsonl").read_text().splitlines()]
    assert sum(1 for r in audit if str(r.get("description", "")).startswith("core:") and r.get("allowed") is True) == 2          # Gate audit izi


def test_no_approval_means_no_handler_call_ever(bw):
    bw.goal()
    for _ in range(15):
        bw.loop.tick()
    assert bw.calls == [] and bw.ledger.entries() == []


def test_mock_approval_is_not_a_human_approval_unless_explicitly_accepted(tmp_path):
    strict = BW(tmp_path / "s", allow_mock=True, accept_mock=False)
    strict.goal()
    strict.tick_until("awaiting_approval")
    assert strict.approve_pending(MOCK)[0].ok                                              # xidmət mock-u qəbul edir...
    assert strict.loop.tick().action == "approval_unavailable" and strict.calls == []       # ...lakin nüvə onu real sahib təsdiqi saymır
    lenient = BW(tmp_path / "l", allow_mock=True, accept_mock=True)
    lenient.goal()
    lenient.tick_until("awaiting_approval")
    lenient.approve_pending(MOCK)
    assert lenient.loop.tick().action == "step_succeeded" and lenient.calls == [("s1", True)]


def test_an_approval_cannot_be_borrowed_by_another_step_or_a_modified_step_and_misuse_invalidates_it(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    plan = bw.faces.engine.current_plan(g.goal_id)
    (t,) = bw.approvals.gateway.pending()
    bw.approve_pending()
    req_a, req_b = bw.request_for(plan, "a"), bw.request_for(plan, "b")
    assert bw.executor.execute(req_b, t.id).status == "blocked" and bw.calls == []                 # a-nın təsdiqi b üçün işləmir
    assert bw.approvals.reader.get(t.id).status == "invalidated"                                  # yanlış istifadə cəhdi təsdiqi ləğv edir (Stage 17 davranışı)
    assert bw.executor.execute(req_a, t.id).status == "blocked" and bw.calls == []                 # artıq legitim istifadə də olmaz: sahib yenidən təsdiqləməlidir


def test_a_modified_step_cannot_use_the_approval_of_the_original(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    plan = bw.faces.engine.current_plan(g.goal_id)
    (t,) = bw.approvals.gateway.pending()
    bw.approve_pending()
    req_a = bw.request_for(plan, "a")
    tampered = dataclasses.replace(req_a, step=dataclasses.replace(req_a.step, title="Dəyişdirilmiş"), op_id="core." + "0" * 40)
    assert bw.executor.execute(tampered, t.id).status == "blocked" and bw.calls == [] and bw.approvals.reader.get(t.id).status == "invalidated"


def test_an_approval_is_single_use_replays_are_refused(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    plan = bw.faces.engine.current_plan(g.goal_id)
    (t,) = bw.approvals.gateway.pending()
    bw.approve_pending()
    req_a = bw.request_for(plan, "a")
    assert bw.executor.execute(req_a, t.id).status == "succeeded" and len(bw.calls) == 1
    assert bw.executor.execute(req_a, t.id).status == "unknown" and len(bw.calls) == 1             # eyni op_id kitabda var: təkrar icra YOXDUR
    assert bw.executor.execute(dataclasses.replace(req_a, op_id="core." + "1" * 40), t.id).status == "blocked" and len(bw.calls) == 1   # yeni op_id: təsdiq istifadə olunub


def test_executor_called_directly_without_a_valid_approval_does_nothing(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    plan = bw.faces.engine.current_plan(g.goal_id)
    for i, ref in enumerate(("bogus", "", "x" * 40), start=1):
        assert bw.executor.execute(dataclasses.replace(bw.request_for(plan, "a"), op_id="core." + str(i) * 40), ref).status == "blocked"
    assert bw.calls == [] and bw.ledger.unknown_entries() == []                                # bilinən «istifadə olunmaz təsdiq» ledger-də naməlum yaratmır


def test_broker_stop_is_enforced_independently_and_does_not_consume_the_approval(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    (t,) = bw.approvals.gateway.pending()
    bw.approve_pending()
    loop_no_stop = KernelLoop(bw.faces.engine, autonomy="L1", stop=lambda: False, planner=Planner(), approvals=bw.port, executor=bw.executor, lease_s=60)    # nüvənin stop-u «xətalı» (False)
    (bw.tmp / "STOP").write_text("stop")
    r = loop_no_stop.tick()
    assert r.action == "step_blocked" and bw.calls == [] and bw.approvals.reader.get(t.id).status == "approved"          # Broker blokladı, təsdiq YANMADI
    rec = bw.faces.engine.steps(bw.faces.engine.current_plan(g.goal_id).plan_id)[0]
    assert rec.state is StepState.BLOCKED and rec.error.startswith("broker_blocked")


def test_loop_stop_uses_the_same_flag_and_wins(bw):
    bw.goal()
    (bw.tmp / "STOP").write_text("stop")
    assert [bw.loop.tick().action for _ in range(3)] == ["halted_stop"] * 3 and bw.approvals.gateway.pending() == []
    (bw.tmp / "STOP").unlink()
    assert bw.loop.tick().action == "proposed_plan"


def test_agent_without_the_handler_permission_never_reaches_approval(tmp_path):
    w = BW(tmp_path, perms=("write_files",))                                              # handler read_files tələb edir
    w.goal()
    actions = [w.loop.tick().action for _ in range(6)]
    assert "plan_invalid" in actions and w.approvals.gateway.pending() == [] and w.calls == []


def test_sensitive_paid_and_unregistered_steps_are_rejected_before_any_approval(tmp_path):
    for bad in (step("a", required_permissions=("spend_money",), requires_owner_approval=True), step("a", cost_estimate_azn=1.0), step("a", cap="ghost.cap"), step("a", cap=None)):
        w = BW(tmp_path / f"w{abs(hash(str(bad)))}", steps=(bad,))
        w.goal()
        actions = [w.loop.tick().action for _ in range(3)]
        assert "plan_invalid" in actions and w.approvals.gateway.pending() == [] and w.calls == []


def test_outcome_mapping_for_handler_failures_and_duplicate_ops(tmp_path):
    def failing(ctx):
        raise HandlerFailed("giriş yanlışdır")

    def crashing(ctx):
        raise RuntimeError("yarımçıq")
    for handler, want, unknown_entries in ((failing, "failed", 0), (crashing, "unknown", 1)):
        w = BW(tmp_path / want, handler=handler, steps=(step("a"),))
        g = w.goal()
        w.tick_until("awaiting_approval")
        w.approve_pending()
        assert w.loop.tick().action == f"step_{want}"
        assert w.faces.engine.steps(w.faces.engine.current_plan(g.goal_id).plan_id)[0].state.value == want
        assert len(w.ledger.unknown_entries()) == unknown_entries                          # bilinən xəta ledger-də naməlum yaratmır; naməlum nəticə yaradır
    w = BW(tmp_path / "dup", steps=(step("a"),))
    g = w.goal()
    w.tick_until("awaiting_approval")
    plan = w.faces.engine.current_plan(g.goal_id)
    w.approve_pending()
    w.ledger.reserve(w.request_for(plan, "a").op_id, "naymera_core", 0.0)                 # eyni op_id artıq kitabdadır
    assert w.loop.tick().action == "step_unknown" and w.calls == []                        # təkrar: handler çağırılmır, nəticə naməlum


def test_denied_approval_blocks_the_step_and_goal(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    (t,) = bw.approvals.gateway.pending()
    bw.approvals.gateway.deny(t.id, HUMAN)
    assert bw.loop.tick().action == "approval_denied" and bw.loop.tick().action == "goal_blocked" and bw.calls == []
    assert bw.faces.engine.get_goal(g.goal_id)[0].status is GoalStatus.BLOCKED


# ------------------------------------------------------------------ reyestr və portlar
def test_registry_rules():
    r = StepHandlerRegistry()
    for bad in ({"permission": "spend_money"}, {"permission": "publish"}, {"permission": "external_account"}, {"permission": "root"}):
        with pytest.raises(ValueError):
            r.register("a.b", lambda ctx: "x", **bad)
    for cap in ("Bad", "nodot", "", None):
        with pytest.raises(Exception):
            r.register(cap, lambda ctx: "x")                                              # type: ignore[arg-type]
    with pytest.raises(ValueError):
        r.register("a.b", "not callable")                                                 # type: ignore[arg-type]
    r.register("a.b", lambda ctx: "x", permission="write_files")
    with pytest.raises(ValueError):
        r.register("a.b", lambda ctx: "y")
    r.seal()
    with pytest.raises(RuntimeError):
        r.register("c.d", lambda ctx: "x")
    assert r.permission("a.b") == "write_files" and r.permission("zz.zz") is None and r.permission(None) is None and r.handler(5) is None   # type: ignore[arg-type]


def test_production_registry_is_empty_by_default_so_nothing_real_can_run():
    import runtime.brokered_core as mod
    assert StepHandlerRegistry()._h == {}
    assert not any(isinstance(v, StepHandlerRegistry) for v in vars(mod).values())          # modul səviyyəsində hazır reyestr/handler yoxdur


def test_supports_reports_reasons(tmp_path):
    w = BW(tmp_path)
    assert w.executor.supports(step("a")) == (True, "")
    assert w.executor.supports(step("a", cap="ghost.cap")) == (False, "no_handler")
    assert w.executor.supports(step("a", required_permissions=("write_files",))) == (False, "permission_mismatch")
    w2 = BW(tmp_path / "x", perms=("write_files",))
    assert w2.executor.supports(step("a")) == (False, "agent_lacks_permission")
    assert w2.port.ensure(StepRequest("goal_x", "plan_x", 1, "0" * 64, step("a", cap="ghost.cap"), "0" * 64, 1, "core.abc12345")).status == "unavailable"


def test_unusable_approval_is_a_known_failure_not_an_unknown_ledger_entry(bw):
    g = bw.goal()
    bw.tick_until("awaiting_approval")
    plan = bw.faces.engine.current_plan(g.goal_id)
    assert bw.executor.execute(bw.request_for(plan, "a"), "bogus").status == "blocked"
    assert bw.ledger.unknown_entries() == [] and bw.calls == []


class FakeTicket:
    status, id, human_approved = "approved", "tk-1", True


class FakeClient:
    def __init__(self, check_ok):
        self.check_ok = check_ok

    def request(self, *a, **k):
        return FakeTicket()

    def check(self, *a, **k):
        return type("O", (), {"ok": self.check_ok})()


def test_ensure_rechecks_the_ticket_even_when_request_reports_it_approved(tmp_path):
    w = BW(tmp_path)
    req = StepRequest("goal_x", "plan_x", 1, "0" * 64, step("a"), "0" * 64, 1, "core.abc12345")
    assert BrokeredApprovals(FakeClient(True), w.spec, w.registry).ensure(req).status == "approved"
    assert BrokeredApprovals(FakeClient(False), w.spec, w.registry).ensure(req).status == "unavailable"               # təsdiq bu arada etibarsızlaşıb


def test_approval_service_outage_is_fail_closed(tmp_path):
    w = BW(tmp_path, steps=(step("a"),))
    w.goal()
    w.approvals.close()
    r = w.loop.tick()
    assert r.action in ("proposed_plan",)                                                    # planlama işləyir
    actions = [w.loop.tick().action for _ in range(4)]
    assert "approval_unavailable" in actions and w.calls == []
