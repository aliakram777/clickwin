"""Mərhələ 18.11: Android API — CORE marşrutları (smoke/regressiya). Simulyasiya olunan cihaz; real şəbəkə/cihaz/model YOXDUR. İstehsala qoşulmayıb."""
from __future__ import annotations

import ast
import hashlib
import pathlib
from datetime import datetime, timezone

import pytest

import api.core_api as core_api
from agents.spec import AgentSpec
from api.core_api import CoreApiApp
from core import FileStop, KernelLoop, StepState, build_store
from kernel import Goal, GoalStatus, Origin, Plan, PlanStepSpec
from runtime.brokered_core import BrokeredApprovals, BrokeredStepExecutor, StepHandlerRegistry
from safety.broker import EffectBroker
from safety.dbconn import connect
from tests.test_api_app import Dev, world  # noqa: F401  (fixture)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = "sk-" + "Jd5K" * 10


def h(text):
    return hashlib.sha256(text.encode()).hexdigest()


class CoreWorld:
    def __init__(self, world):
        base = world.build()
        self.world = world
        self.faces = build_store(connect(":memory:"))
        self.app = CoreApiApp(core_owner=self.faces.owner, auth=base.auth, chat=base.chat, events=base.events, stop_path=base.stop_path, gateway=base.gateway, reader=base.reader,
                              memory=base.memory, company=base.company, proactive=base.proactive, jobs=base.jobs, voice=base.voice, owner_id="owner", audit_path=base.audit_path,
                              clock=world.clock)
        self.dev = Dev(self.app, world.auth)
        self.calls = []
        self.registry = StepHandlerRegistry()
        self.registry.register("note.record", lambda ctx: self.calls.append(ctx.request.step.step_id) or "yazıldı")
        self.registry.seal()
        self.spec = AgentSpec(name="naymera_core", purpose="p", tools=[], permissions=["read_files"], inputs=["x"], outputs=["y"], cost_limit=0.0)
        broker = EffectBroker(world.tmp / "core_audit.jsonl", world.stop, ledger=world.ledger)
        steps = (PlanStepSpec("a", "Qeyd yaz", "Nəticə", ("nonempty",), (), "note.record"),)

        class Planner:
            def propose(_, goal):
                return Plan.new(goal.goal_id, steps, origin=Origin.SYSTEM, now=T0)
        self.loop = KernelLoop(self.faces.engine, autonomy="L1", stop=FileStop(world.stop), planner=Planner(), approvals=BrokeredApprovals(world.svc.client, self.spec, self.registry),
                               executor=BrokeredStepExecutor(broker, self.spec, self.registry, world.svc.client), lease_s=60)

    def create(self, title="Yeni məqsəd", op="client-op-0001", **kw):
        return self.dev.post("/v1/core/goals", {"title": title, "client_op": op, **kw})

    def transition(self, goal_id, to, version, **over):
        ch = self.dev.post(f"/v1/core/goals/{goal_id}/challenge", {"to_status": to})
        if ch.status != 200:
            return ch
        b = ch.body
        proof = over.pop("proof", None) or self.dev.sign(b, "core_goal", goal_id, h(f"{to}:{version}"), to)
        return self.dev.post(f"/v1/core/goals/{goal_id}/transition", {"to_status": to, "challenge_id": b["challenge_id"], "proof": proof, "method": over.pop("method", "biometric")})


@pytest.fixture
def cw(world):
    return CoreWorld(world)


def test_every_core_route_requires_a_valid_session(cw):
    routes = [(m, p) for m, p, auth in cw.app.router.routes() if p.startswith("/v1/core/")]
    assert len(routes) == 10 and all(auth == "session" for m, p, auth in cw.app.router.routes() if p.startswith("/v1/core/"))
    for method, pattern in routes:
        path = pattern.replace("{id}", "x").replace("{plan_id}", "x").replace("{step_id}", "y")
        assert cw.dev.call(method, path, {} if method == "POST" else None, token=None).status == 401, pattern
        assert cw.dev.call(method, path, {} if method == "POST" else None, token="nay_a_" + "x" * 43).status == 401


def test_create_is_draft_only_idempotent_and_rejects_secrets_and_bad_input(cw):
    r = cw.create("Hesabat ğüşəı 🙂", description="təsvir")
    g = r.body["goal"]
    assert r.status == 201 and g["status"] == "draft" and g["origin"] == "owner" and g["version"] == 1
    again = cw.create("Hesabat ğüşəı 🙂", description="təsvir")
    assert again.status == 200 and again.body["duplicate"] is True and again.body["goal"]["id"] == g["id"]
    assert cw.create("Başqa məzmun").status == 409                                                  # eyni client_op, fərqli məzmun
    assert cw.create(f"açar {KEY}", op="client-op-0002").status == 422
    for body in ({"title": "x"}, {"title": "x", "client_op": "BAD"}, {"title": "", "client_op": "client-op-0003"}, {"title": "x", "client_op": "client-op-0004", "priority": "urgent"},
                 {"client_op": "client-op-0005"}, {"title": "a\x00b", "client_op": "client-op-0006"}):
        assert cw.dev.post("/v1/core/goals", body).status == 400, body
    assert len(cw.dev.get("/v1/core/goals").body["goals"]) == 1


def test_goal_transitions_need_a_device_stepup_bound_to_the_current_version(cw):
    gid = cw.create().body["goal"]["id"]
    ch0 = cw.dev.post(f"/v1/core/goals/{gid}/challenge", {"to_status": "active"}).body
    assert (ch0["action"], ch0["subject"], ch0["payload_hash"], ch0["decision"]) == ("core_goal", gid, h("active:1"), "active")           # müştəri imzalanan məzmunu görür
    ok = cw.transition(gid, "active", 1)
    assert ok.status == 200 and ok.body["goal"]["status"] == "active" and ok.body["goal"]["version"] == 2
    assert cw.transition(gid, "active", 1, proof="AAAA").status == 403                              # saxta sübut
    ch = cw.dev.post(f"/v1/core/goals/{gid}/challenge", {"to_status": "paused"}).body
    old_proof = cw.dev.sign(ch, "core_goal", gid, h("paused:1"), "paused")                          # köhnə versiya (1) üçün imza
    r = cw.dev.post(f"/v1/core/goals/{gid}/transition", {"to_status": "paused", "challenge_id": ch["challenge_id"], "proof": old_proof, "method": "biometric"})
    assert r.status == 403                                                                          # sübut CARİ versiyaya (2) bağlı deyil
    assert cw.transition(gid, "paused", 2).status == 200
    assert cw.transition(gid, "draft", 3).status == 400                                             # draft hədəf deyil
    assert cw.transition(gid, "completed", 3).status == 409                                         # kernel keçid qaydası: paused -> completed yoxdur
    assert cw.transition("goal_nope0000000000000", "active", 1).status == 404


def test_voice_method_and_replayed_or_foreign_proofs_are_refused(cw):
    gid = cw.create().body["goal"]["id"]
    assert cw.transition(gid, "active", 1, method="voice").status == 403
    ch = cw.dev.post(f"/v1/core/goals/{gid}/challenge", {"to_status": "active"}).body
    proof = cw.dev.sign(ch, "core_goal", gid, h("active:1"), "active")
    body = {"to_status": "active", "challenge_id": ch["challenge_id"], "proof": proof, "method": "biometric"}
    assert cw.dev.post(f"/v1/core/goals/{gid}/transition", body).status == 200
    assert cw.dev.post(f"/v1/core/goals/{gid}/transition", body).status in (403, 409)               # təkrar: challenge yandı
    other = Dev(cw.app, cw.world.auth, name="Başqa", remote="10.0.0.9")
    ch2 = cw.dev.post(f"/v1/core/goals/{gid}/challenge", {"to_status": "paused"}).body
    stolen = other.post(f"/v1/core/goals/{gid}/transition", {"to_status": "paused", "challenge_id": ch2["challenge_id"], "proof": other.sign(ch2, "core_goal", gid, h("paused:2"), "paused"), "method": "biometric"})
    assert stolen.status == 403                                                                      # başqa cihazın challenge-i/imzası


def test_end_to_end_l1_flow_through_the_android_api(cw):
    """Məqsəd API ilə yaradılıb aktivləşdirilir; döngü plan çıxarır; addım təsdiqi MÖVCUD /v1/approvals step-up yolu ilə verilir; heç nə təsdiqsiz icra olunmur."""
    gid = cw.create("Qeyd yazılsın").body["goal"]["id"]
    assert cw.loop.tick().action == "idle"                                                           # DRAFT: döngü toxunmur
    cw.transition(gid, "active", 1)
    assert [cw.loop.tick().action for _ in range(3)] == ["proposed_plan", "plan_ready", "awaiting_approval"] and cw.calls == []
    st = cw.dev.get("/v1/core/steps/awaiting").body["steps"]
    assert len(st) == 1 and st[0]["goal_id"] == gid and st[0]["step_id"] == "a" and st[0]["capability_id"] == "note.record"
    pending = cw.dev.get("/v1/approvals").body["approvals"]
    assert [a["description"] for a in pending] == [st[0]["approval_description"]]
    assert cw.dev.decide(pending[0]["id"], "approve").status == 200                                  # sahib step-up ilə təsdiqləyir
    assert cw.loop.tick().action == "step_succeeded" and cw.calls == ["a"]
    detail = cw.dev.get(f"/v1/core/goals/{gid}").body
    assert detail["goal"]["status"] == "active" and detail["plan"]["steps"][0]["state"] == "succeeded" and detail["plan"]["steps"][0]["untrusted_output"] is True
    assert cw.dev.get("/v1/core/steps/awaiting").body["steps"] == []
    j = cw.dev.get("/v1/core/journal", query={"goal_id": gid}).body["entries"]
    assert {"plan_added", "step_finished"} <= {e["kind"] for e in j} and all(e["untrusted"] for e in j)
    assert cw.dev.get("/v1/core/status").body == {"autonomy": "L1", "stop_active": False, "steps": cw.faces.owner.health_counts(), "executes_without_owner_approval": False}


def test_unknown_steps_are_resolved_only_with_stepup_and_stop_is_reported(cw):
    gid = cw.create().body["goal"]["id"]
    cw.transition(gid, "active", 1)
    [cw.loop.tick() for _ in range(3)]
    plan = cw.faces.engine.current_plan(gid)
    cw.world.stop.write_text("stop")
    assert cw.dev.get("/v1/core/status").body["stop_active"] is True
    cw.world.stop.unlink()
    assert cw.dev.post(f"/v1/core/steps/{plan.plan_id}/a/challenge", {"resolution": "retry"}).status == 409          # hələ həll oluna bilən deyil (awaiting_approval)
    cw.faces.engine.begin_step(plan.plan_id, 1, "a", "core.apiop001", 1, 60)
    cw.faces.engine.finish_step(plan.plan_id, 1, "a", "core.apiop001", StepState.UNKNOWN, "", "", False, "executor_exception")
    ch = cw.dev.post(f"/v1/core/steps/{plan.plan_id}/a/challenge", {"resolution": "retry"})
    assert ch.status == 200
    b = ch.body
    proof = cw.dev.sign(b, "core_step", f"{plan.plan_id}/a", h("retry:1:unknown"), "retry")
    path = f"/v1/core/steps/{plan.plan_id}/a/resolve"
    assert cw.dev.post(path, {"resolution": "retry", "challenge_id": b["challenge_id"], "proof": "AAAA", "method": "biometric"}).status == 403
    ch2 = cw.dev.post(f"/v1/core/steps/{plan.plan_id}/a/challenge", {"resolution": "retry"}).body
    proof2 = cw.dev.sign(ch2, "core_step", f"{plan.plan_id}/a", h("retry:1:unknown"), "retry")
    assert cw.dev.post(path, {"resolution": "retry", "challenge_id": ch2["challenge_id"], "proof": proof2, "method": "voice"}).status == 403
    ch3 = cw.dev.post(f"/v1/core/steps/{plan.plan_id}/a/challenge", {"resolution": "retry"}).body
    proof3 = cw.dev.sign(ch3, "core_step", f"{plan.plan_id}/a", h("retry:1:unknown"), "retry")
    assert cw.dev.post(path, {"resolution": "retry", "challenge_id": ch3["challenge_id"], "proof": proof3, "method": "biometric"}).status == 200
    assert cw.faces.owner.steps(plan.plan_id)[0].state is StepState.PENDING
    assert cw.dev.post(f"/v1/core/steps/{plan.plan_id}/a/challenge", {"resolution": "explode"}).status == 400


def test_core_api_has_no_privileged_imports_no_env_no_network_and_is_not_mounted_in_production():
    root = pathlib.Path(__file__).resolve().parent.parent
    src = pathlib.Path(core_api.__file__).read_text()
    mods = [a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names] + [n.module or "" for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)]
    assert not [m for m in mods if m.startswith(("safety.broker", "safety.gate", "safety.tools", "safety.workspace", "safety.ledger", "runtime", "socket", "subprocess"))]
    for bad in ("environ", "getenv", "socket", "urllib", "subprocess", "run_session", ".approve(", ".deny("):
        assert bad not in src, bad
    for rel in ("api/bootstrap.py", "api/server.py", "runtime/production.py", "tools/serve.py", "runtime/env.py"):
        text = (root / rel).read_text()
        assert "core_api" not in text and "CoreApiApp" not in text, rel                              # istehsal yığınına QOŞULMAYIB
