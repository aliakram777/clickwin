import json

import pytest

from agents.ceo import CEO, Rule, RulePlanner
from agents.orchestrator import ExecutionError, Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec
from safety.ledger import BudgetPolicy, CostLedger, reconcile


def spec(name, inputs, perms, limit=1.0):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms),
                     inputs=list(inputs), outputs=["netice"], cost_limit=limit)


class Runner:
    def __init__(self, actual=None, exc=None):
        self.calls = []
        self.actual = actual
        self.exc = exc

    def __call__(self, spec, data):
        self.calls.append(spec.name)
        if self.exc:
            raise self.exc
        out = {"netice": "ok"}
        if self.actual is not None:
            out["_cost"] = self.actual
        return out


def cost_of(output):
    return output.get("_cost")


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    reg.add(spec("tasnif_agenti", ["sual"], ["read_files", "publish"], 1.0), "destek")
    reg.add(spec("satis_agenti", ["sorgu"], ["read_files"], 5.0), "satis")
    ledger = CostLedger(tmp_path / "ledger.db")
    paths = dict(audit_path=tmp_path / "audit.jsonl", stop_path=tmp_path / "STOP",
                 errors_path=tmp_path / "errors.jsonl")

    def make(runner=None, policy=None, approver=None, use_cost_of=True):
        runner = runner or Runner()
        orch = Orchestrator(reg, runner, approver=approver, ledger=ledger, policy=policy,
                            actual_cost=cost_of if use_cost_of else None, **paths)
        return orch, runner

    return reg, ledger, make, tmp_path


def task(cost=0.5, op_id="", permission="read_files", department="destek"):
    return Task(department, {"sual": "s", "sorgu": "s"}, permission, cost, op_id=op_id)


def entry(ledger, op):
    return next(e for e in ledger.entries() if e.op_id == op)


def test_global_limit_across_agents_blocks_without_running(env):
    _, ledger, make, _ = env
    orch, runner = make(policy=BudgetPolicy(global_limit=1.0))
    assert orch.execute(task(0.6, "a")).status == "ok"
    res = orch.execute(task(0.6, "b", department="satis"))
    assert res.status == "blocked" and "ümumi" in res.reason
    assert runner.calls == ["tasnif_agenti"]  # ikinci runner çağırılmadı
    assert ledger.total() == pytest.approx(0.6)


def test_task_limit_blocks_before_running(env):
    _, ledger, make, _ = env
    orch, runner = make(policy=BudgetPolicy(task_limit=0.5))
    res = orch.execute(task(0.6))
    assert res.status == "blocked" and "tapşırıq" in res.reason
    assert runner.calls == [] and ledger.entries() == []


def test_policy_can_tighten_but_not_loosen_agent_limit(env):
    _, ledger, make, _ = env
    orch, runner = make(policy=BudgetPolicy(agent_limits={"tasnif_agenti": 0.3}))
    assert orch.execute(task(0.4)).status == "blocked"
    orch2, runner2 = make(policy=BudgetPolicy(agent_limits={"tasnif_agenti": 10.0}))
    res = orch2.execute(task(1.5))  # spec limiti 1.0 qalır
    assert res.status == "blocked" and "limit" in res.reason
    assert runner.calls == [] and runner2.calls == []


def test_success_commits_actual_cost_and_frees_room(env):
    _, ledger, make, _ = env
    orch, runner = make(Runner(actual=0.2), BudgetPolicy(global_limit=1.0))
    res = orch.execute(task(0.5, "a"))
    assert (res.status, res.cost, res.cost_status) == ("ok", 0.2, "committed")
    e = entry(ledger, "a")
    assert (e.status, e.amount, e.reserved) == ("committed", 0.2, 0.5)
    assert orch.execute(task(0.7, "b", department="satis")).status == "ok"  # 0.2 + 0.7 <= 1.0


def test_success_without_reporter_commits_reservation(env):
    _, ledger, make, _ = env
    orch, _ = make(Runner(actual=0.1), use_cost_of=False)
    res = orch.execute(task(0.5, "a"))
    assert res.cost == 0.5 and entry(ledger, "a").amount == 0.5


def test_failure_with_known_cost_is_committed(env):
    _, ledger, make, _ = env
    orch, _ = make(Runner(exc=ExecutionError("çökdü", cost=0.1)))
    res = orch.execute(task(0.5, "a"))
    assert (res.status, res.cost, res.cost_status) == ("error", 0.1, "committed")
    e = entry(ledger, "a")
    assert (e.status, e.amount, e.unknown) == ("committed", 0.1, False)


def test_failure_with_zero_known_cost_costs_nothing(env):
    _, ledger, make, _ = env
    orch, _ = make(Runner(exc=ExecutionError("çökdü", cost=0.0)))
    orch.execute(task(0.5, "a"))
    assert ledger.total() == 0.0


def test_failure_with_unknown_cost_keeps_reservation_flagged(env):
    _, ledger, make, _ = env
    orch, _ = make(Runner(exc=RuntimeError("naməlum")), BudgetPolicy(global_limit=1.0))
    res = orch.execute(task(0.5, "a"))
    assert (res.status, res.cost, res.cost_status) == ("error", 0.5, "reserved_unknown")
    e = entry(ledger, "a")
    assert (e.status, e.amount, e.unknown) == ("reserved", 0.5, True)
    assert orch.execute(task(0.6, "b", department="satis")).status == "blocked"  # rezerv qorunur
    ledger.commit("a", 0.2)  # sonradan məlum oldu
    healthy, _ = make(policy=BudgetPolicy(global_limit=1.0))
    assert healthy.execute(task(0.6, "b", department="satis")).status == "ok"


@pytest.mark.parametrize("bad", [-1, float("nan"), "çox", True])
def test_invalid_reported_cost_is_treated_as_unknown(env, bad):
    _, ledger, make, _ = env
    orch, _ = make(Runner(actual=bad))
    res = orch.execute(task(0.5, "a"))
    assert res.cost_status == "reserved_unknown"
    assert entry(ledger, "a").unknown is True


def test_missing_output_commits_run_cost(env):
    _, ledger, make, _ = env

    class NoOut(Runner):
        def __call__(self, spec, data):
            self.calls.append(spec.name)
            return {"başqa": 1}

    orch, _ = make(NoOut())
    res = orch.execute(task(0.5, "a"))
    assert res.status == "error" and res.cost_status == "committed"
    assert entry(ledger, "a").amount == 0.5


def test_gate_denial_releases_reservation(env):
    _, ledger, make, _ = env
    orch, runner = make()
    res = orch.execute(task(0.5, "a", permission="publish"))  # təsdiq yoxdur
    assert res.status == "blocked" and "təsdiq" in res.reason
    assert runner.calls == []
    e = entry(ledger, "a")
    assert (e.status, e.amount) == ("released", 0.0)
    assert ledger.total() == 0.0
    orch2, _ = make(approver=lambda a: True)
    assert orch2.execute(task(0.5, "a", permission="publish")).status == "ok"


def test_repeated_op_id_never_recounted(env):
    _, ledger, make, _ = env
    orch, runner = make(Runner(exc=RuntimeError("x")))
    orch.execute(task(0.5, "a"))
    res = orch.execute(task(0.5, "a"))
    assert res.status == "duplicate"
    assert len(runner.calls) == 1 and ledger.total() == pytest.approx(0.5)


def test_invalid_cost_blocked_by_gate_without_reservation(env):
    _, ledger, make, tmp = env
    orch, runner = make()
    for bad in (-1, float("nan")):
        assert orch.execute(task(bad)).status == "blocked"
    assert runner.calls == [] and ledger.entries() == []
    rows = [json.loads(l) for l in (tmp / "audit.jsonl").read_text().splitlines()]
    assert [r["allowed"] for r in rows] == [False, False]


def test_reconcile_matches_audit_after_mixed_flows(env):
    _, ledger, make, tmp = env
    orch, _ = make(Runner(actual=0.2), BudgetPolicy(global_limit=3.0))
    orch.execute(task(0.5, "a"))                        # faktiki 0.2
    orch.execute(task(0.5, "a"))                        # təkrar
    orch.execute(task(0.5, "b", permission="publish"))  # gate bloku -> release
    orch.execute(task(9.0, "c"))                        # limit
    orch2, _ = make(Runner(exc=RuntimeError("x")))
    orch2.execute(task(0.4, "d", department="satis"))   # naməlum
    rep = reconcile(ledger, tmp / "audit.jsonl")
    assert rep.ok, rep.mismatches


# --- CEO ---

RULES = [Rule("sual", "destek", "read_files", 0.6), Rule("təklif", "satis", "read_files", 0.6)]
GOAL = "sual və təklif"


def test_ceo_rejects_plan_over_global_room_before_any_dispatch(env):
    _, ledger, make, _ = env
    orch, runner = make(policy=BudgetPolicy(global_limit=1.0))
    report = CEO(orch, RulePlanner(RULES), budget=10.0).run(GOAL)  # plan 1.2 > 1.0
    assert report.status == "rejected" and "ümumi" in report.reason
    assert runner.calls == [] and ledger.entries() == []


def test_ceo_global_room_accounts_for_earlier_spending(env):
    _, ledger, make, _ = env
    orch, runner = make(policy=BudgetPolicy(global_limit=1.0))
    ceo = CEO(orch, RulePlanner([Rule("sual", "destek", "read_files", 0.6)]), budget=10.0)
    assert ceo.run("sual").status == "ok"
    again = ceo.run("sual")  # qalan yer 0.4 < 0.6
    assert again.status == "rejected"
    assert len(runner.calls) == 1


def test_ceo_rejects_step_above_task_limit(env):
    _, _, make, _ = env
    orch, runner = make(policy=BudgetPolicy(task_limit=0.5))
    report = CEO(orch, RulePlanner(RULES), budget=10.0).run(GOAL)
    assert report.status == "rejected" and "tapşırıq" in report.reason
    assert runner.calls == []


def test_ceo_step_blocked_by_agent_limit_never_runs(env):
    _, ledger, make, _ = env
    orch, runner = make(policy=BudgetPolicy(agent_limits={"satis_agenti": 0.1}))
    report = CEO(orch, RulePlanner(RULES), budget=10.0).run(GOAL)
    assert report.status == "partial"
    assert [s.status for s in report.steps] == ["ok", "blocked"]
    assert runner.calls == ["tasnif_agenti"]
    assert ledger.total() == pytest.approx(0.6)


def test_ceo_without_policy_keeps_old_behavior(env):
    _, _, make, _ = env
    orch, runner = make()
    assert CEO(orch, RulePlanner(RULES), budget=10.0).run(GOAL).status == "ok"
