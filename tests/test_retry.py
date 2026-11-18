import json

import pytest

from agents.ceo import CEO, RulePlanner
from agents.orchestrator import ExecutionError, Orchestrator, TransientError
from agents.registry import Registry
from agents.spec import AgentSpec
from agents.workflow import PlanStep
from tests.ledger_helpers import owned_ledger, owner_resolve
from safety.ledger import BudgetPolicy, CostLedger, reconcile


def spec(name):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=["read_files"],
                     inputs=["sorgu"], outputs=["netice"], cost_limit=5.0)


class Script:
    """agent adı -> növbə: Exception atılır, qalan hallarda uğurlu çıxış."""

    def __init__(self, behaviors=None):
        self.behaviors = {k: list(v) for k, v in (behaviors or {}).items()}
        self.calls = []

    def __call__(self, spec, data):
        self.calls.append(spec.name)
        queue = self.behaviors.get(spec.name)
        item = queue.pop(0) if queue else None
        if isinstance(item, Exception):
            raise item
        return {"netice": "ok"}


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    for x in "abcd":
        reg.add(spec(f"agent_{x}"), f"dept_{x}")
    ledger = owned_ledger(tmp_path / "ledger.db")
    paths = dict(audit_path=tmp_path / "audit.jsonl", stop_path=tmp_path / "STOP",
                 errors_path=tmp_path / "errors.jsonl")

    def make(script=None, policy=None, max_retries=2, checks=None, approver=None):
        script = script or Script()
        orch = Orchestrator(reg, script, ledger=ledger, policy=policy, approver=approver, **paths)
        return CEO(orch, RulePlanner([]), budget=100.0, max_retries=max_retries, checks=checks), script

    return reg, ledger, make, tmp_path


def st(id, deps=(), cost=0.5, perm="read_files"):
    return PlanStep(id, f"dept_{id}", perm, cost, tuple(deps))


def statuses(report):
    return {s.step_id: s.status for s in report.steps}


def audit_rows(tmp):
    p = tmp / "audit.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# --- sıra və doğrulama ---

def test_steps_execute_in_dependency_order(env):
    _, _, make, _ = env
    ceo, script = make()
    report = ceo.run_workflow("g", [st("c", "ab"), st("b", "a"), st("a")], job_id="j1")
    assert report.status == "ok"
    assert script.calls == ["agent_a", "agent_b", "agent_c"]
    assert [s.step_id for s in report.steps] == ["a", "b", "c"]


@pytest.mark.parametrize("steps,word", [
    ([st("a", "b"), st("b", "a")], "dövr"),
    ([st("a", "yoxdur")], "olmayan"),
])
def test_invalid_graph_rejected_before_anything_runs(env, steps, word):
    _, ledger, make, tmp = env
    ceo, script = make()
    report = ceo.run_workflow("g", steps, job_id="j1")
    assert report.status == "rejected" and word in report.reason
    assert script.calls == [] and ledger.entries() == [] and audit_rows(tmp) == []


# --- asılılıq uğursuzluğu ---

def test_failed_step_stops_its_dependents_only(env):
    _, _, make, _ = env
    ceo, script = make(Script({"agent_a": [RuntimeError("daimi xəta")]}))
    steps = [st("a"), st("b", "a"), st("c", "b"), st("d")]
    report = ceo.run_workflow("g", steps, job_id="j1")
    assert statuses(report) == {"a": "error", "b": "skipped", "c": "skipped", "d": "ok"}
    assert report.status == "partial"
    assert "a" in next(s for s in report.steps if s.step_id == "b").reason
    assert script.calls == ["agent_a", "agent_d"]


def test_blocked_step_stops_dependents(env):
    _, _, make, tmp = env
    ceo, script = make()
    report = ceo.run_workflow("g", [st("a", perm="write_files"), st("b", "a")], job_id="j1")
    assert statuses(report) == {"a": "blocked", "b": "skipped"}
    assert script.calls == []
    assert audit_rows(tmp)[0]["allowed"] is False  # gate qərarı yazılıb


def test_check_failure_stops_dependents_and_is_not_retried(env):
    _, _, make, _ = env
    ceo, script = make(checks={"dept_a": lambda out: False})
    report = ceo.run_workflow("g", [st("a"), st("b", "a")], job_id="j1")
    assert statuses(report) == {"a": "failed_check", "b": "skipped"}
    assert script.calls == ["agent_a"]


def test_stop_flag_skips_remaining(env):
    _, _, make, tmp = env
    ceo, script = make()
    (tmp / "STOP").write_text("stop")
    report = ceo.run_workflow("g", [st("a"), st("b", "a")], job_id="j1")
    assert statuses(report) == {"a": "skipped", "b": "skipped"}
    assert script.calls == []


# --- retry ---

def test_transient_error_is_retried_with_unique_op_ids_and_costs(env):
    _, ledger, make, _ = env
    err = lambda: TransientError("müvəqqəti", cost=0.1)
    ceo, script = make(Script({"agent_a": [err(), err()]}))
    report = ceo.run_workflow("g", [st("a")], job_id="j1")
    step = report.steps[0]
    assert (step.status, step.attempts) == ("ok", 3)
    assert script.calls == ["agent_a"] * 3
    assert sorted(e.op_id for e in ledger.entries()) == ["j1:a:a0", "j1:a:a1", "j1:a:a2"]
    assert ledger.total() == pytest.approx(0.1 + 0.1 + 0.5)  # uğursuz cəhdlər real xərclə


def test_retries_stop_after_maximum(env):
    _, ledger, make, _ = env
    errs = [TransientError("x", cost=0.0) for _ in range(5)]
    ceo, script = make(Script({"agent_a": errs}))
    report = ceo.run_workflow("g", [st("a")], job_id="j1")
    assert (report.steps[0].status, report.steps[0].attempts) == ("error", 3)
    assert script.calls == ["agent_a"] * 3  # 1 + 2 retry


@pytest.mark.parametrize("retries,calls", [(0, 1), (1, 2), (2, 3)])
def test_retry_count_is_configurable(env, retries, calls):
    _, _, make, _ = env
    errs = [TransientError("x", cost=0.0) for _ in range(5)]
    ceo, script = make(Script({"agent_a": errs}), max_retries=retries)
    ceo.run_workflow("g", [st("a")], job_id="j1")
    assert len(script.calls) == calls


@pytest.mark.parametrize("bad", [-1, 3, 1.5, True, None])
def test_invalid_max_retries_rejected(env, bad):
    _, _, make, _ = env
    with pytest.raises(ValueError):
        make(max_retries=bad)


@pytest.mark.parametrize("exc", [
    RuntimeError("daimi"),
    ValueError("daimi"),
    ExecutionError("daimi", cost=0.1),
])
def test_only_transient_errors_are_retried(env, exc):
    _, _, make, _ = env
    ceo, script = make(Script({"agent_a": [exc, None]}))
    report = ceo.run_workflow("g", [st("a")], job_id="j1")
    assert report.steps[0].status == "error" and report.steps[0].attempts == 1
    assert script.calls == ["agent_a"]


@pytest.mark.parametrize("exc", [TimeoutError("t"), ConnectionError("c")])
def test_builtin_transient_errors_without_cost_are_not_retried(env, exc):
    _, _, make, _ = env
    ceo, script = make(Script({"agent_a": [exc, None]}))
    report = ceo.run_workflow("g", [st("a")], job_id="j1")
    assert script.calls == ["agent_a"]  # xərc naməlum -> avtomatik retry yoxdur
    assert report.steps[0].needs_reconciliation


# --- naməlum xərc ---

def test_unknown_cost_is_never_auto_retried_and_requires_manual_reconciliation(env):
    _, ledger, make, _ = env
    ceo, script = make(Script({"agent_a": [TransientError("naməlum"), None]}),
                       policy=BudgetPolicy(global_limit=10.0))
    report = ceo.run_workflow("g", [st("a"), st("b", "a")], job_id="j1")
    step = report.steps[0]
    assert (step.status, step.attempts, step.needs_reconciliation) == ("error", 1, True)
    assert statuses(report)["b"] == "skipped"
    assert script.calls == ["agent_a"]
    assert [s.op_id for s in report.needs_reconciliation] == ["j1:a:a0"]
    assert "uzlaşdır" in report.summary()
    # rezerv avtomatik silinmir
    e = ledger.unknown_entries()
    assert [(x.op_id, x.status, x.amount) for x in e] == [("j1:a:a0", "reserved", 0.5)]
    assert ledger.total() == pytest.approx(0.5)


def test_manual_reconciliation_clears_unknown(env):
    _, ledger, make, _ = env
    ceo, _ = make(Script({"agent_a": [TransientError("naməlum")]}))
    ceo.run_workflow("g", [st("a")], job_id="j1")
    assert ledger.unknown_entries()
    assert ledger.commit("j1:a:a0", 0.3) is False
    assert owner_resolve(ledger, "j1:a:a0", 0.3)  # sahib təsdiqi ilə uzlaşdırma
    assert ledger.unknown_entries() == [] and ledger.total() == pytest.approx(0.3)


# --- təkrar hesablanma ---

def test_rerun_with_same_job_id_is_not_charged_twice(env):
    _, ledger, make, _ = env
    ceo, script = make()
    steps = [st("a"), st("b", "a")]
    assert ceo.run_workflow("g", steps, job_id="j1").status == "ok"
    total = ledger.total()
    again = ceo.run_workflow("g", steps, job_id="j1")
    assert statuses(again) == {"a": "duplicate", "b": "skipped"}
    assert ledger.total() == total and len(script.calls) == 2


def test_jobs_without_job_id_get_distinct_op_ids(env):
    _, ledger, make, _ = env
    ceo, _ = make()
    ceo.run_workflow("g", [st("a")])
    ceo.run_workflow("g", [st("a")])
    assert len(ledger.entries()) == 2


# --- büdcə ---

def test_budget_block_on_retry_stops_retries_and_is_audited(env):
    _, ledger, make, tmp = env
    errs = [TransientError("x", cost=0.4) for _ in range(5)]
    ceo, script = make(Script({"agent_a": errs}), policy=BudgetPolicy(global_limit=1.0))
    report = ceo.run_workflow("g", [st("a", cost=0.5)], job_id="j1")
    step = report.steps[0]
    assert step.status == "blocked" and "limit" in step.reason
    assert script.calls == ["agent_a"] * 2  # üçüncü cəhd runner-ə çatmadı
    denials = [r for r in audit_rows(tmp) if not r["allowed"]]
    assert len(denials) == 1
    assert denials[0]["agent"] == "agent_a" and "limit" in denials[0]["reason"]
    assert denials[0]["source"] == "budget"
    assert reconcile(ledger, tmp / "audit.jsonl").ok


def test_plan_over_global_room_is_rejected_and_audited(env):
    _, ledger, make, tmp = env
    ceo, script = make(policy=BudgetPolicy(global_limit=1.0))
    report = ceo.run_workflow("g", [st("a", cost=0.6), st("b", cost=0.6)], job_id="j1")
    assert report.status == "rejected" and "ümumi" in report.reason
    assert script.calls == []
    rows = audit_rows(tmp)
    assert len(rows) == 1 and rows[0]["allowed"] is False and rows[0]["source"] == "budget"
    assert reconcile(ledger, tmp / "audit.jsonl").ok


def test_task_limit_rejection_is_audited(env):
    _, _, make, tmp = env
    ceo, script = make(policy=BudgetPolicy(task_limit=0.3))
    report = ceo.run_workflow("g", [st("a", cost=0.5)], job_id="j1")
    assert report.status == "rejected" and "tapşırıq" in report.reason
    assert audit_rows(tmp)[0]["source"] == "budget"


def test_orchestrator_budget_denial_is_audited_without_gate_change(env):
    _, ledger, make, tmp = env
    ceo, script = make(policy=BudgetPolicy(agent_limits={"agent_a": 0.1}))
    report = ceo.run_workflow("g", [st("a", cost=0.5)], job_id="j1")
    assert report.steps[0].status == "blocked"
    rows = audit_rows(tmp)
    assert rows[0]["allowed"] is False and rows[0]["permission"] == "read_files"
    assert script.calls == []


def test_reconcile_ok_after_mixed_workflow(env):
    _, ledger, make, tmp = env
    ceo, _ = make(Script({"agent_a": [TransientError("x", cost=0.1)],
                          "agent_c": [TransientError("naməlum")]}),
                  policy=BudgetPolicy(global_limit=50.0))
    ceo.run_workflow("g", [st("a"), st("b", "a"), st("c")], job_id="j1")
    assert reconcile(ledger, tmp / "audit.jsonl").ok
