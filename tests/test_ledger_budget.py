import json
import sqlite3
import threading

import pytest

from tests.ledger_helpers import owned_ledger, owner_resolve
from safety.ledger import CostLedger, reconcile


@pytest.fixture
def led():
    c = owned_ledger(":memory:")
    yield c
    c.close()


def entry(led, op):
    return next(e for e in led.entries() if e.op_id == op)


def test_reserve_holds_budget(led):
    r = led.reserve("op1", "a_agent", 0.5)
    assert r.status == "reserved"
    e = entry(led, "op1")
    assert (e.status, e.amount, e.reserved, e.unknown) == ("reserved", 0.5, 0.5, False)
    assert led.total() == pytest.approx(0.5)
    assert led.reserved_total() == pytest.approx(0.5)


def test_commit_settles_actual_cost(led):
    led.reserve("op1", "a_agent", 1.0)
    assert led.commit("op1", 0.4) is True
    e = entry(led, "op1")
    assert (e.status, e.amount, e.reserved) == ("committed", 0.4, 1.0)
    assert led.total() == pytest.approx(0.4)
    assert led.reserved_total() == 0.0


def test_release_frees_budget(led):
    led.reserve("op1", "a_agent", 1.0)
    assert led.release("op1") is True
    assert entry(led, "op1").status == "released"
    assert led.total() == 0.0 and led.agent_total("a_agent") == 0.0


def test_settle_only_from_reserved(led):
    led.reserve("op1", "a_agent", 1.0)
    assert led.commit("op1", 0.4) is True
    assert led.commit("op1", 0.4) is False   # ikinci dəfə hesablanmır
    assert led.release("op1") is False
    assert led.commit("yoxdur", 1) is False
    assert led.total() == pytest.approx(0.4)


def test_duplicate_reservation_not_counted_twice(led):
    assert led.reserve("op1", "a_agent", 0.5).status == "reserved"
    assert led.reserve("op1", "a_agent", 0.5).status == "duplicate"
    led.commit("op1", 0.5)
    assert led.reserve("op1", "a_agent", 0.5).status == "duplicate"
    assert led.total() == pytest.approx(0.5)


def test_released_op_can_be_reserved_again(led):
    led.reserve("op1", "a_agent", 0.5)
    led.release("op1")
    assert not led.has("op1")
    assert led.reserve("op1", "a_agent", 0.7).status == "reserved"
    assert len(led.entries()) == 1
    assert led.total() == pytest.approx(0.7)


def test_global_limit_across_agents(led):
    kw = dict(global_limit=1.0)
    assert led.reserve("o1", "a_agent", 0.6, **kw).status == "reserved"
    r = led.reserve("o2", "b_agent", 0.6, **kw)
    assert r.status == "denied" and "ümumi" in r.reason and "limit" in r.reason
    assert led.reserve("o3", "b_agent", 0.4, **kw).status == "reserved"
    assert led.total() == pytest.approx(1.0)


def test_agent_limit(led):
    assert led.reserve("o1", "a_agent", 0.6, agent_limit=1.0).status == "reserved"
    r = led.reserve("o2", "a_agent", 0.6, agent_limit=1.0)
    assert r.status == "denied" and "agent" in r.reason
    assert led.reserve("o3", "b_agent", 0.6, agent_limit=1.0).status == "reserved"  # başqa agent


def test_task_limit(led):
    r = led.reserve("o1", "a_agent", 0.6, task_limit=0.5)
    assert r.status == "denied" and "tapşırıq" in r.reason
    assert led.total() == 0.0 and led.entries() == []


def test_denied_reservation_leaves_no_trace(led):
    led.reserve("o1", "a_agent", 0.6, global_limit=1.0)
    led.reserve("o2", "a_agent", 0.6, global_limit=1.0)
    assert [e.op_id for e in led.entries()] == ["o1"]


def test_released_and_actual_costs_change_room(led):
    led.reserve("o1", "a_agent", 0.9, global_limit=1.0)
    assert led.reserve("o2", "a_agent", 0.5, global_limit=1.0).status == "denied"
    led.commit("o1", 0.2)  # faktiki xərc rezervdən azdır
    assert led.reserve("o2", "a_agent", 0.5, global_limit=1.0).status == "reserved"


def test_unknown_cost_keeps_reservation_and_is_flagged(led):
    led.reserve("o1", "a_agent", 0.5, global_limit=1.0)
    assert led.mark_unknown("o1") is True
    e = entry(led, "o1")
    assert e.status == "reserved" and e.unknown is True
    assert led.total() == pytest.approx(0.5)  # qorunur
    assert led.reserve("o2", "a_agent", 0.6, global_limit=1.0).status == "denied"
    assert [x.op_id for x in led.unknown_entries()] == ["o1"]
    assert led.commit("o1", 0.3) is False and led.release("o1") is False     # naməlum rezervi nə commit, nə release həll edir
    assert led.total() == pytest.approx(0.5)
    assert owner_resolve(led, "o1", 0.3)  # sonradan məlum oldu: yalnız sahib təsdiqi ilə
    e = entry(led, "o1")
    assert (e.status, e.unknown, e.amount) == ("committed", False, 0.3)
    assert led.unknown_entries() == []


def test_mark_unknown_only_on_reserved(led):
    led.reserve("o1", "a_agent", 0.5)
    led.commit("o1", 0.5)
    assert led.mark_unknown("o1") is False


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True])
def test_invalid_limits_rejected(led, bad):
    for key in ("global_limit", "agent_limit", "task_limit"):
        with pytest.raises(ValueError):
            led.reserve("o1", "a_agent", 0.1, **{key: bad})
    assert led.entries() == []


def test_invalid_amounts_rejected(led):
    for amount in (-1, float("nan"), float("inf"), True, "5"):
        with pytest.raises(ValueError):
            led.reserve("o1", "a_agent", amount)
    with pytest.raises(ValueError):
        led.commit("o1", -1)


def test_statuses_survive_reopen(tmp_path):
    path = tmp_path / "l.db"
    c = CostLedger(path)
    c.reserve("o1", "a_agent", 1.0)
    c.reserve("o2", "a_agent", 1.0)
    c.reserve("o3", "a_agent", 1.0)
    c.commit("o1", 0.4)
    c.release("o2")
    c.mark_unknown("o3")
    c.close()
    c2 = CostLedger(path)
    got = {e.op_id: (e.status, e.amount, e.unknown) for e in c2.entries()}
    assert got == {"o1": ("committed", 0.4, False), "o2": ("released", 0.0, False),
                   "o3": ("reserved", 1.0, True)}
    assert c2.total() == pytest.approx(1.4)
    c2.close()


def test_reservation_is_atomic_across_connections(tmp_path):
    path = tmp_path / "l.db"
    a, b = CostLedger(path), CostLedger(path)
    assert a.reserve("o1", "x_agent", 0.7, global_limit=1.0).status == "reserved"
    assert b.reserve("o2", "y_agent", 0.7, global_limit=1.0).status == "denied"
    a.close(); b.close()


def test_concurrent_reservations_never_exceed_limit(tmp_path):
    path = tmp_path / "l.db"
    CostLedger(path).close()
    results = []

    def worker(i):
        c = CostLedger(path)
        results.append(c.reserve(f"op{i}", f"agent_{i}", 0.3, global_limit=1.0).status)
        c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("reserved") == 3
    assert results.count("denied") == 7
    c = CostLedger(path)
    assert c.total() == pytest.approx(0.9)
    c.close()


def test_old_schema_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE costs (op_id TEXT PRIMARY KEY, agent TEXT NOT NULL, "
                 "amount REAL NOT NULL, ts TEXT NOT NULL, description TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO costs VALUES ('old1', 'a_agent', 0.5, 't', 'd')")
    conn.commit()
    conn.close()
    c = CostLedger(path)
    e = c.entries()[0]
    assert (e.op_id, e.status, e.amount, e.reserved) == ("old1", "committed", 0.5, 0.5)
    assert c.reserve("old1", "a_agent", 0.5).status == "duplicate"
    c.close()


# --- audit ilə uyğunluq (rezerv modeli) ---

def write_audit(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def arow(agent, cost, allowed=True):
    return {"agent": agent, "cost": cost, "allowed": allowed, "permission": "read_files"}


def test_reconcile_uses_authorized_reservation_not_actual(led, tmp_path):
    audit = tmp_path / "a.jsonl"
    led.reserve("o1", "a_agent", 1.0)
    led.commit("o1", 0.4)  # faktiki < rezerv
    write_audit(audit, [arow("a_agent", 1.0)])
    assert reconcile(led, audit).ok


def test_reconcile_ignores_released_rows(led, tmp_path):
    audit = tmp_path / "a.jsonl"
    led.reserve("o1", "a_agent", 1.0)
    led.release("o1")
    write_audit(audit, [arow("a_agent", 1.0, allowed=False)])
    assert reconcile(led, audit).ok


def test_reconcile_flags_orphan_reservation(led, tmp_path):
    audit = tmp_path / "a.jsonl"
    led.reserve("o1", "a_agent", 1.0)  # gate-dən keçməyib: audit yoxdur
    write_audit(audit, [])
    assert not reconcile(led, audit).ok


def test_reconcile_flags_actual_above_reservation(led, tmp_path):
    audit = tmp_path / "a.jsonl"
    led.reserve("o1", "a_agent", 0.5)
    led.commit("o1", 0.9)
    write_audit(audit, [arow("a_agent", 0.5)])
    rep = reconcile(led, audit)
    assert not rep.ok and any("aşıb" in m for m in rep.mismatches)
