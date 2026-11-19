"""Təsdiq xidməti. DİQQƏT: buradakı "təsdiqləyicilər" test obyektləridir (kind="mock"), real istifadəçi
təsdiqi deyil. Real kanal adapterləri (Web/Telegram/Android) kind="human" Principal-ı yalnız istifadəçini
autentifikasiya etdikdən sonra yaradacaq."""
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from safety.approvals import ApprovalService, Principal, effect_digest

MOCK = Principal("owner", "mock", "test")
HUMAN = Principal("owner", "human", "web")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    audit = tmp_path / "audit.jsonl"
    db = tmp_path / "approvals.db"

    def make(**kw):
        opts = dict(owners={"owner"}, ttl_seconds=60, clock=clock, allow_mock=True)
        opts.update(kw)
        return ApprovalService(db, audit, **opts)

    return make, clock, audit, db


def events(audit):
    p = Path(audit)
    rows = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if "event" in r]


def codes(audit, event):
    return [r.get("code") for r in events(audit) if r["event"] == event]


ARGS = dict(agent="agent_a", permission="publish", cost=0.5, op_id="op1", description="post",
            payload={"text": "salam"})


def req(svc, **over):
    return svc.client.request(**{**ARGS, **over})


def h(**over):
    return effect_digest(**{**ARGS, **over}).hash


# --- hash ---

def test_hash_is_sha256_of_canonical_json_and_order_independent():
    d = effect_digest(**ARGS)
    assert d.hash == hashlib.sha256(d.json.encode("utf-8")).hexdigest()
    assert len(d.hash) == 64
    reordered = effect_digest(**{**ARGS, "payload": {"text": "salam"}})
    assert reordered.hash == d.hash
    assert effect_digest(**{**ARGS, "payload": {"b": 1, "a": 2}}).hash == \
        effect_digest(**{**ARGS, "payload": {"a": 2, "b": 1}}).hash


@pytest.mark.parametrize("field,value", [
    ("agent", "agent_b"), ("permission", "spend_money"), ("cost", 0.6), ("op_id", "op2"),
    ("description", "başqa"), ("payload", {"text": "dəyişdi"}),
])
def test_any_field_change_changes_hash(field, value):
    assert h(**{field: value}) != h()


@pytest.mark.parametrize("payload", [{"x": {1, 2}}, {"x": float("nan")}, object(), {"x": "a" * 70000}])
def test_unserializable_or_huge_payload_rejected(payload):
    with pytest.raises(ValueError):
        effect_digest(**{**ARGS, "payload": payload})


# --- sorğu ---

def test_request_creates_unique_pending_ticket_bound_to_hash_and_audited(env):
    make, _, audit, _ = env
    seen = []

    class Chan:
        def notify(self, ticket):
            seen.append(ticket)

    svc = make(channels=[Chan()])
    t = req(svc)
    assert t.status == "pending" and t.payload_hash == h()
    assert len(t.id) == 32 and t.expires_at == 1_000_060.0
    assert [x.id for x in seen] == [t.id]
    assert [r["event"] for r in events(audit)] == ["approval_requested"]
    row = events(audit)[0]
    assert row["approval_id"] == t.id and row["payload_hash"] == t.payload_hash
    assert t.id != req(svc, op_id="op2").id


def test_same_effect_requested_twice_returns_same_ticket(env):
    make, _, audit, _ = env
    notified = []

    class Chan:
        def notify(self, ticket):
            notified.append(ticket.id)

    svc = make(channels=[Chan()])
    assert req(svc).id == req(svc).id
    assert len(notified) == 1 and len(svc.gateway.pending()) == 1
    assert len(events(audit)) == 1


def test_notify_failure_does_not_break_request(env):
    make, _, audit, _ = env

    class Bad:
        def notify(self, ticket):
            raise RuntimeError("kanal düşüb")

    svc = make(channels=[Bad()])
    assert req(svc).status == "pending"
    assert "approval_notify_failed" in [r["event"] for r in events(audit)]


# --- qərar və istifadə ---

def test_approve_then_consume_once_then_replay_rejected(env):
    make, _, audit, _ = env
    svc = make()
    t = req(svc)
    assert svc.gateway.approve(t.id, MOCK).ok
    first = svc.client.consume(t.id, t.payload_hash)
    assert first.ok
    again = svc.client.consume(t.id, t.payload_hash)
    assert not again.ok and "təkrar" in again.reason
    assert codes(audit, "approval_rejected") == ["replay"]
    assert [r["event"] for r in events(audit)] == [
        "approval_requested", "approval_approved", "approval_consumed", "approval_rejected"]


def test_fake_id_rejected_and_audited(env):
    make, _, audit, _ = env
    svc = make()
    res = svc.client.consume("0" * 32, h())
    assert not res.ok
    assert svc.client.check("saxta", h()).ok is False
    assert set(codes(audit, "approval_rejected")) == {"unknown_id"}
    assert not svc.gateway.approve("0" * 32, MOCK).ok


def test_unapproved_ticket_cannot_be_consumed(env):
    make, _, audit, _ = env
    svc = make()
    t = req(svc)
    res = svc.client.consume(t.id, t.payload_hash)
    assert not res.ok and codes(audit, "approval_rejected") == ["not_approved"]


def test_pending_expires(env):
    make, clock, audit, _ = env
    svc = make()
    t = req(svc)
    clock.advance(61)
    res = svc.gateway.approve(t.id, MOCK)
    assert not res.ok and "vaxt" in res.reason
    assert svc.gateway.get(t.id).status == "expired"
    assert svc.gateway.pending() == []
    assert "approval_expired" in [r["event"] for r in events(audit)]


def test_approved_but_unused_ticket_expires(env):
    make, clock, audit, _ = env
    svc = make()
    t = req(svc)
    svc.gateway.approve(t.id, MOCK)
    clock.advance(61)
    res = svc.client.consume(t.id, t.payload_hash)
    assert not res.ok and svc.gateway.get(t.id).status == "expired"
    assert [r["event"] for r in events(audit)].count("approval_expired") == 1  # bir dəfə yazılır


def test_expired_effect_gets_a_fresh_request(env):
    make, clock, _, _ = env
    svc = make()
    old = req(svc)
    clock.advance(61)
    new = req(svc)
    assert new.id != old.id and new.status == "pending"


def test_payload_change_invalidates_approval_permanently(env):
    make, _, audit, _ = env
    svc = make()
    t = req(svc)
    svc.gateway.approve(t.id, MOCK)
    changed = svc.client.consume(t.id, h(payload={"text": "başqa"}))
    assert not changed.ok and "payload" in changed.reason
    assert svc.gateway.get(t.id).status == "invalidated"
    assert not svc.client.consume(t.id, t.payload_hash).ok  # orijinalla da artıq olmur
    assert "hash_mismatch" in codes(audit, "approval_rejected")
    assert "approval_invalidated" in [r["event"] for r in events(audit)]


def test_tampered_db_row_is_detected(env):
    make, _, audit, db = env
    svc = make()
    t = req(svc)
    svc.gateway.approve(t.id, MOCK)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE approvals SET payload_json = ?", ('{"v":1,"cost":999}',))
    conn.commit()
    conn.close()
    res = svc.client.consume(t.id, t.payload_hash)
    assert not res.ok
    assert "tamper" in codes(audit, "approval_rejected")
    assert svc.gateway.get(t.id).status == "invalidated"


def test_denied_ticket_blocks_effect_until_expiry(env):
    make, clock, audit, _ = env
    svc = make()
    t = req(svc)
    assert svc.gateway.deny(t.id, MOCK, "yox").ok
    assert not svc.client.consume(t.id, t.payload_hash).ok
    assert req(svc).id == t.id and req(svc).status == "denied"  # eyni effekt yenidən soruşulmur
    clock.advance(61)
    assert req(svc).status == "pending"
    assert "approval_denied" in [r["event"] for r in events(audit)]


def test_only_pending_tickets_can_be_decided(env):
    make, _, audit, _ = env
    svc = make()
    t = req(svc)
    svc.gateway.approve(t.id, MOCK)
    assert not svc.gateway.approve(t.id, MOCK).ok
    assert not svc.gateway.deny(t.id, MOCK).ok
    assert svc.gateway.get(t.id).status == "approved"


# --- kim təsdiqləyə bilər ---

def test_agents_cannot_approve_their_own_actions(env):
    make, _, audit, _ = env
    svc = make(owners={"owner", "agent_a"})  # sahib siyahısında olsa belə
    t = req(svc)
    for p in (Principal("agent_a", "human", "web"), Principal("x", "agent", "internal"),
              Principal("x", "system", "internal")):
        assert not svc.gateway.approve(t.id, p).ok
    assert svc.gateway.get(t.id).status == "pending"
    assert "self_approval" in codes(audit, "approval_rejected")


def test_unauthorized_principal_rejected(env):
    make, _, audit, _ = env
    svc = make()
    t = req(svc)
    assert not svc.gateway.approve(t.id, Principal("yad", "human", "telegram")).ok
    assert svc.gateway.get(t.id).status == "pending"
    assert "unauthorized" in codes(audit, "approval_rejected")


def test_mock_approvals_rejected_by_default_and_marked_when_allowed(env):
    make, _, audit, _ = env
    strict = make(allow_mock=False)
    t = req(strict)
    assert not strict.gateway.approve(t.id, MOCK).ok
    assert "mock_rejected" in codes(audit, "approval_rejected")
    assert strict.gateway.approve(t.id, HUMAN).ok  # real kanal adapterinin yaratdığı human principal
    assert strict.gateway.get(t.id).human_approved is True

    lax = make(allow_mock=True)
    t2 = req(lax, op_id="op9")
    lax.gateway.approve(t2.id, MOCK)
    ticket = lax.gateway.get(t2.id)
    assert ticket.human_approved is False and ticket.approver_kind == "mock"
    lax.client.consume(t2.id, ticket.payload_hash)
    last = [r for r in events(audit) if r["event"] == "approval_consumed"][-1]
    assert (last["source_kind"], last["human"]) == ("mock", False)


def test_default_is_deny(env, tmp_path):
    make, _, _, _ = env
    with pytest.raises(ValueError):
        make(owners=set())  # sahibsiz təsdiq xidməti qurulmur
    for bad in (0, -1, 86401, True, "5"):
        with pytest.raises(ValueError):
            make(ttl_seconds=bad)
    for p in (("", "human", "web"), ("o", "robot", "web"), ("o", "human", "")):
        with pytest.raises(ValueError):
            Principal(*p)


# --- davamlılıq və paralellik ---

def test_state_survives_reopen_and_consumed_stays_consumed(env):
    make, _, _, _ = env
    s1 = make()
    t = req(s1)
    s1.gateway.approve(t.id, MOCK)
    s1.close()
    s2 = make()
    assert s2.gateway.get(t.id).status == "approved"
    assert s2.client.consume(t.id, t.payload_hash).ok
    s2.close()
    s3 = make()
    assert s3.gateway.get(t.id).status == "consumed"
    assert not s3.client.consume(t.id, t.payload_hash).ok


def test_concurrent_consume_only_one_wins(env):
    make, _, _, _ = env
    s = make()
    t = req(s)
    s.gateway.approve(t.id, MOCK)
    results = []

    def worker():
        svc = make()
        results.append(svc.client.consume(t.id, t.payload_hash).ok)
        svc.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert results.count(True) == 1 and results.count(False) == 7


# --- interfeys ---

def test_facets_are_separated_and_ui_view_is_complete(env):
    make, _, _, _ = env
    svc = make()
    assert not hasattr(svc.client, "approve") and not hasattr(svc.client, "deny")
    assert not hasattr(svc.gateway, "consume") and not hasattr(svc.gateway, "request")
    t = req(svc)
    view = svc.gateway.pending()[0]
    assert (view.id, view.agent, view.permission, view.cost, view.description, view.op_id) == \
        (t.id, "agent_a", "publish", 0.5, "post", "op1")
    assert view.payload == {"text": "salam"} and view.payload_hash == t.payload_hash
