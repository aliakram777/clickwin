"""Mərhələ 18.5: təhlükəsiz handler kitabxanası — smoke. Kernel Loop (L1) + Broker + təsdiq + yaddaş + workspace."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.spec import AgentSpec
from core import FileStop, KernelLoop, build_store
from kernel import Goal, GoalStatus, Origin, Plan, PlanStepSpec
from memory import EpisodeRecorder, MemActor, MemoryKind, MemoryStore
from reasoning import Critic, CriteriaVerifier, GuardedPlanner, ModelPlanner
from runtime.brokered_core import BrokeredApprovals, BrokeredStepExecutor, StepHandlerRegistry
from runtime.core_handlers import register_safe_handlers
from safety.approvals import ApprovalService, Principal
from safety.broker import EffectBroker
from safety.dbconn import connect
from safety.ledger import CostLedger
from safety.workspace import Workspace

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
HUMAN = Principal("owner", "human", "web")


def st(sid, cap, deps=()):
    return PlanStepSpec(sid, f"Addım {sid}", "Nəticə", ("nonempty",), tuple(deps), cap)


class Planner:
    def __init__(self, *caps):
        self.caps = caps

    def propose(self, goal):
        steps = tuple(st(f"s{i}", c, deps=(f"s{i-1}",) if i else ()) for i, c in enumerate(self.caps))
        return Plan.new(goal.goal_id, steps, origin=Origin.SYSTEM, now=T0)


class W:
    def __init__(self, tmp, *caps, perms=("read_files", "write_files"), workspace=True, with_memory=True):
        self.tmp = tmp
        self.ledger = CostLedger(tmp / "l.db", global_limit=5.0)
        self.approvals = ApprovalService(tmp / "a.db", tmp / "aa.jsonl", owners={"owner"}, ttl_seconds=600)
        self.broker = EffectBroker(tmp / "audit.jsonl", tmp / "STOP", ledger=self.ledger)
        self.spec = AgentSpec(name="naymera_core", purpose="p", tools=[], permissions=list(perms), inputs=["x"], outputs=["y"], cost_limit=0.0)
        self.faces = build_store(connect(":memory:"))
        self.memory = MemoryStore(connect(":memory:")) if with_memory else None
        (tmp / "ws").mkdir()
        (tmp / "ws" / "notes.txt").write_text("salam")
        (tmp / "ws" / ".env").write_text("X=1")
        self.registry = StepHandlerRegistry()
        self.registered = register_safe_handlers(self.registry, goals=self.faces.engine, memory=self.memory, workspace=Workspace(tmp / "ws") if workspace else None)
        self.registry.seal()
        ex = BrokeredStepExecutor(self.broker, self.spec, self.registry, self.approvals.client)
        self.loop = KernelLoop(self.faces.engine, autonomy="L1", stop=FileStop(tmp / "STOP"), planner=Planner(*caps), approvals=BrokeredApprovals(self.approvals.client, self.spec, self.registry),
                               executor=ex, verifier=CriteriaVerifier(), observer=EpisodeRecorder(self.memory) if self.memory else None, lease_s=60)
        self.goal = Goal.new("Hesabat və yaddaş işi", origin=Origin.OWNER, now=T0)
        self.faces.owner.create_goal(self.goal, "goal.create:g1")
        self.faces.owner.transition_goal(self.goal.goal_id, GoalStatus.ACTIVE, "goal.act:g1", 1)

    def drive(self, n=40):
        actions = []
        for _ in range(n):
            for t in self.approvals.gateway.pending():
                self.approvals.gateway.approve(t.id, HUMAN)
            r = self.loop.tick()
            actions.append(r.action)
            if r.action in ("awaiting_owner_acceptance", "goal_blocked", "halted_stop", "idle"):
                break
        return actions


def test_registration_depends_on_available_resources(tmp_path):
    w = W(tmp_path)
    assert sorted(w.registered) == ["memory.consolidate", "memory.recall", "report.progress", "workspace.list"] and w.registry.capabilities() == set(w.registered)
    bare = StepHandlerRegistry()
    assert register_safe_handlers(bare, goals=w.faces.engine) == [] and bare.capabilities() == set()                    # resurs yoxdursa heç nə qeydə alınmır


def test_full_l1_run_with_safe_handlers_records_memory_and_completes_the_plan(tmp_path):
    w = W(tmp_path, "workspace.list", "memory.recall", "report.progress", "memory.consolidate")
    w.memory.write(MemActor.OWNER, MemoryKind.LONG_TERM, "Hesabat üçün əvvəlki qeyd")
    w.memory.write(MemActor.AGENT, MemoryKind.PROJECT, "hesabat barədə agent qeydi")
    actions = w.drive()
    assert actions[-1] == "awaiting_owner_acceptance" and "plan_completed_awaiting_acceptance" in actions
    plan = w.faces.engine.plans_for_goal(w.goal.goal_id)[0]
    recs = {r.step_id: r for r in w.faces.engine.steps(plan.plan_id)}
    assert all(r.state.value == "succeeded" and r.verified for r in recs.values())
    assert "notes.txt" in recs["s0"].summary and ".env" not in recs["s0"].summary
    assert "2 qeyd (high=1, medium=0, low=1)" in recs["s1"].summary and recs["s2"].summary.startswith("hesabat yazıldı: mem_") and "xülasə yazıldı: mem_" in recs["s3"].summary
    kinds = w.memory.stats()
    assert kinds["episodic"] >= 4 and kinds["project"] == 2 and kinds["long_term"] == 2                                  # epizodlar + hesabat + xülasə
    assert w.faces.engine.get_goal(w.goal.goal_id)[0].status is GoalStatus.ACTIVE                                       # məqsədi yalnız sahib tamamlayır
    assert w.ledger.total() == 0.0 and all(e.op_id.startswith("core.") for e in w.ledger.entries() if not e.op_id.startswith("llm:"))


def test_workspace_listing_hides_sensitive_names_and_agent_permissions_gate_handlers(tmp_path):
    w = W(tmp_path, "workspace.list")
    w.drive()
    plan = w.faces.engine.plans_for_goal(w.goal.goal_id)[0]
    summary = w.faces.engine.steps(plan.plan_id)[0].summary
    assert "1 giriş" in summary and "notes.txt" in summary and ".env" not in summary                                    # workspace həssas adları (.env) gizlədir
    (tmp_path / "ro").mkdir()
    ro = W(tmp_path / "ro", "memory.consolidate", perms=("read_files",))
    actions = ro.drive()
    assert "plan_invalid" in actions and ro.approvals.gateway.pending() == []                                            # write_files icazəsi yoxdur ⇒ plan rədd, təsdiq istənmir


def test_model_planner_only_sees_registered_capabilities(tmp_path):
    w = W(tmp_path)
    seen = {}
    mp = ModelPlanner(lambda s, p: (seen.update(p=p), '{"steps": []}')[1], w.registry.capabilities)
    mp.propose(w.goal)
    assert "memory.recall" in seen["p"] and "shell" not in seen["p"] and "net." not in seen["p"]
    gp = GuardedPlanner(mp, Critic(w.registry.capabilities))
    assert gp.propose(w.goal) is None
