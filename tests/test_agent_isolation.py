"""Agent izolyasiya sərhədi (PR #20, audit B-1/B-2): agent kodu AYRI OS prosesində, minimal autentifikasiyalı RPC. Exploit-reqressiya E1a–E1e və E2.

Testlər istehsal bootstrap-ı ilə (`build_app(trust=...)`, real `TrustAnchor`, `AuthService`, `OwnerGate`, `AgentGateway`) işləyir və HƏQİQİ işçi proseslər açır.
Heç bir real şəbəkə/ödəniş/açar yoxdur (yalnız loopback test soketi); agent "proqramları" testin yazdığı kiçik skriptlərdir.
uid-endirmə testləri yalnız root mühitdə işləyir (aksi halda atlanır): root olmayan mühitdə agent EYNİ istifadəçidir və fayl sistemi izolyasiyası YOXDUR (sənədləşdirilmiş məhdudiyyət)."""
import dataclasses
import json
import os
import socket
import struct
import sys
import time
import types
from pathlib import Path

import pytest

from runtime import agent_wire
from agents.agent_factory import AgentFactory, FactoryAdmin, FactoryControl, FactoryRuntime
from runtime.agent_gateway import ALLOWED_OPS, AgentGateway, IsolationError
from agents.factory_auth import CredentialError, Keyring, TrustedSigner, generate_signing_seed
from api.bootstrap import build_app
from runtime.factory_bootstrap import CONSOLE_OPS, FactoryStack, OwnerConsole, TrustAnchor, load_signing_config
from tests.test_api_auth import pair
from tests.test_security_boundary import H, authenticate, reach
from tests.test_agent_runtime import task, world  # noqa: F401  (fixture)

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_root = pytest.mark.skipif(not IS_ROOT, reason="uid endirmə yalnız root mühitdə sınanır (root olmayanda agent eyni istifadəçidir)")
SAME_UID = {"uid_pool": None, "allow_insecure_same_uid": True}          # AÇIQ seçim: unikal uid YOXDUR, TƏHLÜKƏSİZLİK SƏRHƏDİ DEYİL


def make_prod(tmp_path, *, drop: bool):
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-iso-001"})
    anchor = TrustAnchor(cfg)
    svc = build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, trust=anchor, agent_isolation=None if drop else SAME_UID)
    dev = pair(svc.auth)
    p = types.SimpleNamespace(anchor=anchor, svc=svc, stack=svc.factory, dev=dev, auth=svc.auth, root=tmp_path / "data" / "workspaces" / "agent_programs")
    return p


def owner(p):
    return authenticate(p)


def program(p, name: str, code: str) -> str:
    p.root.mkdir(parents=True, exist_ok=True)
    (p.root / name).write_text(code, encoding="utf-8")
    return name


def agent(p, role="programmer", **kw):
    return p.stack.console.create_agent(role, owner(p), **kw)


@pytest.fixture
def prod(tmp_path):
    p = make_prod(tmp_path, drop=False)
    yield p
    p.stack.agents.close()


@pytest.fixture
def prod_drop(tmp_path):
    p = make_prod(tmp_path, drop=True)
    yield p
    p.stack.agents.close()


def out(res):
    return json.loads(res.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------------------------- E1 (əlçatanlıq): agent prosesində serverin heç bir obyekti yoxdur

REACH = '''
import gc, json, sys
def main(c):
    names = {"Keyring","KeyringControl","TrustedSigner","TrustAnchor","_Core","CompanyAdmin","CompanyView","Company","Registry","FactoryAdmin","FactoryControl",
             "FactoryRuntime","AgentFactory","AgentHandle","DispatcherIssuer","SystemIssuer","OwnerIssuer","OwnerGate","Ed25519PrivateKey","Connection","AuthService",
             "AgentDispatcher","AgentGateway","CostLedger","ApprovalService"}
    r = {"types": sorted({type(o).__name__ for o in gc.get_objects() if type(o).__name__ in names})}
    r["modules"] = sorted(m for m in sys.modules if m.split(".")[0] in ("agents","api","runtime","safety","cryptography","sqlite3"))
    r["path"] = list(sys.path)
    print(json.dumps(r))
'''


def test_E1_agent_process_contains_no_server_objects_or_modules(prod):
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "reach.py", REACH), timeout_s=15)
    r = out(res)
    assert res.exit_code == 0 and res.killed == "", res
    assert r["types"] == [], r["types"]                       # nə Keyring, nə Connection, nə fabrik, nə imza obyekti
    assert r["modules"] == [], r["modules"]                   # deponun modulları/sqlite/cryptography belə yüklənməyib
    assert not any("clickwin" in x for x in r["path"])         # depo sys.path-də deyil


IMPORT_REPO = '''
import gc, json, sys
def main(c):
    repo = c.args[0]
    sys.path.insert(0, repo)
    res = {}
    try:
        import agents.agent_factory as af
        res["imported"] = True
    except Exception as e:
        res["imported"] = False
        res["error"] = type(e).__name__
    res["instances"] = sorted({type(o).__name__ for o in gc.get_objects() if type(o).__name__ in ("AgentFactory","_Core","Keyring","CompanyAdmin","Registry","TrustedSigner")})
    print(json.dumps(res))
'''


def test_E1_even_importing_the_repo_code_yields_no_instances(prod):
    a = agent(prod)
    repo = str(Path(__file__).resolve().parent.parent)
    r = out(prod.stack.agents.run_session(a.agent_id, program(prod, "imp.py", IMPORT_REPO), args=[repo], timeout_s=20))
    assert r["instances"] == [], r                              # kod ola bilər, amma serverin obyektləri başqa prosesdədir


# ----------------------------------------------------------------------------- E1a: keyring / E1c-E1e: imtiyazlı əməliyyatlar RPC-də mövcud deyil

ATTACK = '''
import json
def main(c):
    op = c.args[0]
    try:
        res = c.call(op, **json.loads(c.args[1]) if len(c.args) > 1 else {})
        print(json.dumps({"ok": True, "result": res}))
    except Exception as e:
        print(json.dumps({"ok": False, "code": getattr(e, "code", type(e).__name__)}))
'''
FORBIDDEN_OPS = [
    # E1a: keyring
    "add_public_key", "revoke_key", "revoke_credential", "freeze", "take_control", "keyring", "verifier",
    # E1c/E1d: fabrik/nüvə, tapşırıq vəziyyəti, hesabat
    "assign_task", "core_assign", "assign", "submit_result", "claim", "fail", "await_approval", "note_success", "new_agent", "spawn", "stop", "restart", "archive",
    "cancel_task", "sql", "exec", "eval", "query", "execute_sql",
    # E1b/E1e: Company/admin/registry
    "set_budget", "set_authority", "grant", "create_agent", "set_policy", "resolve_request", "delete", "registry_update", "registry_add", "company", "admin",
    "authorize", "issue_authorization", "from_step_up", "owner", "sign", "mint",
    "__class__", "info.__self__", "", " ", "INFO", "Info", "info ", "handoff;drop",
]


@pytest.mark.parametrize("op", FORBIDDEN_OPS)
def test_E1_privileged_operations_do_not_exist_on_the_rpc_surface(prod, op):
    a = agent(prod)
    before = (prod.stack.view.agent(a.agent_id), len(prod.stack.view.tasks()), prod.stack.view.requests(), prod.anchor.verifier.key_ids())
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "attack.py", ATTACK), args=[op], timeout_s=15)
    r = out(res)
    assert r == {"ok": False, "code": "unknown_op"}, (op, r)
    assert res.denied == ((op, "unknown_op"),)
    after = (prod.stack.view.agent(a.agent_id), len(prod.stack.view.tasks()), prod.stack.view.requests(), prod.anchor.verifier.key_ids())
    assert before == after                                      # heç nə dəyişməyib; açar çoxluğu eynidir


def test_rpc_surface_is_exactly_the_documented_minimum():
    assert ALLOWED_OPS == ("hello", "info", "request_permission", "handoff")


def test_repeated_unauthorized_operations_kill_the_session(prod):
    a = agent(prod)
    code = '''
def main(c):
    for op in ("sql", "assign_task", "set_budget", "add_public_key", "info"):
        try:
            c.call(op)
        except Exception:
            pass
'''
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "spam.py", code), timeout_s=15)
    assert res.killed.startswith("violations") and res.violations >= 3 and res.exit_code != 0


# ----------------------------------------------------------------------------- icazəli əməliyyatlar işləyir və yalnız öz agentinə aiddir

OK = '''
import json
def main(c):
    out = {"info": c.info()}
    out["perm"] = c.request_permission("write_files", "kod yazmaq üçün")
    out["perm_again"] = c.request_permission("write_files", "yenə")
    try:
        c.request_permission("not_a_permission", "x")
    except Exception as e:
        out["unknown_permission"] = e.code
    other = c.args[0]
    out["handoff"] = c.handoff(other, "yoxla", {"kod": "def f(): pass"}, "read_files", 0.0)
    try:
        c.handoff(c.info()["agent_id"], "özünə", {"a": "b"})
    except Exception as e:
        out["self_handoff"] = e.code
    print(json.dumps(out))
'''


def test_authorized_operations_work_and_are_bound_to_the_session_agent(prod):
    a, b = agent(prod, delegate_to=("development",)), agent(prod, "test_engineer")      # ötürmə səlahiyyəti yalnız sahibin verdiyi qədərdir
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "ok.py", OK), args=[b.agent_id], timeout_s=20)
    r = out(res)
    assert res.exit_code == 0 and res.violations == 0, res
    assert r["info"]["agent_id"] == a.agent_id and set(r["info"]) == {"agent_id", "name", "role_id", "title", "department", "status", "permissions"}
    assert r["perm"]["state"] == "pending" and r["perm_again"]["request_id"] == r["perm"]["request_id"]
    assert r["unknown_permission"] == "unknown_permission" and r["self_handoff"] == "self_handoff"
    assert r["handoff"]["agent_id"] == b.agent_id and r["handoff"]["state"] == "queued"
    reqs = prod.stack.view.requests(state="pending")
    assert [(x.agent_id, x.permission) for x in reqs] == [(a.agent_id, "write_files")]       # istək sahibə qərar üçün qaldı, icazə verilmədi
    assert prod.stack.view.agent(a.agent_id).permissions == frozenset({"read_files"})
    assert res.calls == {"hello": 1, "info": 2, "request_permission": 3, "handoff": 2}


SPOOF = '''
import json
def main(c):
    op, args = {"info": ("info", {"agent_id": c.args[0]}),
                "perm": ("request_permission", {"permission": "read_files", "reason": "x", "agent_id": c.args[0]}),
                "handoff": ("handoff", {"to_agent_id": c.args[0], "title": "t", "payload": {"a": "b"}, "from_agent_id": c.args[0]})}[c.args[1]]
    try:
        c.call(op, **args)
        print(json.dumps("ACCEPTED"))
    except Exception as e:
        print(json.dumps(e.code))
'''


@pytest.mark.parametrize("which", ["info", "perm", "handoff"])
def test_the_session_agent_id_cannot_be_chosen_by_the_client(prod, which):
    a, victim = agent(prod), agent(prod, "researcher")
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "spoof.py", SPOOF), args=[victim.agent_id, which], timeout_s=15)
    assert out(res) == "bad_args"
    assert prod.stack.view.requests() == []


BADARGS = '''
import json
def main(c):
    out = []
    for op, args in (("request_permission", {"permission": "X" * 50, "reason": "r"}), ("request_permission", {"permission": "read_files", "reason": "r" * 400}),
                     ("handoff", {"to_agent_id": "a", "title": "t", "payload": {"k": {"nested": 1}}}), ("handoff", {"to_agent_id": "a", "title": "t", "payload": []}),
                     ("handoff", {"to_agent_id": "a", "title": "t", "payload": {"k": "v"}, "cost": -1}), ("handoff", {"to_agent_id": "a", "title": "t", "payload": {"k": "v"}, "cost": True}),
                     ("handoff", {"to_agent_id": "../x", "title": "t", "payload": {"k": "v"}}), ("info", {"x": 1})):
        try:
            c.call(op, **args)
            out.append("ACCEPTED")
        except Exception as e:
            out.append(e.code)
            if len(out) >= 2:
                break
    print(json.dumps(out))
'''


def test_arguments_are_strictly_validated(prod):
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "bad.py", BADARGS), timeout_s=15)
    assert out(res) == ["bad_args", "bad_args"]


# ----------------------------------------------------------------------------- autentifikasiya: çərçivə MAC/sıra/ölçü pozuntuları sessiyanı öldürür

FRAMES = '''
import json, struct
import agent_wire
def main(c):
    mode = c.args[0]
    w = c._w
    if mode == "bad_mac":
        good = w.pack({"id": 99, "op": "info", "args": {}})
        bad = bytearray(good); bad[6] ^= 1
        c._sock.sendall(bytes(bad))
    elif mode == "replay":
        good = w.pack({"id": 99, "op": "info", "args": {}})
        c._sock.sendall(good); c._sock.sendall(good)
    elif mode == "wrong_key":
        evil = agent_wire.Writer(b"k" * 32, agent_wire.C2S)
        c._sock.sendall(evil.pack({"id": 1, "op": "info", "args": {}}))
    elif mode == "oversize":
        c._sock.sendall(struct.pack(">I", 10_000_000) + b"x" * 100)
    elif mode == "garbage":
        c._sock.sendall(b"\\x00\\x00\\x00\\x40" + b"z" * 64)
    elif mode == "reflected_direction":
        s2c = agent_wire.Writer(b"k" * 32, agent_wire.S2C)
        c._sock.sendall(s2c.pack({"id": 1, "op": "info", "args": {}}))
    elif mode == "bad_shape":
        c._sock.sendall(w.pack({"id": "x", "op": "info", "args": {}}))
    elif mode == "extra_key":
        c._sock.sendall(w.pack({"id": 5, "op": "info", "args": {}, "agent_id": "agt_x"}))
    import time
    time.sleep(5)
'''


@pytest.mark.parametrize("mode,reason", [("bad_mac", "wire:bad_mac"), ("replay", "wire:bad_mac"), ("wrong_key", "wire:bad_mac"), ("oversize", "wire:bad_length"),
                                         ("garbage", "wire:bad_mac"), ("reflected_direction", "wire:bad_mac"), ("bad_shape", "bad_request"), ("extra_key", "bad_request")])
def test_unauthenticated_or_malformed_frames_terminate_the_session(prod, mode, reason):
    a = agent(prod)
    t0 = time.monotonic()
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "frames.py", FRAMES), args=[mode], timeout_s=20)
    assert res.killed == reason, (mode, res)
    assert time.monotonic() - t0 < 4.5                          # sessiya 5 s yatmadan əvvəl serverin qərarı ilə bağlandı


def test_wire_codec_authenticates_orders_and_bounds_frames():
    key = os.urandom(32)
    w, r = agent_wire.Writer(key, agent_wire.C2S), agent_wire.Reader(key, agent_wire.C2S)
    f1, f2 = w.pack({"id": 1, "op": "info", "args": {}}), w.pack({"id": 2, "op": "info", "args": {}})
    assert [m["id"] for m in r.feed(f1 + f2)] == [1, 2]
    r2 = agent_wire.Reader(key, agent_wire.C2S)
    with pytest.raises(agent_wire.WireError):                   # sıra pozulması (f2 birinci)
        r2.feed(f2)
    with pytest.raises(agent_wire.WireError):                   # başqa istiqamət
        agent_wire.Reader(key, agent_wire.S2C).feed(f1)
    with pytest.raises(agent_wire.WireError):                   # başqa açar
        agent_wire.Reader(os.urandom(32), agent_wire.C2S).feed(f1)
    with pytest.raises(agent_wire.WireError):
        agent_wire.Writer(key, agent_wire.C2S).pack({"x": "a" * (agent_wire.MAX_BODY + 1)})
    with pytest.raises(ValueError):
        agent_wire.Writer(key, agent_wire.C2S).pack({"x": float("nan")})
    bad = agent_wire.Writer(key, agent_wire.C2S)
    payload = b'{"id": NaN}'
    seq1 = agent_wire._mac(key, agent_wire.C2S, 1, payload)
    with pytest.raises(agent_wire.WireError):
        agent_wire.Reader(key, agent_wire.C2S).feed(struct.pack(">I", len(seq1 + payload)) + seq1 + payload)
    seq1 = agent_wire._mac(key, agent_wire.C2S, 1, b"[1,2]")
    with pytest.raises(agent_wire.WireError):
        agent_wire.Reader(key, agent_wire.C2S).feed(struct.pack(">I", len(seq1 + b"[1,2]")) + seq1 + b"[1,2]")
    del bad


# ----------------------------------------------------------------------------- yaşam dövrü və qapalı qalma

LOOP = '''
import time
def main(c):
    for _ in range(200):
        c.info()
        time.sleep(0.05)
'''


def test_stopping_the_agent_ends_its_session(prod):
    a = agent(prod)
    sid = prod.stack.agents.open_session(a.agent_id, program(prod, "loop.py", LOOP), timeout_s=20)
    for _ in range(10):
        prod.stack.agents.pump(0.05)
    assert not prod.stack.agents._sessions[sid].done
    prod.stack.console.stop(a.agent_id, owner(prod), "sınaq")
    while not prod.stack.agents._sessions[sid].done:
        prod.stack.agents.pump(0.05)
    res = prod.stack.agents.result(sid)
    assert res.killed == "agent_not_active" and ("info", "agent_not_active") in res.denied


def test_sessions_are_refused_for_unknown_stopped_agents_and_bad_programs(prod, tmp_path):
    a = agent(prod)
    good = program(prod, "ok2.py", "def main(c):\n    pass\n")
    gw = prod.stack.agents
    for bad_agent in ("agt_nope", "", None, 5):
        with pytest.raises(IsolationError):
            gw.open_session(bad_agent, good)
    for bad_prog in ("../x.py", "sub/x.py", "x.txt", "", "nope.py", ".hidden.py", "a b.py", None):
        with pytest.raises(IsolationError):
            gw.open_session(a.agent_id, bad_prog)
    (prod.root / "link.py").symlink_to(prod.root / "ok2.py")
    with pytest.raises(IsolationError):
        gw.open_session(a.agent_id, "link.py")
    prod.stack.console.stop(a.agent_id, owner(prod))
    with pytest.raises(IsolationError):
        gw.open_session(a.agent_id, good)


def test_timeout_kills_a_runaway_agent_process(prod):
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "hang.py", "import time\ndef main(c):\n    time.sleep(60)\n"), timeout_s=1.0)
    assert res.killed == "timeout" and res.exit_code != 0


def test_default_gateway_fails_closed_without_os_level_uid_drop(tmp_path):
    called = []
    gw = AgentGateway(lambda i: called.append(i), lambda i: "active", program_root=tmp_path, uid_pool=None, harden_server=False)    # uid_pool yoxdur, same-uid seçilməyib
    (tmp_path / "x.py").write_text("def main(c):\n    pass\n")
    with pytest.raises(IsolationError):
        gw.open_session("agt_x", "x.py")
    gw2 = AgentGateway(lambda i: None, lambda i: "active", program_root=tmp_path, harden_server=False)     # defolt uid_pool, lakin root olmayan server
    if not IS_ROOT:
        with pytest.raises(IsolationError):
            gw2.open_session("agt_x", "x.py")
    gw.close(); gw2.close()
    assert called == []


def test_production_bootstrap_default_requires_the_os_drop(tmp_path):
    p = make_prod(tmp_path, drop=True)
    a = agent(p)
    code = program(p, "d.py", "def main(c):\n    pass\n")
    if IS_ROOT:
        res = p.stack.agents.run_session(a.agent_id, code, timeout_s=15)
        assert res.exit_code == 0 and res.isolation["uid_unique"] is True and res.security_boundary is True
    else:
        with pytest.raises(IsolationError):
            p.stack.agents.open_session(a.agent_id, code)


def test_worker_environment_is_empty_and_server_secrets_are_not_inherited(prod, monkeypatch):
    monkeypatch.setenv("NAYMERA_SIGNING_KEY", "A" * 43)
    monkeypatch.setenv("NAYMERA_LLM_API_KEY", "sk-test-nothing")
    a = agent(prod)
    code = '''
import json, os
def main(c):
    fds = sorted(int(x) for x in os.listdir("/proc/self/fd"))
    print(json.dumps({"env": dict(os.environ), "cwd": os.getcwd(), "fds": fds}))
'''
    r = out(prod.stack.agents.run_session(a.agent_id, program(prod, "env.py", code), timeout_s=15))
    assert set(r["env"]) <= {"LC_CTYPE"} and "NAYMERA" not in json.dumps(r) and "sk-test" not in json.dumps(r) and "clickwin" not in r["cwd"]   # LC_CTYPE-i Python özü qoyur
    assert len([f for f in r["fds"] if f > 2]) <= 3                     # yalnız socket + (bağlı) boru/dir fd-ləri; server fd-ləri irsi verilməyib


# ----------------------------------------------------------------------------- OS səviyyəsi: uid endirmə, fayl sistemi, şəbəkə, proses yaddaşı (yalnız root)

DBTAMPER = '''
import json, os, sqlite3
def main(c):
    out = {}
    for path in c.args:
        res = {}
        try:
            with open(path, "rb") as f:
                res["read"] = len(f.read(16))
        except Exception as e:
            res["read"] = type(e).__name__
        try:
            con = sqlite3.connect(path, timeout=1)
            con.execute("UPDATE company_agents SET budget = 4.0")
            con.commit()
            res["sql_write"] = "OK"
        except Exception as e:
            res["sql_write"] = type(e).__name__
        out[os.path.basename(path)] = res
    print(json.dumps(out))
'''


def _dbs(p):
    return [str(x) for x in (p.svc.data_dir / "company.db", p.svc.data_dir / "factory.db", p.svc.data_dir / "auth.db", p.svc.data_dir / "ledger.db")]


@needs_root
def test_E1b_with_uid_drop_the_agent_cannot_read_or_tamper_with_server_databases(prod_drop):
    p = prod_drop
    a = agent(p)
    before = p.svc.company.view.get(a.name).budget
    res = p.stack.agents.run_session(a.agent_id, program(p, "db.py", DBTAMPER), args=_dbs(p), timeout_s=20)
    r = out(res)
    assert res.isolation["uid_unique"] is True and res.security_boundary is True
    for name, x in r.items():
        assert x["read"] == "PermissionError" and x["sql_write"] != "OK", (name, x)
    assert p.svc.company.view.get(a.name).budget == before
    assert oct(os.stat(p.svc.data_dir).st_mode & 0o777) == "0o700"


def test_same_uid_mode_has_NO_filesystem_isolation_documented_limitation(prod):
    """Dürüst məhdudiyyət: root olmayan (uid endirmə yox) rejimdə agent serverlə EYNİ istifadəçidir və DB fayllarını oxuya/yaza bilər.
    Bu rejim yalnız `allow_insecure_same_uid=True` ilə açıq şəkildə seçilir; istehsal defoltu belə sessiya açmır."""
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "db.py", DBTAMPER), args=[str(prod.svc.data_dir / "company.db")], timeout_s=20)
    r = out(res)
    assert res.isolation["uid_unique"] is False and res.isolation["filesystem_isolated"] is False and res.security_boundary is False
    assert r["company.db"]["sql_write"] == "OK"                 # sənədləşdirilmiş zəiflik: bu rejim sandbox SAYILMIR
    assert prod.svc.company.view.get(a.name).budget == 4.0


ESCALATE = '''
import json, os, socket
def main(c):
    out = {}
    try:
        os.setuid(0); out["setuid0"] = "SUCCEEDED"
    except Exception as e:
        out["setuid0"] = type(e).__name__
    out["uid"] = [os.getuid(), os.geteuid(), os.getgid()]
    out["groups"] = os.getgroups()
    try:
        with open("/proc/%s/mem" % c.args[0], "rb") as f:
            f.read(8)
        out["server_mem"] = "READ"
    except Exception as e:
        out["server_mem"] = type(e).__name__
    try:
        s = socket.create_connection(("127.0.0.1", int(c.args[1])), timeout=2)
        out["net"] = "CONNECTED"
    except Exception as e:
        out["net"] = type(e).__name__
    try:
        open("/etc/naymera_probe", "w").close(); out["write_etc"] = "WROTE"
    except Exception as e:
        out["write_etc"] = type(e).__name__
    out["isolation"] = c.isolation
    print(json.dumps(out))
'''


def _netns_possible() -> bool:
    """Bu mühitdə (root + CAP_SYS_ADMIN) şəbəkə namespace-i yaradıla bilirmi? (ayrıca prosesdə yoxlanır)"""
    import subprocess
    r = subprocess.run([sys.executable, "-c", "import os; os.unshare(os.CLONE_NEWNET)"], capture_output=True)
    return r.returncode == 0


@needs_root
def test_E1_os_level_hardening_with_unique_uid_and_server_verified_isolation(prod_drop):
    p = prod_drop
    a = agent(p)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        if _netns_possible():
            res = p.stack.agents.run_session(a.agent_id, program(p, "esc.py", ESCALATE), args=[str(os.getpid()), str(listener.getsockname()[1])], timeout_s=20)
            r = out(res)
            lo, hi = p.stack.agents._uid_pool
            assert lo <= r["uid"][0] < hi and r["uid"] == [r["uid"][0]] * 2 + [r["uid"][0]] and r["groups"] == [] and r["setuid0"] == "PermissionError"
            assert r["server_mem"] == "PermissionError" and r["write_etc"] == "PermissionError"
            assert p.stack.agents.server_dumpable_off is True
            iso = res.isolation
            assert res.security_boundary is True and res.killed == ""
            for k in ("uid_unique", "groups_empty", "caps_zero", "no_new_privs", "dumpable_off", "rlimits", "network_namespace", "seccomp_filter"):
                assert iso[k] is True, (k, iso)
            assert iso["filesystem_isolated"] is False and iso["pid_namespace"] is False          # dürüst: bunlar YOXDUR
            assert r["net"] != "CONNECTED"
        else:
            # mühit şəbəkə namespace-i yarada bilmir: təhlükəsizlik xassəsi təsdiqlənə bilmir -> sessiya QAPALI qalmalıdır (atlama YOX)
            res = p.stack.agents.run_session(a.agent_id, program(p, "esc.py", ESCALATE), args=["1", "1"], timeout_s=20)
            assert res.killed.startswith("isolation_unverified") and "network_namespace" in res.killed and res.stdout == "" and res.security_boundary is False
    finally:
        listener.close()


# ----------------------------------------------------------------------------- Keyring (B-1 hissə 2)

def test_keyring_has_no_public_mutators_and_control_is_single_use():
    ring = TrustedSigner("k-kr-001", bytes((i * 37 + 11) % 256 for i in range(32))).verifier()
    for name in ("add_public_key", "revoke_key", "revoke_credential", "freeze", "replace_key"):
        assert not hasattr(ring, name), name
    control = ring.take_control()
    with pytest.raises(CredentialError) as e:
        ring.take_control()
    assert e.value.code == "keyring_control_taken"
    assert control is not None and ring.frozen is False


def test_E1a_keyring_is_frozen_after_bootstrap_and_revocation_is_control_plane_only(prod):
    ring = prod.anchor.verifier
    assert ring.frozen is True and ring.key_ids() == ("k-iso-001",)
    # E1a: hücumçunun açıq açarını etibarlı etmək cəhdləri
    evil = TrustedSigner("evil-key-1", bytes((i * 53 + 7) % 256 for i in range(32)))
    with pytest.raises(AttributeError):
        ring.add_public_key("evil-key-1", evil.public_key)                   # ictimai mutator yoxdur
    with pytest.raises(CredentialError) as e:
        ring.take_control()                                                  # control artıq bootstrap-dadır
    assert e.value.code == "keyring_control_taken"
    with pytest.raises(CredentialError) as e:
        ring._add("evil-key-1", evil.public_key)                             # hətta daxili metod da dondurulmuş konfiqurasiyada rədd edir
    assert e.value.code == "keyring_frozen"
    forged = evil.owner_issuer(lambda r: ("owner", "dev"), ttl=600).from_step_up(object())
    with pytest.raises(Exception) as e2:                                     # hücumçu imzası etibarlı deyil: Company da, fabrik də rədd edir
        prod.stack.console.set_policy(forged, global_budget_cap=5.0)
    assert getattr(e2.value, "code", "") == "forged_principal"
    assert ring.key_ids() == ("k-iso-001",)
    # ləğv yalnız etibarlı control-plane (TrustAnchor) ilə
    ow = owner(prod)
    prod.stack.console.create_agent("programmer", ow)
    prod.anchor.revoke_credential(ow.nonce)
    with pytest.raises(Exception) as e3:
        prod.stack.console.create_agent("researcher", ow)
    assert getattr(e3.value, "code", "") == "credential_revoked"
    assert ring.frozen is True                                               # ləğv dondurulmanı pozmur


def test_keyring_frozen_state_is_irreversible_and_add_is_refused_for_any_key():
    signer = TrustedSigner("k-fz-001", bytes((i * 29 + 3) % 256 for i in range(32)))
    ring = signer.verifier()
    control = ring.take_control()
    other = TrustedSigner("k-fz-002", bytes((i * 31 + 5) % 256 for i in range(32)))
    control.add_public_key("k-fz-002", other.public_key)
    control.freeze()
    with pytest.raises(CredentialError):
        control.add_public_key("k-fz-003", other.public_key)
    assert not hasattr(control, "unfreeze") and not hasattr(ring, "unfreeze")
    control.revoke_key("k-fz-002")                                          # daralma yenə mümkündür
    with pytest.raises(CredentialError):
        ring.check_principal(other.system_issuer().principal(), "system")


# ----------------------------------------------------------------------------- B-2 / E2: paylama səthi

def test_E2_no_bound_factory_method_or_privileged_facet_is_distributed(prod):
    s = prod.stack
    assert not hasattr(s, "agent_handle") and "agent_handle" not in {f.name for f in dataclasses.fields(FactoryStack)}
    for f in dataclasses.fields(s):
        v = getattr(s, f.name)
        owner_obj = getattr(v, "__self__", None)
        assert not isinstance(owner_obj, (AgentFactory, FactoryAdmin, FactoryControl, FactoryRuntime)), f.name
        assert not isinstance(v, (AgentFactory, FactoryAdmin, FactoryControl, FactoryRuntime)), f.name
    console = s.console
    assert isinstance(console, OwnerConsole)
    for name in CONSOLE_OPS:
        bound = getattr(console, name)
        assert bound.__self__ is console                                    # yalnız OwnerConsole-un özünün metodu; arxa uc obyektinə istinad yoxdur
        fn = bound.__func__
        assert fn.__defaults__ is None and fn.__kwdefaults__ is None and fn.__closure__ is None
    assert not any(hasattr(console, n) for n in ("admin", "control", "runtime", "_admin", "factory", "core"))
    with pytest.raises(AttributeError):
        console.create_agent = lambda *a, **k: None
    # gateway-in verilmiş funksiyaları da bağlı AgentFactory metodu deyil
    gw = s.agents
    for fn in (gw._ports_for, gw._status_of):
        assert not isinstance(getattr(fn, "__self__", None), (AgentFactory, FactoryAdmin, FactoryControl, FactoryRuntime))


def test_in_process_agent_handle_is_NOT_a_boundary_positive_control(world):
    """Analiz alətinin dişi: eyni proses daxilində `factory.handle()` hələ də nüvəyə/DB bağlantılarına/reyestrə çatır (audit E1). Buna görə agent kodu bu
    obyektlə verilmir; yalnız ayrı proses + RPC (yuxarıdakı testlər) sərhəddir."""
    w = world
    a, _ = task(w)
    names = {type(o).__name__ for o in reach(w.factory.handle(a.agent_id))}
    assert {"_Core", "Connection", "Keyring", "Registry"} <= names
    # eyni zondu agent prosesində işlətmək (E1 testi) boş nəticə verir; fərq prosesdən gəlir, gizlətmədən deyil
