"""Şirkət qatına əlavə: məhdud (baseline) agent qeydiyyatı və geri qaytarıla bilən dayandırma (suspend/resume)."""
import pytest

from agents.company import Authority, Company, CompanyDenied
from agents.registry import Registry
from agents.spec import AgentSpec
from safety.approvals import Principal

OWNER = Principal("owner", "human", "console")
SYSTEM = Principal("naymera", "system", "core")
AGENT = Principal("prog_aaa111", "agent", "runtime")
MOCK = Principal("owner", "mock", "test")
STRANGER = Principal("mallory", "human", "console")


def spec(name="prog_aaa111", perms=("read_files", "write_files", "spend_money"), cost=5.0):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms), inputs=["sorgu"], outputs=["netice"], cost_limit=cost)


@pytest.fixture
def world(tmp_path):
    reg = Registry(":memory:")
    return Company(reg, tmp_path / "c.db", tmp_path / "audit.jsonl", owners={"owner"}), reg, tmp_path


def test_baseline_registration_is_always_limited_and_allowed_for_the_system(world):
    c, reg, _ = world
    a = c.admin.register_baseline_agent(spec(), "development", "Proqramçı", SYSTEM)
    assert a.authority.permissions == frozenset({"read_files"}) and a.authority.sensitive_ok is False
    assert a.authority.delegate_to == frozenset() and a.authority.max_task_cost == 0.0 and a.budget == 0.0 and a.status == "active"
    assert reg.get("prog_aaa111").spec.permissions == ["read_files"] and reg.get("prog_aaa111").spec.cost_limit == 0.0
    assert c.admin.register_baseline_agent(spec("prog_bbb222", perms=("write_files",)), "development", "x", OWNER).authority.permissions == frozenset()


def test_baseline_registration_rejects_agents_mock_strangers_duplicates_and_unknown_departments(world):
    c, _, _ = world
    for who, code in ((AGENT, "self_grant"), (MOCK, "mock_rejected"), (STRANGER, "unauthorized")):
        with pytest.raises(CompanyDenied) as e:
            c.admin.register_baseline_agent(spec(), "development", "x", who)
        assert e.value.code == code
    c.admin.register_baseline_agent(spec(), "development", "x", SYSTEM)
    with pytest.raises(CompanyDenied) as e:
        c.admin.register_baseline_agent(spec(), "development", "x", SYSTEM)
    assert e.value.code == "exists"
    with pytest.raises(CompanyDenied):
        c.admin.register_baseline_agent(spec("prog_ccc333"), "narnia", "x", SYSTEM)


def test_suspend_removes_effective_permissions_and_resume_restores_exactly_the_old_authority(world):
    c, reg, _ = world
    c.admin.register_agent(spec(perms=("read_files", "write_files")), "development", "x",
                           Authority(frozenset({"read_files", "write_files"}), False, frozenset(), 1.0), 2.0, OWNER)
    s = c.admin.suspend("prog_aaa111", SYSTEM)
    assert s.status == "suspended_system" and reg.get("prog_aaa111").spec.permissions == []
    assert [a.name for a in c.view.agents()] == []                      # aktiv siyahıda yoxdur
    r = c.admin.resume("prog_aaa111", SYSTEM)
    assert r.status == "active" and r.authority.permissions == frozenset({"read_files", "write_files"}) and r.budget == 2.0
    assert reg.get("prog_aaa111").spec.permissions == ["read_files", "write_files"]


def test_owner_suspension_can_only_be_lifted_by_the_owner(world):
    c, reg, _ = world
    c.admin.register_baseline_agent(spec(), "development", "x", SYSTEM)
    assert c.admin.suspend("prog_aaa111", OWNER).status == "suspended_owner"
    with pytest.raises(CompanyDenied) as e:
        c.admin.resume("prog_aaa111", SYSTEM)
    assert e.value.code == "owner_suspended"
    assert c.admin.resume("prog_aaa111", OWNER).status == "active"


def test_agents_cannot_suspend_or_resume_and_retired_agents_stay_retired(world):
    c, reg, _ = world
    c.admin.register_baseline_agent(spec(), "development", "x", SYSTEM)
    for fn in (c.admin.suspend, c.admin.resume):
        with pytest.raises(CompanyDenied) as e:
            fn("prog_aaa111", AGENT)
        assert e.value.code == "self_grant"
    with pytest.raises(CompanyDenied):
        c.admin.resume("prog_aaa111", SYSTEM)                       # aktiv agenti davam etdirmək mənasızdır
    c.admin.suspend("prog_aaa111", SYSTEM)
    assert c.admin.retire("prog_aaa111", SYSTEM).status == "retired"  # dayandırılmışı arxivləmək olar
    with pytest.raises(CompanyDenied) as e:
        c.admin.resume("prog_aaa111", OWNER)
    assert e.value.code == "not_suspended"
    with pytest.raises(CompanyDenied):
        c.admin.suspend("yoxdur_agent", SYSTEM)


def test_suspended_agents_cannot_hand_off_tasks(world):
    from agents.orchestrator import Task
    c, _, _ = world
    c.admin.register_agent(spec(perms=("read_files",)), "development", "x",
                           Authority(frozenset({"read_files"}), False, frozenset({"research"}), 1.0), 2.0, OWNER)
    c.admin.register_baseline_agent(spec("res_agent_01", perms=("read_files",)), "research", "r", SYSTEM)
    t = Task("research", {"sorgu": "x"}, "read_files", 0.5, "n")
    assert c.view.handoff("prog_aaa111", "research", t).ok
    c.admin.suspend("prog_aaa111", SYSTEM)
    assert not c.view.handoff("prog_aaa111", "research", t).ok


def test_company_audit_records_baseline_and_suspension_events(world):
    import json
    c, _, tmp = world
    c.admin.register_baseline_agent(spec(), "development", "x", SYSTEM)
    c.admin.suspend("prog_aaa111", OWNER)
    events = [json.loads(l)["event"] for l in (tmp / "audit.jsonl").read_text().splitlines()]
    assert "company_baseline_registered" in events and "company_agent_suspended" in events
