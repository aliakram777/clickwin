"""Təhlükəsizlik sərhədi (PR #19, audit A-1..A-6): asimmetrik imza, rola xas imza obyektləri, Company-nin müstəqil yoxlaması, hərəkətə bağlı bir-dəfəlik
səlahiyyət, step-up ilə sahib etibarnaməsi, açar keyfiyyəti/rotasiya/ləğv/vaxt. Audit ssenariləri P1/P1b, P2/P2b, P3, P4/P4b, P5/P6 exploit-reqressiya kimi.
Heç bir real şəbəkə, ödəniş və ya açar yoxdur; açarlar hər proseskdə `secrets` ilə yaradılır; provayderlər saxtadır."""
import base64
import dataclasses
import functools
import secrets
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.agent_factory import AgentFactory, AgentHandle, FactoryAdmin, FactoryControl, FactoryDenied, FactoryRuntime
from agents.agent_runtime import AgentDispatcher, AgentLLMRunner
from agents.company import Authority, Company, CompanyAdmin, CompanyDenied
from agents.factory_auth import (ACTIONS, CredentialError, DispatchAuthorization, DispatcherIssuer, Keyring, MockOwnerIssuer, OwnerIssuer, SignedPrincipal,
                                 SystemIssuer, TrustedSigner, decode_signing_seed, generate_signing_seed, validate_signing_seed)
from agents.registry import Registry
from agents.spec import AgentSpec
from api.auth import AuthError, AuthService, StepUpReceipt
from api.bootstrap import build_app
from api.owner_gate import OwnerGate
from runtime.factory_bootstrap import BootstrapError, FactoryStack, TrustAnchor, load_signing_config
from safety.approvals import Principal
from tests.factory_keys import DISPATCHER, KEY_ID, OWNER, SEED, SIGNER, SYSTEM, T0, owner_cred, ring, ring_with_control, trust
from tests.test_agent_runtime import task, world  # noqa: F401  (fixture)
from tests.test_api_auth import H, pair
from tests.owner_proxy import ScopedCompany

FORGED_OWNER = Principal("owner", "human", "console")
FORGED_SYSTEM = Principal("naymera", "system", "core")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


# ----------------------------------------------------------------------------- əlçatanlıq analizi (A-1/A-2/A-3 üçün)

FORBIDDEN = (TrustedSigner, DispatcherIssuer, SystemIssuer, OwnerIssuer, MockOwnerIssuer, Ed25519PrivateKey, CompanyAdmin, Company, FactoryAdmin,
             FactoryControl, FactoryRuntime, AgentFactory, AgentDispatcher, AgentLLMRunner, OwnerGate, AuthService, TrustAnchor)
PRIVILEGED_NAMES = ("set_authority", "set_budget", "register_agent", "register_baseline_agent", "from_step_up", "claim", "suspend",
                    "retire", "issue_authorization", "owner_issuer", "dispatcher_issuer", "system_issuer", "_principal_minter", "_sign_auth")
_SKIP = (str, bytes, int, float, bool, type(None), type, types.ModuleType)


def reach(root, limit=60000):
    """`root`-dan atributlar, `__slots__`, konteynerlər, bağlanışlar (`__closure__`), `__self__`, `__func__`, `functools.partial` ilə çatılan bütün obyektlər."""
    seen, stack, out = set(), [root], []
    while stack and len(seen) < limit:
        o = stack.pop()
        if id(o) in seen or isinstance(o, _SKIP):
            continue
        seen.add(id(o))
        out.append(o)
        kids = []
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict):
            kids += list(d.values())
        for cls in type(o).__mro__:
            for slot in getattr(cls, "__slots__", ()) if isinstance(getattr(cls, "__slots__", ()), (tuple, list)) else ():
                try:
                    kids.append(object.__getattribute__(o, slot))
                except AttributeError:
                    pass
        if isinstance(o, dict):
            kids += list(o.keys()) + list(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            kids += list(o)
        for attr in ("__closure__", "__self__", "__func__", "__wrapped__", "__defaults__", "__kwdefaults__"):
            try:
                v = getattr(o, attr)
            except Exception:  # noqa: BLE001
                continue
            if attr == "__closure__" and v:
                kids += [c.cell_contents for c in v if _filled(c)]
            elif v is not None:
                kids.append(v)
        if isinstance(o, functools.partial):
            kids += [o.func, *o.args, *(o.keywords or {}).values()]
        stack += kids
    return out


def _filled(cell):
    try:
        cell.cell_contents
        return True
    except ValueError:
        return False


def found(root, forbidden=FORBIDDEN):
    return sorted({type(o).__name__ for o in reach(root) if isinstance(o, forbidden)})


def privileged_callables(root):
    hits = set()
    for o in reach(root):
        for name in PRIVILEGED_NAMES:
            if not isinstance(o, type) and callable(getattr(o, name, None)) and not isinstance(o, types.ModuleType):
                hits.add((type(o).__name__, name))
    return sorted(hits)


def holds_bytes(root, secret: bytes) -> bool:
    for o in reach(root):
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict) and any(v == secret for v in d.values() if isinstance(v, (bytes, bytearray))):
            return True
        if isinstance(o, (bytes, bytearray)) and bytes(o) == secret:
            return True
    return False


# ----------------------------------------------------------------------------- A-1: P1 / P1b

def test_P1_agent_handle_cannot_reach_signing_authority_company_admin_or_runtime(world):
    """Yalnız imza obyekti/`Company.admin`/icra fasadı üçün. DİQQƏT: bu proses-daxili `AgentHandle`-ın SƏRHƏD olduğunu ÖZÜ sübut etmir: nüvə, DB bağlantısı və
    reyestr hələ də çatandır (audit B-1; `tests/test_agent_isolation.py::test_in_process_agent_handle_is_NOT_a_boundary_positive_control`). Agent kodu ayrı prosesdədir."""
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    h = w.factory.handle(a.agent_id)
    # P1 ssenarisi: əvvəl h._k.authority.owner("owner") işləyirdi
    for name in ("_k", "k", "core", "_core", "authority", "signer", "issuer", "admin", "company", "runtime", "control", "_c", "factory"):
        assert not hasattr(h, name), name
    assert not hasattr(h, "__dict__") and {n for n in dir(h) if not n.startswith("_")} == {"agent_id", "info", "request_permission", "submit_result", "handoff"}
    with pytest.raises(AttributeError):
        h.extra = 1
    # əlçatan bütün obyektlərdə imza/admin/icra imkanı yoxdur (atribut, bağlanış və __self__ izləməklə)
    assert found(h) == [], found(h)
    assert privileged_callables(h) == [], privileged_callables(h)
    # nüvənin özündə də yoxdur (hətta agent nüvəyə çatsa belə): yalnız oxu üzü + açıq açarlar
    assert found(w.factory._core) == [] and privileged_callables(w.factory._core) == []
    assert found(w.factory.view) == [] and found(w.company.view) == []


def test_P1_walker_has_teeth_positive_controls(world):
    """Analiz alətinin həqiqətən tapdığını göstərir (yoxsa yuxarıdakı boş nəticələr mənasızdır)."""
    w = world
    assert {"FactoryAdmin", "FactoryRuntime", "FactoryControl"} <= set(found(w.factory))           # etibarlı tərəf fasadlara sahibdir
    assert "Ed25519PrivateKey" in found(w.trust.dispatcher) and "TrustedSigner" in found(w.trust.signer)   # imza obyektində açar var
    assert {"AgentFactory", "ScopedAgentFactory"} & set(found(types.SimpleNamespace(f=w.factory)))     # (sınaq fabriki AgentFactory alt sinfidir)
    assert {"Company", "ScopedCompany"} & set(found(types.SimpleNamespace(c=w.company))) and "CompanyAdmin" in found(w.company)


def test_P1b_private_signing_key_is_not_reachable_from_factory_company_verifier_or_handles(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    for root in (w.factory, w.company, w.trust.ring, w.factory.handle(a.agent_id), w.company.view):
        assert "Ed25519PrivateKey" not in found(root) and "TrustedSigner" not in found(root)
        assert not holds_bytes(root, SEED)
    assert not any(hasattr(w.trust.ring, n) for n in ("sign", "_sign", "_key", "private_key", "seed", "issuer"))
    assert "private" not in repr(w.trust.ring).lower() or "hidden" in repr(w.trust.ring).lower()
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(w.trust.signer)
    assert "Ed25519PrivateKey" in found(w.trust.signer.system_issuer())                     # yalnız imza tərəfində (etibarlı bootstrap)


def test_issuers_are_role_bound_and_immutable():
    sys_i = SIGNER.system_issuer(ttl=60)
    p = sys_i.principal()
    assert p.role == "system" and p.kind == "system"
    with pytest.raises(AttributeError):
        sys_i.principal = lambda: OWNER
    disp = SIGNER.dispatcher_issuer()
    assert disp.principal().role == "dispatcher"
    assert not any(hasattr(disp, n) for n in ("owner", "from_step_up", "system_issuer", "_sign"))
    assert not hasattr(sys_i, "authorize") and not hasattr(sys_i, "from_step_up")
    mock = SIGNER.mock_owner_issuer().principal("owner", "t")
    assert mock.role == "mock_owner" and mock.kind == "mock"


# ----------------------------------------------------------------------------- A-2: P2 / P2b

ADMIN_CALLS = [
    ("register_baseline_agent", lambda c, p: c.admin.register_baseline_agent(_spec("zz_base"), "development", "Rol", p)),
    ("register_agent", lambda c, p: c.admin.register_agent(_spec("zz_full"), "development", "Rol", Authority(frozenset({"read_files"}), max_task_cost=1.0), 1.0, p)),
    ("set_authority", lambda c, p: c.admin.set_authority("victim", Authority(frozenset({"read_files", "write_files"}), True, max_task_cost=5.0), p)),
    ("set_budget", lambda c, p: c.admin.set_budget("victim", 5.0, p)),
    ("suspend", lambda c, p: c.admin.suspend("victim", p)),
    ("retire", lambda c, p: c.admin.retire("victim", p)),
    ("set_department_cost", lambda c, p: c.admin.set_department_cost("development", 3.0, p)),
]


def _spec(name):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=["read_files", "write_files"], inputs=["sorgu"], outputs=["netice"], cost_limit=5.0)


@pytest.fixture
def company(tmp_path):
    clock = Clock()
    ring_, control = ring_with_control(clock)
    c = ScopedCompany(Registry(":memory:"), tmp_path / "c.db", tmp_path / "audit.jsonl", owners={"owner"}, clock=clock, verifier=ring_)
    c.test_control = control            # yalnız testin control-plane tutacağı
    c.admin.register_baseline_agent(_spec("victim"), "development", "Rol", SYSTEM)
    return c, clock


@pytest.mark.parametrize("op,call", ADMIN_CALLS, ids=[x[0] for x in ADMIN_CALLS])
def test_P2_company_admin_rejects_unsigned_and_forged_principals(company, op, call):
    c, _ = company
    before = (c.view.get("victim").authority, c.view.get("victim").budget, c.view.get("victim").status)
    real = owner_cred("set_budget", "victim")
    for forged in (FORGED_OWNER, FORGED_SYSTEM, Principal("owner", "mock", "test"),
                   dataclasses.replace(real, id="mallory"), dataclasses.replace(real, signature="A" * 86),
                   dataclasses.replace(SYSTEM, role="owner", kind="human", id="owner")):
        with pytest.raises(CompanyDenied) as e:
            call(c, forged)
        assert e.value.code in ("forged_principal", "role_not_allowed", "unauthorized"), (op, forged, e.value.code)
    assert (c.view.get("victim").authority, c.view.get("victim").budget, c.view.get("victim").status) == before


def test_P2b_escalation_via_unsigned_owner_no_longer_works_and_signed_owner_still_does(company):
    c, _ = company
    with pytest.raises(CompanyDenied):
        c.admin.set_authority("victim", Authority(frozenset({"read_files", "write_files"}), True), FORGED_OWNER)
    assert c.view.get("victim").authority.permissions == frozenset({"read_files"}) and c.view.get("victim").authority.sensitive_ok is False
    c.admin.set_budget("victim", 2.0, OWNER)                                       # etibarlı imzalı sahib işləyir
    assert c.view.get("victim").budget == 2.0


def test_company_without_a_verifier_fails_closed_for_every_admin_operation(tmp_path):
    c = ScopedCompany(Registry(":memory:"), tmp_path / "c.db", tmp_path / "a.jsonl", owners={"owner"})
    for op, call in ADMIN_CALLS:
        with pytest.raises(CompanyDenied) as e:
            call(c, OWNER)
        assert e.value.code == "owner_auth_unavailable", op
    assert c.view.agents() == [] if hasattr(c.view, "agents") else True


def test_company_rejects_expired_revoked_and_wrong_role_credentials(company):
    c, clock = company
    with pytest.raises(CompanyDenied) as e:
        c.admin.set_budget("victim", 1.0, DISPATCHER)                              # imzalıdır, amma dispetçer rolu
    assert e.value.code == "role_not_allowed"
    short = owner_cred("set_budget", "victim", ttl=30)
    c.admin.set_budget("victim", 1.0, short)
    clock.t = T0 + 31
    with pytest.raises(CompanyDenied) as e:
        c.admin.set_budget("victim", 2.0, short)
    assert e.value.code == "credential_expired"
    clock.t = T0
    revoked = owner_cred("set_budget", "victim")
    c.test_control.revoke_credential(revoked.nonce)
    with pytest.raises(CompanyDenied) as e:
        c.admin.set_budget("victim", 2.0, revoked)
    assert e.value.code == "credential_revoked"
    c.test_control.revoke_key(KEY_ID)
    with pytest.raises(CompanyDenied) as e:
        c.admin.suspend("victim", SYSTEM)
    assert e.value.code == "credential_revoked"


def test_company_object_and_admin_are_not_reachable_from_agent_facing_references(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    agent_facing = [w.factory.handle(a.agent_id), w.factory.view, w.factory._core, w.company.view]
    for root in agent_facing:
        names = found(root, (Company, CompanyAdmin))
        assert names == [], (type(root).__name__, names)
    assert not hasattr(w.factory._core, "company") and not hasattr(w.factory._core, "company_admin")


# ----------------------------------------------------------------------------- A-3: P3

def mint(w, claimed, action="submit_result"):
    return w.trust.dispatcher.authorize(claimed.task_id, claimed.agent_id, claimed.run_op, claimed.op_id, claimed.attempts, action)


def test_P3_no_agent_reachable_object_can_issue_dispatch_authorizations(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    h = w.factory.handle(a.agent_id)
    for obj in (h, w.factory._core, w.factory.runtime, w.factory.view, w.factory.control, w.factory.admin):
        assert not any(hasattr(obj, n) for n in ("issue_authorization", "authorize", "_token", "_dispatch_key", "_sign_auth", "sign", "issuer")), type(obj).__name__
    assert "TrustedSigner" not in found(w.factory) and "DispatcherIssuer" not in found(w.factory)
    # saxta səlahiyyət (təsadüfi imza, düzgün sahələr) rədd edilir
    forged = DispatchAuthorization(claimed.task_id, claimed.agent_id, claimed.run_op, claimed.op_id, claimed.attempts, "submit_result", KEY_ID, T0, T0 + 60,
                                   secrets.token_hex(12), base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("="))
    with pytest.raises(FactoryDenied) as e:
        h.submit_result(t.task_id, "saxta nəticə", authorization=forged)
    assert e.value.code == "dispatch_authorization_invalid" and w.factory.view.reports() == []
    # başqa açarla (hücumçunun öz açarı, eyni açar id-si) imzalanmış səlahiyyət
    evil = TrustedSigner(KEY_ID, secrets.token_bytes(32), clock=w.clock).dispatcher_issuer()
    bad = evil.authorize(claimed.task_id, claimed.agent_id, claimed.run_op, claimed.op_id, claimed.attempts, "submit_result")
    with pytest.raises(FactoryDenied) as e:
        h.submit_result(t.task_id, "saxta nəticə", authorization=bad)
    assert e.value.code == "dispatch_authorization_invalid"
    assert h.submit_result(t.task_id, "həqiqi", authorization=mint(w, claimed)).summary == "həqiqi"


@pytest.mark.parametrize("field,value", [("task_id", "tsk_other"), ("agent_id", "agt_other"), ("run_op", "x:a9"), ("op_id", "op_other"),
                                         ("attempt", 7), ("action", "fail"), ("key_id", "unknown-key"), ("nonce", "ff" * 12)])
def test_authorization_binds_every_field(world, field, value):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    tampered = dataclasses.replace(mint(w, claimed), **{field: value})
    with pytest.raises(FactoryDenied):
        w.factory.handle(a.agent_id).submit_result(t.task_id, "x", authorization=tampered)
    assert w.factory.view.task(t.task_id).state == "running"


def test_an_authorization_is_valid_only_for_its_intended_action(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    sub, fail, wait, ok = (mint(w, claimed, x) for x in ("submit_result", "fail", "await_approval", "note_success"))
    with pytest.raises(FactoryDenied):
        w.factory.handle(a.agent_id).submit_result(t.task_id, "x", authorization=fail)
    with pytest.raises(FactoryDenied):
        w.factory.runtime.fail(sub, "x")
    with pytest.raises(FactoryDenied):
        w.factory.runtime.await_approval(ok, "apr")
    with pytest.raises(FactoryDenied):
        w.factory.runtime.note_success(wait)
    assert w.factory.view.task(t.task_id).state == "running" and w.factory.view.reports() == []


# ----------------------------------------------------------------------------- A-4: P4 / P4b

def failures(w, a):
    return w.factory.view.agent(a.agent_id).failures


def test_P4_stale_authorization_cannot_fail_a_completed_task_or_stop_the_agent(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    sub = mint(w, claimed, "submit_result")
    w.factory.handle(a.agent_id).submit_result(t.task_id, "tamam", authorization=sub)
    for _ in range(4):                                                              # audit P4: 3 çağırış agenti dayandırırdı
        with pytest.raises(FactoryDenied) as e:
            w.factory.runtime.fail(mint(w, claimed, "fail"), "yalançı uğursuzluq")
        assert e.value.code == "task_not_running"
    ag = w.factory.view.agent(a.agent_id)
    assert w.factory.view.task(t.task_id).state == "done" and ag.failures == 0 and ag.status == "active"
    with pytest.raises(FactoryDenied):                                              # submit təkrarı da yoxdur
        w.factory.handle(a.agent_id).submit_result(t.task_id, "ikinci", authorization=sub)


def test_P4b_note_success_is_one_shot_and_replay_cannot_reset_the_failure_counter(world):
    w = world
    a = w.factory.control.spawn("programmer", SYSTEM)
    t1 = w.factory.control.assign(a.agent_id, "bir", "iş bir", SYSTEM)
    t2 = w.factory.control.assign(a.agent_id, "iki", "iş iki", SYSTEM)
    c1 = w.factory.runtime.claim(t1.task_id, DISPATCHER)
    w.factory.handle(a.agent_id).submit_result(t1.task_id, "tamam", authorization=mint(w, c1, "submit_result"))
    ok = mint(w, c1, "note_success")
    assert w.factory.runtime.note_success(ok) is True
    c2 = w.factory.runtime.claim(t2.task_id, DISPATCHER)
    out = w.factory.runtime.fail(mint(w, c2, "fail"), "real uğursuzluq")
    assert out.transitioned is True and failures(w, a) == 1
    with pytest.raises(FactoryDenied) as e:                                         # eyni səlahiyyətin təkrarı sayğacı sıfırlaya bilmir
        w.factory.runtime.note_success(ok)
    assert e.value.code == "dispatch_authorization_replayed" and failures(w, a) == 1
    with pytest.raises(FactoryDenied):                                              # təzə, amma tamamlanmamış tapşırığa note_success də yoxdur
        w.factory.runtime.note_success(mint(w, c2, "note_success"))
    assert failures(w, a) == 1


def test_awaiting_approval_invalidates_other_authorizations_and_counts_nothing(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    sub, fail = mint(w, claimed, "submit_result"), mint(w, claimed, "fail")
    wait = mint(w, claimed, "await_approval")
    assert w.factory.runtime.await_approval(wait, "apr_1") is True
    assert w.factory.view.task(t.task_id).state == "awaiting_approval"
    for fn, code in ((lambda: w.factory.runtime.await_approval(wait, "apr_2"), "dispatch_authorization_replayed"),
                     (lambda: w.factory.handle(a.agent_id).submit_result(t.task_id, "x", authorization=sub), "task_not_running"),
                     (lambda: w.factory.runtime.fail(fail, "x"), "task_not_running")):
        with pytest.raises(FactoryDenied) as e:
            fn()
        assert e.value.code in (code, "task_not_running", "dispatch_authorization_replayed"), code
    assert failures(w, a) == 0 and w.factory.view.task(t.task_id).state == "awaiting_approval" and w.factory.view.reports() == []


def test_failure_is_counted_once_only_when_the_transition_really_happens(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    f1, f2 = mint(w, claimed, "fail"), mint(w, claimed, "fail")
    out = w.factory.runtime.fail(f1, "bir")
    assert out.transitioned is True and failures(w, a) == 1
    with pytest.raises(FactoryDenied):                                              # eyni səlahiyyətin təkrarı
        w.factory.runtime.fail(f1, "bir")
    with pytest.raises(FactoryDenied):                                              # tapşırıq artıq failed: yeni səlahiyyət də keçmir
        w.factory.runtime.fail(f2, "iki")
    assert failures(w, a) == 1 and w.factory.view.task(t.task_id).state == "failed"


def test_agent_stopped_mid_run_leaves_the_counter_alone(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    w.factory.control.stop(a.agent_id, SYSTEM)                                      # icra edən tapşırıq failed olur
    with pytest.raises(FactoryDenied):
        w.factory.runtime.fail(mint(w, claimed, "fail"), "gec")
    assert failures(w, a) == 0


def test_dispatcher_repeated_failures_stop_the_agent_through_the_system_issuer_only(world):
    w = world
    w.cloud._state = "unavailable"
    a = w.factory.control.spawn("programmer", SYSTEM)
    for i in range(3):
        r = w.disp.execute_with_retry(w.factory.control.assign(a.agent_id, f"t{i}", f"iş {i}", SYSTEM).task_id)
        assert r.status == "failed"
    ag = w.factory.view.agent(a.agent_id)
    assert ag.status == "stopped" and ag.stopped_by == "system" and ag.failures == 3


def test_authorizations_expire_quickly(world):
    w = world
    a, t = task(w)
    claimed = w.factory.runtime.claim(t.task_id, DISPATCHER)
    sub = mint(w, claimed, "submit_result")
    assert sub.expires_at - sub.issued_at <= 300
    w.clock.t += 61
    with pytest.raises(FactoryDenied) as e:
        w.factory.handle(a.agent_id).submit_result(t.task_id, "gec", authorization=sub)
    assert e.value.code == "credential_expired" and w.factory.view.reports() == []


# ----------------------------------------------------------------------------- A-6: açar keyfiyyəti, rotasiya, ləğv, vaxt

WEAK = [bytes(32), b"\xff" * 32, bytes(range(32)), bytes(range(32, 0, -1)), b"ab" * 16, b"abcd" * 8, bytes(range(16)) * 2,
        b"naymera-static-signing-key-123456", b"0123456789abcdef0123456789abcdef", bytes([i % 5 for i in range(32)])]


@pytest.mark.parametrize("weak", WEAK)
def test_weak_or_static_key_material_is_rejected(weak):
    with pytest.raises(CredentialError):
        validate_signing_seed(weak)
    with pytest.raises(CredentialError):
        TrustedSigner("k-weak-1", weak)


def test_key_material_must_be_exactly_32_bytes_and_properly_encoded():
    good = generate_signing_seed()
    assert len(decode_signing_seed(good)) == 32 and len(good) == 43
    for bad in ("", "short", good[:-1], good + "AAAA", "!" * 43, base64.b64encode(b"a" * 32).decode(), "A" * 43, None, 12345, good.replace(good[0], " ", 1)):
        with pytest.raises(CredentialError):
            decode_signing_seed(bad)
    for _ in range(40):                                                              # yaradıcı həmişə keçən açar verir
        validate_signing_seed(decode_signing_seed(generate_signing_seed()))
    assert len({generate_signing_seed() for _ in range(20)}) == 20


def test_signatures_are_ed25519_and_credentials_are_short_lived():
    s = TrustedSigner("k-1-test", secrets.token_bytes(32), clock=lambda: T0)
    p = s.system_issuer().principal()
    assert len(base64.urlsafe_b64decode(p.signature + "==")) == 64 and p.key_id == "k-1-test"
    assert p.expires_at - p.issued_at <= 900 and p.nonce and p.expires_at > p.issued_at
    for ttl in (0, -1, 901, float("nan"), float("inf"), True, "60"):
        with pytest.raises(CredentialError):
            s.system_issuer(ttl=ttl)
    a = s.dispatcher_issuer().authorize("t", "a", "r", "o", 1, "fail")
    assert a.expires_at - a.issued_at <= 300 and a.key_id == "k-1-test"
    with pytest.raises(CredentialError):
        s.dispatcher_issuer(auth_ttl=301)


def test_credentials_expire_and_are_not_valid_before_issue_time():
    clock = Clock()
    s = TrustedSigner("k-2-test", secrets.token_bytes(32), clock=clock)
    r = s.verifier()
    r._clock = clock
    p = s.system_issuer(ttl=60).principal()
    r.check_principal(p, "system")
    clock.t = T0 + 59.9
    r.check_principal(p, "system")
    clock.t = T0 + 60
    with pytest.raises(CredentialError) as e:
        r.check_principal(p, "system")
    assert e.value.code == "credential_expired"
    clock.t = T0 - 100                                                              # gələcəkdə verilmiş (saat sürüşməsi aşılıb)
    with pytest.raises(CredentialError) as e:
        r.check_principal(p, "system")
    assert e.value.code == "credential_expired"


def test_key_rotation_and_revocation():
    clock = Clock()
    old = TrustedSigner("k-old-001", secrets.token_bytes(32), clock=clock)
    new = TrustedSigner("k-new-001", secrets.token_bytes(32), clock=clock)
    r = Keyring(clock)
    ctl = r.take_control()
    ctl.add_public_key("k-old-001", old.public_key)
    p_old = old.system_issuer().principal()
    r.check_principal(p_old, "system")
    ctl.add_public_key("k-new-001", new.public_key)                                 # rotasiya: yeni açar aktiv, köhnə yalnız yoxlama üçün qalır
    p_new = new.system_issuer().principal()
    r.check_principal(p_new, "system")
    r.check_principal(p_old, "system")
    with pytest.raises(CredentialError):
        ctl.add_public_key("k-new-001", new.public_key)                             # eyni id ikinci dəfə əlavə olunmur
    ctl.revoke_key("k-old-001")                                                     # kompromis: köhnə açarın BÜTÜN etibarnamələri rədd
    with pytest.raises(CredentialError) as e:
        r.check_principal(p_old, "system")
    assert e.value.code == "credential_revoked"
    r.check_principal(p_new, "system")
    ctl.revoke_credential(p_new.nonce)                                              # tək etibarnamənin ləğvi
    with pytest.raises(CredentialError) as e:
        r.check_principal(p_new, "system")
    assert e.value.code == "credential_revoked"
    other = new.system_issuer().principal()
    r.check_principal(other, "system")                                              # başqa etibarnamə təsirlənmir
    with pytest.raises(CredentialError):                                            # naməlum açar id-si
        Keyring(clock).check_principal(other, "system")


def test_malformed_credentials_fail_closed():
    clock = Clock()
    s = TrustedSigner("k-3-test", secrets.token_bytes(32), clock=clock)
    r = s.verifier()
    p = s.system_issuer().principal()
    mal = [dataclasses.replace(p, nonce=""), dataclasses.replace(p, nonce=None), dataclasses.replace(p, issued_at=float("nan")),
           dataclasses.replace(p, expires_at=float("inf")), dataclasses.replace(p, expires_at=p.issued_at), dataclasses.replace(p, issued_at="x"),
           dataclasses.replace(p, key_id=None), dataclasses.replace(p, signature=None), dataclasses.replace(p, signature="%%%"),
           dataclasses.replace(p, role="root"), dataclasses.replace(p, expires_at=p.issued_at + 100000), Principal("naymera", "system", "core"), "owner", None, 5]
    for x in mal:
        with pytest.raises(CredentialError):
            r.check_principal(x, "system")
    a = s.dispatcher_issuer().authorize("t", "a", "r", "o", 1, "fail")
    for x in (dataclasses.replace(a, attempt=True), dataclasses.replace(a, attempt="1"), dataclasses.replace(a, task_id=""), dataclasses.replace(a, action="x"),
              dataclasses.replace(a, expires_at=a.issued_at + 99999), None, "tok"):
        with pytest.raises(CredentialError):
            r.check_authorization(x)
    r.check_authorization(a, "fail")
    with pytest.raises(CredentialError):
        r.check_authorization(a, "submit_result")


def test_P6_environment_loading_rejects_weak_partial_and_malformed_configuration():
    good = generate_signing_seed()
    assert load_signing_config({}) is None and load_signing_config({"NAYMERA_SIGNING_KEY": "", "NAYMERA_SIGNING_KEY_ID": ""}) is None
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "k-2026-11"})
    assert cfg.key_id == "k-2026-11" and len(cfg.seed) == 32 and good not in repr(cfg) and "seed" not in repr(cfg)
    bad_envs = [
        {"NAYMERA_SIGNING_KEY": good},                                                              # id yoxdur
        {"NAYMERA_SIGNING_KEY_ID": "k-1-test"},                                                    # açar yoxdur
        {"NAYMERA_SIGNING_KEY": "a" * 40, "NAYMERA_SIGNING_KEY_ID": "k-1-test"},                    # statik/zəif (köhnə HMAC tipli mətn açarı)
        {"NAYMERA_SIGNING_KEY": base64.urlsafe_b64encode(bytes(32)).decode().rstrip("="), "NAYMERA_SIGNING_KEY_ID": "k-1-test"},
        {"NAYMERA_SIGNING_KEY": base64.urlsafe_b64encode(b"ab" * 16).decode().rstrip("="), "NAYMERA_SIGNING_KEY_ID": "k-1-test"},
        {"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "x"},                              # etibarsız id
        {"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "k-1-test", "NAYMERA_REVOKED_KEYS": "k-1-test"},
        {"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "k-1-test", "NAYMERA_VERIFY_KEYS": "k-1-test=" + good},   # id təkrarı
        {"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "k-1-test", "NAYMERA_VERIFY_KEYS": "k-old-001"},          # format
        {"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "k-1-test", "NAYMERA_VERIFY_KEYS": "k-old-001=short"},
    ]
    for env in bad_envs:
        with pytest.raises(BootstrapError):
            load_signing_config(env)
    other = TrustedSigner("k-old-001", secrets.token_bytes(32))
    pub = base64.urlsafe_b64encode(other.public_key).decode().rstrip("=")
    cfg2 = load_signing_config({"NAYMERA_SIGNING_KEY": good, "NAYMERA_SIGNING_KEY_ID": "k-new-001", "NAYMERA_VERIFY_KEYS": f"k-old-001={pub}",
                                "NAYMERA_REVOKED_KEYS": "k-bad-001"})
    anchor = TrustAnchor(cfg2)
    assert set(anchor.verifier.key_ids()) == {"k-new-001", "k-old-001"}
    p_old = other.system_issuer().principal()
    anchor.verifier.check_principal(p_old, "system")                                                  # rotasiya: köhnə açar yoxlanılır
    anchor.revoke_key("k-old-001")
    with pytest.raises(CredentialError):
        anchor.verifier.check_principal(p_old, "system")


# ----------------------------------------------------------------------------- A-5: istehsal bootstrap-ı və step-up ilə sahib şəxsiyyəti (P5)

@pytest.fixture
def prod(tmp_path):
    """Etibarlı bootstrap ilə qurulmuş real yığın (yerli SQLite, MOCK mühərriklər, saxta cihaz). Real şəbəkə/açar/ödəniş yoxdur."""
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-prod-001"})
    anchor = TrustAnchor(cfg)
    svc = build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, trust=anchor)
    dev = pair(svc.auth)
    ns = types.SimpleNamespace(anchor=anchor, svc=svc, stack=svc.factory, dev=dev, auth=svc.auth)
    return ns


def authenticate(p, op="create_agent", subject="programmer", phash=H, dev=None, challenge_op=None):
    dev = dev or p.dev
    ch = p.stack.owner_gate.challenge(dev.device_id, challenge_op or op, subject, phash)
    proof = dev.sign(ch, "factory:" + (challenge_op or op), subject, phash, "approve")
    return p.stack.owner_gate.authenticate(dev.device_id, ch.id, op, subject, phash, proof)


def owner_for(p, op, *args, dev=None, **kwargs):
    """Sahib bu DƏQİQ əməliyyat üçün step-up edir: əhatə `console.scope(...)` ilə (əməliyyata verilən eyni arqumentlər) hesablanır."""
    _, subject, phash = p.stack.console.scope(op, *args, **kwargs)
    return authenticate(p, op, subject, phash, dev=dev)


def test_P5_production_bootstrap_builds_the_factory_and_exposes_only_restricted_capabilities(prod):
    s = prod.stack
    assert isinstance(s, FactoryStack)
    assert {f.name for f in dataclasses.fields(s)} == {"view", "owner_gate", "console", "dispatcher", "team", "agents"}
    names = {type(x).__name__ for x in (s.view, s.owner_gate, s.console, s.dispatcher, s.team, s.agents)}
    assert names == {"FactoryView", "OwnerGate", "OwnerConsole", "AgentDispatcher", "TeamRunner", "AgentGateway"}
    assert prod.svc.company._core.verifier is prod.anchor.verifier                      # Company eyni açıq açarlarla MÜSTƏQİL yoxlayır
    assert not any(hasattr(s, n) for n in ("factory", "control", "runtime", "company", "signer", "issuer", "anchor", "admin", "agent_handle"))
    # B-2: stack-in heç bir sahəsi AgentFactory-nin bağlı metodu və ya fasad deyil
    for f in dataclasses.fields(s):
        v = getattr(s, f.name)
        assert not isinstance(getattr(v, "__self__", None), AgentFactory) and not isinstance(v, (FactoryAdmin, FactoryControl, FactoryRuntime, AgentFactory))
    owner = owner_for(prod, "create_agent", "programmer")
    agent = s.console.create_agent("programmer", owner)
    assert agent.status == "active"


def test_without_a_trust_anchor_there_is_no_factory_and_company_admin_is_closed(tmp_path):
    svc = build_app(tmp_path / "d", owner_id="owner", global_limit=0.0)
    assert svc.factory is None
    with pytest.raises(CompanyDenied) as e:
        svc.company.admin.set_department_cost("development", 1.0, owner_cred("set_department_cost", "development"))
    assert e.value.code == "owner_auth_unavailable"


def test_company_must_be_built_with_the_anchors_verifier(tmp_path):
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-prod-001"})
    anchor = TrustAnchor(cfg)
    clock = Clock()
    loose = ScopedCompany(Registry(":memory:"), tmp_path / "c.db", tmp_path / "a.jsonl", owners={"owner"}, verifier=ring(clock))     # başqa yoxlayıcı
    auth = AuthService(tmp_path / "auth.db", tmp_path / "a.jsonl", clock=clock)
    with pytest.raises(BootstrapError):
        anchor.build_factory(company=loose, auth=auth, owners={"owner"}, db_path=tmp_path / "f.db", audit_path=tmp_path / "a.jsonl",
                             workspace_root=tmp_path / "ws", team_db=tmp_path / "t.db", orchestrator_factory=lambda r: None, router=None)


def test_unauthenticated_callers_cannot_mint_an_owner_identity(prod):
    s = prod.stack
    # 1) ixtiyari owner id: imza yoxdur
    for forged in (FORGED_OWNER, Principal("owner", "mock", "x")):
        with pytest.raises(FactoryDenied) as e:
            s.console.create_agent("programmer", forged)
        assert e.value.code == "forged_principal"
    # 2) gate yalnız step-up ilə: səhv/boş/başqasının sübutu
    ch = s.owner_gate.challenge(prod.dev.device_id, "create_agent", "programmer", H)
    for proof in (None, "", "AAAA", base64.b64encode(b"x" * 70).decode()):
        with pytest.raises(AuthError):
            s.owner_gate.authenticate(prod.dev.device_id, ch.id, "create_agent", "programmer", H, proof)
    other = pair(prod.auth, "Başqa cihaz")
    ch2 = s.owner_gate.challenge(prod.dev.device_id, "create_agent", "programmer", H)
    with pytest.raises(AuthError):                                                        # başqa cihazın açarı ilə imzalanmış sübut
        s.owner_gate.authenticate(prod.dev.device_id, ch2.id, "create_agent", "programmer", H,
                                  other.sign(ch2, "factory:create_agent", "programmer", H, "approve"))
    # 3) qəbz saxtakarlığı: təsadüfi/özü qurulmuş qəbz
    issuer = s.owner_gate._issuer
    for receipt in (None, StepUpReceipt("a" * 32, prod.dev.device_id, "factory:create_agent", "programmer", H), "receipt", object()):
        with pytest.raises(CredentialError) as e:
            issuer.from_step_up(receipt)
        assert e.value.code == "proof_required"
    assert s.view.agents() == []


def test_valid_step_up_mints_a_short_lived_owner_identity_that_works(prod):
    owner = owner_for(prod, "create_agent", "programmer")
    assert isinstance(owner, SignedPrincipal) and owner.role == "owner" and owner.kind == "human" and owner.id == "owner"
    assert owner.channel == "stepup:" + prod.dev.device_id and owner.expires_at - owner.issued_at <= 120
    a = prod.stack.console.create_agent("programmer", owner)
    assert a.status == "active" and prod.stack.view.agent(a.agent_id) is not None
    with pytest.raises(FactoryDenied) as e:                                                # sahib etibarnaməsi sistem əməliyyatı üçün deyil, amma admin üçündür
        prod.stack.console.create_agent("programmer", FORGED_OWNER)
    assert e.value.code == "forged_principal"


def test_step_up_proof_is_bound_single_use_and_not_convertible_from_other_actions(prod):
    s, auth = prod.stack, prod.auth
    ch = s.owner_gate.challenge(prod.dev.device_id, "create_agent", "programmer", H)
    proof = prod.dev.sign(ch, "factory:create_agent", "programmer", H, "approve")
    with pytest.raises(AuthError):                                                          # başqa əməliyyat/subject üçün istifadə
        s.owner_gate.authenticate(prod.dev.device_id, ch.id, "create_agent", "researcher", H, proof)
    with pytest.raises(AuthError):                                                          # challenge artıq yandı (bir-dəfəlik)
        s.owner_gate.authenticate(prod.dev.device_id, ch.id, "create_agent", "programmer", H, proof)
    # adi təsdiq (approval) step-up-ı admin şəxsiyyətinə çevrilə bilməz: `factory:` prefiksi tələb olunur
    ch2 = auth.create_challenge(prod.dev.device_id, "approval", "abc", H, "approve")
    receipt = auth.verify_proof(prod.dev.device_id, ch2.id, "approval", "abc", H, "approve", prod.dev.sign(ch2, "approval", "abc", H, "approve"))
    with pytest.raises(CredentialError):
        s.owner_gate._issuer.from_step_up(receipt)
    # düzgün qəbz yalnız bir dəfə qəbul olunur
    ch3 = auth.create_challenge(prod.dev.device_id, "factory:grant", "agt", H, "approve")
    good = auth.verify_proof(prod.dev.device_id, ch3.id, "factory:grant", "agt", H, "approve", prod.dev.sign(ch3, "factory:grant", "agt", H, "approve"))
    assert s.owner_gate._issuer.from_step_up(good).role == "owner"
    with pytest.raises(CredentialError):
        s.owner_gate._issuer.from_step_up(good)
    tampered = dataclasses.replace(good, subject="başqa")
    with pytest.raises(CredentialError):
        s.owner_gate._issuer.from_step_up(tampered)


def test_revoked_device_cannot_obtain_or_use_owner_authentication(prod):
    s, auth = prod.stack, prod.auth
    ch = auth.create_challenge(prod.dev.device_id, "factory:grant", "agt", H, "approve")
    receipt = auth.verify_proof(prod.dev.device_id, ch.id, "factory:grant", "agt", H, "approve", prod.dev.sign(ch, "factory:grant", "agt", H, "approve"))
    auth.revoke_device(prod.dev.device_id)                                                  # qəbz alındıqdan sonra cihaz ləğv edildi
    with pytest.raises(CredentialError):
        s.owner_gate._issuer.from_step_up(receipt)
    with pytest.raises(AuthError):
        s.owner_gate.challenge(prod.dev.device_id, "create_agent", "programmer", H)


def test_owner_identity_expires_quickly_and_the_key_can_be_revoked(prod):
    owner = owner_for(prod, "create_agent", "programmer")
    prod.stack.console.create_agent("programmer", owner)
    prod.anchor.revoke_credential(owner.nonce)                                      # tək etibarnamə ləğvi
    with pytest.raises(FactoryDenied) as e:
        prod.stack.console.create_agent("researcher", owner)
    assert e.value.code == "credential_revoked"
    owner2 = owner_for(prod, "create_agent", "researcher")
    prod.stack.console.create_agent("researcher", owner2)
    prod.anchor.revoke_key("k-prod-001")                                            # açar kompromisi: hamısı bağlanır
    with pytest.raises(FactoryDenied) as e:
        prod.stack.console.create_agent("data_analyst", owner2)
    assert e.value.code == "credential_revoked"
    with pytest.raises(FactoryDenied):
        prod.stack.console.create_agent("data_analyst", owner_for(prod, "create_agent", "data_analyst"))


def test_production_dispatcher_and_team_use_short_lived_credentials(prod):
    d = prod.stack.dispatcher
    p1 = d._issuer.principal()
    p2 = d._issuer.principal()
    assert p1.nonce != p2.nonce and p1.role == "dispatcher" and p1.expires_at - p1.issued_at <= 120
    assert prod.stack.team.system.principal().role == "system"


# ----------------------------------------------------------------------------- struktur

def test_structure_hmac_authority_is_gone_and_env_is_read_only_in_runtime():
    root = Path(__file__).resolve().parent.parent
    for name in ("agents/factory_auth.py", "agents/agent_factory.py", "agents/agent_runtime.py", "agents/team.py", "agents/company.py", "api/owner_gate.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "import hmac" not in text and "PrincipalAuthority" not in text and "environ" not in text and "getenv" not in text, name
    assert "build_principal_authority" not in (root / "runtime/env.py").read_text(encoding="utf-8")
    assert "NAYMERA_FACTORY_AUTH_KEY" not in (root / ".env.example").read_text(encoding="utf-8")
    env_text = (root / ".env.example").read_text(encoding="utf-8")
    assert "NAYMERA_SIGNING_KEY=\n" in env_text and "NAYMERA_SIGNING_KEY_ID=\n" in env_text
    assert "cryptography" in (root / "requirements.txt").read_text(encoding="utf-8")
