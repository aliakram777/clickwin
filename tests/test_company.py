"""Virtual şirkət. Buradakı "sahib" principal-ları test obyektləridir (kind="human"/"mock"), real istifadəçi
təsdiqi deyil. Heç bir real API, ödəniş və ya xarici çağırış yoxdur."""
import ast
import json
from pathlib import Path

import pytest

from agents.ceo import CEO, RulePlanner
from agents.company import (
    DEPARTMENTS, Authority, Company, CompanyDenied, merge_results, record_approval, record_job_results,
    record_plan_decision,
)
from agents.llm import GuardedLLM, LLMConfig, MockProvider
from agents.llm_planner import LLMPlanner
from agents.memory import Actor, MemoryService
from agents.orchestrator import Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec
from agents.workflow import PlanStep
from safety.approvals import ApprovalService, Principal
from safety.ledger import CostLedger, reconcile

ROOT = Path(__file__).resolve().parent.parent
HUMAN = Principal("owner", "human", "web")
MOCK = Principal("owner", "mock", "test")
SYS = Principal("ceo", "system", "internal")
AGENT_P = Principal("res_agent", "agent", "internal")
STRANGER = Principal("yad", "human", "telegram")


def spec(name, perms=("read_files",), limit=5.0):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms), inputs=["sorgu"],
                     outputs=["netice"], cost_limit=limit)


def auth(perms=("read_files",), delegate=(), max_cost=1.0, sensitive=False):
    return Authority(frozenset(perms), sensitive, frozenset(delegate), max_cost)


@pytest.fixture
def env(tmp_path):
    reg = Registry(":memory:")
    audit = tmp_path / "audit.jsonl"

    def make(allow_mock=False):
        return Company(reg, tmp_path / "company.db", audit, owners={"owner"}, allow_mock=allow_mock)

    return reg, make, audit, tmp_path


def events(audit):
    p = Path(audit)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def codes(audit, event):
    return [e.get("code") for e in events(audit) if e["event"] == event]


def add(adm, name="res_agent", dept="research", perms=("read_files", "write_files"), limit=5.0,
        authority=None, budget=2.0, principal=HUMAN):
    return adm.register_agent(spec(name, perms, limit), dept, "Rol: " + name, authority or auth(), budget, principal)


# --- şöbələr və qeydiyyat ---

def test_department_catalog_is_fixed():
    assert set(DEPARTMENTS) == {"research", "development", "finance", "security", "marketing", "sales",
                                "operations", "support"}


def test_register_agent_stores_role_authority_budget_and_registers_effective_spec(env):
    reg, make, audit, _ = env
    c = make()
    a = add(c.admin)
    assert (a.name, a.department, a.role, a.status, a.budget) == ("res_agent", "research", "Rol: res_agent", "active", 2.0)
    assert a.authority.permissions == frozenset({"read_files"})
    eff = reg.get("res_agent")
    assert eff.department == "research" and eff.spec.permissions == ["read_files"]  # spec ∩ authority
    assert eff.spec.cost_limit == 2.0  # min(spec limiti, büdcə)
    assert c.view.get("res_agent").authority == a.authority
    assert "company_agent_registered" in [e["event"] for e in events(audit)]


@pytest.mark.parametrize("kw,word", [
    (dict(dept="legal"), "şöbə"),
    (dict(budget=6.0), "büdcə"),
    (dict(authority=auth(("read_files", "publish"), sensitive=True)), "spec"),            # spec-dən artıq
    (dict(perms=("read_files", "publish"), authority=auth(("publish",))), "həssas"),       # sensitive_ok yox
    (dict(authority=auth(max_cost=9.0)), "tapşırıq"),                                       # max_task_cost > büdcə
])
def test_invalid_registrations_denied_and_audited(env, kw, word):
    reg, make, audit, _ = env
    c = make()
    with pytest.raises(CompanyDenied) as exc:
        add(c.admin, **kw)
    assert word in str(exc.value)
    assert reg.list() == [] and c.view.agents() == []
    assert "company_denied" in [e["event"] for e in events(audit)]


def test_role_must_be_meaningful_and_duplicates_rejected(env):
    _, make, _, _ = env
    c = make()
    with pytest.raises(CompanyDenied):
        c.admin.register_agent(spec("res_agent"), "research", "   ", auth(), 1.0, HUMAN)
    add(c.admin)
    with pytest.raises(CompanyDenied):
        add(c.admin)


def test_only_the_owner_can_create_authority(env):
    reg, make, audit, _ = env
    c = make()
    for p in (STRANGER, SYS, AGENT_P, MOCK):  # mock defoltda qəbul olunmur
        with pytest.raises(CompanyDenied):
            add(c.admin, principal=p)
    assert reg.list() == []
    assert set(codes(audit, "company_denied")) >= {"unauthorized", "system_cannot_expand", "self_grant", "mock_rejected"}


def test_mock_owner_is_allowed_only_when_enabled_and_is_marked(env):
    _, make, audit, _ = env
    c = make(allow_mock=True)
    add(c.admin, principal=MOCK)
    row = [e for e in events(audit) if e["event"] == "company_agent_registered"][0]
    assert (row["source_kind"], row["human"]) == ("mock", False)


# --- agent özünə səlahiyyət verə bilməz ---

def test_expansion_needs_the_owner_and_narrowing_is_allowed_for_system(env):
    reg, make, audit, _ = env
    c = make()
    add(c.admin)
    wider = auth(("read_files", "write_files"), delegate=("finance",), max_cost=2.0)
    for p in (AGENT_P, SYS, STRANGER):
        with pytest.raises(CompanyDenied):
            c.admin.set_authority("res_agent", wider, p)
    assert reg.get("res_agent").spec.permissions == ["read_files"]  # dəyişməyib
    updated = c.admin.set_authority("res_agent", wider, HUMAN)
    assert reg.get("res_agent").spec.permissions == ["read_files", "write_files"]
    assert updated.authority.delegate_to == frozenset({"finance"})
    narrow = auth(("read_files",))
    c.admin.set_authority("res_agent", narrow, SYS)  # daralma: sistem də edə bilər
    assert reg.get("res_agent").spec.permissions == ["read_files"]
    assert "company_authority_changed" in [e["event"] for e in events(audit)]


def test_authority_can_never_exceed_the_agents_spec_or_skip_sensitive_flag(env):
    reg, make, _, _ = env
    c = make()
    add(c.admin, perms=("read_files", "publish"))
    for bad in (auth(("read_files", "spend_money"), sensitive=True), auth(("publish",))):
        with pytest.raises(CompanyDenied):
            c.admin.set_authority("res_agent", bad, HUMAN)
    c.admin.set_authority("res_agent", auth(("publish",), sensitive=True), HUMAN)
    assert reg.get("res_agent").spec.permissions == ["publish"]


def test_budget_changes_follow_the_same_rule(env):
    reg, make, _, _ = env
    c = make()
    add(c.admin)
    with pytest.raises(CompanyDenied):
        c.admin.set_budget("res_agent", 3.0, SYS)       # artırma: yalnız sahib
    with pytest.raises(CompanyDenied):
        c.admin.set_budget("res_agent", 6.0, HUMAN)     # spec limitindən çox
    c.admin.set_budget("res_agent", 3.0, HUMAN)
    assert reg.get("res_agent").spec.cost_limit == 3.0
    c.admin.set_budget("res_agent", 1.5, SYS)
    assert c.view.get("res_agent").budget == 1.5 and reg.get("res_agent").spec.cost_limit == 1.5
    with pytest.raises(CompanyDenied):
        c.admin.set_budget("res_agent", 1.5, AGENT_P)


def test_retire_removes_all_effective_permissions(env):
    reg, make, _, _ = env
    c = make()
    add(c.admin)
    for p in (AGENT_P, STRANGER):
        with pytest.raises(CompanyDenied):
            c.admin.retire("res_agent", p)
    c.admin.retire("res_agent", SYS)  # sistem agentin hüquqlarını azalda bilər
    assert reg.get("res_agent").spec.permissions == []
    assert c.view.agents() == [] and [a.name for a in c.view.agents(include_retired=True)] == ["res_agent"]
    assert c.view.get("res_agent").status == "retired"


def test_facets_are_separated(env):
    _, make, _, _ = env
    c = make()
    for name in ("register_agent", "set_authority", "set_budget", "retire", "set_department_cost"):
        assert not hasattr(c.view, name) and hasattr(c.admin, name)
    assert not hasattr(c.admin, "handoff")


def test_orchestration_modules_cannot_reach_company_admin():
    for name in ("orchestrator.py", "ceo.py", "llm.py", "llm_planner.py", "workflow.py", "registry.py"):
        tree = ast.parse((ROOT / "agents" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"agents.company"}, name
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"set_authority", "register_agent", "set_budget", "retire"}, (name, node.lineno)


# --- departament xərcləri və büdcə siyasəti ---

def test_department_costs_only_for_departments_with_active_agents(env):
    _, make, _, _ = env
    c = make()
    assert c.view.department_costs() == {}
    add(c.admin)
    add(c.admin, "fin_agent", "finance", perms=("read_files",))
    assert c.view.department_costs() == {"research": 0.5, "finance": 0.5}
    c.admin.set_department_cost("finance", 0.8, HUMAN)
    assert c.view.department_costs()["finance"] == 0.8
    for p in (SYS, AGENT_P, STRANGER):
        with pytest.raises(CompanyDenied):
            c.admin.set_department_cost("finance", 0.1, p)
    with pytest.raises(CompanyDenied):
        c.admin.set_department_cost("legal", 0.1, HUMAN)
    add(c.admin, "dev_agent", "development")
    c.admin.retire("dev_agent", HUMAN)
    assert "development" not in c.view.department_costs()


def test_budget_policy_maps_agent_budgets(env):
    _, make, _, _ = env
    c = make()
    add(c.admin, budget=2.0)
    add(c.admin, "fin_agent", "finance", budget=1.0)
    pol = c.view.budget_policy(global_limit=10.0, task_limit=1.0)
    assert (pol.global_limit, pol.task_limit, dict(pol.agent_limits)) == (10.0, 1.0, {"res_agent": 2.0, "fin_agent": 1.0})


# --- agentlər arası ötürmə ---

def seeded(c):
    add(c.admin, "ops_agent", "operations", perms=("read_files", "write_files"),
        authority=auth(("read_files",), delegate=("research", "finance"), max_cost=1.0), budget=2.0)
    add(c.admin, "res_agent", "research", budget=2.0)
    add(c.admin, "fin_agent", "finance", perms=("read_files", "spend_money"),
        authority=auth(("read_files", "spend_money"), sensitive=True, max_cost=1.0), budget=2.0)
    add(c.admin, "boss_agent", "operations", perms=("read_files", "spend_money"),
        authority=auth(("read_files", "spend_money"), delegate=("finance",), max_cost=1.0, sensitive=True),
        budget=2.0)


def htask(perm="read_files", cost=0.5, op_id=""):
    return Task("operations", {"sorgu": "s"}, perm, cost, description="məxfi təsvir", op_id=op_id)


def test_authorized_handoff_creates_audited_task_for_the_target_department(env):
    _, make, audit, _ = env
    c = make()
    seeded(c)
    res = c.view.handoff("ops_agent", "research", htask(), reason="araşdırma lazımdır")
    assert res.ok and res.task.department == "research" and res.task.permission == "read_files"
    assert res.task.op_id.startswith("handoff:")
    row = [e for e in events(audit) if e["event"] == "company_handoff"][0]
    assert (row["from_agent"], row["to_department"], row["permission"], row["cost"]) == \
        ("ops_agent", "research", "read_files", 0.5)
    assert "məxfi təsvir" not in json.dumps(row)  # mətn yox, yalnız hash
    assert len(row["description_sha256"]) == 64


@pytest.mark.parametrize("src,dst,task_kw,code", [
    ("yoxdur", "research", {}, "unknown_agent"),
    ("ops_agent", "legal", {}, "unknown_department"),
    ("ops_agent", "support", {}, "no_active_agents"),
    ("ops_agent", "operations", {}, "not_delegable"),                     # delegate_to-da yoxdur
    ("ops_agent", "research", {"perm": "write_files"}, "permission_not_held"),
    ("ops_agent", "research", {"cost": 1.5}, "cost_over_limit"),
    ("ops_agent", "finance", {"perm": "spend_money"}, "permission_not_held"),
    ("res_agent", "finance", {}, "not_delegable"),                        # delegate_to boş
])
def test_unauthorized_handoffs_are_denied_and_audited(env, src, dst, task_kw, code):
    _, make, audit, _ = env
    c = make()
    seeded(c)
    res = c.view.handoff(src, dst, htask(**task_kw))
    assert not res.ok and res.task is None
    assert codes(audit, "company_handoff_denied")[-1] == code
    assert not [e for e in events(audit) if e["event"] == "company_handoff"]


def test_sensitive_handoff_and_retired_agents(env):
    _, make, audit, _ = env
    c = make()
    seeded(c)
    assert c.view.handoff("boss_agent", "finance", htask("spend_money", op_id="op-1")).ok
    assert not c.view.handoff("ops_agent", "finance", htask("spend_money")).ok  # ops_agent bu icazəni daşımır
    c.admin.retire("boss_agent", HUMAN)
    res = c.view.handoff("boss_agent", "finance", htask("spend_money", op_id="op-2"))
    assert not res.ok and codes(audit, "company_handoff_denied")[-1] == "agent_retired"


def test_financial_handoff_still_goes_through_broker_and_approval(env):
    reg, make, audit, tmp = env
    c = make()
    seeded(c)
    svc = ApprovalService(tmp / "approvals.db", audit, owners={"owner"}, allow_mock=True)
    ledger = CostLedger(tmp / "ledger.db")
    calls = []
    orch = Orchestrator(reg, lambda s, d: calls.append(s.name) or {"netice": "ok"}, audit_path=audit,
                        stop_path=tmp / "STOP", errors_path=tmp / "e.jsonl", ledger=ledger,
                        approvals=svc.client, policy=c.view.budget_policy(global_limit=10.0))
    handed = c.view.handoff("boss_agent", "finance", htask("spend_money", op_id="pay-1")).task
    first = orch.execute(handed)
    assert first.status == "pending_approval" and calls == []
    assert svc.gateway.approve(first.approval_id, Principal("owner", "mock", "test")).ok
    assert orch.execute(handed).status == "ok" and calls == ["fin_agent"]
    assert orch.execute(handed).status == "duplicate" and calls == ["fin_agent"]


def test_agent_budget_from_company_is_enforced_by_the_broker(env):
    reg, make, audit, tmp = env
    c = make()
    seeded(c)
    ledger = CostLedger(tmp / "ledger.db")
    orch = Orchestrator(reg, lambda s, d: {"netice": "ok"}, audit_path=audit, stop_path=tmp / "STOP",
                        errors_path=tmp / "e.jsonl", ledger=ledger, policy=c.view.budget_policy())
    t = Task("research", {"sorgu": "s"}, "read_files", 1.5)
    assert orch.execute(t).status == "ok"
    assert orch.execute(Task("research", {"sorgu": "s"}, "read_files", 1.5)).status == "blocked"  # büdcə 2.0


def test_authority_clipping_is_enforced_by_the_gate(env):
    reg, make, audit, tmp = env
    c = make()
    add(c.admin, perms=("read_files", "write_files"))  # authority yalnız read_files
    orch = Orchestrator(reg, lambda s, d: {"netice": "ok"}, audit_path=audit, stop_path=tmp / "STOP",
                        errors_path=tmp / "e.jsonl")
    assert orch.execute(Task("research", {"sorgu": "s"}, "write_files", 0.0)).status == "blocked"
    assert orch.execute(Task("research", {"sorgu": "s"}, "read_files", 0.0)).status == "ok"


# --- nəticələrin birləşdirilməsi və yaddaşa yazılması ---

@pytest.fixture
def job(env):
    reg, make, audit, tmp = env
    c = make()
    seeded(c)
    ledger = CostLedger(tmp / "ledger.db")
    svc = ApprovalService(tmp / "approvals.db", audit, owners={"owner"}, allow_mock=True)

    def runner(spec, data):
        if spec.name == "fin_agent":
            return {"netice": "kart sk-test-FAKEKEY1234567890abcdef istifadə edildi"}
        return {"netice": f"{spec.name}-nəticə"}

    orch = Orchestrator(reg, runner, audit_path=audit, stop_path=tmp / "STOP", errors_path=tmp / "e.jsonl",
                        ledger=ledger, approvals=svc.client)
    ceo = CEO(orch, RulePlanner([]), budget=10.0)
    steps = [PlanStep("a", "research", "read_files", 0.5), PlanStep("b", "finance", "read_files", 0.5, ("a",)),
             PlanStep("c", "finance", "spend_money", 0.5, ("b",))]
    report = ceo.run_workflow("hesabat", steps, job_id="j1")
    mem = MemoryService(tmp / "mem.db", tmp / "mem_audit.jsonl")
    return report, mem, svc, audit, ledger


def test_merge_results_bundles_status_source_and_pending(job):
    report, *_ = job
    bundle = merge_results(report)
    assert [(s.step_id, s.agent, s.status) for s in bundle.steps] == [
        ("a", "res_agent", "ok"), ("b", "fin_agent", "ok"), ("c", "fin_agent", "pending_approval")]
    assert not bundle.ok and len(bundle.pending_approvals) == 1 and bundle.failures[0].step_id == "c"
    assert bundle.steps[0].output == {"netice": "res_agent-nəticə"}


def test_job_results_are_saved_with_source_status_and_without_secrets(job):
    report, mem, *_ = job
    results = record_job_results(mem, report, job_id="j1", goal="hesabat")
    recs = mem.search(category="result", limit=100)
    by_step = {r.meta["step_id"]: r for r in recs}
    assert set(by_step) == {"a", "b", "c"}
    a = by_step["a"]
    assert (a.source, a.provenance, a.meta["status"], a.meta["job_id"]) == ("agent:res_agent", "agent", "ok", "j1")
    assert "res_agent-nəticə" in a.content
    assert "sk-test" not in by_step["b"].content and by_step["b"].meta["output_withheld"] is True
    assert by_step["c"].meta["status"] == "pending_approval" and by_step["c"].meta["approval_id"]
    assert [r.category for r in mem.search(category="task")] == ["task"]
    again = record_job_results(mem, report, job_id="j1", goal="hesabat")  # təkrar yazılmır
    assert all(r.duplicate for r in again) and len(mem.search(category="result", limit=100)) == 3
    assert len(results) == len(again)


def test_plan_decision_keeps_mock_and_real_llm_apart(job, tmp_path):
    report, mem, *_ = job
    provider = MockProvider(default='{"steps":[{"id":"a","department":"research","permission":"read_files"}]}')
    client = GuardedLLM(provider, LLMConfig(provider="mock", max_response_chars=5000),
                        log_path=tmp_path / "llm.jsonl", stop_path=tmp_path / "STOP")
    proposal = LLMPlanner(client, {"research": 0.5}).propose("g")
    rec = record_plan_decision(mem, "g", proposal, job_id="j1").record
    assert (rec.category, rec.provenance, rec.source) == ("decision", "llm_mock", "llm:mock")
    assert rec.meta["is_mock"] is True and len(rec.meta["response_sha256"]) == 64
    assert "research" in rec.content and '"steps"' not in rec.content  # xam model mətni saxlanmır
    from dataclasses import replace
    real = record_plan_decision(mem, "g", replace(proposal, is_mock=False, provider="real-x"), job_id="j2").record
    assert (real.provenance, real.source) == ("llm_real", "llm:real-x")


def test_user_approvals_are_recorded_but_never_used_as_authority(job):
    report, mem, svc, *_ = job
    pending = svc.gateway.pending()[0]
    svc.gateway.approve(pending.id, Principal("owner", "mock", "test"))
    rec = record_approval(mem, svc.gateway.get(pending.id)).record
    assert (rec.category, rec.provenance) == ("approval", "mock")  # mock təsdiqi real kimi göstərilmir
    assert rec.meta["human"] is False and rec.meta["approval_id"] == pending.id
    # yaddaşdakı təsdiq qeydi yeni effekt üçün təsdiq yerinə keçmir
    assert svc.client.check("yoxdur", pending.payload_hash).ok is False
