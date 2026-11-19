"""Mərhələ 18.8: Operations — smoke/regressiya (lease heartbeat, hash-zəncirli audit, scheduler, watchdog/bərpa, ehtiyat nüsxə). Şəbəkə YOXDUR."""
from __future__ import annotations

import ast
import os
import pathlib
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

import ops
from core import KernelLoop, build_store
from kernel import ContractError, EventLog, Goal, GoalStatus, Origin
from memory import MemActor, MemoryKind, MemoryStore
from ops import AuditChain, AuditMirror, BackupError, Scheduler, Watchdog, backup, restore, verify_backup
from safety.dbconn import connect
from tests.test_core_loop import World, step

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
KEY = "sk-" + "Hq8M" * 10


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


# ------------------------------------------------------------------ lease heartbeat
def _run_long_step(heartbeat: bool):
    w = World("L1", steps=[(step("a"),)])
    w.loop = KernelLoop(w.engine, autonomy="L1", stop=lambda: False, planner=w.planner, approvals=w.approvals, executor=w.executor, lease_s=60, heartbeat=heartbeat, heartbeat_interval_s=0.05)
    g = w.goal()
    plan = w.to_awaiting(g)
    w.approvals.approve_all(w.engine, plan.plan_id)
    seen = {}

    def slow(req):
        w.clock.advance(100)                                  # lease (60s) keçdi
        time.sleep(0.4)                                       # heartbeat ipinə vaxt ver
        seen["recovered"] = w.engine.recover_expired()
    w.executor.on_execute = slow
    action = w.loop.tick().action
    return w, plan, seen, action


def test_heartbeat_keeps_a_live_long_step_from_becoming_unknown():
    w, plan, seen, action = _run_long_step(True)
    assert seen["recovered"] == [] and action == "step_succeeded" and w.engine.steps(plan.plan_id)[0].state.value == "succeeded"


def test_without_heartbeat_the_expired_lease_turns_the_running_step_unknown_and_late_result_is_ignored():
    w, plan, seen, action = _run_long_step(False)
    assert seen["recovered"] == [(plan.plan_id, "a")] and w.engine.steps(plan.plan_id)[0].state.value == "unknown"
    assert any(e["kind"] == "step_late_result_ignored" for e in w.engine.journal_entries())


def test_lease_cannot_be_renewed_for_non_running_steps_or_other_ops():
    w = World("L1", steps=[(step("a"),)])
    g = w.goal()
    plan = w.to_awaiting(g)
    assert w.engine.renew_lease(plan.plan_id, 1, "a", "core.nothing", 60) is False                       # RUNNING deyil
    w.approvals.approve_all(w.engine, plan.plan_id)
    w.engine.begin_step(plan.plan_id, 1, "a", "core.op00001", 1, 60)
    assert w.engine.renew_lease(plan.plan_id, 1, "a", "core.otherop", 60) is False and w.engine.renew_lease(plan.plan_id, 1, "a", "core.op00001", 60) is True


# ------------------------------------------------------------------ hash-zəncirli audit
def test_audit_chain_detects_modification_deletion_and_verifies_anchors():
    conn = connect(":memory:")
    c = AuditChain(conn, Clock())
    for i in range(5):
        c.append("evt", "test", {"i": i})
    assert c.verify().ok and c.verify().count == 5
    anchor = c.anchor()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE chain SET detail = '{}'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM chain")
    c.append("evt", "test", {"i": 5})
    assert c.verify_anchor(anchor)                                                                        # ankerdən sonra əlavə normaldır
    conn.execute("DROP TRIGGER chain_no_update")
    conn.execute("UPDATE chain SET detail = '{\"i\":999}' WHERE seq = 3")                                 # fayl səviyyəsində saxtakarlıq
    rep = c.verify()
    assert not rep.ok and rep.broken_at == 3 and rep.reason == "hash_mismatch" and not c.verify_anchor(anchor)
    conn2 = connect(":memory:")
    c2 = AuditChain(conn2, Clock())
    for i in range(4):
        c2.append("evt", "test", {"i": i})
    conn2.execute("DROP TRIGGER chain_no_delete")
    conn2.execute("DELETE FROM chain WHERE seq = 2")
    assert c2.verify().reason == "missing_or_reordered_row" and c2.verify().broken_at == 2


def test_audit_redacts_secrets_and_bounds_oversized_details():
    c = AuditChain(connect(":memory:"), Clock())
    c.append("evt", "t", {"note": f"açar {KEY}"})
    c.append("evt", "t", {"big": "x" * 5000})
    entries = c.entries()
    assert entries[0]["detail"] == {"redacted": "secret_like_content"} and entries[1]["detail"]["redacted"] == "invalid_or_too_long" and KEY not in str(entries)
    for bad in ("", "x" * 61, "a\x00b"):
        with pytest.raises(ContractError):
            c.append(bad, "t")
    assert c.verify().ok


def test_audit_mirror_copies_the_core_journal_once_with_a_durable_cursor():
    w = World("L0", steps=[(step("a"),)])
    w.goal()
    w.loop.run(10)
    chain = AuditChain(connect(":memory:"), Clock())
    mirror = AuditMirror(chain, w.engine)
    n1 = mirror.sync()
    assert n1 >= 3 and mirror.sync() == 0 and chain.verify().ok and chain.entries()[0]["source"] == "core_journal"
    w.engine.record("extra", "goal_x")
    assert mirror.sync() == 1 and chain.verify().count == n1 + 1 and chain.entries(limit=1000)[-1]["detail"]["goal"] == "goal_x"


# ------------------------------------------------------------------ scheduler
def make_sched(stop=None, **kw):
    clock, ran, stop = Clock(), [], stop if stop is not None else [False]
    jobs = {"ok": lambda: ran.append("ok") or "tamam", "boom": lambda: 1 / 0, "secret": lambda: f"açar {KEY}", "other": lambda: ran.append("other") or "ok"}
    return Scheduler(connect(":memory:"), jobs, lambda: stop[0], clock, **kw), clock, ran, stop


def test_scheduler_accepts_only_registered_jobs_and_sane_intervals():
    s, _, _, _ = make_sched()
    for args in (("a", "unknown_job", 60), ("Bad", "ok", 60), ("a", "ok", 0), ("a", "ok", 10 ** 9), ("a", "ok", True), ("a", "ok", "5")):
        with pytest.raises(ContractError):
            s.add(*args)
    s.add("a", "ok", 60)
    with pytest.raises(ContractError):
        s.add("a", "ok", 60)
    with pytest.raises(ValueError):
        Scheduler(connect(":memory:"), {"x": "not callable"}, lambda: False)                              # type: ignore[dict-item]


def test_scheduler_runs_due_jobs_without_catch_up_storms_and_stop_wins():
    s, clock, ran, stop = make_sched()
    s.add("a", "ok", 60)
    assert s.run_due() == [] and ran == []                                                                # hələ vaxtı çatmayıb
    clock.advance(60)
    assert s.run_due() == [("a", "tamam")] and ran == ["ok"]
    clock.advance(60 * 10)                                                                                # 10 interval buraxıldı
    assert s.run_due() == [("a", "tamam")] and ran == ["ok", "ok"] and s.run_due() == []                  # yalnız BİR dəfə (yığılma yox)
    clock.advance(60)
    stop[0] = True
    before = s.list()
    assert s.run_due() == [] and s.list() == before and ran == ["ok", "ok"]                               # STOP: heç nə işləmir, vəziyyət dəyişmir
    stop[0] = False
    assert s.run_due() == [("a", "tamam")]


def test_scheduler_failures_disable_a_job_until_the_owner_reenables_it_and_secrets_are_redacted():
    s, clock, ran, _ = make_sched(max_failures=3)
    s.add("bad", "boom", 10)
    s.add("sec", "secret", 10)
    for _ in range(3):
        clock.advance(10)
        res = dict(s.run_due())
    rows = {r["name"]: r for r in s.list()}
    assert rows["bad"]["enabled"] == 0 and rows["bad"]["failures"] == 3 and rows["bad"]["last_status"] == "error:ZeroDivisionError"
    assert rows["sec"]["enabled"] == 1 and KEY not in rows["sec"]["last_status"] and "redaktə" in rows["sec"]["last_status"]
    clock.advance(10)
    assert "bad" not in dict(s.run_due())                                                                 # dayandırılıb
    assert s.set_enabled("bad", True) and s.list()[0]["failures"] == 0 and not s.set_enabled("nope", True)


def test_scheduler_runs_at_most_ten_jobs_per_call():
    s, clock, ran, _ = make_sched()
    for i in range(14):
        s.add(f"j{i:02d}", "other", 5)
    clock.advance(5)
    assert len(s.run_due()) == 10 and len(s.run_due()) == 4


# ------------------------------------------------------------------ watchdog / bərpa
def test_watchdog_reports_health_and_recovers_after_a_crash(tmp_path):
    w = World("L1", steps=[(step("a"),)], with_log=True)
    g = w.goal()
    stop = [False]
    chain = AuditChain(connect(":memory:"), w.clock)
    wd = Watchdog(connect(":memory:"), w.engine, stop=lambda: stop[0], clock=w.clock, max_heartbeat_age_s=30, disk_path=tmp_path, min_free_mb=1, audit=chain)
    assert wd.check().issues == ("no_heartbeat",)
    wd.beat()
    assert wd.check().ok
    w.clock.advance(31)
    assert wd.check().issues == ("heartbeat_stale",)
    wd.beat()
    plan = w.to_awaiting(g)
    w.approvals.approve_all(w.engine, plan.plan_id)
    w.executor.script = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        w.loop.tick()                                                                                      # proses öldü: addım RUNNING qalıb
    w.clock.advance(61)
    rec = wd.recover(w.log)
    assert rec["expired_to_unknown"] == 1 and w.engine.health_counts()["outbox_pending"] == 0 and w.engine.steps(plan.plan_id)[0].state.value == "unknown" and len(w.executor.calls) == 1
    wd.beat()
    rep = wd.check()
    assert "unknown_steps_need_owner" in rep.issues and rep.details["steps"]["unknown"] == 1                 # avtomatik təkrar YOXDUR: sahib həllini gözləyir
    stop[0] = True
    assert "stop_active" in wd.check().issues
    assert chain.entries()[-1]["kind"] == "recovery" and chain.verify().ok
    assert "low_disk" in Watchdog(connect(":memory:"), w.engine, stop=lambda: False, clock=w.clock, disk_path=tmp_path, min_free_mb=10 ** 9).check().issues


# ------------------------------------------------------------------ ehtiyat nüsxə
def _make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


def test_backup_verify_and_restore_roundtrip_with_tamper_detection(tmp_path):
    _make_db(tmp_path / "core.db", ["a", "ğ", "🙂"])
    _make_db(tmp_path / "mem.db", ["x"])
    clock = Clock()
    b = backup({"core": tmp_path / "core.db", "mem": tmp_path / "mem.db"}, tmp_path / "bk", clock=clock)
    assert verify_backup(b) == (True, []) and stat.S_IMODE(os.stat(b).st_mode) == 0o700 and stat.S_IMODE(os.stat(b / "core.db").st_mode) == 0o600
    names = restore(b, tmp_path / "restored")
    assert names == ["core", "mem"] and sqlite3.connect(tmp_path / "restored" / "core.db").execute("SELECT v FROM t ORDER BY rowid").fetchall() == [("a",), ("ğ",), ("🙂",)]
    with pytest.raises(BackupError):
        restore(b, tmp_path / "restored")                                                                  # mövcud fayl: overwrite yox
    assert restore(b, tmp_path / "restored", overwrite=True) == ["core", "mem"]
    raw = bytearray((b / "mem.db").read_bytes())
    raw[-10] ^= 0xFF
    (b / "mem.db").write_bytes(bytes(raw))
    ok, problems = verify_backup(b)
    assert not ok and any("mem" in p for p in problems)
    with pytest.raises(BackupError):
        restore(b, tmp_path / "restored2")                                                                 # pozulmuş nüsxə bərpa olunmur
    assert not (tmp_path / "restored2").exists() or not list((tmp_path / "restored2").glob("*.db"))


def test_backup_rejects_bad_names_missing_sources_and_applies_retention(tmp_path):
    _make_db(tmp_path / "a.db", ["1"])
    for bad in ({"../evil": tmp_path / "a.db"}, {"Bad Name": tmp_path / "a.db"}, {"": tmp_path / "a.db"}):
        with pytest.raises(BackupError):
            backup(bad, tmp_path / "bk")
    with pytest.raises(BackupError):
        backup({"x": tmp_path / "missing.db"}, tmp_path / "bk")
    clock = Clock()
    made = []
    for _ in range(4):
        clock.advance(60)
        made.append(backup({"a": tmp_path / "a.db"}, tmp_path / "bk2", keep=2, clock=clock))
    left = sorted(p.name for p in (tmp_path / "bk2").iterdir())
    assert left == sorted(m.name for m in made[-2:]) and verify_backup(tmp_path / "nope") == (False, ["manifest oxunmadı"])


# ------------------------------------------------------------------ inteqrasiya və təmizlik
def test_standard_ops_jobs_run_through_the_scheduler_without_side_effects_beyond_ops(tmp_path):
    w = World("L0", steps=[(step("a"),)])
    w.goal()
    clock = Clock()
    mem = MemoryStore(connect(":memory:"), clock)
    mem.write(MemActor.AGENT, MemoryKind.SHORT_TERM, "müvəqqəti", ttl_s=5)
    chain = AuditChain(connect(":memory:"), clock)
    mirror = AuditMirror(chain, w.engine)
    wd = Watchdog(connect(":memory:"), w.engine, stop=lambda: False, clock=clock, audit=chain)
    jobs = {"loop.tick": lambda: f"{len(w.loop.run(5))} tick", "memory.purge": lambda: f"{mem.purge_expired()} təmizləndi", "audit.mirror": lambda: f"{mirror.sync()} köçürüldü",
            "watchdog.check": lambda: (wd.beat(), "ok" if wd.check().ok else ",".join(wd.check().issues))[1]}
    s = Scheduler(connect(":memory:"), jobs, lambda: False, clock)
    for name in jobs:
        s.add(name.replace(".", "-"), name, 10)
    clock.advance(10)
    res = dict(s.run_due())
    assert set(res) == {"loop-tick", "memory-purge", "audit-mirror", "watchdog-check"} and res["memory-purge"] == "1 təmizləndi" and chain.verify().ok
    first = chain.verify().count                                                                           # audit.mirror loop.tick-dən ƏVVƏL işlədi (əlifba sırası): yalnız goal yazıları
    clock.advance(10)
    s.run_due()
    assert chain.verify().ok and chain.verify().count > first                                             # növbəti dövrdə loop jurnalı köçürüldü


def test_ops_package_is_pure():
    root = pathlib.Path(ops.__file__).parent
    allowed = {"__future__", "dataclasses", "datetime", "hashlib", "json", "os", "pathlib", "re", "shutil", "sqlite3", "threading", "typing"}
    for f in root.glob("*.py"):
        src = f.read_text()
        mods = [a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names] + [n.module or "" for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)]
        assert all(m in allowed or m.split(".")[0] in ("ops", "kernel") or m == "safety.dbconn" for m in mods), (f.name, mods)
        for bad in ("environ", "getenv", "socket", "urllib", "subprocess", "eval(", "exec(", "run_session", "requests"):
            assert bad not in src, (f.name, bad)
