"""Mərhələ 17 təhlükəsizlik möhkəmləndirməsi (audit F-1, F-2, F-3). Heç bir real şəbəkə/ödəniş/açar yoxdur: yalnız saxta provayder və yerli Ledger."""
import dataclasses
import pickle
import sqlite3
from pathlib import Path

import pytest

from agents.agent_factory import AgentFactory, DispatchAuthorization, FactoryDenied
from agents.agent_runtime import AgentLLMRunner, AgentDispatcher, approval_input
from agents.company import Company
from agents.factory_auth import PrincipalAuthority, SignedPrincipal
from agents.hybrid import AiStore, CloudEngine, HybridRouter, Mode
from agents.llm import (GuardedLLM, LLMBudgetExceeded, LLMConfig, LLMProviderError, LLMResponse, SpendScope, spend_scope)
from agents.orchestrator import ExecutionError, Task
from agents.registry import Registry
from runtime.env import build_principal_authority
from safety.approvals import Principal
from tests.factory_keys import AUTH, DISPATCHER, MOCK, OWNER, STRANGER, SYSTEM
from tests.test_agent_runtime import publisher, rows, task, world  # noqa: F401  (fixture)
from tests.test_hybrid_router import Fake

FORGED_OWNER = Principal("owner", "human", "console")        # sahibin id-si gizli deyil: hər kəs bunu yarada bilər
FORGED_SYSTEM = Principal("naymera", "system", "core")
FORGED_DISPATCHER = Principal("dispatcher", "system", "runtime")


# ======================= F-1: şəxsiyyət saxtakarlığı və fabrik imtiyazları =======================

def test_forged_owner_and_system_principals_are_rejected_everywhere(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    t = w.factory.control.assign(a.agent_id, "t", "mətn", SYSTEM)
    attempts = [
        lambda p: w.factory.control.spawn("researcher", p),
        lambda p: w.factory.control.assign(a.agent_id, "x", "y", p),
        lambda p: w.factory.control.stop(a.agent_id, p),
        lambda p: w.factory.control.cancel_task(t.task_id, p),
        lambda p: w.factory.control.archive(a.agent_id, p),
        lambda p: w.factory.admin.create_agent("programmer", p),
        lambda p: w.factory.admin.set_budget(a.agent_id, 1.0, p),
        lambda p: w.factory.admin.grant(a.agent_id, p, permissions=("read_files", "write_files")),
        lambda p: w.factory.admin.set_policy(p, global_budget_cap=100.0),
        lambda p: w.factory.admin.delete(a.agent_id, p),
    ]
    for fn in attempts:
        for forged in (FORGED_OWNER, FORGED_SYSTEM, FORGED_DISPATCHER):
            with pytest.raises(FactoryDenied) as e:
                fn(forged)
            assert e.value.code == "forged_principal", (fn, forged)
    # heç nə dəyişməyib
    assert w.factory.view.agent(a.agent_id).status == "active" and w.factory.view.agent(a.agent_id).budget == 0.0
    assert w.factory.view.task(t.task_id).state == "queued" and len(w.factory.view.agents()) == 1
    assert w.factory.view.policy.global_budget_cap == 0.0
    assert sum(1 for r in rows(w) if r.get("event") == "factory_denied" and r.get("code") == "forged_principal") >= len(attempts) * 3


def test_tampered_or_copied_signatures_do_not_authenticate(world):
    w = world
    stolen_proof = OWNER.proof
    for forged in (dataclasses.replace(OWNER, id="mallory"), dataclasses.replace(OWNER, channel="other"),
                   SignedPrincipal("owner", "human", "console", "owner", stolen_proof[:-1] + ("0" if stolen_proof[-1] != "0" else "1")),
                   SignedPrincipal("owner", "human", "console", "owner", ""), SignedPrincipal("owner", "human", "console", "owner", "x" * 64),
                   dataclasses.replace(SYSTEM, kind="human", id="owner")):
        with pytest.raises(FactoryDenied) as e:
            w.factory.admin.create_agent("programmer", forged)
        assert e.value.code in ("forged_principal", "unauthorized"), forged
    other = PrincipalAuthority(b"\x07" * 32)                                   # başqa açarla imzalanmış şəxsiyyət
    with pytest.raises(FactoryDenied) as e:
        w.factory.admin.create_agent("programmer", other.owner("owner", "console"))
    assert e.value.code == "forged_principal"


def test_roles_cannot_be_swapped(world):
    w = world
    with pytest.raises(FactoryDenied) as e:                      # dispetçer rolu control əməliyyatı edə bilməz
        w.factory.control.spawn("programmer", DISPATCHER)
    assert e.value.code == "forged_principal"
    with pytest.raises(FactoryDenied) as e:                      # sistem sahib-yalnız əməliyyat edə bilməz
        w.factory.admin.create_agent("programmer", SYSTEM)
    assert e.value.code == "owner_only"
    a = w.factory.control.spawn("programmer", SYSTEM)
    t = w.factory.control.assign(a.agent_id, "t", "p", SYSTEM)
    for who in (OWNER, SYSTEM):                                  # sahib/sistem tapşırığı tuta bilməz
        with pytest.raises(FactoryDenied) as e:
            w.factory.runtime.claim(t.task_id, who)
        assert e.value.code == "forged_principal"
    assert w.factory.view.task(t.task_id).state == "queued"
    with pytest.raises(FactoryDenied) as e:                      # sahib siyahısında olmayan imzalanmış şəxs
        w.factory.admin.create_agent("programmer", STRANGER)
    assert e.value.code == "unauthorized"
    with pytest.raises(FactoryDenied) as e:                      # mock principal imzalansa da real sahib deyil
        w.factory.admin.create_agent("programmer", MOCK)
    assert e.value.code == "mock_rejected"


def test_without_trusted_authentication_privileged_operations_fail_closed(tmp_path):
    reg = Registry(":memory:")
    audit = tmp_path / "a.jsonl"
    company = Company(reg, tmp_path / "c.db", audit, owners={"owner"})
    f = AgentFactory(company, tmp_path / "f.db", audit, tmp_path / "ws", owners={"owner"})          # authority=None
    for p in (OWNER, SYSTEM, FORGED_OWNER, FORGED_SYSTEM):
        for fn in (lambda: f.control.spawn("programmer", p), lambda: f.admin.create_agent("programmer", p),
                   lambda: f.admin.set_policy(p, max_agents=3)):
            with pytest.raises(FactoryDenied) as e:
                fn()
            assert e.value.code == "owner_auth_unavailable"
    with pytest.raises(FactoryDenied) as e:
        f.runtime.claim("tsk_x", DISPATCHER)
    assert e.value.code == "owner_auth_unavailable"
    assert f.view.agents() == [] and f.view.policy.max_agents == 16                              # oxu işləyir, yazı yoxdur


def test_authority_is_built_from_the_server_env_only_and_never_leaks():
    assert build_principal_authority({}) is None and build_principal_authority({"NAYMERA_FACTORY_AUTH_KEY": "qısa"}) is None
    auth = build_principal_authority({"NAYMERA_FACTORY_AUTH_KEY": "k" * 40})
    assert isinstance(auth, PrincipalAuthority) and "k" * 8 not in repr(auth) and auth.verify(auth.owner("owner"))
    with pytest.raises(TypeError):
        pickle.dumps(auth)
    with pytest.raises(ValueError):
        PrincipalAuthority(b"short")
    assert "k" * 8 not in repr(auth.owner("owner")) and auth.owner("owner").proof not in repr(auth.owner("owner"))


def test_factory_no_longer_exposes_a_system_principal(world):
    assert not hasattr(world.factory, "system")
    assert not hasattr(world.factory._core, "system")


def test_results_cannot_be_submitted_without_task_bound_dispatcher_authorization(world):
    w = world
    a, t = task(w)
    h = w.factory.handle(a.agent_id)
    with pytest.raises(FactoryDenied) as e:                                     # tapşırıq hətta queued ikən də səlahiyyət tələb olunur
        h.submit_result(t.task_id, "uydurma nəticə")
    assert e.value.code == "dispatch_authorization_required"
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    real = claimed.authorization
    forged = DispatchAuthorization(t.task_id, a.agent_id, claimed.task.run_op, claimed.task.attempts, "0" * 64)
    for bad, code in ((None, "dispatch_authorization_required"), (forged, "dispatch_authorization_invalid"),
                      (dataclasses.replace(real, token=""), "dispatch_authorization_required"),
                      (dataclasses.replace(real, attempt=real.attempt + 1), "dispatch_authorization_invalid"),
                      (dataclasses.replace(real, run_op="x:a9"), "dispatch_authorization_invalid"),
                      ("token", "dispatch_authorization_required")):
        with pytest.raises(FactoryDenied) as e:
            h.submit_result(t.task_id, "uydurma nəticə", authorization=bad)
        assert e.value.code == code, bad
    assert w.factory.view.reports() == [] and w.factory.view.task(t.task_id).state == "running"
    rep = h.submit_result(t.task_id, "həqiqi nəticə", authorization=real)
    assert rep.summary == "həqiqi nəticə"
    with pytest.raises(FactoryDenied):                                           # təkrar istifadə yoxdur
        h.submit_result(t.task_id, "ikinci", authorization=real)


def test_authorization_is_bound_to_its_own_task_and_attempt(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    t1 = w.factory.control.assign(a.agent_id, "bir", "iş bir", SYSTEM)
    t2 = w.factory.control.assign(a.agent_id, "iki", "iş iki", SYSTEM)
    c1 = w.factory.runtime.claim(t1.task_id, DISPATCHER)
    c2 = w.factory.runtime.claim(t2.task_id, DISPATCHER)
    with pytest.raises(FactoryDenied) as e:                                      # t1 səlahiyyəti ilə t2 nəticəsi
        w.factory.handle(a.agent_id).submit_result(t2.task_id, "qarışdırma", authorization=c1.authorization)
    assert e.value.code == "dispatch_authorization_invalid"
    # köhnə cəhd: retry ilə run_op sıfırlanır, köhnə səlahiyyət keçmir
    w.factory.runtime.fail(c2.authorization, "müvəqqəti", retry=True)
    c2b = w.factory.runtime.claim(t2.task_id, DISPATCHER)
    with pytest.raises(FactoryDenied) as e:
        w.factory.handle(a.agent_id).submit_result(t2.task_id, "köhnə", authorization=c2.authorization)
    assert e.value.code == "dispatch_authorization_invalid"
    assert w.factory.handle(a.agent_id).submit_result(t2.task_id, "yeni", authorization=c2b.authorization).summary == "yeni"
    # icra vəziyyəti dəyişiklikləri də səlahiyyətsiz mümkün deyil
    for fn in (lambda: w.factory.runtime.fail(None, "x"), lambda: w.factory.runtime.await_approval(forged_auth(t1), "apr_1"),
               lambda: w.factory.runtime.note_success(forged_auth(t1))):
        with pytest.raises(FactoryDenied):
            fn()
    assert w.factory.view.task(t1.task_id).state == "running"


def forged_auth(t):
    return DispatchAuthorization(t.task_id, t.agent_id, "", 0, "f" * 64)


def test_dispatcher_with_a_forged_identity_cannot_run_anything(world):
    w = world
    a, t = task(w)
    bad = AgentDispatcher(w.factory, w.orch, w.runner, principal=FORGED_DISPATCHER, approvals=w.approvals.reader)
    with pytest.raises(FactoryDenied) as e:
        bad.execute(t.task_id)
    assert e.value.code == "forged_principal" and w.cloud.calls == 0 and w.factory.view.task(t.task_id).state == "queued"
    assert w.disp.execute(t.task_id).status == "done"                           # imzalı dispetçer işləyir


# ======================= F-2: təsdiq ilə icra olunan mətn eynidir =======================

def approved_publish(w, prompt="saytın qaralamasını hazırla"):
    a = publisher(w)
    t = w.factory.control.assign(a.agent_id, "yayımla", prompt, SYSTEM, permission="publish", kind="content")
    r = w.disp.execute(t.task_id)
    assert r.status == "awaiting_approval"
    return a, t, r


def test_the_approved_text_is_exactly_the_executed_text(world):
    w = world
    prompt = "saytın qaralamasını hazırla: giriş bölməsi"
    a, t, r = approved_publish(w, prompt)
    ticket = w.approvals.reader.get(r.approval_id)
    assert ticket.payload == approval_input(a, w.factory.view.task(t.task_id))          # sahibin gördüyü giriş
    assert ticket.payload["sorgu"] == prompt
    assert w.approvals.gateway.approve(r.approval_id, OWNER, "ok").ok
    assert w.disp.execute(t.task_id).status == "done"
    assert w.cloud.calls == 1 and w.cloud.prompts[0] == prompt and w.cloud.systems[0] == a.system_instruction     # modelə gedən = təsdiqlənən


def test_prompt_altered_after_approval_request_is_not_executed(world):
    w = world
    a, t, r = approved_publish(w)
    assert w.approvals.gateway.approve(r.approval_id, OWNER, "ok").ok
    w.factory._core.conn.execute("UPDATE factory_tasks SET prompt = ? WHERE task_id = ?", ("başqa, təsdiqlənməmiş mətn", t.task_id))
    r2 = w.disp.execute(t.task_id)
    assert r2.status == "failed" and "prompt_integrity" in r2.reason and w.cloud.calls == 0
    assert w.approvals.reader.get(r.approval_id).status == "approved"                    # təsdiq işlədilməyib


def test_prompt_and_hash_both_rewritten_still_fails_the_approval_digest_check(world):
    w = world
    a, t, r = approved_publish(w)
    assert w.approvals.gateway.approve(r.approval_id, OWNER, "ok").ok
    import hashlib
    evil = "təsdiqlənməmiş mətn"
    w.factory._core.conn.execute("UPDATE factory_tasks SET prompt = ?, prompt_sha = ? WHERE task_id = ?",
                                 (evil, hashlib.sha256(evil.encode()).hexdigest(), t.task_id))
    r2 = w.disp.execute(t.task_id)
    assert r2.status == "failed" and w.cloud.calls == 0 and evil not in "".join(w.cloud.prompts)
    assert any("approval" in str(x.get("stage", "")) or "təsdiq" in str(x.get("reason", "")) for x in rows(w) if "reason" in x)


def test_runner_rejects_any_mismatch_between_approved_input_and_stored_task(world):
    w = world
    a, t = task(w, prompt="əsl mətn")
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    good = approval_input(a, claimed.task)
    spec = w.reg.list(a.department)[0].spec
    spec = next(x.spec for x in w.reg.list(a.department) if x.spec.name == a.name)
    for key, value in (("sorgu", "dəyişdirilmiş mətn"), ("prompt_sha256", "0" * 64), ("agent_id", "agt_other"), ("kind", "review"),
                       ("system_sha256", "1" * 64)):
        with pytest.raises(ExecutionError):
            w.runner(spec, {**good, key: value})
    with pytest.raises(ExecutionError):
        w.runner(spec, {k: v for k, v in good.items() if k != "sorgu"})
    assert w.cloud.calls == 0
    assert w.runner(spec, dict(good))["netice"]                                         # dəyişməmiş giriş işləyir
    assert w.cloud.prompts == ["əsl mətn"]


def test_runner_uses_the_approved_input_not_a_fresh_database_read(world):
    w = world
    a, t = task(w, prompt="əsl mətn")
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    spec = next(x.spec for x in w.reg.list(a.department) if x.spec.name == a.name)
    good = approval_input(a, claimed.task)
    w.factory._core.conn.execute("UPDATE factory_tasks SET prompt = 'icra zamanı dəyişdi' WHERE task_id = ?", (t.task_id,))
    with pytest.raises(ExecutionError):                                                   # icradan dərhal əvvəl yenidən yoxlama
        w.runner(spec, good)
    assert w.cloud.calls == 0


def test_approval_cannot_be_replayed_or_moved_to_another_task(world):
    w = world
    a, t, r = approved_publish(w, "birinci mətn")
    assert w.approvals.gateway.approve(r.approval_id, OWNER, "ok").ok
    assert w.disp.execute(t.task_id).status == "done" and w.cloud.calls == 1
    assert w.disp.execute(t.task_id).status == "not_claimable"                           # eyni tapşırıq təkrar icra olunmur
    # təkrar: eyni təsdiq/op_id ilə birbaşa Orkestrator (istehlak olunmuş təsdiq)
    t_row = w.factory.view.task(t.task_id)
    replay = Task(a.department, approval_input(a, t_row), "publish", t_row.cost, t_row.title, op_id=t_row.op_id + ":replay", approval_id=r.approval_id,
                  agent=a.name)
    res = w.orch.execute(replay)
    assert res.status in ("blocked", "duplicate") and w.cloud.calls == 1
    # başqa tapşırığa köçürmə: təsdiq başqa mətnə bağlıdır
    t2 = w.factory.control.assign(a.agent_id, "ikinci", "tamam fərqli mətn", SYSTEM, permission="publish", kind="content")
    w.factory._core.conn.execute("UPDATE factory_tasks SET approval_id = ? WHERE task_id = ?", (r.approval_id, t2.task_id))
    r2 = w.disp.execute(t2.task_id)
    assert r2.status == "failed" and w.cloud.calls == 1


def test_sensitive_tasks_get_no_memory_context_so_the_approved_text_is_the_whole_prompt(world):
    w = world

    class Rec:
        category, text, id, project, ts = "project", "gizli kontekst", "m1", "p", 1.0
    w.runner.memory = lambda: [Rec()]
    a, t, r = approved_publish(w, "yalnız bu mətn")
    assert w.approvals.gateway.approve(r.approval_id, OWNER, "ok").ok
    assert w.disp.execute(t.task_id).status == "done"
    assert w.cloud.prompts == ["yalnız bu mətn"]


# ======================= F-3: LLM xərci agent büdcəsinə və Ledger-ə bağlıdır =======================

class PaidProvider:
    """Saxta ödənişli provayder: şəbəkə yoxdur; yalnız çağırış sayır."""
    name, is_mock, model = "fake_paid", False, "fake-model"

    def __init__(self, script=None):
        self.calls, self.script = 0, list(script or [])

    def complete(self, request):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ödənişli cavab"
        if isinstance(item, BaseException):
            raise item
        return LLMResponse(item, 40, 20, self.model)


@pytest.fixture
def paid(world):
    w = world
    w.store.set_mode(Mode.CLOUD, "owner")
    w.store.set_consent("cloud_paid_use", True, "owner")
    w.provider = PaidProvider()
    cfg = LLMConfig(provider="openai_compat", model="fake-model", price_in_per_1k=0.1, price_out_per_1k=0.1, cost_limit=1.0, max_output_tokens=100,
                    timeout_s=2.0)
    w.llm = GuardedLLM(w.provider, cfg, log_path=w.tmp / "llm.jsonl", stop_path=w.stop, ledger=w.ledger)
    w.router = HybridRouter(CloudEngine(w.llm), Fake("local", state="not_configured"), w.store, clock=w.clock, stop_path=w.stop)
    w.runner.router = w.router
    w.factory.admin.set_policy(OWNER, global_budget_cap=5.0)
    return w


def funded(w, budget, role="programmer"):
    return w.factory.admin.create_agent(role, OWNER, budget=budget)


def llm_entries(w):
    return [e for e in w.ledger.entries() if e.op_id.startswith("llm:")]


def test_zero_budget_agent_never_triggers_paid_cloud(paid):
    w = paid
    a = w.factory.control.spawn("programmer", SYSTEM)                                   # defolt büdcə 0 AZN
    t = w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM)
    r = w.disp.execute_with_retry(t.task_id)
    assert r.status == "failed" and w.provider.calls == 0 and llm_entries(w) == [] and w.ledger.total() == 0.0
    assert "budget_exhausted" in r.reason and r.attempts == 1                          # təkrar cəhd yoxdur


def test_gateless_cloud_clients_are_still_stopped_by_the_ledger_for_zero_budget(paid):
    """Router-in `allow_paid` qoruması olmasa belə GuardedLLM sıfır büdcəni AÇIQ çağırışdan əvvəl rədd edir."""
    w = paid
    with spend_scope(SpendScope("programmer_x", "agt:tsk_1", 0.0)):
        with pytest.raises(LLMBudgetExceeded):
            w.llm.complete("sual")
    assert w.provider.calls == 0 and llm_entries(w) == []


def test_funded_agent_spend_is_reserved_and_settled_in_the_ledger_under_its_own_name_and_operation(paid):
    w = paid
    a = funded(w, 0.5)
    t = w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM)
    r = w.disp.execute(t.task_id)
    assert r.status == "done" and w.provider.calls == 1
    (e,) = llm_entries(w)
    assert e.op_id == f"llm:agt:{t.task_id}" and e.agent == a.name and e.status == "committed" and 0 < e.amount <= 0.5
    assert w.ledger.agent_total(a.name) >= e.amount and "llm" not in w.ledger.by_agent()      # ümumi "llm" hesabına yox, agentə yazılıb
    # eyni tapşırığa təkrar generasiya yoxdur (router nəticəni saxlayır, ikinci ödəniş olmur)
    assert w.router.route(f"agt:{t.task_id}", "chat", "sual", a.system_instruction).duplicate is True and w.provider.calls == 1


def test_exhausted_budget_blocks_further_paid_requests_and_total_never_exceeds_the_budget(paid):
    w = paid
    a = funded(w, 0.03)
    outcomes = []
    for i in range(12):
        t = w.factory.control.assign(a.agent_id, f"t{i}", f"sual {i}", SYSTEM)
        outcomes.append(w.disp.execute_with_retry(t.task_id).status)
        assert sum(e.amount for e in llm_entries(w) if e.agent == a.name) <= 0.03 + 1e-9
        if w.factory.view.agent(a.agent_id).status != "active":
            break
    assert "done" in outcomes and "failed" in outcomes
    assert w.provider.calls == outcomes.count("done")                                   # rədd olunan sorğu provayderə getməyib
    assert w.ledger.agent_total(a.name) <= 0.03 + 1e-9


def test_budget_checked_by_the_ledger_for_each_agent_independently(paid):
    w = paid
    rich, poor = funded(w, 0.5), funded(w, 0.0, "researcher")
    assert w.disp.execute(w.factory.control.assign(rich.agent_id, "a", "bir", SYSTEM).task_id).status == "done"
    assert w.disp.execute(w.factory.control.assign(poor.agent_id, "b", "iki", SYSTEM, kind="research").task_id).status == "failed"
    assert w.provider.calls == 1 and w.ledger.agent_total(poor.name) == 0.0 and w.ledger.agent_total(rich.name) > 0


def test_global_consent_is_still_required_for_funded_agents(paid):
    w = paid
    w.store.set_consent("cloud_paid_use", False, "owner")
    a = funded(w, 0.5)
    r = w.disp.execute_with_retry(w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM).task_id)
    assert r.status == "failed" and w.provider.calls == 0 and llm_entries(w) == []


def test_llm_wide_cost_cap_still_applies_across_agents(paid):
    w = paid
    w.llm.config = dataclasses.replace(w.llm.config, cost_limit=0.06)                     # LLM-ə ümumi tavan
    w.factory.admin.set_policy(OWNER, auto_stop_after_failures=1000)
    a1, a2 = funded(w, 1.0), funded(w, 1.0, "researcher")
    statuses = []
    for i in range(6):
        for ag, kind in ((a1, None), (a2, "research")):
            statuses.append(w.disp.execute_with_retry(w.factory.control.assign(ag.agent_id, f"t{i}", f"sual {i}{ag.name}", SYSTEM, kind=kind).task_id).status)
    assert "done" in statuses and "failed" in statuses
    assert sum(e.amount for e in llm_entries(w)) <= 0.06 + 1e-9


def test_global_ledger_limit_is_enforced_for_agent_spend(paid):
    w = paid
    w.runner.global_limit = lambda: 0.0001                                                # ümumi büdcə demək olar ki, bitib
    a = funded(w, 0.5)
    r = w.disp.execute_with_retry(w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM).task_id)
    assert r.status == "failed" and w.provider.calls == 0


def test_duplicate_operation_ids_never_charge_twice(paid):
    w = paid
    a = funded(w, 0.5)
    t = w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM)
    assert w.ledger.reserve(f"llm:agt:{t.task_id}", a.name, 0.001).ok                    # əvvəlki naməlum/açıq rezerv
    r = w.disp.execute_with_retry(t.task_id)
    assert r.status == "failed" and w.provider.calls == 0 and len(llm_entries(w)) == 1


def test_failed_settlement_fails_closed_keeps_the_reservation_and_marks_it_for_reconciliation(paid, monkeypatch):
    w = paid
    a = funded(w, 0.5)
    t = w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM)
    real_commit = w.ledger.commit
    monkeypatch.setattr(w.ledger, "commit", lambda op_id, actual: False if op_id.startswith("llm:") else real_commit(op_id, actual))
    r = w.disp.execute_with_retry(t.task_id)
    assert r.status == "failed" and w.factory.view.reports() == []                        # nəticə qəbul edilmir
    assert w.provider.calls == 1                                                          # təkrar ödəniş olmayıb
    (e,) = llm_entries(w)
    assert e.status == "reserved" and e.unknown is True and e.agent == a.name and e.amount > 0
    assert [x.op_id for x in w.ledger.unknown_entries()] == [e.op_id] and w.ledger.agent_total(a.name) >= e.amount


def test_settlement_exception_also_fails_closed(paid, monkeypatch):
    w = paid
    a = funded(w, 0.5)

    real_commit = w.ledger.commit

    def boom(op_id, actual):
        if op_id.startswith("llm:"):
            raise sqlite3.OperationalError("disk")
        return real_commit(op_id, actual)
    monkeypatch.setattr(w.ledger, "commit", boom)
    r = w.disp.execute_with_retry(w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM).task_id)
    assert r.status == "failed" and w.provider.calls == 1 and w.factory.view.reports() == []
    assert any(x.unknown for x in llm_entries(w))


def test_unknown_provider_outcome_keeps_the_cost_reserved_against_the_agent_budget(paid):
    w = paid
    w.provider.script = [LLMProviderError("kəsildi", charged=None)]
    a = funded(w, 0.5)
    t = w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM)
    r = w.disp.execute(t.task_id)
    assert r.status in ("retry", "failed")
    (e,) = llm_entries(w)
    assert e.unknown is True and e.status == "reserved" and w.ledger.agent_total(a.name) >= e.amount > 0
    r2 = w.disp.execute_with_retry(t.task_id)                                             # eyni əməliyyat ikinci dəfə ödənilmir
    assert w.provider.calls == 1 and r2.status == "failed"


def test_known_uncharged_failure_releases_the_reservation(paid):
    w = paid
    w.provider.script = [LLMProviderError("rədd", charged=False)]
    a = funded(w, 0.5)
    w.disp.execute(w.factory.control.assign(a.agent_id, "iş", "sual", SYSTEM).task_id)
    assert w.ledger.agent_total(a.name) == 0.0 and w.provider.calls == 1


def test_agent_scope_without_a_ledger_fails_closed_for_real_providers(tmp_path):
    provider = PaidProvider()
    llm = GuardedLLM(provider, LLMConfig(provider="openai_compat", model="m", price_in_per_1k=0.1, price_out_per_1k=0.1), log_path=tmp_path / "l.jsonl")
    with spend_scope(SpendScope("a", "agt:t1", 1.0)):
        with pytest.raises(LLMBudgetExceeded):
            llm.complete("sual")
    assert provider.calls == 0
    assert llm.complete("sual").text                                                       # əhatə yoxdursa köhnə davranış dəyişməyib


@pytest.mark.parametrize("kw", [dict(agent="", op_id="x", agent_limit=1.0), dict(agent="a", op_id="bad op!", agent_limit=1.0),
                                dict(agent="a", op_id="x", agent_limit=-1.0), dict(agent="a", op_id="x", agent_limit=float("nan")),
                                dict(agent="a", op_id="x", agent_limit=True), dict(agent="a", op_id="x", agent_limit=1.0, global_limit=float("inf"))])
def test_spend_scope_validates_its_inputs(kw):
    with pytest.raises(ValueError):
        SpendScope(**kw)


def test_ledger_group_limit_is_atomic_and_prefix_based(tmp_path):
    from safety.ledger import CostLedger
    led = CostLedger(tmp_path / "l.db")
    assert led.reserve("llm:1", "a", 0.6, group_prefix="llm:", group_limit=1.0).ok
    assert led.reserve("other:1", "a", 5.0, group_prefix="llm:", group_limit=1.0).ok       # başqa prefiks sayılmır
    assert led.reserve("llm:2", "b", 0.5, group_prefix="llm:", group_limit=1.0).status == "denied"
    assert led.reserve("llm:3", "b", 0.4, group_prefix="llm:", group_limit=1.0).ok


# ======================= struktur =======================

def test_hardened_modules_keep_the_architecture_rules():
    import ast
    root = Path(__file__).resolve().parent.parent
    for name in ("agent_factory.py", "agent_runtime.py", "team.py", "factory_auth.py"):
        text = (root / "agents" / name).read_text(encoding="utf-8")
        assert "environ" not in text and "getenv" not in text, name
        imported = {n.module for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom)}
        assert not imported & {"safety.broker", "safety.gate", "safety.tools", "safety.workspace", "safety.ledger"}, (name, imported)
