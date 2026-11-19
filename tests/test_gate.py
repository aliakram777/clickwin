import json

import pytest

from agents.spec import AgentSpec
from safety.gate import Action, Gate


def make_spec(permissions=("read_files",), limit=1.0):
    return AgentSpec(
        name="test_agent",
        purpose="p",
        tools=[],
        permissions=list(permissions),
        inputs=["a"],
        outputs=["b"],
        cost_limit=limit,
    )


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "logs" / "audit.jsonl", tmp_path / "STOP"


def gate(paths, spec=None, approver=None):
    audit, stop = paths
    return Gate(spec or make_spec(), audit_path=audit, stop_path=stop, approver=approver)


def rows(audit):
    return [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]


def test_allowed_permission_passes(paths):
    assert gate(paths).check(Action("read_files")).allowed


def test_permission_not_in_list_is_blocked(paths):
    d = gate(paths).check(Action("write_files"))
    assert not d.allowed
    assert "icazə" in d.reason


def test_unknown_permission_is_blocked(paths):
    assert not gate(paths).check(Action("root_access")).allowed


@pytest.mark.parametrize("perm", ["spend_money", "external_account", "publish"])
def test_sensitive_without_approver_is_blocked(paths, perm):
    g = gate(paths, make_spec([perm]))
    d = g.check(Action(perm))
    assert not d.allowed
    assert "təsdiq" in d.reason


def test_sensitive_approved_passes_and_approval_is_single_use(paths):
    calls = []

    def approver(action):
        calls.append(action)
        return len(calls) == 1  # yalnız birincini təsdiqlə

    g = gate(paths, make_spec(["publish"]), approver)
    assert g.check(Action("publish")).allowed
    assert not g.check(Action("publish")).allowed
    assert len(calls) == 2  # hər əməl üçün yenidən soruşulur


def test_approver_exception_fails_closed(paths):
    def approver(action):
        raise RuntimeError("x")

    g = gate(paths, make_spec(["publish"]), approver)
    assert not g.check(Action("publish")).allowed


def test_approver_not_asked_for_non_sensitive(paths):
    def approver(action):
        raise AssertionError("soruşulmamalı idi")

    assert gate(paths, approver=approver).check(Action("read_files")).allowed


def test_cost_over_limit_blocked(paths):
    d = gate(paths, make_spec(limit=1.0)).check(Action("read_files", cost=1.01))
    assert not d.allowed
    assert "limit" in d.reason


def test_cumulative_cost_blocked(paths):
    g = gate(paths, make_spec(limit=1.0))
    assert g.check(Action("read_files", cost=0.6)).allowed
    assert not g.check(Action("read_files", cost=0.5)).allowed
    assert g.spent == pytest.approx(0.6)  # bloklanan xərc yazılmır
    assert g.check(Action("read_files", cost=0.4)).allowed


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf")])
def test_invalid_cost_blocked(paths, cost):
    assert not gate(paths).check(Action("read_files", cost=cost)).allowed


def test_denied_by_approver_does_not_spend(paths):
    g = gate(paths, make_spec(["spend_money"], 10), approver=lambda a: False)
    assert not g.check(Action("spend_money", cost=5)).allowed
    assert g.spent == 0


def test_stop_flag_blocks_everything(paths):
    g = gate(paths)
    assert g.check(Action("read_files")).allowed
    g.request_stop()
    d = g.check(Action("read_files"))
    assert not d.allowed
    assert "stop" in d.reason.lower()
    g.clear_stop()
    assert g.check(Action("read_files")).allowed


def test_external_stop_file_is_honoured(paths):
    _, stop = paths
    g = gate(paths)
    stop.write_text("")
    assert not g.check(Action("read_files")).allowed


def test_audit_log_records_every_decision(paths):
    audit, _ = paths
    g = gate(paths)
    g.check(Action("read_files", description="oxu"))
    g.check(Action("write_files"))
    r = rows(audit)
    assert [x["allowed"] for x in r] == [True, False]
    assert r[0]["agent"] == "test_agent"
    assert r[0]["permission"] == "read_files"
    assert r[1]["reason"]
    assert "ts" in r[0]
