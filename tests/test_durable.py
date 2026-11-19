"""Davamlı workflow və təhlükəsiz davam etdirmə. Real API və ya xarici çağırış yoxdur; təsdiqləyici test
obyektidir (kind="mock"), real istifadəçi təsdiqi deyil."""
import json
import sqlite3
from pathlib import Path

import pytest

from agents.ceo import CEO, RulePlanner
from agents.durable import (
    DurableRunner, WorkflowBusy, WorkflowExists, WorkflowNotFound, WorkflowStore, WorkflowTampered,
)
from agents.orchestrator import ExecutionError, Orchestrator, TransientError
from agents.registry import Registry
from agents.spec import AgentSpec
from agents.workflow import PlanStep
from safety.approvals import ApprovalService, Principal
from tests.ledger_helpers import owned_ledger, owner_resolve
from safety.ledger import CostLedger, reconcile

MOCK = Principal("owner", "mock", "test")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Script:
    """agent adı -> növbə (Exception atılır, qalanı uğurlu). `calls` agent adlarını sayır."""

    def __init__(self, behaviors=None):
        self.behaviors = {k: list(v) for k, v in (behaviors or {}).items()}
        self.calls = []

    def __call__(self, spec, data):
        self.calls.append(spec.name)
        q = self.behaviors.get(spec.name)
        item = q.pop(0) if q else None
        if isinstance(item, BaseException):
            raise item
        return {"netice": f"{spec.name}-ok"}


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    for x in "abc":
        reg.add(AgentSpec(name=f"agent_{x}", purpose="p", tools=[], permissions=["read_files", "publish"],
                          inputs=["sorgu"], outputs=["netice"], cost_limit=50.0), f"dept_{x}")
    audit = tmp_path / "audit.jsonl"
    ledger = owned_ledger(tmp_path / "ledger.db")
    svc = ApprovalService(tmp_path / "approvals.db", audit, owners={"owner"}, allow_mock=True)
    clock = Clock()

    def build(script=None, max_retries=2):
        script = script or Script()
        orch = Orchestrator(reg, script, audit_path=audit, stop_path=tmp_path / "STOP",
                            errors_path=tmp_path / "e.jsonl", ledger=ledger, approvals=svc.client)
        ceo = CEO(orch, RulePlanner([]), budget=100.0, max_retries=max_retries)
        store = WorkflowStore(tmp_path / "wf.db", clock=clock, lease_s=60)
        return DurableRunner(ceo, store), script, store

    return build, ledger, svc, audit, clock, tmp_path


def steps(perm_b="read_files"):
    return [PlanStep("a", "dept_a", "read_files", 0.5), PlanStep("b", "dept_b", perm_b, 0.5, ("a",)),
            PlanStep("c", "dept_c", "read_files", 0.5, ("b",))]


def st(report):
    return {s.step_id: s.status for s in report.steps}


# --- başlanğıc və saxlama ---

def test_start_persists_plan_results_and_status(env):
    build, ledger, *_ = env
    runner, script, store = build()
    report = runner.start("hesabat", steps(), "j1")
    assert report.status == "ok" and script.calls == ["agent_a", "agent_b", "agent_c"]
    wf = store.load("j1")
    assert wf.status == "ok" and wf.goal == "hesabat" and [s.id for s in wf.steps] == ["a", "b", "c"]
    rec = wf.records["a"]
    assert (rec.status, rec.agent, rec.op_id, rec.output) == ("ok", "agent_a", "j1:a:a0", {"netice": "agent_a-ok"})
    with pytest.raises(WorkflowExists):
        runner.start("hesabat", steps(), "j1")
    assert len(ledger.entries()) == 3


def test_invalid_plan_is_rejected_and_not_persisted(env):
    build, *_ = env
    runner, script, store = build()
    cyc = [PlanStep("a", "dept_a", "read_files", 0, ("b",)), PlanStep("b", "dept_b", "read_files", 0, ("a",))]
    assert runner.start("g", cyc, "jx").status == "rejected" and script.calls == []
    with pytest.raises(WorkflowNotFound):
        store.load("jx")


def test_unknown_job_cannot_be_resumed(env):
    build, *_ = env
    runner, *_ = build()
    with pytest.raises(WorkflowNotFound):
        runner.resume("yoxdur")


# --- yarımçıq workflow-un davamı ---

def test_resume_after_interruption_between_steps_runs_only_the_rest(env):
    build, ledger, svc, audit, _, tmp = env
    runner, script, store = build()
    orig, n = store.record_step, {"i": 0}

    def crash_after_first(job_id, step, report):
        orig(job_id, step, report)
        n["i"] += 1
        if n["i"] == 1:
            raise KeyboardInterrupt  # proses a addımından sonra kəsildi

    store.record_step = crash_after_first
    with pytest.raises(KeyboardInterrupt):
        runner.start("g", steps(), "j1")
    assert store.load("j1").status == "interrupted" and script.calls == ["agent_a"]

    # yeni proses: yeni store/runner eyni fayl üzərində
    runner2, script2, store2 = build()
    report = runner2.resume("j1")
    assert report.status == "ok" and script2.calls == ["agent_b", "agent_c"]
    assert [(s.step_id, s.restored) for s in report.steps] == [("a", True), ("b", False), ("c", False)]
    assert report.steps[0].output == {"netice": "agent_a-ok"}
    assert len(ledger.entries()) == 3 and ledger.total() == pytest.approx(1.5)  # a təkrar hesablanmadı
    assert reconcile(ledger, audit).ok
    assert runner2.resume("j1").status == "ok" and script2.calls == ["agent_b", "agent_c"]  # artıq bitib: icra yoxdur


def test_crash_inside_a_step_blocks_resume_until_the_owner_reconciles(env):
    build, ledger, *_ = env
    runner, script, store = build(Script({"agent_b": [KeyboardInterrupt()]}))
    with pytest.raises(KeyboardInterrupt):
        runner.start("g", steps(), "j1")
    assert [e.op_id for e in ledger.unknown_entries()] == ["j1:b:a0"]
    assert store.load("j1").status == "interrupted"

    runner2, script2, _ = build()
    report = runner2.resume("j1")
    assert st(report) == {"a": "ok", "b": "skipped", "c": "skipped"} and script2.calls == []
    assert report.steps[1].needs_reconciliation and "uzlaşdır" in report.steps[1].reason
    assert [e.status for e in ledger.unknown_entries()] == ["reserved"]  # avtomatik silinmir

    assert ledger.release("j1:b:a0") is False
    assert owner_resolve(ledger, "j1:b:a0", None)  # sahib: icra olmayıb/xərc yoxdur
    runner3, script3, _ = build()
    assert runner3.resume("j1").status == "ok" and script3.calls == ["agent_b", "agent_c"]


def test_pending_approval_resumes_after_the_owner_decides(env):
    build, ledger, svc, *_ = env
    runner, script, store = build()
    first = runner.start("g", steps("publish"), "j1")
    assert st(first) == {"a": "ok", "b": "pending_approval", "c": "skipped"} and first.status == "partial"
    pending = svc.gateway.pending()
    assert len(pending) == 1 and script.calls == ["agent_a"]
    assert runner.resume("j1").steps[1].status == "pending_approval"  # hələ təsdiq yoxdur
    assert script.calls == ["agent_a"] and len(svc.gateway.pending()) == 1  # yeni sorğu açılmadı
    assert svc.gateway.approve(pending[0].id, MOCK).ok
    report = runner.resume("j1")
    assert report.status == "ok" and script.calls == ["agent_a", "agent_b", "agent_c"]
    assert svc.gateway.get(pending[0].id).status == "consumed"
    assert len(ledger.entries()) == 3


def test_executed_failures_are_terminal_unless_retry_is_requested(env):
    build, ledger, *_ = env
    runner, script, store = build(Script({"agent_b": [ExecutionError("daimi xəta", cost=0.1)]}))
    first = runner.start("g", steps(), "j1")
    assert st(first) == {"a": "ok", "b": "error", "c": "skipped"}
    again = runner.resume("j1")
    assert again.steps[1].restored and again.steps[1].status == "error" and script.calls == ["agent_a", "agent_b"]
    retried = runner.resume("j1", retry_failed=True)
    assert retried.status == "ok" and script.calls == ["agent_a", "agent_b", "agent_b", "agent_c"]
    assert sorted(e.op_id for e in ledger.entries()) == ["j1:a:a0", "j1:b:a0", "j1:b:a1", "j1:c:a0"]


def test_unknown_cost_failure_stays_blocked_even_for_retry_until_reconciled(env):
    build, ledger, *_ = env
    runner, script, store = build(Script({"agent_b": [TransientError("naməlum")]}))
    first = runner.start("g", steps(), "j1")
    assert first.steps[1].needs_reconciliation
    blocked = runner.resume("j1", retry_failed=True)
    assert blocked.steps[1].status == "skipped" and "uzlaşdır" in blocked.steps[1].reason
    assert script.calls == ["agent_a", "agent_b"]
    assert ledger.commit("j1:b:a0", 0.2) is False
    assert owner_resolve(ledger, "j1:b:a0", 0.2)  # xərc sahib tərəfindən təsdiqləndi
    final = runner.resume("j1", retry_failed=True)
    assert final.status == "ok" and "j1:b:a1" in [e.op_id for e in ledger.entries()]


def test_retry_limit_holds_across_resumes(env):
    build, *_ = env
    errs = [ExecutionError("x", cost=0.0) for _ in range(5)]
    runner, script, store = build(Script({"agent_a": errs}), max_retries=1)
    runner.start("g", steps(), "j1")           # cəhd a0
    runner.resume("j1", retry_failed=True)     # cəhd a1
    third = runner.resume("j1", retry_failed=True)
    assert script.calls == ["agent_a", "agent_a"] and "retry limiti" in third.steps[0].reason


# --- təhlükəsizlik ---

def test_tampered_plan_is_refused_and_nothing_runs(env):
    build, _, _, _, _, tmp = env
    runner, script, store = build(Script({"agent_b": [ExecutionError("x", cost=0.0)]}))
    runner.start("g", steps(), "j1")
    conn = sqlite3.connect(tmp / "wf.db")
    conn.execute("UPDATE workflows SET plan_json = replace(plan_json, 'publish', 'spend_money') "
                 "WHERE job_id = 'j1'")
    conn.execute("UPDATE workflows SET plan_json = replace(plan_json, 'read_files', 'publish')")
    conn.commit()
    conn.close()
    calls = list(script.calls)
    with pytest.raises(WorkflowTampered):
        runner.resume("j1", retry_failed=True)
    assert script.calls == calls


def test_resume_uses_the_stored_goal_and_plan(env):
    build, *_ = env
    runner, script, store = build()
    runner.start("orijinal məqsəd", steps(), "j1")
    store2 = store.load("j1")
    assert store2.goal == "orijinal məqsəd"
    assert runner.resume("j1").goal == "orijinal məqsəd"


def test_lease_prevents_concurrent_resume_and_recovers_after_expiry(env):
    build, _, _, _, clock, _ = env
    runner, script, store = build(Script({"agent_b": [ExecutionError("x", cost=0.0)]}))
    runner.start("g", steps(), "j1")
    store.acquire("j1")  # başqa proses davam etdirir
    with pytest.raises(WorkflowBusy):
        runner.resume("j1", retry_failed=True)
    clock.t += 61  # lease bitdi (proses çöküb)
    assert runner.resume("j1", retry_failed=True).status == "ok"


def test_stop_flag_keeps_the_job_resumable(env):
    build, _, _, _, _, tmp = env
    runner, script, store = build()
    (tmp / "STOP").write_text("stop")
    first = runner.start("g", steps(), "j1")
    assert st(first) == {"a": "skipped", "b": "skipped", "c": "skipped"} and script.calls == []
    assert store.load("j1").status == "failed"
    (tmp / "STOP").unlink()
    assert runner.resume("j1").status == "ok" and script.calls == ["agent_a", "agent_b", "agent_c"]


def test_run_audit_and_ledger_stay_consistent_after_resume(env):
    build, ledger, svc, audit, _, _ = env
    runner, script, store = build()
    first = runner.start("g", steps("publish"), "j1")
    svc.gateway.approve(svc.gateway.pending()[0].id, MOCK)
    runner.resume("j1")
    assert reconcile(ledger, audit).ok
    rows = [json.loads(l) for l in Path(audit).read_text().splitlines()]
    assert [r["allowed"] for r in rows if "event" not in r] == [True, True, True]
