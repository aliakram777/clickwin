import json

import pytest

from safety.ledger import CostLedger, reconcile


@pytest.fixture
def led():
    c = CostLedger(":memory:")
    yield c
    c.close()


def test_record_and_totals(led):
    assert led.record("op1", "a_agent", 0.5)
    assert led.record("op2", "a_agent", 0.25)
    assert led.record("op3", "b_agent", 1.0)
    assert led.agent_total("a_agent") == pytest.approx(0.75)
    assert led.agent_total("b_agent") == pytest.approx(1.0)
    assert led.total() == pytest.approx(1.75)
    assert led.by_agent() == {"a_agent": pytest.approx(0.75), "b_agent": pytest.approx(1.0)}


def test_unknown_agent_total_is_zero(led):
    assert led.agent_total("yoxdur") == 0.0
    assert led.total() == 0.0


def test_duplicate_operation_counted_once(led):
    assert led.record("op1", "a_agent", 0.5) is True
    assert led.record("op1", "a_agent", 0.5) is False
    assert led.record("op1", "a_agent", 9.0) is False  # eyni op_id: başqa məbləğ də yazılmır
    assert led.agent_total("a_agent") == pytest.approx(0.5)
    assert len(led.entries()) == 1
    assert led.has("op1") and not led.has("op2")


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "data" / "ledger.db"
    c1 = CostLedger(path)
    c1.record("op1", "a_agent", 0.6, "ilk")
    c1.close()
    c2 = CostLedger(path)
    assert c2.agent_total("a_agent") == pytest.approx(0.6)
    assert c2.has("op1")
    assert c2.record("op1", "a_agent", 0.6) is False  # təkrar yenidən açıldıqdan sonra da tutulur
    c2.close()


def test_entries_filter_by_agent(led):
    led.record("op1", "a_agent", 1, "x")
    led.record("op2", "b_agent", 2)
    rows = led.entries("a_agent")
    assert [(r.op_id, r.agent, r.amount, r.description) for r in rows] == [("op1", "a_agent", 1.0, "x")]


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), True, "5", None])
def test_invalid_amount_rejected(led, amount):
    with pytest.raises(ValueError):
        led.record("op1", "a_agent", amount)
    assert led.total() == 0.0


@pytest.mark.parametrize("op_id,agent", [("", "a"), ("op", ""), ("  ", "a"), (None, "a")])
def test_invalid_ids_rejected(led, op_id, agent):
    with pytest.raises(ValueError):
        led.record(op_id, agent, 1.0)


def test_sql_injection_text_is_just_data(led):
    led.record("op1'; DROP TABLE costs;--", "a_agent", 1.0, "x'); DELETE FROM costs;--")
    assert led.total() == 1.0


# --- audit ilə uyğunluq ---

def write_audit(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def arow(agent, cost, allowed=True):
    return {"agent": agent, "cost": cost, "allowed": allowed, "permission": "read_files"}


def test_reconcile_ok(led, tmp_path):
    audit = tmp_path / "audit.jsonl"
    led.record("op1", "a_agent", 0.5)
    led.record("op2", "a_agent", 0.0)
    led.record("op3", "b_agent", 1.0)
    write_audit(audit, [arow("a_agent", 0.5), arow("a_agent", 0.0),
                        arow("b_agent", 1.0), arow("b_agent", 5.0, allowed=False)])
    rep = reconcile(led, audit)
    assert rep.ok and rep.mismatches == []


def test_reconcile_empty_everything_ok(led, tmp_path):
    assert reconcile(led, tmp_path / "yoxdur.jsonl").ok


def test_reconcile_detects_audit_entry_missing_in_ledger(led, tmp_path):
    audit = tmp_path / "audit.jsonl"
    led.record("op1", "a_agent", 0.5)
    write_audit(audit, [arow("a_agent", 0.5), arow("a_agent", 0.5)])
    rep = reconcile(led, audit)
    assert not rep.ok
    assert any("a_agent" in m for m in rep.mismatches)


def test_reconcile_detects_amount_difference(led, tmp_path):
    audit = tmp_path / "audit.jsonl"
    led.record("op1", "a_agent", 0.5)
    write_audit(audit, [arow("a_agent", 0.9)])
    assert not reconcile(led, audit).ok


def test_reconcile_detects_ledger_entry_without_audit(led, tmp_path):
    audit = tmp_path / "audit.jsonl"
    led.record("op1", "ghost_agent", 2.0)
    write_audit(audit, [])
    rep = reconcile(led, audit)
    assert not rep.ok and any("ghost_agent" in m for m in rep.mismatches)


def test_reconcile_flags_unreadable_audit_line(led, tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("bu json deyil\n", encoding="utf-8")
    assert not reconcile(led, audit).ok
