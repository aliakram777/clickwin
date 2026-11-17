import json

import pytest

from agents.ceo import CEO, Rule, RulePlanner
from agents.orchestrator import Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec
from safety.ledger import CostLedger, reconcile


def spec(name, inputs, perms, limit=1.0):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms),
                     inputs=list(inputs), outputs=["netice"], cost_limit=limit)


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, spec, data):
        self.calls.append(spec.name)
        return {"netice": "ok"}


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    reg.add(spec("tasnif_agenti", ["sual"], ["read_files", "publish"], 1.0), "destek")
    reg.add(spec("satis_agenti", ["sorgu"], ["read_files", "write_files"], 5.0), "satis")
    paths = dict(audit_path=tmp_path / "audit.jsonl", stop_path=tmp_path / "STOP",
                 errors_path=tmp_path / "errors.jsonl")
    db = tmp_path / "ledger.db"
    runner = Runner()

    def make(ledger=None, approver=None):
        led = ledger or CostLedger(db)
        return Orchestrator(reg, runner, approver=approver, ledger=led, **paths), led

    return reg, runner, make, tmp_path, db


def task(cost=0.0, op_id="", permission="read_files", department="destek"):
    return Task(department, {"sual": "s", "sorgu": "s"}, permission, cost, op_id=op_id)


def test_cost_is_recorded_per_agent(env):
    _, _, make, _, _ = env
    orch, led = make()
    assert orch.execute(task(0.4)).status == "ok"
    assert orch.execute(task(0.3, department="satis")).status == "ok"
    assert led.by_agent() == {"tasnif_agenti": pytest.approx(0.4), "satis_agenti": pytest.approx(0.3)}


def test_limit_holds_across_restart(env):
    _, runner, make, _, db = env
    orch1, led1 = make()
    assert orch1.execute(task(0.6)).status == "ok"
    led1.close()
    orch2, led2 = make(ledger=CostLedger(db))  # proses yenidən açıldı
    res = orch2.execute(task(0.6))
    assert res.status == "blocked" and "limit" in res.reason
    assert orch2.execute(task(0.4)).status == "ok"
    assert len(runner.calls) == 2
    assert led2.agent_total("tasnif_agenti") == pytest.approx(1.0)


def test_repeated_operation_not_run_or_charged_twice(env):
    _, runner, make, tmp, _ = env
    orch, led = make()
    assert orch.execute(task(0.4, op_id="op-1")).status == "ok"
    res = orch.execute(task(0.4, op_id="op-1"))
    assert res.status == "duplicate"
    assert len(runner.calls) == 1
    assert led.total() == pytest.approx(0.4)
    assert len((tmp / "audit.jsonl").read_text().splitlines()) == 1  # ikinci cəhd gate-ə də yazılmır


def test_operations_without_op_id_are_each_charged(env):
    _, _, make, _, _ = env
    orch, led = make()
    orch.execute(task(0.2))
    orch.execute(task(0.2))
    assert led.total() == pytest.approx(0.4)
    assert len(led.entries()) == 2


@pytest.mark.parametrize("kind", ["limit", "permission", "approval", "stop"])
def test_blocked_operations_are_not_charged(env, kind):
    _, runner, make, tmp, _ = env
    orch, led = make()
    if kind == "limit":
        t = task(2.0)
    elif kind == "permission":
        t = task(0.1, permission="write_files")
    elif kind == "approval":
        t = task(0.1, permission="publish")
    else:
        (tmp / "STOP").write_text("stop")
        t = task(0.1)
    assert orch.execute(t).status == "blocked"
    assert led.total() == 0.0 and runner.calls == []


def test_blocked_op_id_can_be_retried_after_approval(env):
    _, runner, make, _, _ = env
    orch, led = make()
    assert orch.execute(task(0.1, "op-p", "publish")).status == "blocked"
    orch2, _ = make(ledger=led, approver=lambda a: True)
    assert orch2.execute(task(0.1, "op-p", "publish")).status == "ok"
    assert led.total() == pytest.approx(0.1)


def test_ledger_matches_audit_after_mixed_operations(env):
    _, _, make, tmp, _ = env
    orch, led = make()
    orch.execute(task(0.5, "a"))
    orch.execute(task(0.5, "a"))          # təkrar
    orch.execute(task(5.0, "b"))          # limit aşır
    orch.execute(task(0.1, "c", "write_files"))  # icazə yoxdur
    orch.execute(task(0.2, "d", department="satis"))
    rep = reconcile(led, tmp / "audit.jsonl")
    assert rep.ok, rep.mismatches


def test_tampered_ledger_is_detected(env):
    _, _, make, tmp, _ = env
    orch, led = make()
    orch.execute(task(0.5, "a"))
    led._conn.execute("DELETE FROM costs")
    led._conn.commit()
    assert not reconcile(led, tmp / "audit.jsonl").ok


def test_ceo_with_ledger_and_job_id_is_idempotent(env):
    _, runner, make, tmp, _ = env
    orch, led = make()
    rules = [Rule("sual", "destek", "read_files", 0.3), Rule("təklif", "satis", "write_files", 0.2)]
    ceo = CEO(orch, RulePlanner(rules), budget=5.0)
    goal = "sual və təklif"
    first = ceo.run(goal, job_id="job-1")
    assert first.status == "ok"
    again = ceo.run(goal, job_id="job-1")
    assert [s.status for s in again.steps] == ["duplicate", "duplicate"]
    assert led.total() == pytest.approx(0.5)
    assert len(runner.calls) == 2
    assert reconcile(led, tmp / "audit.jsonl").ok
    # job_id verilməyibsə hər iş yeni sayılır
    ceo.run(goal)
    assert led.total() == pytest.approx(1.0)


def test_ceo_agent_limit_enforced_by_persistent_ledger(env):
    _, _, make, _, db = env
    rules = [Rule("sual", "destek", "read_files", 0.6)]
    orch, led = make()
    assert CEO(orch, RulePlanner(rules), budget=5).run("sual").status == "ok"
    led.close()
    orch2, _ = make(ledger=CostLedger(db))
    report = CEO(orch2, RulePlanner(rules), budget=5).run("sual")
    assert report.status == "failed" and "limit" in report.failures[0].reason


def test_orchestrator_without_ledger_still_works(env):
    reg, runner, _, tmp, _ = env
    orch = Orchestrator(reg, runner, audit_path=tmp / "a.jsonl", stop_path=tmp / "S",
                        errors_path=tmp / "e.jsonl")
    assert orch.execute(task(0.6)).status == "ok"
    assert orch.execute(task(0.6)).status == "blocked"
