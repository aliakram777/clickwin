import ast
import json
from pathlib import Path

import pytest

from agents.orchestrator import Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec
from safety.broker import (
    BrokerRequired, Effect, EffectBroker, ExecutionError, TransientError, broker_only,
)
from safety.ledger import BudgetPolicy, CostLedger, reconcile

ROOT = Path(__file__).resolve().parent.parent


def spec(name="tasnif_agenti", perms=("read_files", "publish", "spend_money"), limit=5.0):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms),
                     inputs=["sorgu"], outputs=["netice"], cost_limit=limit)


class Exec:
    """Broker-ə verilən icraçı: çağırışları sayır, istəyə görə xəta atır."""

    def __init__(self, result=None, exc=None, on_call=None):
        self.calls = 0
        self.result = {"netice": "ok"} if result is None else result
        self.exc = exc
        self.on_call = on_call

    def __call__(self):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.exc:
            raise self.exc
        return self.result


@pytest.fixture
def env(tmp_path):
    ledger = CostLedger(tmp_path / "ledger.db")
    audit, stop = tmp_path / "audit.jsonl", tmp_path / "STOP"

    def make(policy=None, approver=None, actual_cost=None, use_ledger=True, audit_path=None):
        return EffectBroker(audit_path or audit, stop, approver=approver,
                            ledger=ledger if use_ledger else None, policy=policy,
                            actual_cost=actual_cost)

    return ledger, make, audit, stop


def rows(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def fx(cost=0.5, op="op1", perm="read_files", s=None):
    return Effect(s or spec(), perm, cost, op_id=op, description="test")


def entry(ledger, op):
    return next(e for e in ledger.entries() if e.op_id == op)


# --- əsas axın ---

def test_ok_flow_gate_reserve_execute_settle(env):
    ledger, make, audit, _ = env
    seen = {}

    def inside():  # icradan əvvəl: audit yazılıb, rezerv tutulub, hələ hesablaşma yoxdur
        seen["audit"] = rows(audit)
        seen["entry"] = entry(ledger, "op1").status

    ex = Exec(on_call=inside)
    res = make().run(fx(), ex)
    assert (res.status, res.cost, res.cost_status) == ("ok", 0.5, "committed")
    assert ex.calls == 1
    assert [r["allowed"] for r in seen["audit"]] == [True]
    assert seen["entry"] == "reserved"
    assert entry(ledger, "op1").status == "committed"


def test_permission_denied_never_executes_and_releases(env):
    ledger, make, audit, _ = env
    ex = Exec()
    res = make().run(fx(perm="write_files"), ex)
    assert res.status == "blocked" and "icazə" in res.reason
    assert ex.calls == 0
    assert entry(ledger, "op1").status == "released" and ledger.total() == 0.0
    assert rows(audit)[-1]["allowed"] is False


@pytest.mark.parametrize("approved", [False, True])
def test_sensitive_effects_need_approval(env, approved):
    _, make, _, _ = env
    asked = []
    ex = Exec()
    res = make(approver=lambda a: asked.append(a) or approved).run(fx(perm="publish"), ex)
    assert (res.status == "ok") is approved
    assert ex.calls == (1 if approved else 0)
    assert len(asked) == 1


# --- stop bayrağı ---

def test_stop_flag_blocks_before_reservation_and_is_audited(env):
    ledger, make, audit, stop = env
    stop.write_text("stop")
    ex = Exec()
    res = make().run(fx(), ex)
    assert res.status == "blocked" and "stop" in res.reason.lower()
    assert ex.calls == 0 and ledger.entries() == []
    last = rows(audit)[-1]
    assert last["allowed"] is False and last["source"] == "stop"
    stop.unlink()
    assert make().run(fx(op="op2"), ex).status == "ok"


# --- büdcə ---

@pytest.mark.parametrize("policy,cost,word", [
    (BudgetPolicy(task_limit=0.3), 0.5, "tapşırıq"),
    (BudgetPolicy(global_limit=0.3), 0.5, "ümumi"),
    (BudgetPolicy(agent_limits={"tasnif_agenti": 0.3}), 0.5, "agent"),
])
def test_budget_denial_never_executes_and_is_audited(env, policy, cost, word):
    ledger, make, audit, _ = env
    ex = Exec()
    res = make(policy=policy).run(fx(cost), ex)
    assert res.status == "blocked" and word in res.reason
    assert ex.calls == 0 and ledger.entries() == []
    last = rows(audit)[-1]
    assert (last["allowed"], last["source"]) == (False, "budget")
    assert reconcile(ledger, audit).ok


def test_spec_limit_cannot_be_loosened_by_policy(env):
    _, make, _, _ = env
    ex = Exec()
    res = make(policy=BudgetPolicy(agent_limits={"tasnif_agenti": 99.0})).run(
        fx(1.5, s=spec(limit=1.0)), ex)
    assert res.status == "blocked" and ex.calls == 0


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True])
def test_invalid_cost_blocked_without_reservation(env, bad):
    ledger, make, audit, _ = env
    ex = Exec()
    res = make().run(fx(bad), ex)
    assert res.status == "blocked" and ex.calls == 0 and ledger.entries() == []
    assert rows(audit)[-1]["allowed"] is False


# --- təkrar action ---

def test_duplicate_effect_runs_once(env):
    ledger, make, audit, _ = env
    broker, ex = make(), Exec()
    assert broker.run(fx(), ex).status == "ok"
    again = broker.run(fx(), ex)
    assert again.status == "duplicate" and ex.calls == 1
    assert ledger.total() == pytest.approx(0.5)
    assert len(rows(audit)) == 1  # təkrar cəhd yeni audit qərarı yaratmır


def test_duplicate_protection_without_ledger(env):
    _, make, _, _ = env
    broker, ex = make(use_ledger=False), Exec()
    assert broker.run(fx(), ex).status == "ok"
    assert broker.run(fx(), ex).status == "duplicate"
    assert ex.calls == 1


def test_effects_without_op_id_are_distinct(env):
    ledger, make, _, _ = env
    broker, ex = make(), Exec()
    broker.run(fx(op=""), ex)
    broker.run(fx(op=""), ex)
    assert ex.calls == 2 and len(ledger.entries()) == 2


def test_blocked_effect_can_be_retried_with_same_op_id(env):
    _, make, _, _ = env
    ex = Exec()
    assert make().run(fx(perm="publish"), ex).status == "blocked"
    assert make(approver=lambda a: True).run(fx(perm="publish"), ex).status == "ok"
    assert ex.calls == 1


def test_in_memory_limit_without_ledger(env):
    _, make, _, _ = env
    broker, ex = make(use_ledger=False), Exec()
    s = spec(limit=1.0)
    assert broker.run(fx(0.6, "a", s=s), ex).status == "ok"
    res = broker.run(fx(0.6, "b", s=s), ex)
    assert res.status == "blocked" and "limit" in res.reason


# --- hesablaşma və naməlum xərc ---

def test_reported_actual_cost_is_committed(env):
    ledger, make, _, _ = env
    res = make(actual_cost=lambda out: out["_cost"]).run(fx(), Exec({"netice": 1, "_cost": 0.2}))
    assert (res.cost, res.cost_status) == (0.2, "committed")
    assert entry(ledger, "op1").amount == 0.2


@pytest.mark.parametrize("reported", [-1, float("nan"), "çox", True])
def test_invalid_reported_cost_keeps_reservation_unknown(env, reported):
    ledger, make, _, _ = env
    res = make(actual_cost=lambda out: out["_cost"]).run(fx(), Exec({"netice": 1, "_cost": reported}))
    assert res.cost_status == "reserved_unknown"
    e = entry(ledger, "op1")
    assert (e.status, e.unknown, e.amount) == ("reserved", True, 0.5)


def test_reporter_exception_keeps_reservation_unknown(env):
    ledger, make, _, _ = env

    def boom(out):
        raise KeyError("x")

    assert make(actual_cost=boom).run(fx(), Exec()).cost_status == "reserved_unknown"
    assert entry(ledger, "op1").unknown


def test_executor_error_with_known_cost_is_committed(env):
    ledger, make, _, _ = env
    res = make().run(fx(), Exec(exc=ExecutionError("çökdü", cost=0.1)))
    assert (res.status, res.cost, res.cost_status, res.transient) == ("error", 0.1, "committed", False)
    assert "çökdü" in res.reason and res.stage == "run"
    assert entry(ledger, "op1").amount == 0.1


@pytest.mark.parametrize("exc,transient", [
    (TransientError("t", cost=0.1), True),
    (TimeoutError("t"), True),
    (ConnectionError("c"), True),
    (RuntimeError("r"), False),
])
def test_transient_classification(env, exc, transient):
    _, make, _, _ = env
    assert make().run(fx(), Exec(exc=exc)).transient is transient


def test_unknown_cost_is_kept_flagged_and_never_auto_released(env):
    ledger, make, audit, _ = env
    res = make().run(fx(), Exec(exc=RuntimeError("naməlum")))
    assert (res.status, res.cost, res.cost_status) == ("error", 0.5, "reserved_unknown")
    assert [(e.op_id, e.status) for e in ledger.unknown_entries()] == [("op1", "reserved")]
    assert make().run(fx(), Exec()).status == "duplicate"  # təkrar icra və ikiqat hesab yoxdur
    assert ledger.total() == pytest.approx(0.5)
    assert reconcile(ledger, audit).ok
    ledger.commit("op1", 0.2)  # yalnız əl ilə uzlaşdırma
    assert ledger.unknown_entries() == [] and ledger.total() == pytest.approx(0.2)


def test_validation_error_commits_run_cost_as_error(env):
    ledger, make, _, _ = env
    res = make().run(fx(), Exec({"başqa": 1}), validate=lambda out: "çıxış yoxdur")
    assert (res.status, res.stage, res.cost_status) == ("error", "output", "committed")
    assert entry(ledger, "op1").status == "committed"


def test_non_exception_interrupt_leaves_reservation_unknown(env):
    ledger, make, _, _ = env
    with pytest.raises(KeyboardInterrupt):
        make().run(fx(), Exec(exc=KeyboardInterrupt()))
    e = entry(ledger, "op1")
    assert (e.status, e.unknown) == ("reserved", True)


def test_gate_failure_releases_reservation_and_propagates(env, tmp_path):
    ledger, make, _, _ = env
    ex = Exec()
    with pytest.raises(OSError):
        make(audit_path=tmp_path).run(fx(), ex)  # audit yolu qovluqdur: yazmaq mümkün deyil
    assert ex.calls == 0
    assert entry(ledger, "op1").status == "released"


# --- broker-dən yan keçməmək ---

def test_broker_only_tools_refuse_direct_calls(env):
    _, make, _, _ = env

    @broker_only
    def tool(x):
        return x * 2

    with pytest.raises(BrokerRequired):
        tool(2)
    assert make().run(fx(), lambda: {"netice": tool(2)}).output == {"netice": 4}
    with pytest.raises(BrokerRequired):  # icradan sonra yenidən bağlanır
        tool(2)


def test_broker_context_resets_after_executor_error(env):
    _, make, _, _ = env

    @broker_only
    def tool():
        return 1

    make().run(fx(), Exec(exc=RuntimeError("x")))
    with pytest.raises(BrokerRequired):
        tool()


def test_broker_only_tool_blocked_effect_cannot_run_it(env):
    _, make, _, _ = env
    called = []

    @broker_only
    def tool():
        called.append(1)

    make().run(fx(perm="write_files"), tool)  # Gate rədd edir: alət heç çağırılmır
    assert called == []


def test_orchestrator_runner_only_runs_inside_broker(env, tmp_path):
    ledger, _, audit, _ = env
    reg = Registry(":memory:")
    reg.add(spec(), "dept_a")
    seen = []

    @broker_only
    def side_effect():
        seen.append(rows(audit))  # icra anında audit-də icazə sətri artıq var

    def runner(s, data):
        side_effect()
        return {"netice": "ok"}

    orch = Orchestrator(reg, runner, audit_path=audit, stop_path=tmp_path / "STOP",
                        errors_path=tmp_path / "e.jsonl", ledger=ledger)
    assert orch.execute(Task("dept_a", {"sorgu": "s"}, "read_files", 0.5)).status == "ok"
    assert [r["allowed"] for r in seen[0]] == [True]
    with pytest.raises(BrokerRequired):
        side_effect()


def test_orchestrators_can_share_one_broker(env, tmp_path):
    ledger, make, _, _ = env
    broker = make(policy=BudgetPolicy(global_limit=1.0))
    reg = Registry(":memory:")
    reg.add(spec("agent_a"), "dept_a")
    reg.add(spec("agent_b"), "dept_b")
    calls = []

    def runner(s, d):
        calls.append(s.name)
        return {"netice": "ok"}

    o1 = Orchestrator(reg, runner, broker=broker, errors_path=tmp_path / "e.jsonl")
    o2 = Orchestrator(reg, runner, broker=broker, errors_path=tmp_path / "e.jsonl")
    assert o1.execute(Task("dept_a", {"sorgu": "s"}, "read_files", 0.6)).status == "ok"
    res = o2.execute(Task("dept_b", {"sorgu": "s"}, "read_files", 0.6))
    assert res.status == "blocked" and "ümumi" in res.reason
    assert calls == ["agent_a"]


def test_policy_requires_ledger(tmp_path):
    with pytest.raises(ValueError):
        EffectBroker(tmp_path / "a", tmp_path / "s", policy=BudgetPolicy(global_limit=1.0))


# --- statik yoxlama: runner-ə birbaşa çağırış yoxdur ---

def _direct_runner_calls(path: Path):
    """`runner(...)` / `self.runner(...)` çağırışları (lambda daxilindəkilər sayılmır: broker-ə ötürülür)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name != "runner":
            continue
        p = node
        while p in parents and not isinstance(parents[p], ast.Lambda):
            p = parents[p]
        if p not in parents:  # lambda daxilində deyil
            found.append(node.lineno)
    return found


def test_no_module_calls_a_runner_outside_the_broker():
    callers = {p.name for p in (ROOT / "agents").glob("*.py") if _direct_runner_calls(p)}
    callers |= {p.name for p in (ROOT / "safety").glob("*.py") if _direct_runner_calls(p)}
    # verify.py: yalnız test tapşırığı üçün mock/sandbox runner (bilinən məhdudiyyət, STATUS.md).
    # Yeni birbaşa çağırış əlavə olunsa bu test düşür.
    assert callers == {"verify.py"}
