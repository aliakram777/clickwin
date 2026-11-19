"""Effect Broker + təsdiq xidməti. Buradakı təsdiqləyici test obyektidir (kind="mock"), real istifadəçi
təsdiqi deyil."""
import ast
import json
from pathlib import Path

import pytest

from agents.ceo import CEO, RulePlanner
from agents.orchestrator import Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec
from agents.workflow import PlanStep
from safety.approvals import ApprovalService, Principal
from safety.broker import Effect, EffectBroker
from safety.ledger import BudgetPolicy, CostLedger, reconcile

ROOT = Path(__file__).resolve().parent.parent
MOCK = Principal("owner", "mock", "test")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def spec(name="agent_a", perms=("read_files", "publish", "spend_money"), limit=5.0):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms),
                     inputs=["sorgu"], outputs=["netice"], cost_limit=limit)


class Exec:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return {"netice": "ok"}


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    audit = tmp_path / "audit.jsonl"
    ledger = CostLedger(tmp_path / "ledger.db")
    svc = ApprovalService(tmp_path / "approvals.db", audit, owners={"owner"}, ttl_seconds=60,
                          clock=clock, allow_mock=True)

    def broker(policy=None, **kw):
        return EffectBroker(audit, tmp_path / "STOP", ledger=ledger, policy=policy,
                            approvals=svc.client, **kw)

    return svc, ledger, broker, audit, clock, tmp_path


def fx(op="op1", perm="publish", payload=None, approval_id="", cost=0.5, s=None):
    return Effect(s or spec(), perm, cost, op_id=op, description="post",
                  payload={"text": "salam"} if payload is None else payload, approval_id=approval_id)


def rows(audit):
    p = Path(audit)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def events(audit):
    return [r["event"] for r in rows(audit) if "event" in r]


def approve_pending(svc, approval_id):
    assert svc.gateway.approve(approval_id, MOCK).ok


# --- pending və icra ---

def test_sensitive_effect_becomes_pending_and_never_runs(env):
    svc, ledger, broker, audit, _, _ = env
    ex = Exec()
    res = broker().run(fx(), ex)
    assert res.status == "pending_approval" and res.approval_id
    assert ex.calls == 0 and ledger.entries() == []  # rezerv də yoxdur
    assert events(audit) == ["approval_requested"]
    assert [t.id for t in svc.gateway.pending()] == [res.approval_id]


def test_pending_is_idempotent(env):
    svc, _, broker, _, _, _ = env
    ex = Exec()
    a = broker().run(fx(), ex)
    b = broker().run(fx(), ex)
    assert a.approval_id == b.approval_id and len(svc.gateway.pending()) == 1


def test_runner_runs_only_after_approval_and_only_once(env):
    svc, ledger, broker, audit, _, _ = env
    ex = Exec()
    pending = broker().run(fx(), ex)
    approve_pending(svc, pending.approval_id)
    res = broker().run(fx(), ex)  # eyni effekt: təsdiq hash ilə tapılır
    assert res.status == "ok" and ex.calls == 1
    assert svc.gateway.get(pending.approval_id).status == "consumed"
    assert broker().run(fx(), ex).status == "duplicate" and ex.calls == 1
    assert events(audit) == ["approval_requested", "approval_approved", "approval_consumed"]
    gate_rows = [r for r in rows(audit) if "event" not in r]
    assert [r["allowed"] for r in gate_rows] == [True]  # Gate yalnız istifadə edilmiş təsdiqdən sonra icazə verir
    assert ledger.total() == pytest.approx(0.5)
    assert reconcile(ledger, audit).ok


def test_explicit_approval_id(env):
    svc, _, broker, _, _, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    approve_pending(svc, p.approval_id)
    assert broker().run(fx(approval_id=p.approval_id), ex).status == "ok"


def test_fake_approval_id_blocks(env):
    _, ledger, broker, audit, _, _ = env
    ex = Exec()
    res = broker().run(fx(approval_id="f" * 32), ex)
    assert res.status == "blocked" and ex.calls == 0 and ledger.entries() == []
    assert "approval_rejected" in events(audit)
    assert [r for r in rows(audit) if r.get("source") == "approval"]


def test_changed_payload_cost_or_agent_voids_approval(env):
    svc, _, broker, audit, _, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    approve_pending(svc, p.approval_id)
    res = broker().run(fx(payload={"text": "başqa"}, approval_id=p.approval_id), ex)
    assert res.status == "blocked" and "payload" in res.reason
    assert svc.gateway.get(p.approval_id).status == "invalidated"
    assert broker().run(fx(approval_id=p.approval_id), ex).status == "blocked"  # orijinal də artıq keçmir
    assert ex.calls == 0
    for kw in ({"cost": 0.9}, {"s": spec("agent_b")}):  # cost/agent dəyişsə də hash dəyişir
        q = broker().run(fx(op="op2", **kw), ex)
        assert q.status == "pending_approval"


def test_approval_cannot_be_reused_for_another_effect(env):
    svc, _, broker, _, _, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    approve_pending(svc, p.approval_id)
    assert broker().run(fx(), ex).status == "ok"
    res = broker().run(fx(op="op2", approval_id=p.approval_id), ex)
    assert res.status == "blocked" and ex.calls == 1


def test_expired_approval_blocks(env):
    svc, ledger, broker, audit, clock, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    approve_pending(svc, p.approval_id)
    clock.t += 61
    res = broker().run(fx(approval_id=p.approval_id), ex)
    assert res.status == "blocked" and ex.calls == 0 and ledger.entries() == []
    assert "approval_expired" in events(audit)
    assert broker().run(fx(), ex).status == "pending_approval"  # yeni sorğu


def test_denied_approval_blocks(env):
    svc, _, broker, _, _, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    svc.gateway.deny(p.approval_id, MOCK, "yox")
    res = broker().run(fx(), ex)
    assert res.status == "blocked" and ex.calls == 0


def test_sensitive_effect_requires_explicit_op_id(env):
    svc, _, broker, _, _, _ = env
    ex = Exec()
    res = broker().run(fx(op=""), ex)
    assert res.status == "blocked" and "op_id" in res.reason
    assert ex.calls == 0 and svc.gateway.pending() == []


def test_non_json_payload_is_denied(env):
    _, _, broker, _, _, _ = env
    ex = Exec()
    res = broker().run(fx(payload={"x": object()}), ex)
    assert res.status == "blocked" and ex.calls == 0


def test_no_pending_for_actions_the_agent_may_not_do(env):
    svc, _, broker, audit, _, _ = env
    ex = Exec()
    res = broker().run(fx(s=spec(perms=("read_files",))), ex)
    assert res.status == "blocked" and "icazə" in res.reason
    assert svc.gateway.pending() == []


def test_non_sensitive_effects_need_no_approval(env):
    svc, _, broker, _, _, _ = env
    ex = Exec()
    assert broker().run(fx(perm="read_files", op=""), ex).status == "ok"
    assert svc.gateway.pending() == []


def test_budget_denial_does_not_consume_approval(env):
    svc, ledger, broker, _, _, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    approve_pending(svc, p.approval_id)
    tight = broker(policy=BudgetPolicy(task_limit=0.1))
    assert tight.run(fx(), ex).status == "blocked" and ex.calls == 0
    assert svc.gateway.get(p.approval_id).status == "approved"  # istifadə olunmayıb
    assert broker().run(fx(), ex).status == "ok"


def test_consumption_race_blocks_and_releases_reservation(env):
    svc, ledger, broker, audit, _, _ = env
    ex = Exec()
    p = broker().run(fx(), ex)
    approve_pending(svc, p.approval_id)
    client = svc.client
    orig = client.check

    def sneaky(aid, digest):  # yoxlamadan sonra başqası təsdiqi istifadə edir
        r = orig(aid, digest)
        client.consume(aid, digest)
        return r

    client.check = sneaky
    res = broker().run(fx(approval_id=p.approval_id), ex)
    assert res.status == "blocked" and ex.calls == 0
    assert ledger.entries()[0].status == "released"
    assert [r["allowed"] for r in rows(audit) if "event" not in r] == [False]
    assert reconcile(ledger, audit).ok


def test_legacy_approver_still_works_and_cannot_mix(env, tmp_path):
    _, ledger, _, audit, _, _ = env
    legacy = EffectBroker(audit, tmp_path / "STOP", approver=lambda a: True)
    ex = Exec()
    assert legacy.run(fx(op=""), ex).status == "ok"
    with pytest.raises(ValueError):
        EffectBroker(audit, tmp_path / "STOP", approver=lambda a: True,
                     approvals=env[0].client)


# --- agentlər öz əməliyyatını təsdiqləyə bilməz ---

def test_broker_and_agents_have_no_approval_capability(env):
    svc, _, broker, _, _, _ = env
    b = broker()
    assert not any(hasattr(b, n) for n in ("approve", "deny", "gateway"))
    assert not hasattr(b.approvals, "approve") and not hasattr(b.approvals, "deny")

    forbidden_names = {"ApprovalGateway", "ApprovalService"}
    for path in [*(ROOT / "agents").glob("*.py"), ROOT / "safety" / "broker.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not forbidden_names & {a.name for a in node.names}, path.name
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"approve", "deny", "gateway"}, (path.name, node.lineno)


# --- Orchestrator və CEO ---

def make_orch(env, runner):
    svc, ledger, _, audit, _, tmp = env
    reg = Registry(":memory:")
    reg.add(spec(), "dept_a")
    orch = Orchestrator(reg, runner, audit_path=audit, stop_path=tmp / "STOP",
                        errors_path=tmp / "e.jsonl", ledger=ledger, approvals=svc.client)
    return orch


def sensitive_task(**kw):
    base = dict(department="dept_a", input={"sorgu": "mətn"}, permission="publish", cost=0.5,
                description="post", op_id="job1:a0")
    base.update(kw)
    return Task(**base)


def test_orchestrator_end_to_end_with_payload_binding(env):
    svc = env[0]
    calls = []
    orch = make_orch(env, lambda s, d: calls.append(d) or {"netice": "ok"})
    pending = orch.execute(sensitive_task())
    assert pending.status == "pending_approval" and pending.approval_id and calls == []
    approve_pending(svc, pending.approval_id)
    changed = orch.execute(sensitive_task(input={"sorgu": "başqa mətn"}))
    assert changed.status == "pending_approval" and calls == []  # başqa payload: ayrıca təsdiq
    res = orch.execute(sensitive_task())
    assert res.status == "ok" and calls == [{"sorgu": "mətn"}]


def test_ceo_surfaces_pending_approvals_and_skips_dependents(env):
    svc, _, _, _, _, _ = env
    calls = []
    orch = make_orch(env, lambda s, d: calls.append(s.name) or {"netice": "ok"})
    ceo = CEO(orch, RulePlanner([]), budget=10.0)
    steps = [PlanStep("a", "dept_a", "publish", 0.5), PlanStep("b", "dept_a", "read_files", 0.1, ("a",))]
    report = ceo.run_workflow("g", steps, job_id="job1")
    assert [s.status for s in report.steps] == ["pending_approval", "skipped"]
    assert [s.approval_id for s in report.pending_approvals] == [svc.gateway.pending()[0].id]
    assert "təsdiq" in report.summary() and calls == []
