import json

import pytest

from agents.orchestrator import Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec


def spec(name="tasnif_agenti", perms=("read_files",), limit=1.0, outputs=("kateqoriya",)):
    return AgentSpec(
        name=name,
        purpose="p",
        tools=[],
        permissions=list(perms),
        inputs=["sual"],
        outputs=list(outputs),
        cost_limit=limit,
    )


class Runner:
    def __init__(self, fail=None, output=None):
        self.calls = []
        self.fail = fail
        self.output = output

    def __call__(self, spec, data):
        self.calls.append((spec.name, data))
        if self.fail:
            raise self.fail
        return self.output if self.output is not None else {"kateqoriya": "x"}


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    reg.add(spec(), "destek")
    runner = Runner()

    def make(approver=None, r=runner):
        return Orchestrator(
            reg,
            r,
            audit_path=tmp_path / "audit.jsonl",
            stop_path=tmp_path / "STOP",
            errors_path=tmp_path / "errors.jsonl",
            approver=approver,
        )

    return reg, runner, make, tmp_path


def lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def task(**kw):
    base = dict(department="destek", input={"sual": "salam"}, permission="read_files")
    base.update(kw)
    return Task(**base)


def test_routes_to_department_agent_and_runs(env):
    reg, runner, make, _ = env
    res = make().execute(task())
    assert res.status == "ok"
    assert res.agent == "tasnif_agenti"
    assert res.output == {"kateqoriya": "x"}
    assert runner.calls == [("tasnif_agenti", {"sual": "salam"})]


def test_routes_by_department(env):
    reg, runner, make, _ = env
    reg.add(spec("satis_agenti"), "satis")
    assert make().execute(task(department="satis")).agent == "satis_agenti"


def test_unknown_department_is_unrouted(env):
    _, runner, make, _ = env
    res = make().execute(task(department="yoxdur"))
    assert res.status == "unrouted"
    assert runner.calls == []


def test_agent_with_missing_inputs_is_not_routed(env):
    _, runner, make, _ = env
    res = make().execute(task(input={"başqa": 1}))
    assert res.status == "unrouted"
    assert runner.calls == []


def test_prefers_agent_that_holds_permission(env):
    reg, runner, make, _ = env
    reg.add(spec("a_yazan", perms=("write_files",)), "destek")
    res = make().execute(task(permission="write_files"))
    assert res.status == "ok"
    assert res.agent == "a_yazan"


def test_missing_permission_blocked_and_runner_not_called(env):
    _, runner, make, tmp = env
    res = make().execute(task(permission="write_files"))
    assert res.status == "blocked"
    assert "icazə" in res.reason
    assert runner.calls == []
    assert lines(tmp / "audit.jsonl")[-1]["allowed"] is False


@pytest.mark.parametrize("perm", ["spend_money", "external_account", "publish"])
def test_sensitive_blocked_without_approval(env, perm):
    reg, runner, make, _ = env
    reg.update(spec(perms=("read_files", perm), limit=10))
    res = make().execute(task(permission=perm))
    assert res.status == "blocked"
    assert "təsdiq" in res.reason
    assert runner.calls == []


def test_sensitive_runs_with_approval(env):
    reg, runner, make, _ = env
    reg.update(spec(perms=("publish",)))
    res = make(approver=lambda a: True).execute(task(permission="publish"))
    assert res.status == "ok"
    assert len(runner.calls) == 1


def test_cost_limit_enforced_across_tasks(env):
    _, runner, make, _ = env
    orch = make()
    assert orch.execute(task(cost=0.6)).status == "ok"
    res = orch.execute(task(cost=0.6))
    assert res.status == "blocked"
    assert "limit" in res.reason
    assert len(runner.calls) == 1


def test_stop_flag_blocks_all_agents(env):
    reg, runner, make, tmp = env
    reg.add(spec("satis_agenti"), "satis")
    orch = make()
    (tmp / "STOP").write_text("stop")
    assert orch.execute(task()).status == "blocked"
    assert orch.execute(task(department="satis")).status == "blocked"
    assert runner.calls == []


def test_registry_update_takes_effect_immediately(env):
    reg, runner, make, _ = env
    orch = make()
    assert orch.execute(task()).status == "ok"
    reg.update(spec(perms=()))  # icazə geri alındı
    assert orch.execute(task()).status == "blocked"
    assert len(runner.calls) == 1


def test_runner_exception_becomes_error_and_is_logged(env):
    _, _, make, tmp = env
    res = make(r=Runner(fail=RuntimeError("partladı"))).execute(task())
    assert res.status == "error"
    assert "partladı" in res.reason
    row = lines(tmp / "errors.jsonl")[0]
    assert row["agent"] == "tasnif_agenti"
    assert row["stage"] == "run"


def test_missing_declared_output_is_error(env):
    _, _, make, tmp = env
    res = make(r=Runner(output={"başqa": 1})).execute(task())
    assert res.status == "error"
    assert lines(tmp / "errors.jsonl")[0]["stage"] == "output"


def test_invalid_task_rejected_without_running(env):
    _, runner, make, _ = env
    for bad in (task(department=""), task(permission=""), task(cost=-1)):
        assert make().execute(bad).status == "blocked"
    assert runner.calls == []


def test_every_decision_is_audited(env):
    _, _, make, tmp = env
    orch = make()
    orch.execute(task())
    orch.execute(task(permission="write_files"))
    assert [r["allowed"] for r in lines(tmp / "audit.jsonl")] == [True, False]
