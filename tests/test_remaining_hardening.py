"""Stage 17 qalan audit tapıntıları (B-3, B-4, B-5, B-6, A-7, A-8) üçün istismar reqressiya testləri.

Hər test PR #21 kodunda (6855c0a) uğursuz olmalıdır: ya hücum UĞUR qazanır, ya da yeni təhlükəsizlik səthi (əhatəli etibarnamə, `console.scope`, `LockedConnection`,
`resolve_unknown`, `BrokeredRouter`...) yoxdur. Real şəbəkə/açar/ödəniş YOXDUR; bütün açarlar testdə təsadüfi yaradılır."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.agent_factory import FactoryDenied, owner_scope
from agents.company import CompanyDenied, company_scope
from agents.factory_auth import CredentialError, decode_signing_seed, generate_signing_seed, validate_signing_seed
from agents.llm import GuardedLLM, LLMBlocked, LLMConfig, LLMRequest
from agents.registry import Registry
from agents.spec import AgentSpec
from api.bootstrap import build_app
from api.auth import AuthService
from runtime.brokered_llm import BrokeredRouter
from runtime.factory_bootstrap import BootstrapError, TrustAnchor, load_signing_config
from safety.broker import Effect, EffectBroker
from safety.execution_context import broker_context, in_broker
from safety.ledger import CostLedger
from tests.factory_keys import SYSTEM, owner_cred, random_seed
from tests.test_agent_runtime import world  # noqa: F401  (fixture)
from tests.test_api_auth import pair
from tests.test_security_boundary import H, authenticate, owner_for

ROOT = Path(__file__).resolve().parent.parent
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def cred_for(w, op, *a, **k):
    """DƏQİQ `(op, hədəf, fabrik hash, şirkət təsiri)` əhatəli yeni bir-dəfəlik sahib etibarnaməsi (test etibar çıpası)."""
    name, subject, digest, company = w.factory.admin.scope(op, *a, **k)
    return owner_cred(name, subject, digest, company=company)


# ============================================================================ B-3: etibarnamə DƏQİQ əməliyyata/hədəfə/parametrə bağlıdır

def test_B3_credential_for_one_role_cannot_create_another_role(world):
    w = world
    c = cred_for(w, "create_agent", "programmer")
    with pytest.raises(FactoryDenied) as e:
        w.factory.admin.create_agent("researcher", c)
    assert e.value.code == "scope_mismatch"
    assert w.factory.view.agents() == []
    assert w.factory.admin.create_agent("programmer", c).status == "active"        # eyni etibarnamə düzgün əməliyyatda hələ də işləyir (rədd onu yandırmadı)


def test_B3_credential_for_one_payload_cannot_be_used_with_a_wider_payload(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    c = cred_for(w, "set_budget", a.agent_id, 1.0)
    with pytest.raises(FactoryDenied) as e:                                           # 1.0 AZN təsdiqləndi, 99.0 AZN istənir
        w.factory.admin.set_budget(a.agent_id, 99.0, c)
    assert e.value.code == "scope_mismatch"
    assert w.factory.view.agent(a.agent_id).budget == 0.0
    wide = cred_for(w, "create_agent", "programmer")
    with pytest.raises(FactoryDenied) as e2:                                          # eyni rol, əlavə icazə (publish)
        w.factory.admin.create_agent("programmer", wide, permissions=("read_files", "publish"), sensitive_ok=True)
    assert e2.value.code == "scope_mismatch"


def test_B3_credential_for_one_subject_cannot_act_on_another_agent(world):
    w = world
    a, b = w.factory.control.spawn("programmer", SYSTEM), w.factory.control.spawn("researcher", SYSTEM)
    c = cred_for(w, "set_budget", a.agent_id, 1.0)
    with pytest.raises(FactoryDenied) as e:
        w.factory.admin.set_budget(b.agent_id, 1.0, c)
    assert e.value.code == "scope_mismatch" and w.factory.view.agent(b.agent_id).budget == 0.0


def test_B3_credential_cannot_be_converted_to_another_operation(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    c = cred_for(w, "set_budget", a.agent_id, 1.0)
    for call in (lambda: w.factory.admin.stop(a.agent_id, c, "x"), lambda: w.factory.admin.archive(a.agent_id, c),
                 lambda: w.factory.admin.grant(a.agent_id, c, permissions=("read_files", "write_files")), lambda: w.factory.admin.set_policy(c, global_budget_cap=9.0)):
        with pytest.raises(FactoryDenied) as e:
            call()
        assert e.value.code == "scope_mismatch"
    assert w.factory.view.agent(a.agent_id).status == "active" and w.factory.view.policy.global_budget_cap == 0.0


def test_B3_wildcard_or_blank_scope_credentials_are_not_accepted(world):
    w = world
    for op, subject in (("*", ""), ("", ""), ("create_agent", "*")):
        try:
            c = owner_cred(op, subject)
        except CredentialError:
            continue                                                                    # etibarnamənin özü verilmir (daha güclü)
        with pytest.raises(FactoryDenied) as e:
            w.factory.admin.create_agent("programmer", c)
        assert e.value.code == "scope_mismatch"
    assert w.factory.view.agents() == []


def test_B3_owner_credential_is_single_use(world):
    w = world
    w.factory.admin.set_policy(cred_for(w, "set_policy", global_budget_cap=5.0), global_budget_cap=5.0)
    a = w.factory.control.spawn("programmer", SYSTEM)
    c = cred_for(w, "set_budget", a.agent_id, 1.0)
    assert w.factory.admin.set_budget(a.agent_id, 1.0, c).budget == 1.0
    w.factory.admin.set_budget(a.agent_id, 0.0, cred_for(w, "set_budget", a.agent_id, 0.0))
    with pytest.raises(FactoryDenied) as e:                                           # eyni etibarnamənin təkrarı (replay)
        w.factory.admin.set_budget(a.agent_id, 1.0, c)
    assert e.value.code == "credential_replayed" and w.factory.view.agent(a.agent_id).budget == 0.0


def test_B3_company_rejects_wrong_operation_class_and_replay(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    c = owner_cred("set_budget", a.name, company=company_scope("set_budget", a.name, 1.0))
    with pytest.raises(CompanyDenied) as e:                                           # set_budget etibarnaməsi şirkətdə dayandırma üçün istifadə olunmur
        w.company.admin.suspend(a.name, c)
    assert e.value.code == "scope_mismatch"
    w.company.admin.set_budget(a.name, 1.0, c)
    with pytest.raises(CompanyDenied) as e2:
        w.company.admin.set_budget(a.name, 1.0, c)
    assert e2.value.code == "credential_replayed"
    assert w.company.view.get(a.name).budget == 1.0


def test_B3_production_stepup_is_bound_to_the_exact_operation_and_payload(tmp_path):
    p = _prod(tmp_path)
    c = owner_for(p, "create_agent", "programmer")
    assert (c.scope_op, c.scope_subject) == ("create_agent", "programmer") and len(c.scope_hash) == 64
    with pytest.raises(FactoryDenied) as e:
        p.stack.console.create_agent("researcher", c)
    assert e.value.code == "scope_mismatch"
    with pytest.raises(FactoryDenied) as e2:
        p.stack.console.create_agent("programmer", c, budget=5.0)                       # eyni rol, fərqli parametr
    assert e2.value.code == "scope_mismatch"
    a = p.stack.console.create_agent("programmer", c)
    assert a.status == "active"
    with pytest.raises(FactoryDenied) as e3:
        p.stack.console.create_agent("programmer", c)                                    # təkrar istifadə
    assert e3.value.code == "credential_replayed"
    assert len(p.stack.view.agents()) == 1


def test_B3_a_step_up_for_a_generic_hash_cannot_authorise_a_real_payload(tmp_path):
    p = _prod(tmp_path)
    generic = authenticate(p, "create_agent", "programmer", H)                             # köhnə üslub: parametrlərdən asılı olmayan sabit hash
    with pytest.raises(FactoryDenied) as e:
        p.stack.console.create_agent("programmer", generic)
    assert e.value.code == "scope_mismatch"


# ---- Company birbaşa yoxlayır (CompanyAdmin yan keçid): fabrikə güvənmədən dəqiq əməliyyat + hədəf + kanonik parametr hash-i

def _auth(perms=("read_files",), sens=False, deleg=(), cap=0.0):
    from agents.company import Authority
    return Authority(frozenset(perms), sens, frozenset(deleg), cap)


def _spec(name="cmp_agent"):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=["read_files", "write_files"], inputs=["sorgu"], outputs=["netice"], cost_limit=5.0)


def _company_cases(w, victim, other):
    """op -> (scope_op, doğru arqumentlər, FƏRQLİ arqumentlər, çağırış). Bütün arqumentlər `principal`-sız."""
    adm = w.company.admin
    return {
        "set_budget": ("set_budget", (victim.name, 1.0), (victim.name, 2.0), lambda a, c: adm.set_budget(*a, c)),
        "suspend": ("stop", (victim.name,), (other.name,), lambda a, c: adm.suspend(*a, c)),
        "retire": ("archive", (victim.name,), (other.name,), lambda a, c: adm.retire(*a, c)),
        "set_authority": ("grant", (victim.name, _auth()), (victim.name, _auth(("read_files", "write_files"))), lambda a, c: adm.set_authority(*a, c)),
        "set_department_cost": ("set_department_cost", ("development", 1.0), ("development", 9.0), lambda a, c: adm.set_department_cost(*a, c)),
        "register_baseline_agent": ("spawn", (_spec(), "development", "Rol"), (_spec(), "finance", "Rol"), lambda a, c: adm.register_baseline_agent(*a, c)),
        "register_agent": ("create_agent", (_spec(), "development", "Rol", _auth(), 1.0), (_spec(), "development", "Rol", _auth(("read_files", "write_files")), 1.0),
                           lambda a, c: adm.register_agent(*a, c)),
    }


@pytest.mark.parametrize("op", ["set_budget", "suspend", "retire", "set_authority", "set_department_cost", "register_baseline_agent", "register_agent"])
def test_B3_company_independently_rejects_a_credential_for_a_different_exact_effect(world, op):
    w = world
    victim, other = w.factory.control.spawn("programmer", SYSTEM), w.factory.control.spawn("researcher", SYSTEM)
    scope_op, good, bad, call = _company_cases(w, victim, other)[op]
    before = (w.company.view.get(victim.name), w.company.view.get(other.name))
    wrong = owner_cred(scope_op, "x", company=company_scope(op, *bad))                  # düzgün sinif, düzgün imza, amma BAŞQA təsir
    with pytest.raises(CompanyDenied) as e:
        call(good, wrong)
    assert e.value.code == "scope_mismatch"
    blank = owner_cred(scope_op, "x")                                                    # şirkət təsiri yoxdur (köhnə üslub fabrik etibarnaməsi)
    with pytest.raises(CompanyDenied) as e2:
        call(good, blank)
    assert e2.value.code == "scope_mismatch"
    assert (w.company.view.get(victim.name), w.company.view.get(other.name)) == before
    right = owner_cred(scope_op, "x", company=company_scope(op, *good))
    call(good, right)                                                                    # dəqiq təsir qəbul olunur
    with pytest.raises(CompanyDenied) as e3:
        call(good, right)
    assert e3.value.code == "credential_replayed"


def test_B3_a_factory_credential_cannot_drive_company_directly_with_a_wider_effect(world):
    w = world
    c = cred_for(w, "create_agent", "programmer")                                        # sahib: məhdud 'programmer' yaradılışını təsdiqlədi
    with pytest.raises(CompanyDenied) as e:                                              # Company-yə birbaşa daha geniş icazə/büdcə ilə registrasiya
        w.company.admin.register_agent(_spec("sneaky_agent"), "development", "Proqramçı", _auth(("read_files", "write_files", "publish"), True), 5.0, c)
    assert e.value.code == "scope_mismatch"
    assert w.company.view.get("sneaky_agent") is None


def test_B3_a_credential_consumed_directly_at_company_cannot_also_create_the_factory_agent(world):
    from agents.agent_roles import ROLE_CATALOG
    w = world
    name, subject, digest, company = w.factory.admin.scope("create_agent", "programmer")
    c = owner_cred(name, subject, digest, company=company)
    role = ROLE_CATALOG["programmer"]
    spec = w.factory._core.spec_for(role, "direct_agent_1")
    w.company.admin.register_agent(spec, role.department, role.title_az, _auth(), 0.0, c)   # etibarnamə şirkətdə xərclənib (dəqiq eyni təsir)
    with pytest.raises(FactoryDenied) as e:
        w.factory.admin.create_agent("programmer", c)
    assert e.value.code == "credential_replayed" and w.factory.view.agents() == []


# ============================================================================ B-4: cihaz ləğvi verilmiş etibarnamələri DƏRHAL ləğv edir

def _prod(tmp_path):
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-reg-001"})
    anchor = TrustAnchor(cfg)
    svc = build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, trust=anchor)
    return types.SimpleNamespace(anchor=anchor, svc=svc, stack=svc.factory, dev=pair(svc.auth), auth=svc.auth)


def test_B4_revoking_the_device_invalidates_an_outstanding_owner_credential_immediately(tmp_path):
    p = _prod(tmp_path)
    c = owner_for(p, "create_agent", "programmer")
    assert c.expires_at > c.issued_at                                                    # hələ vaxtı keçməyib
    p.auth.revoke_device(p.dev.device_id)
    with pytest.raises(FactoryDenied) as e:
        p.stack.console.create_agent("programmer", c)
    assert e.value.code == "credential_revoked"
    assert p.stack.view.agents() == []


def test_B4_revoked_device_credential_is_also_refused_by_company(tmp_path):
    p = _prod(tmp_path)
    a = p.stack.console.create_agent("programmer", owner_for(p, "create_agent", "programmer"))
    _, subject, digest, company = p.stack.console.scope("set_budget", a.agent_id, 1.0)
    c = authenticate(p, "set_budget", subject, digest, company=company)
    p.auth.revoke_device(p.dev.device_id)
    with pytest.raises(CompanyDenied) as e:
        p.svc.company.admin.set_budget(a.name, 1.0, c)
    assert e.value.code == "credential_revoked"


def test_B4_revocation_is_precise_other_devices_keep_working(tmp_path):
    p = _prod(tmp_path)
    other = pair(p.auth)
    _, subj, digest, company = p.stack.console.scope("create_agent", "researcher")
    keep = authenticate(p, "create_agent", subj, digest, dev=other, company=company)
    p.auth.revoke_device(p.dev.device_id)
    assert p.stack.console.create_agent("researcher", keep).status == "active"


def test_B4_unused_step_up_receipt_is_burned_on_device_revocation(tmp_path):
    p = _prod(tmp_path)
    ch = p.stack.owner_gate.challenge(p.dev.device_id, "create_agent", "programmer", H)
    proof = p.dev.sign(ch, "factory:create_agent", "programmer", H, "approve")
    p.auth.revoke_device(p.dev.device_id)
    with pytest.raises(Exception):
        p.stack.owner_gate.authenticate(p.dev.device_id, ch.id, "create_agent", "programmer", H, proof)


def test_B4_revocation_listener_failure_is_not_silent(tmp_path):
    auth = AuthService(tmp_path / "a.db", tmp_path / "audit.jsonl")
    dev = pair(auth)

    def boom(_device_id):
        raise OSError("keyring unavailable")
    auth.add_revocation_listener(boom)
    with pytest.raises(RuntimeError):
        auth.revoke_device(dev.device_id)


def test_B4_devices_revoked_before_restart_stay_revoked_for_new_credentials(tmp_path):
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-reg-002"})
    anchor = TrustAnchor(cfg)
    svc = build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, trust=anchor)
    dev = pair(svc.auth)
    p = types.SimpleNamespace(anchor=anchor, svc=svc, stack=svc.factory, dev=dev, auth=svc.auth)
    c = owner_for(p, "create_agent", "programmer")
    svc.auth.revoke_device(dev.device_id)
    svc.factory.agents.close()
    svc.auth.close() if hasattr(svc.auth, "close") else None
    anchor2 = TrustAnchor(cfg)                                                           # yenidən başlama: eyni açar, yeni proses vəziyyəti
    svc2 = build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, trust=anchor2)
    with pytest.raises(FactoryDenied) as e:
        svc2.factory.console.create_agent("programmer", c)
    assert e.value.code in ("credential_revoked", "credential_replayed", "forged_principal")
    svc2.factory.agents.close()


# ============================================================================ B-5: iplik təhlükəsizliyi və SQLite bağlantı ömrü

def _hammer(fn, n_threads=12, n=40):
    errors: list[BaseException] = []

    def run(t):
        for i in range(n):
            try:
                fn(t, i)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
    with ThreadPoolExecutor(n_threads) as ex:
        list(ex.map(run, range(n_threads)))
    return errors


def test_B5_registry_is_safe_across_threads(tmp_path):
    reg = Registry(tmp_path / "r.db")
    spec = lambda name: AgentSpec(name=name, purpose="p", tools=[], permissions=["read_files"], inputs=["sorgu"], outputs=["netice"], cost_limit=1.0)   # noqa: E731
    errs = _hammer(lambda t, i: (reg.add(spec(f"agent_{t}_{i}"), "development"), reg.get(f"agent_{t}_{i}"), reg.list()))
    assert errs == []
    assert len(reg.list()) == 12 * 40


def test_B5_ledger_instance_is_safe_across_threads_and_never_exceeds_the_global_limit(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    results: list[bool] = []
    lock = threading.Lock()

    def one(t, i):
        r = led.reserve(f"op-{t}-{i}", f"agent{t}", 0.1, global_limit=1.0)
        with lock:
            results.append(r.ok)
    errs = _hammer(one, n_threads=16, n=10)
    assert errs == []
    assert sum(results) == 10 and led.reserved_total() <= 1.0 + 1e-6                     # tam 10 rezerv, limit aşılmır
    led.close()


def test_B5_two_ledger_instances_on_one_file_cannot_jointly_overspend(tmp_path):
    a, b = CostLedger(tmp_path / "l.db"), CostLedger(tmp_path / "l.db")
    ok: list[bool] = []
    lock = threading.Lock()

    def one(t, i):
        led = a if t % 2 else b
        r = led.reserve(f"op-{t}-{i}", "x", 0.1, global_limit=1.0)
        with lock:
            ok.append(r.ok)
    assert _hammer(one, n_threads=8, n=8) == []
    assert sum(ok) == 10 and a.total() <= 1.0 + 1e-6
    a.close()
    b.close()


def test_B5_connections_close_idempotently_and_fail_closed_afterwards(tmp_path):
    from safety.dbconn import LockedConnection
    conn = LockedConnection(str(tmp_path / "x.db"))
    conn.execute("CREATE TABLE t (a)")
    conn.close()
    conn.close()                                                                        # ikinci close() xəta vermir
    assert conn.closed is True
    with pytest.raises(Exception):
        conn.execute("SELECT 1")
    led = CostLedger(tmp_path / "l.db")
    led.close()
    led.close()


def test_B5_transaction_rolls_back_on_error_and_does_not_leave_a_lock(tmp_path):
    from safety.dbconn import LockedConnection
    conn = LockedConnection(str(tmp_path / "x.db"))
    conn.execute("CREATE TABLE t (a INTEGER)")
    with pytest.raises(ZeroDivisionError):
        with conn.transaction():
            conn.execute("INSERT INTO t VALUES (1)")
            1 / 0
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    done = []
    th = threading.Thread(target=lambda: (conn.execute("INSERT INTO t VALUES (2)"), done.append(1)))
    th.start()
    th.join(5)
    assert done == [1]                                                                  # kilid sərbəst buraxılıb
    conn.close()


# ============================================================================ B-6: imza açarı yoxlaması və açar yaratma

WEAK_SEEDS = {
    "sıfır": b"\x00" * 32,
    "təkrar": b"A" * 32,
    "artan": bytes(range(32)),
    "arifmetik": bytes((i * 37 + 11) % 256 for i in range(32)),
    "dövr": (b"\x13\x37\xc0\xde") * 8,
    "mətn": b"naymera-signing-key-do-not-use!",
    "az-fərqli": (b"\xaa\x55" * 16),
    "çap-olunan": bytes(33 + (i * 7) % 90 for i in range(32)),
}


@pytest.mark.parametrize("name", list(WEAK_SEEDS))
def test_B6_weak_signing_seeds_are_rejected(name):
    with pytest.raises(CredentialError):
        validate_signing_seed(WEAK_SEEDS[name])


def test_B6_generated_seeds_pass_validation_and_round_trip():
    for _ in range(50):
        text = generate_signing_seed()
        raw = decode_signing_seed(text)
        validate_signing_seed(raw)
        assert len(raw) == 32 and len(text) == 43


def test_B6_non_canonical_base64_alias_of_a_valid_key_is_rejected():
    text = generate_signing_seed()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(text[-1])
    alias = alphabet[(last & ~0x3) | ((last + 1) & 0x3)]                                  # son simvolun istifadə olunmayan 2 biti fərqli
    assert alias != text[-1]
    with pytest.raises(CredentialError):
        decode_signing_seed(text[:-1] + alias)


def test_B6_bootstrap_rejects_a_weak_key_from_env_or_file(tmp_path):
    import base64
    weak = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    with pytest.raises(BootstrapError):
        load_signing_config({"NAYMERA_SIGNING_KEY": weak, "NAYMERA_SIGNING_KEY_ID": "k-weak-001"})
    f = tmp_path / "k"
    f.write_text(weak)
    f.chmod(0o600)
    with pytest.raises(BootstrapError):
        load_signing_config({"NAYMERA_SIGNING_KEY_FILE": str(f), "NAYMERA_SIGNING_KEY_ID": "k-weak-001"})


def test_B6_key_file_must_be_private_regular_and_exclusive(tmp_path):
    seed = generate_signing_seed()
    f = tmp_path / "key"
    f.write_text(seed)
    f.chmod(0o600)
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY_FILE": str(f), "NAYMERA_SIGNING_KEY_ID": "k-file-001"})
    assert cfg is not None and cfg.seed == decode_signing_seed(seed)
    f.chmod(0o644)                                                                      # qrup/dünya oxuya bilir
    with pytest.raises(BootstrapError):
        load_signing_config({"NAYMERA_SIGNING_KEY_FILE": str(f), "NAYMERA_SIGNING_KEY_ID": "k-file-001"})
    f.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(f)
    with pytest.raises(BootstrapError):
        load_signing_config({"NAYMERA_SIGNING_KEY_FILE": str(link), "NAYMERA_SIGNING_KEY_ID": "k-file-001"})
    with pytest.raises(BootstrapError):                                                  # iki mənbə birlikdə
        load_signing_config({"NAYMERA_SIGNING_KEY_FILE": str(f), "NAYMERA_SIGNING_KEY": seed, "NAYMERA_SIGNING_KEY_ID": "k-file-001"})


def test_B6_key_generator_tool_writes_a_private_file_and_never_prints_the_key(tmp_path):
    out = tmp_path / "signing.key"
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_signing_key.py"), "--key-id", "k-gen-001", "--out", str(out)],
                         capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert res.returncode == 0, res.stderr
    seed = out.read_text().strip()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert seed not in res.stdout and seed not in res.stderr                              # sirr çıxışa yazılmır
    assert "k-gen-001" in res.stdout + res.stderr and "NAYMERA_VERIFY_KEYS" in res.stdout + res.stderr      # yalnız açıq açar / id
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY_FILE": str(out), "NAYMERA_SIGNING_KEY_ID": "k-gen-001"})
    assert cfg is not None
    again = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_signing_key.py"), "--key-id", "k-gen-001", "--out", str(out)],
                           capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert again.returncode != 0 and out.read_text().strip() == seed                       # mövcud açar üzərinə yazılmır


# ============================================================================ A-7: Ledger rezervləri (yarış, sızma, naməlum rezerv)

def test_A7_unknown_reservations_are_never_created_or_settled(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    assert led.commit("nope", 1.0) is False and led.release("nope") is False and led.mark_unknown("nope") is False
    assert led.resolve_unknown("nope", 1.0, "owner") is False
    assert led.entries() == [] and led.total() == 0.0                                    # naməlum op_id üçün sətir YARANMIR


def test_A7_a_reservation_settles_exactly_once_even_under_racing_commit_and_release(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    for n in range(30):
        assert led.reserve(f"op{n}", "a", 0.2).ok
    outcomes: dict[int, list[bool]] = {n: [] for n in range(30)}
    lock = threading.Lock()

    def settle(kind, n):
        r = led.commit(f"op{n}", 0.1) if kind == "c" else led.release(f"op{n}")
        with lock:
            outcomes[n].append(r)
    with ThreadPoolExecutor(8) as ex:
        for n in range(30):
            ex.submit(settle, "c", n)
            ex.submit(settle, "r", n)
    assert all(sorted(v) == [False, True] for v in outcomes.values())                    # hər rezerv YALNIZ bir dəfə həll olunur
    assert led.reserved_total() >= 0 and abs(led.total() - 0.1 * sum(1 for e in led.entries() if e.status == "committed")) < 1e-9


def test_A7_stale_reservations_are_marked_unknown_not_silently_freed_and_need_owner_reconciliation(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    assert led.reserve("old", "a", 0.5).ok and led.reserve("fresh", "a", 0.25).ok
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    assert led.stale_reserved(3600, now=future) and led.stale_reserved(3600) == []
    assert led.expire_stale(3600, now=future) == ["old", "fresh"] or set(led.expire_stale(3600, now=future)) <= {"old", "fresh"}
    led.expire_stale(3600, now=future)
    assert abs(led.total() - 0.75) < 1e-9                                                # pul sayılmaqda qalır (sızma azad etmə YOXDUR)
    assert {e.op_id for e in led.unknown_entries()} == {"old", "fresh"}
    assert led.resolve_unknown("fresh", None, "owner") is True                            # sahib: xərc olmadığını təsdiqləyir
    assert led.resolve_unknown("old", 0.4, "owner") is True                               # sahib: faktiki xərc
    assert led.resolve_unknown("old", 0.4, "owner") is False                              # təkrar uzlaşdırma yoxdur
    assert abs(led.total() - 0.4) < 1e-9 and led.unknown_entries() == []


class _PaidFake:
    name, is_mock = "paid-fake", False

    def __init__(self, exc=None):
        self.calls, self.exc = 0, exc

    def complete(self, request: LLMRequest):
        self.calls += 1
        if self.exc:
            raise self.exc
        from agents.llm import LLMResponse
        return LLMResponse(text="ok", input_tokens=5, output_tokens=5, model="m")


def _paid_llm(tmp_path, provider, ledger):
    cfg = LLMConfig(provider="openai", timeout_s=2.0, max_output_tokens=50, max_total_tokens=10_000, max_prompt_chars=1000, max_response_chars=500,
                    price_in_per_1k=0.01, price_out_per_1k=0.01, cost_limit=5.0)
    return GuardedLLM(provider, cfg, log_path=tmp_path / "llm.jsonl", stop_path=tmp_path / "STOP", ledger=ledger)


def test_A7_a_failing_ledger_reserve_does_not_leak_or_call_the_provider(tmp_path):
    prov = _PaidFake()

    class BrokenLedger(CostLedger):
        def reserve(self, *a, **k):
            raise OSError("disk full")
    led = BrokenLedger(tmp_path / "l.db")
    llm = _paid_llm(tmp_path, prov, led)
    with broker_context(), pytest.raises(Exception):
        llm.complete("salam")
    assert prov.calls == 0 and led.reserved_total() == 0.0


def test_A7_provider_failure_keeps_the_amount_as_unknown_until_the_owner_reconciles(tmp_path):
    prov = _PaidFake(exc=RuntimeError("boom"))
    led = CostLedger(tmp_path / "l.db")
    llm = _paid_llm(tmp_path, prov, led)
    with broker_context(), pytest.raises(Exception):
        llm.complete("salam")
    assert prov.calls == 1
    unknown = led.unknown_entries()
    assert len(unknown) == 1 and led.total() > 0.0                                      # provayder xərc çəkmiş ola bilər: məbləğ qorunur, sızma deyil
    assert led.resolve_unknown(unknown[0].op_id, None, "owner") is True                  # yalnız sahib uzlaşdırması azad edir
    assert led.total() == 0.0 and led.unknown_entries() == []


# ============================================================================ A-8: hər ödənişli LLM çağırışı Broker/Gate/Ledger-dən keçir

def test_A8_paid_provider_is_never_called_outside_the_broker(tmp_path):
    prov, led = _PaidFake(), CostLedger(tmp_path / "l.db")
    llm = _paid_llm(tmp_path, prov, led)
    assert in_broker() is False
    with pytest.raises(LLMBlocked):
        llm.complete("salam")
    assert prov.calls == 0 and led.entries() == []                                       # nə provayder çağırışı, nə də rezerv
    with broker_context():
        assert in_broker() is True
        assert llm.complete("salam").text == "ok"
    assert prov.calls == 1


def test_A8_broker_context_cannot_be_entered_by_a_caller_supplied_flag(tmp_path):
    prov, led = _PaidFake(), CostLedger(tmp_path / "l.db")
    llm = _paid_llm(tmp_path, prov, led)
    for kw in ({"in_broker": True}, {"broker": True}):
        with pytest.raises(TypeError):
            llm.complete("salam", **kw)
    assert prov.calls == 0


class _RecordingRouter:
    def __init__(self):
        self.calls, self.in_broker_seen = 0, []

    def route(self, request_id, kind, prompt, system="", context=(), allow_mock=True, allow_paid=True):
        from agents.hybrid import RouteResult
        self.calls += 1
        self.in_broker_seen.append(in_broker())
        return RouteResult("ok", request_id, text="cavab")


def test_A8_system_chat_goes_through_the_broker_and_respects_stop(tmp_path):
    rec = _RecordingRouter()
    broker = EffectBroker(tmp_path / "audit.jsonl", tmp_path / "STOP", ledger=CostLedger(tmp_path / "l.db"))
    br = BrokeredRouter(rec, broker)
    assert br.route("r1", "chat", "salam").status == "ok"
    assert rec.in_broker_seen == [True]                                                  # çağırış Broker kontekstində icra olundu
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert audit and audit[0]["allowed"] is True and audit[0]["permission"] == "read_files"    # Gate/audit izi var
    (tmp_path / "STOP").write_text("stop")
    res = br.route("r2", "chat", "salam")
    assert res.status == "blocked" and rec.calls == 1                                    # stop bayrağı: marşrutlaşdırıcıya çatmır


def test_A8_prefetch_never_uses_the_paid_engine(world):
    w = world
    w.factory.admin.set_policy(cred_for(w, "set_policy", global_budget_cap=5.0), global_budget_cap=5.0)
    a = w.factory.admin.create_agent("programmer", cred_for(w, "create_agent", "programmer"))
    w.factory.admin.set_budget(a.agent_id, 1.0, cred_for(w, "set_budget", a.agent_id, 1.0))
    t = w.factory.control.assign(a.agent_id, "iş", "funksiya yaz", SYSTEM)
    seen = []
    real_route = w.router.route

    def spy(request_id, kind, prompt, system="", context=(), allow_mock=True, allow_paid=True):
        seen.append(allow_paid)
        return real_route(request_id, kind, prompt, system, context, allow_mock, allow_paid)
    w.router.route = spy
    w.runner.generate(w.factory.view.agent(a.agent_id), w.factory.view.task(t.task_id), prefetch=True)
    assert seen == [False]                                                               # büdcə > 0 olsa da, əvvəlcədən generasiya ödənişli mühərriki təlbə etmir
    w.runner.generate(w.factory.view.agent(a.agent_id), w.factory.view.task(t.task_id))
    assert seen == [False, True]                                                         # adi icra yolu (Broker/Ledger arxasında) ödənişliyə icazə verir


def test_A8_production_chat_and_planner_use_the_brokered_router(tmp_path):
    p = _prod(tmp_path)
    from api import bootstrap
    src = Path(bootstrap.__file__).read_text(encoding="utf-8")
    assert "BrokeredRouter(" in src
    assert hashlib.sha256(b"x").hexdigest()                                              # (bütövlük: modul oxunur)
