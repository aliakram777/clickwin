import json

import pytest

from agents.ceo import CEO, Rule, RulePlanner
from agents.orchestrator import Orchestrator
from agents.registry import Registry
from agents.spec import AgentSpec


def spec(name, inputs, perms, limit=5.0):
    return AgentSpec(
        name=name, purpose="p", tools=[], permissions=list(perms),
        inputs=list(inputs), outputs=["netice"], cost_limit=limit,
    )


class Runner:
    def __init__(self, fail_for=(), on_call=None):
        self.calls = []
        self.fail_for = set(fail_for)
        self.on_call = on_call

    def __call__(self, spec, data):
        self.calls.append((spec.name, data))
        if self.on_call:
            self.on_call(spec.name)
        if spec.name in self.fail_for:
            raise RuntimeError(f"{spec.name} çökdü")
        return {"netice": f"{spec.name}-ok"}


RULES = [
    Rule("sual", "destek", "read_files", 1.0),
    Rule("təklif", "satis", "write_files", 1.0),
    Rule("ödəniş", "maliyye", "spend_money", 1.0),
]
GOAL = "müştəri sualı və satış təklifi hazırla"


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    reg.add(spec("tasnif_agenti", ["sual"], ["read_files"]), "destek")
    reg.add(spec("satis_agenti", ["sorgu"], ["read_files", "write_files"]), "satis")
    reg.add(spec("pay_agenti", ["sorgu"], ["read_files", "spend_money"], 10), "maliyye")
    runner = Runner()
    paths = dict(
        audit_path=tmp_path / "audit.jsonl",
        stop_path=tmp_path / "STOP",
        errors_path=tmp_path / "errors.jsonl",
    )

    def make(runner=runner, budget=10.0, rules=RULES, checks=None, approver=None):
        orch = Orchestrator(reg, runner, approver=approver, **paths)
        return CEO(orch, RulePlanner(rules), budget=budget, checks=checks)

    return reg, runner, make, tmp_path


def lines(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_analyzes_selects_dispatches_and_collects(env):
    _, runner, make, _ = env
    report = make().run(GOAL)
    assert report.status == "ok" and report.ok
    assert [s.department for s in report.steps] == ["destek", "satis"]
    assert report.outputs == {
        "destek": {"netice": "tasnif_agenti-ok"},
        "satis": {"netice": "satis_agenti-ok"},
    }
    # CEO hər şöbə agentinin elan etdiyi girişləri doldurur
    assert dict(runner.calls)["tasnif_agenti"] == {"sual": GOAL}
    assert dict(runner.calls)["satis_agenti"] == {"sorgu": GOAL}


def test_no_matching_rule_means_no_plan(env):
    _, runner, make, tmp = env
    report = make().run("tamamilə əlaqəsiz mətn")
    assert report.status == "no_plan"
    assert runner.calls == []
    assert lines(tmp / "audit.jsonl") == []


def test_empty_goal_rejected(env):
    _, runner, make, _ = env
    assert make().run("   ").status == "no_plan"
    assert runner.calls == []


def test_unknown_department_is_reported_others_still_run(env):
    _, runner, make, _ = env
    rules = RULES + [Rule("sual", "yoxdur", "read_files", 0.0)]
    report = make(rules=rules).run(GOAL)
    assert report.status == "partial"
    bad = [s for s in report.failures]
    assert [(s.department, s.status) for s in bad] == [("yoxdur", "unrouted")]
    assert len(runner.calls) == 2


def test_plan_over_budget_rejected_before_any_dispatch(env):
    _, runner, make, tmp = env
    report = make(budget=1.5).run(GOAL)  # plan 2.0 > 1.5
    assert report.status == "rejected"
    assert "büdcə" in report.reason
    assert runner.calls == []
    assert lines(tmp / "audit.jsonl") == []


def test_agent_cost_limit_blocks_step(env):
    reg, runner, make, _ = env
    reg.update(spec("tasnif_agenti", ["sual"], ["read_files"], limit=0.5))
    report = make().run(GOAL)
    assert report.status == "partial"
    step = report.failures[0]
    assert step.status == "blocked" and "limit" in step.reason
    assert [c[0] for c in runner.calls] == ["satis_agenti"]


def test_missing_permission_blocks_step(env):
    reg, runner, make, _ = env
    reg.update(spec("satis_agenti", ["sorgu"], ["read_files"]))
    report = make().run(GOAL)
    assert report.failures[0].status == "blocked"
    assert "icazə" in report.failures[0].reason


@pytest.mark.parametrize("approved", [False, True])
def test_money_step_needs_owner_approval(env, approved):
    _, runner, make, _ = env
    report = make(approver=lambda a: approved).run("ödəniş et")
    if approved:
        assert report.status == "ok"
        assert len(runner.calls) == 1
    else:
        assert report.status == "failed"
        assert "təsdiq" in report.failures[0].reason
        assert runner.calls == []


def test_stop_flag_halts_remaining_steps(env):
    _, _, make, tmp = env
    runner = Runner(on_call=lambda name: (tmp / "STOP").write_text("stop"))
    report = make(runner=runner).run(GOAL)
    assert [s.status for s in report.steps] == ["ok", "skipped"]
    assert [c[0] for c in runner.calls] == ["tasnif_agenti"]


def test_failed_agent_is_reported_and_others_continue(env):
    _, _, make, tmp = env
    runner = Runner(fail_for={"tasnif_agenti"})
    report = make(runner=runner).run(GOAL)
    assert report.status == "partial" and not report.ok
    fail = report.failures[0]
    assert (fail.agent, fail.status) == ("tasnif_agenti", "error")
    assert "çökdü" in fail.reason
    assert "satis" in report.outputs and "destek" not in report.outputs
    assert lines(tmp / "errors.jsonl")[0]["agent"] == "tasnif_agenti"
    assert "tasnif_agenti" in report.summary()


def test_all_steps_failing_gives_failed_status(env):
    _, _, make, _ = env
    runner = Runner(fail_for={"tasnif_agenti", "satis_agenti"})
    assert make(runner=runner).run(GOAL).status == "failed"


def test_output_check_rejects_bad_result_and_logs(env):
    _, _, make, tmp = env
    checks = {"satis": lambda out: out["netice"] == "gözlənilməyən"}
    report = make(checks=checks).run(GOAL)
    step = report.failures[0]
    assert (step.department, step.status) == ("satis", "failed_check")
    assert "satis" not in report.outputs and "destek" in report.outputs
    assert lines(tmp / "errors.jsonl")[-1]["stage"] == "check"


def test_check_exception_counts_as_failure(env):
    _, _, make, _ = env

    def boom(out):
        raise KeyError("yoxdur")

    report = make(checks={"destek": boom}).run(GOAL)
    assert report.failures[0].status == "failed_check"


def test_every_dispatch_passes_the_gate(env):
    reg, runner, make, tmp = env
    reg.update(spec("satis_agenti", ["sorgu"], ["read_files"]))  # biri bloklanacaq
    make().run(GOAL)
    audit = lines(tmp / "audit.jsonl")
    assert len(audit) == 2  # hər iki addım gate-dən keçib
    assert sum(r["allowed"] for r in audit) == len(runner.calls) == 1


def test_invalid_budget_rejected(env):
    _, _, make, _ = env
    for bad in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            make(budget=bad)
