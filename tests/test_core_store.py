"""Mərhələ 18.2-B: GoalStore (kernel.Goal/Plan üçün davamlı SQLite). Şəbəkə YOXDUR."""
from __future__ import annotations

import dataclasses
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import core
from core import (EngineGoalStore, NotPermitted, OpConflict, OwnerGoalStore, StaleVersion, StepState, StoreIntegrityError, build_store, safe_summary)
from kernel import (ContractError, EventLog, Goal, GoalStatus, InvalidTransition, Origin, Plan, PlanStatus, PlanStepSpec, Priority, UnsupportedVersion)
from safety.dbconn import connect

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FAKE_KEY = "sk-" + "Rt6V" * 10                       # çalışma vaxtında qurulur


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def step(sid="s1", cap="note.record", **kw) -> PlanStepSpec:
    base = dict(step_id=sid, title="Addım", expected_outcome="Nəticə", verification_criteria=("yoxla",), capability_id=cap)
    base.update(kw)
    return PlanStepSpec(**base)


def goal(title="Məqsəd ğüşəı 🙂", **kw) -> Goal:
    kw.setdefault("origin", Origin.OWNER)
    kw.setdefault("now", T0)
    return Goal.new(title, **kw)


def plan_for(g: Goal, *steps: PlanStepSpec, origin=Origin.LLM) -> Plan:
    return Plan.new(g.goal_id, tuple(steps) or (step(),), origin=origin, now=T0)


@pytest.fixture()
def world():
    clock = Clock()
    conn = connect(":memory:")
    faces = build_store(conn, clock)
    yield faces.engine, faces.owner, clock, conn
    conn.close()


def active(owner: OwnerGoalStore, g: Goal | None = None) -> Goal:
    g = g or goal()
    owner.create_goal(g, f"goal.create:{g.goal_id}")
    owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, f"goal.act:{g.goal_id}", 1)
    return g


# ------------------------------------------------------------------ məqsəd
def test_goal_roundtrip_unicode_and_persistence(tmp_path):
    path = str(tmp_path / "core.db")
    conn = connect(path)
    f = build_store(conn)
    g = goal("Azərbaycan: ə ğ ı ö ü ş ç İ 🙂 é", priority=Priority.HIGH)
    assert f.owner.create_goal(g, "goal.create:a1").duplicate is False
    conn.close()
    conn = connect(path)
    f2 = build_store(conn)
    got, version = f2.owner.get_goal(g.goal_id)
    assert got == g and version == 1 and f2.engine.list_goals()[0][0] == g
    conn.close()


def test_new_goals_must_be_draft_and_creation_is_idempotent(world):
    engine, owner, _, _ = world
    g = goal()
    with pytest.raises(NotPermitted):
        owner.create_goal(dataclasses.replace(g, status=GoalStatus.ACTIVE), "goal.create:x1")
    assert owner.create_goal(g, "goal.create:op1").duplicate is False
    assert owner.create_goal(g, "goal.create:op1").duplicate is True and len(owner.list_goals()) == 1
    with pytest.raises(OpConflict):
        owner.create_goal(goal("başqa"), "goal.create:op1")                       # eyni op_id, fərqli məzmun
    with pytest.raises(OpConflict):
        engine.create_goal(g, "goal.create:op2")                                    # eyni goal_id, yeni op_id
    for bad in ("", "BAD OP", "x", None, 5):
        with pytest.raises(ContractError):
            owner.create_goal(goal(), bad)                                          # type: ignore[arg-type]


def test_secrets_and_bad_text_rejected_by_contracts_before_storage(world):
    _, owner, _, _ = world
    with pytest.raises(ContractError):
        goal(f"açar {FAKE_KEY}")
    assert owner.list_goals() == []


def test_engine_cannot_activate_complete_cancel_or_resume_goals(world):
    engine, owner, _, _ = world
    g = goal()
    owner.create_goal(g, "goal.create:a")
    for to in (GoalStatus.ACTIVE, GoalStatus.CANCELLED):
        with pytest.raises(NotPermitted):
            engine.transition_goal(g.goal_id, to, f"goal.t:{to.value}", 1)
    owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, "goal.act:a", 1)
    with pytest.raises(NotPermitted):
        engine.transition_goal(g.goal_id, GoalStatus.COMPLETED, "goal.done", 2)
    assert engine.transition_goal(g.goal_id, GoalStatus.BLOCKED, "goal.block", 2, reason="səbəb").version == 3
    with pytest.raises(NotPermitted):
        engine.transition_goal(g.goal_id, GoalStatus.ACTIVE, "goal.unblock", 3)       # blokdan çıxarmaq yalnız sahib
    assert owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, "goal.unblock", 3).version == 4
    assert engine.get_goal(g.goal_id)[0].status is GoalStatus.ACTIVE


def test_kernel_transition_rules_still_apply_and_terminal_states_are_final(world):
    _, owner, _, _ = world
    g = active(owner)
    with pytest.raises(InvalidTransition):
        owner.transition_goal(g.goal_id, GoalStatus.DRAFT, "goal.back", 2)
    owner.transition_goal(g.goal_id, GoalStatus.COMPLETED, "goal.done", 2)
    for to in GoalStatus:
        with pytest.raises(InvalidTransition):
            owner.transition_goal(g.goal_id, to, f"goal.after:{to.value}", 3)


def test_optimistic_versioning_and_op_idempotency(world):
    _, owner, _, _ = world
    g = goal()
    owner.create_goal(g, "goal.create:a")
    owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, "goal.act", 1)
    with pytest.raises(StaleVersion):
        owner.transition_goal(g.goal_id, GoalStatus.PAUSED, "goal.pause", 1)           # köhnə versiya
    assert owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, "goal.act", 1).duplicate is True      # eyni op təkrarı: dublikat
    with pytest.raises(OpConflict):
        owner.transition_goal(g.goal_id, GoalStatus.PAUSED, "goal.act", 1)             # eyni op_id, fərqli məqsəd
    assert owner.get_goal(g.goal_id)[1] == 2
    with pytest.raises(ContractError):
        owner.get_goal("nope") or owner.transition_goal("goal_nope000000000000", GoalStatus.ACTIVE, "goal.x1", 1)


def test_concurrent_transitions_exactly_one_wins(world):
    _, owner, _, _ = world
    g = goal()
    owner.create_goal(g, "goal.create:a")
    out = []

    def w(i):
        try:
            owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, f"goal.act:{i}", 1)
            out.append("ok")
        except StaleVersion:
            out.append("stale")
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("ok") == 1 and out.count("stale") == 7


def test_faces_are_separate_and_immutable(world):
    engine, owner, _, _ = world
    for name in ("resolve_step", "revise_plan"):
        assert not hasattr(engine, name)
    for name in ("begin_step", "finish_step", "recover_expired", "flush_outbox", "mark_awaiting", "block_step"):
        assert not hasattr(owner, name)
    with pytest.raises(AttributeError):
        engine.anything = 1                                                            # type: ignore[attr-defined]
    assert isinstance(engine, EngineGoalStore) and isinstance(owner, OwnerGoalStore)


def test_tampered_rows_are_detected_on_read(tmp_path):
    path = str(tmp_path / "t.db")
    conn = connect(path)
    f = build_store(conn)
    g = goal()
    f.owner.create_goal(g, "goal.create:a")
    conn.execute("UPDATE goals SET doc = replace(doc, 'Məqsəd', 'HACK')")
    conn.execute("UPDATE goals SET doc = replace(doc, 'M\\u0259qs\\u0259d', 'HACK')")
    with pytest.raises(StoreIntegrityError):
        f.owner.get_goal(g.goal_id)
    conn.close()


def test_future_schema_version_rejected():
    conn = connect(":memory:")
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(UnsupportedVersion):
        build_store(conn)


# ------------------------------------------------------------------ planlar
def test_plan_rules_active_goal_single_active_plan_and_events(world):
    engine, owner, _, _ = world
    g = goal()
    owner.create_goal(g, "goal.create:a")
    with pytest.raises(NotPermitted):
        engine.add_plan(plan_for(g), "plan.add:early")                                 # məqsəd hələ DRAFT
    owner.transition_goal(g.goal_id, GoalStatus.ACTIVE, "goal.act", 1)
    p = plan_for(g)
    assert engine.add_plan(p, "plan.add:p1").version == 1 and engine.add_plan(p, "plan.add:p1").duplicate is True
    with pytest.raises(NotPermitted):
        engine.add_plan(plan_for(g), "plan.add:p2")                                    # artıq aktiv plan var
    with pytest.raises(NotPermitted):
        engine.add_plan(dataclasses.replace(plan_for(g), status=PlanStatus.READY), "plan.add:p3")
    assert engine.current_plan(g.goal_id).plan_id == p.plan_id and [s.state for s in engine.steps(p.plan_id)] == [StepState.PENDING]


def test_engine_plan_transitions_require_validation_hash_and_completion_needs_steps(world):
    engine, owner, _, _ = world
    g = active(owner)
    p = plan_for(g, step("a"), step("b", depends_on=("a",)))
    engine.add_plan(p, "plan.add:p1")
    with pytest.raises(NotPermitted):
        engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready:1", 1)                                  # yoxlama hash-i yoxdur
    with pytest.raises(NotPermitted):
        engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready:2", 1, validated_hash="0" * 64)          # başqa məzmun
    engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready:3", 1, validated_hash=p.content_hash)
    for to in (PlanStatus.CANCELLED, PlanStatus.PAUSED, PlanStatus.COMPLETED, PlanStatus.FAILED):
        with pytest.raises((NotPermitted, InvalidTransition)):
            engine.transition_plan(p.plan_id, to, f"plan.x:{to.value}", 1)
    engine.transition_plan(p.plan_id, PlanStatus.EXECUTING, "plan.exec", 1)
    with pytest.raises(NotPermitted):
        engine.transition_plan(p.plan_id, PlanStatus.COMPLETED, "plan.done", 1)                                  # addımlar uğurlu deyil
    with pytest.raises(NotPermitted):
        owner.transition_plan(p.plan_id, PlanStatus.COMPLETED, "plan.done2", 1)                                  # sahib üçün də


def test_owner_can_revise_new_version_resets_steps_and_old_version_is_kept(world):
    engine, owner, _, conn = world
    g = active(owner)
    p = plan_for(g, step("a"))
    engine.add_plan(p, "plan.add:p1")
    assert not hasattr(engine, "revise_plan")                                           # mühərrikin revise_plan üzü yoxdur
    assert owner.revise_plan(p.plan_id, (step("a"), step("b")), "plan.rev:1", 1).version == 2
    cur, ver = owner.get_plan(p.plan_id)
    assert ver == 2 and cur.status is PlanStatus.DRAFT and [s.step_id for s in cur.steps] == ["a", "b"]
    assert [r.step_id for r in owner.steps(p.plan_id)] == ["a", "b"]
    assert conn.execute("SELECT COUNT(*) FROM plans WHERE plan_id = ?", (p.plan_id,)).fetchone()[0] == 2
    with pytest.raises(StaleVersion):
        owner.revise_plan(p.plan_id, (step("a"),), "plan.rev:2", 1)


# ------------------------------------------------------------------ addımlar
def ready_plan(engine, owner, *steps: PlanStepSpec) -> tuple[Goal, Plan]:
    g = active(owner)
    p = plan_for(g, *steps)
    engine.add_plan(p, f"plan.add:{p.plan_id}")
    engine.transition_plan(p.plan_id, PlanStatus.READY, f"plan.ready:{p.plan_id}", 1, validated_hash=p.content_hash)
    return g, p


def test_begin_step_is_an_atomic_cas_with_attempt_numbers(world):
    engine, owner, _, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    out = []

    def w(i):
        out.append(engine.begin_step(p.plan_id, 1, "a", f"core.op{i}xx", 1, 60))
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count(True) == 1 and engine.steps(p.plan_id)[0].state is StepState.RUNNING
    assert engine.begin_step(p.plan_id, 1, "a", "core.again1", 2, 60) is False           # RUNNING-dən başlamaq olmaz
    _, p2 = ready_plan(engine, owner, step("a"))
    assert engine.begin_step(p2.plan_id, 1, "a", "core.skip001", 5, 60) is False         # attempt = köhnə+1 olmalıdır
    assert engine.begin_step(p2.plan_id, 2, "a", "core.wrongv1", 1, 60) is False         # cari versiya deyil


def test_finish_only_for_running_step_with_the_same_op_and_late_results_are_ignored(world):
    engine, owner, clock, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    assert engine.finish_step(p.plan_id, 1, "a", "core.nothing", StepState.SUCCEEDED, "x", "", True, "") is False
    engine.begin_step(p.plan_id, 1, "a", "core.op00001", 1, 10)
    assert engine.finish_step(p.plan_id, 1, "a", "core.otherop", StepState.SUCCEEDED, "x", "", True, "") is False           # başqa op
    clock.advance(11)
    assert engine.recover_expired() == [(p.plan_id, "a")] and engine.steps(p.plan_id)[0].state is StepState.UNKNOWN
    assert engine.finish_step(p.plan_id, 1, "a", "core.op00001", StepState.SUCCEEDED, "gec", "", True, "") is False         # UNKNOWN: gec nəticə yazılmır
    assert engine.steps(p.plan_id)[0].state is StepState.UNKNOWN
    kinds = [e["kind"] for e in engine.journal_entries()]
    assert "step_unknown_after_crash" in kinds and "step_late_result_ignored" in kinds
    with pytest.raises(ContractError):
        engine.finish_step(p.plan_id, 1, "a", "core.op00001", StepState.PENDING, "", "", False, "")


def test_unexpired_leases_are_not_recovered(world):
    engine, owner, clock, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    engine.begin_step(p.plan_id, 1, "a", "core.op00001", 1, 100)
    clock.advance(50)
    assert engine.recover_expired() == [] and engine.steps(p.plan_id)[0].state is StepState.RUNNING


def test_only_the_owner_resolves_unknown_blocked_or_failed_steps(world):
    engine, owner, clock, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    engine.begin_step(p.plan_id, 1, "a", "core.op00001", 1, 1)
    clock.advance(5)
    engine.recover_expired()
    assert not hasattr(engine, "resolve_step")
    for bad in ("", "x", None, "bad ref!!!"):
        with pytest.raises(ContractError):
            owner.resolve_step(p.plan_id, "a", "retry", bad)                              # type: ignore[arg-type]
    with pytest.raises(ContractError):
        owner.resolve_step(p.plan_id, "a", "explode", "stepup-receipt-001")
    assert owner.resolve_step(p.plan_id, "a", "retry", "stepup-receipt-001", "yenidən") is True
    rec = owner.steps(p.plan_id)[0]
    assert rec.state is StepState.PENDING and rec.attempt == 1 and rec.verified is False
    assert owner.resolve_step(p.plan_id, "a", "retry", "stepup-receipt-002") is False     # PENDING həll edilmir


def test_block_step_and_summary_redaction(world):
    engine, owner, _, _ = world
    _, p = ready_plan(engine, owner, step("a"), step("b"))
    assert engine.block_step(p.plan_id, 1, "a", "no_handler") is True and engine.block_step(p.plan_id, 1, "a", "again") is False
    engine.begin_step(p.plan_id, 1, "b", "core.op00001", 1, 60)
    engine.finish_step(p.plan_id, 1, "b", "core.op00001", StepState.SUCCEEDED, f"nəticə {FAKE_KEY}", "h", False, "")
    rec = next(r for r in engine.steps(p.plan_id) if r.step_id == "b")
    assert FAKE_KEY not in rec.summary and "redaktə" in rec.summary
    s, digest = safe_summary("Salam, dünya! ğüşəı " * 100)
    assert len(s) <= 500 and len(digest) == 64 and safe_summary("a\x00b")[0].startswith("[redaktə")
    assert safe_summary(None) == ("", "") and safe_summary(5) == ("", "")


# ------------------------------------------------------------------ jurnal və outbox
def test_journal_is_append_only(world):
    engine, owner, _, conn = world
    active(owner)
    assert engine.journal_entries()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE journal SET kind = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM journal")


def test_outbox_flushes_idempotently_into_the_kernel_event_log_and_survives_failures(world, monkeypatch):
    engine, owner, _, _ = world
    g, p = ready_plan(engine, owner, step("a"))
    log = EventLog(connect(":memory:"))
    real = log.append
    calls = {"n": 0}

    def flaky(ev):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("jurnal çökdü")
        return real(ev)
    monkeypatch.setattr(log, "append", flaky)
    with pytest.raises(RuntimeError):
        engine.flush_outbox(log)                                                          # ikinci hadisədə xəta
    assert log.count() == 1
    monkeypatch.setattr(log, "append", real)
    done = engine.flush_outbox(log)
    assert log.count() == 1 + done and engine.flush_outbox(log) == 0                      # təkrar flush yeni sətir yaratmır
    types = [e.event_type for _, e in log.after(0)]
    assert types[:2] == ["goal.created", "goal.status_changed"] and "plan.created" in types and "plan.status_changed" in types


def test_core_package_exports_no_l2_l3_and_no_owner_functions_on_engine():
    assert [a.value for a in core.Autonomy] == ["L0", "L1"]
    with pytest.raises(ValueError):
        core.Autonomy("L2")
    with pytest.raises(ValueError):
        core.Autonomy("L3")


# ------------------------------------------------------------------ mutasiya boşluqları: əlavə yoxlamalar
def test_engine_cannot_complete_a_plan_whose_steps_are_unverified(world):
    engine, owner, _, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    engine.transition_plan(p.plan_id, PlanStatus.EXECUTING, "plan.exec", 1)
    engine.begin_step(p.plan_id, 1, "a", "core.op00001", 1, 60)
    engine.finish_step(p.plan_id, 1, "a", "core.op00001", StepState.SUCCEEDED, "ok", "", False, "")                   # uğurlu, lakin YOXLANMAYIB
    with pytest.raises(NotPermitted):
        engine.transition_plan(p.plan_id, PlanStatus.COMPLETED, "plan.done", 1)
    assert owner.transition_plan(p.plan_id, PlanStatus.COMPLETED, "plan.done.owner", 1).version == 1                      # sahib üçün uğurlu addımlar kifayətdir


def test_new_plans_must_be_draft_version_one_on_their_own_goal(world):
    engine, owner, _, _ = world
    g = active(owner)
    with pytest.raises(NotPermitted):
        engine.add_plan(dataclasses.replace(plan_for(g), status=PlanStatus.READY), "plan.add:ready")
    revised = plan_for(g).revise((step("a"), step("b")))
    with pytest.raises(NotPermitted):
        engine.add_plan(revised, "plan.add:v2")                                                                           # versiya 2


def test_plan_transitions_are_version_checked_and_op_idempotent(world):
    engine, owner, _, _ = world
    g = active(owner)
    p = plan_for(g)
    engine.add_plan(p, "plan.add:p1")
    with pytest.raises(StaleVersion):
        engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready", 7, validated_hash=p.content_hash)
    assert engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready", 1, validated_hash=p.content_hash).duplicate is False
    assert engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready", 1, validated_hash=p.content_hash).duplicate is True


def test_begin_step_requires_a_current_ready_or_executing_plan(world):
    engine, owner, _, _ = world
    g = active(owner)
    p = plan_for(g, step("a"))
    engine.add_plan(p, "plan.add:p1")                                                                                     # hələ DRAFT
    assert engine.begin_step(p.plan_id, 1, "a", "core.op00001", 1, 60) is False and engine.steps(p.plan_id)[0].state is StepState.PENDING
    engine.transition_plan(p.plan_id, PlanStatus.READY, "plan.ready", 1, validated_hash=p.content_hash)
    owner.revise_plan(p.plan_id, (step("a"),), "plan.rev", 1)                                                             # v2 (DRAFT) yaranır, v1 köhnəlir
    assert engine.begin_step(p.plan_id, 1, "a", "core.op00002", 1, 60) is False                                           # köhnə versiyanın addımı başlamır
    assert engine.begin_step(p.plan_id, 2, "a", "core.op00003", 1, 60) is False                                           # v2 hələ DRAFT


def test_owner_only_operations_are_refused_at_the_backend_for_the_engine_actor(world):
    engine, owner, _, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    backend = engine._b                                                                                                    # üz atlanılsa belə backend qoruyur
    with pytest.raises(NotPermitted):
        backend.resolve_step(p.plan_id, "a", "retry", core.Actor.ENGINE, "stepup-receipt-001")
    with pytest.raises(NotPermitted):
        backend.revise_plan(p.plan_id, (step("a"),), core.Actor.ENGINE, "plan.rev:1", 1)


def test_secret_summaries_are_classified_and_redacted(world):
    engine, owner, _, _ = world
    _, p = ready_plan(engine, owner, step("a"))
    engine.begin_step(p.plan_id, 1, "a", "core.op00001", 1, 60)
    engine.finish_step(p.plan_id, 1, "a", "core.op00001", StepState.SUCCEEDED, f"nəticə {FAKE_KEY}", "", False, "")
    assert engine.steps(p.plan_id)[0].summary == "[redaktə olundu: sirr bənzər məzmun]"
    assert safe_summary(f"x {FAKE_KEY}")[0] == "[redaktə olundu: sirr bənzər məzmun]" and safe_summary("a\x00b")[0] == "[redaktə olundu: yararsız simvollar]"


def test_tampered_plan_rows_are_detected_on_read(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    f = build_store(conn)
    g = active(f.owner)
    p = plan_for(g)
    f.engine.add_plan(p, "plan.add:p1")
    conn.execute("UPDATE plans SET doc = replace(doc, 'Nəticə', 'HACK')")
    conn.execute("UPDATE plans SET doc = replace(doc, 'N\\u0259tic\\u0259', 'HACK')")
    with pytest.raises(StoreIntegrityError):
        f.engine.get_plan(p.plan_id)
    conn.close()
