"""Agent izolyasiyası: audit H-1..H-4 və M-1..M-4 exploit-reqressiya testləri (PR #21).

Hər test audit ssenarisini (X1..X8) təkrarlayır və zəif (PR #20) icrada uğursuz olmalıdır. Real işçi proseslər istehsal bootstrap-ı ilə (`build_app(trust=...)`) açılır.
Root tələb edən testlər (unikal uid, çarpaz-siqnal, fork/setsid qaçışı, şəbəkə namespace-i) root olmayan mühitdə atlanır; bu halda həmin xassələr BU mühitdə TƏSDİQLƏNMİR
(CI root olmayan olduğundan onlar CI-da yoxlanmır). Root-da bir təhlükəsizlik yoxlaması uğursuz olarsa atlama YOXDUR: test uğursuz olur və ya sessiyanın qapalı qalması təsdiqlənir."""
import gc
import json
import os
import resource
import shutil
import socket
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from runtime import agent_gateway, agent_wire
from runtime.agent_gateway import AgentGateway, IsolationError
from runtime.factory_bootstrap import CONSOLE_OPS, OwnerConsole, _BACKENDS
from tests.test_agent_isolation import IS_ROOT, SAME_UID, agent, make_prod, needs_root, out, program, owner
from tests.test_security_boundary import reach


@pytest.fixture
def prod(tmp_path):
    p = make_prod(tmp_path, drop=False)           # root olmayan: same-uid (sərhəd DEYİL); root: bu fixture da same-uid seçir (uid_pool=None)
    yield p
    p.stack.agents.close()


@pytest.fixture
def prod_iso(tmp_path):
    p = make_prod(tmp_path, drop=True)            # istehsal defoltu: unikal uid + server-təsdiqli izolyasiya
    yield p
    p.stack.agents.close()


def drive(gw, sid, limit_s=30.0):
    """Sessiya bitənə qədər pump edir; ən uzun tək `pump()` müddətini qaytarır."""
    worst, end = 0.0, time.monotonic() + limit_s
    while not gw._sessions[sid].done:
        t = time.monotonic()
        gw.pump(0.05)
        worst = max(worst, time.monotonic() - t)
        assert time.monotonic() < end, "sessiya vaxtında bitmədi"
    return worst


def procs_with_uid(uid):
    """Gateway-dən MÜSTƏQİL /proc skanı."""
    found = []
    for n in os.listdir("/proc"):
        if n.isdigit():
            try:
                lines = open(f"/proc/{n}/status").read().splitlines()
                st = {k: v.strip() for k, _, v in (ln.partition(":") for ln in lines)}
                if st.get("Uid", "").split()[:1] == [str(uid)] and not st.get("State", "").startswith("Z"):      # zombi = ölü
                    found.append(int(n))
            except OSError:
                pass
    return found


# ======================================================================== H-1: JSON dərinlik/rekursiya DoS

BOMB = '''
import struct, agent_wire
def main(c):
    body = b"[" * 30000 + b"]" * 30000
    seq = c._w._seq + 1
    mac = agent_wire._mac(c._w._key, agent_wire.C2S, seq, body)
    c._sock.sendall(struct.pack(">I", len(mac) + len(body)) + mac + body)
    import time; time.sleep(3)
'''


def test_H1_json_depth_bomb_with_a_valid_mac_ends_the_session_and_never_crashes_the_pump(prod):
    a = agent(prod)
    gw = prod.stack.agents
    sid = gw.open_session(a.agent_id, program(prod, "bomb.py", BOMB), timeout_s=10)
    drive(gw, sid, 15)                                   # pump() İSTİSNA ATMAMALIDIR (zəif icrada RecursionError)
    res = gw.result(sid)
    assert res.killed == "wire:too_deep", res
    ok = gw.run_session(a.agent_id, program(prod, "ok.py", "def main(c):\n    c.info()\n"), timeout_s=10)   # server sağdır
    assert ok.exit_code == 0 and ok.killed == ""


@pytest.mark.parametrize("body,code", [(b"[" * 7 + b"]" * 7, "too_deep"), (b'{"a":' * 7 + b"1" + b"}" * 7, "too_deep"), (b"]]", "bad_json"), (b'{"a":1}}', "bad_json"),
                                       (b"[" * 100000, "bad_length"), (b'{"a": "' + b"x" * 10 + b"\\", "bad_json")])
def test_H1_codec_enforces_depth_and_structure_before_parsing(body, code):
    key = os.urandom(32)
    mac = agent_wire._mac(key, agent_wire.C2S, 1, body)
    with pytest.raises(agent_wire.WireError) as e:
        agent_wire.Reader(key, agent_wire.C2S).feed(struct.pack(">I", len(mac) + len(body)) + mac + body)
    assert e.value.code in (code, "bad_json")


def test_H1_codec_accepts_legitimate_nesting_and_ignores_brackets_inside_strings():
    key = os.urandom(32)
    w, r = agent_wire.Writer(key, agent_wire.C2S), agent_wire.Reader(key, agent_wire.C2S)
    msg = {"id": 1, "op": "handoff", "args": {"payload": {"k": "[[[[[[[[[[ {{{{{{{{{{ \\\" ]]]]"}}}
    assert r.feed(w.pack(msg)) == [msg]
    assert r.feed(w.pack({"a": [[[[[1]]]]]})) == [{"a": [[[[[1]]]]]}]          # 6 səviyyə həddindədir


def test_H1_unexpected_parser_or_dispatch_failures_end_only_that_session(prod, monkeypatch):
    a = agent(prod)
    gw = prod.stack.agents
    monkeypatch.setattr(agent_wire.Reader, "feed", lambda self, data: (_ for _ in ()).throw(RuntimeError("boom")))
    sid = gw.open_session(a.agent_id, program(prod, "ok.py", "def main(c):\n    c.info()\n"), timeout_s=10)
    drive(gw, sid, 15)
    assert gw.result(sid).killed == "wire:internal"
    monkeypatch.undo()
    monkeypatch.setattr(AgentGateway, "_dispatch", lambda self, s, m: (_ for _ in ()).throw(RuntimeError("boom")))
    sid = gw.open_session(a.agent_id, program(prod, "ok2.py", "def main(c):\n    c.info()\n"), timeout_s=10)
    drive(gw, sid, 15)
    assert gw.result(sid).killed == "internal_error"


# ======================================================================== H-2: bloklanma / ödəniş müddəti / təmizlik

HOLD = '''
import time
def main(c):
    for i in range(190):
        c._sock.sendall(c._w.pack({"id": 1000 + i, "op": "info", "args": {}}))
    time.sleep(15)
'''


def test_H2_unread_replies_cannot_stall_the_gateway_or_outlive_the_deadline(prod):
    a = agent(prod)
    gw = prod.stack.agents
    sid = gw.open_session(a.agent_id, program(prod, "hold.py", HOLD), timeout_s=3.0)
    t0 = time.monotonic()
    worst = drive(gw, sid, 12)
    res = gw.result(sid)
    assert worst < 1.0, worst                                   # zəif icrada tək pump() 15 s bloklanırdı (ödəniş müddəti 3 s)
    assert time.monotonic() - t0 < 8.0
    assert res.killed in ("backpressure", "timeout"), res.killed


SLOW = '''
import time
def main(c):
    c._sock.close()
    time.sleep(30)
'''


def test_H2_agent_closing_its_socket_cannot_block_the_pump(prod):
    a = agent(prod)
    gw = prod.stack.agents
    sid = gw.open_session(a.agent_id, program(prod, "slow.py", SLOW), timeout_s=40)
    t0 = time.monotonic()
    worst = drive(gw, sid, 15)
    assert worst < 1.0, worst                                   # zəif icrada _finalize 5 s bloklayırdı
    assert gw.result(sid).killed == "socket_closed" and time.monotonic() - t0 < 6.0


def test_H2_concurrent_stalling_agents_do_not_delay_a_healthy_session(prod):
    a, b = agent(prod), agent(prod, "researcher")
    gw = prod.stack.agents
    s1 = gw.open_session(a.agent_id, program(prod, "hold.py", HOLD), timeout_s=5.0)
    s2 = gw.open_session(b.agent_id, program(prod, "ok.py", "def main(c):\n    for _ in range(20):\n        c.info()\n"), timeout_s=20)
    t0 = time.monotonic()
    worst = 0.0
    while not gw._sessions[s2].done:
        t = time.monotonic()
        gw.pump(0.05)
        worst = max(worst, time.monotonic() - t)
        assert time.monotonic() - t0 < 10
    assert gw.result(s2).exit_code == 0 and gw.result(s2).calls.get("info") == 20 and worst < 1.0
    drive(gw, s1, 12)


def test_H2_session_dirs_and_processes_are_cleaned_up_and_stale_bases_are_swept(prod, tmp_path):
    a = agent(prod)
    gw = prod.stack.agents
    gw.run_session(a.agent_id, program(prod, "ok.py", "def main(c):\n    pass\n"), timeout_s=10)
    assert list(gw._base.iterdir()) == []                       # sessiya qovluğu silinib
    stale = Path(os.path.join(os.path.dirname(str(gw._base)), "naymera-agents-999999-stale"))
    stale.mkdir()
    (stale / "x").write_text("x")
    AgentGateway(lambda i: None, lambda i: "active", program_root=tmp_path, harden_server=False, uid_pool=None).close()
    assert not stale.exists()                                   # ölü server prosesindən qalan baza süpürülüb
    live = Path(os.path.join(os.path.dirname(str(gw._base)), f"naymera-agents-{os.getppid()}-live"))
    live.mkdir()
    AgentGateway(lambda i: None, lambda i: "active", program_root=tmp_path, harden_server=False, uid_pool=None).close()
    assert live.exists()                                        # yaşayan serverin qovluğuna toxunulmur
    shutil.rmtree(live)


# ======================================================================== H-3: şəbəkə izolyasiyası (server tərəfdən yoxlama, qapalı qalma)

GW_SCRIPT = '''
import json, os, socket, sys
sys.path.insert(0, {repo!r})
from runtime.agent_gateway import AgentGateway
root = {root!r}
os.makedirs(root, exist_ok=True)
open(os.path.join(root, "net.py"), "w").write(""" 
import json, socket
def main(c):
    try:
        socket.create_connection(("127.0.0.1", int(c.args[0])), timeout=2); r = "CONNECTED"
    except Exception as e:
        r = type(e).__name__
    print(json.dumps(r))
""")
lst = socket.socket(); lst.bind(("127.0.0.1", 0)); lst.listen(4)
kw = {kw}
gw = AgentGateway(lambda i: None, lambda i: "active", program_root=root, **kw)
res = gw.run_session("agt_x", "net.py", args=[str(lst.getsockname()[1])], timeout_s=15)
print("RESULT", json.dumps({{"stdout": res.stdout.strip(), "killed": res.killed, "boundary": getattr(res, "security_boundary", None)}}))
gw.close()
'''


def run_gw_script(tmp_path, kw="{}", prefix=()):
    script = tmp_path / "gw_script.py"
    script.write_text(GW_SCRIPT.format(repo=str(Path(__file__).resolve().parent.parent), root=str(tmp_path / "progs"), kw=kw))
    r = subprocess.run([*prefix, sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=120)
    line = [x for x in r.stdout.splitlines() if x.startswith("RESULT ")]
    assert line, (r.stdout, r.stderr)
    return json.loads(line[-1][7:])


needs_setpriv = pytest.mark.skipif(shutil.which("setpriv") is None, reason="setpriv yoxdur: CAP_SYS_ADMIN-siz mühiti simulyasiya etmək mümkün deyil (xassə BU mühitdə təsdiqlənmir)")


@needs_root
@needs_setpriv
def test_H3_without_cap_sys_admin_the_session_is_refused_not_opened_with_open_network(tmp_path):
    """Audit X8: CAP_SYS_ADMIN olmayan root server (Docker defoltu) -> zəif icra sessiyanı AÇIB agentə tam şəbəkə verirdi."""
    res = run_gw_script(tmp_path, prefix=("setpriv", "--bounding-set=-sys_admin"))
    assert res["stdout"] == "" and "CONNECTED" not in json.dumps(res)             # agent kodu HEÇ VAXT işləmədi
    assert res["killed"].startswith(("isolation_unverified", "no_hello")) and res["boundary"] is False        # agent kodu başlamadı (işçi də, server də qapadı)


@needs_root
def test_H3_with_the_capability_the_namespace_is_verified_and_the_network_is_closed(tmp_path):
    res = run_gw_script(tmp_path)
    assert res["stdout"] != '"CONNECTED"' and res["killed"] == "" and res["boundary"] is True


LIAR = '''#!/bin/sh
# Yalan danışan işçi: server-tərəfli yoxlamanın işçinin hello bayraqlarına DEYİL /proc-a baxdığını göstərir.
shift 3
exec {real} -I -S -B -c 'import sys
src = open(sys.argv[1]).read()
ns = {{"__name__": "lib", "__file__": sys.argv[1]}}
exec(compile(src, sys.argv[1], "exec"), ns)
{patch}
sys.argv = sys.argv[1:]
raise SystemExit(ns["main"](sys.argv))' "$@"
'''


def liar_gateway(tmp_path, patch, **kw):
    py = tmp_path / "liar_python.sh"
    py.write_text(LIAR.format(real=sys.executable, patch=patch))
    py.chmod(0o755)
    progs = tmp_path / "liar_progs"
    progs.mkdir(exist_ok=True)
    (progs / "p.py").write_text("import json\ndef main(c):\n    print(json.dumps({'ran': True, 'claimed': c.isolation}))\n")
    gw = AgentGateway(lambda i: None, lambda i: "active", program_root=progs, python=str(py), harden_server=False, **kw)
    return gw


@needs_root
def test_M2_a_lying_worker_cannot_fake_the_network_namespace(tmp_path):
    gw = liar_gateway(tmp_path, 'import os\nos.unshare = lambda *a, **k: None')       # unshare "uğurlu" görünür, amma namespace YARANMIR
    res = gw.run_session("agt_x", "p.py", timeout_s=15)
    assert res.reported.get("network_namespace") is True                              # işçi yalan danışdı
    assert res.isolation["network_namespace"] is False                                # server /proc ilə gördü
    assert res.killed.startswith("isolation_unverified") and "network_namespace" in res.killed and res.stdout == ""
    gw.close()


@needs_root
def test_M2_a_lying_worker_cannot_fake_seccomp_or_other_flags(tmp_path):
    gw = liar_gateway(tmp_path, 'ns["_install_seccomp"] = lambda: True\nns["_prctl"] = lambda *a: True')
    res = gw.run_session("agt_x", "p.py", timeout_s=15)
    assert res.reported.get("seccomp") is True and res.reported.get("no_new_privs") is True and res.reported.get("dumpable_off") is True
    assert res.isolation["seccomp_filter"] is False and res.isolation["no_new_privs"] is False
    assert res.killed.startswith("isolation_unverified") and res.stdout == "" and res.security_boundary is False
    gw.close()


@needs_root
def test_M2_rlimit_tampering_is_detected_by_the_server(tmp_path):
    gw = liar_gateway(tmp_path, 'ns["_harden_orig"] = ns["_harden"]\nns["_harden"] = lambda o: ns["_harden_orig"]({**o, "limits": {"nproc": 100000}})')
    res = gw.run_session("agt_x", "p.py", timeout_s=15)
    assert res.isolation["rlimits"] is False and res.killed.startswith("isolation_unverified") and "rlimits" in res.killed
    gw.close()


# ======================================================================== H-4: unikal uid, çarpaz-siqnal, fork/setsid qaçışı, seccomp, resurs limitləri

VICTIM = '''
import time
def main(c):
    for _ in range(80):
        c.info(); time.sleep(0.05)
'''
KILLER = '''
import json, os, signal
def main(c):
    me, res = os.getpid(), {}
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) != me:
            try:
                cmd = open(f"/proc/{d}/cmdline", "rb").read()
            except Exception:
                continue
            if b"agent_worker.py" in cmd:
                try:
                    os.kill(int(d), signal.SIGKILL); res[d] = "KILLED"
                except Exception as e:
                    res[d] = type(e).__name__
    print(json.dumps(res))
'''


@needs_root
def test_H4_each_session_gets_its_own_uid_and_cannot_signal_other_sessions(prod_iso):
    p = prod_iso
    a, b = agent(p), agent(p, "researcher")
    gw = p.stack.agents
    v = gw.open_session(a.agent_id, program(p, "victim.py", VICTIM), timeout_s=30)
    for _ in range(20):
        gw.pump(0.05)
    k = gw.open_session(b.agent_id, program(p, "killer.py", KILLER), timeout_s=30)
    assert gw._sessions[v].uid != gw._sessions[k].uid and None not in (gw._sessions[v].uid, gw._sessions[k].uid)
    while not (gw._sessions[v].done and gw._sessions[k].done):
        gw.pump(0.05)
    rv, rk = gw.result(v), gw.result(k)
    attempts = json.loads(rk.stdout.strip().splitlines()[-1])
    assert attempts and set(attempts.values()) == {"PermissionError"}, attempts      # zəif icrada "KILLED"
    assert rv.exit_code == 0 and rv.killed == "" and rv.calls.get("info") == 80       # qurban ölmədi
    assert rv.security_boundary and rk.security_boundary


FORKER = '''
import json, os, time
def main(c):
    pids, err = [], None
    for i in range(100):
        try:
            pid = os.fork()
        except OSError as e:
            err = e.errno
            break
        if pid == 0:
            os.setsid()
            time.sleep(60)
            os._exit(0)
        pids.append(pid)
    print(json.dumps({"forked": len(pids), "err": err, "pids": pids}))
'''


@needs_root
def test_H4_fork_and_setsid_children_cannot_survive_the_session_and_forks_are_capped(prod_iso):
    p = prod_iso
    a = agent(p)
    gw = p.stack.agents
    sid = gw.open_session(a.agent_id, program(p, "forker.py", FORKER), timeout_s=20)
    uid = gw._sessions[sid].uid
    worst = drive(gw, sid, 25)
    res = gw.result(sid)
    r = json.loads(res.stdout.strip().splitlines()[-1])
    assert r["forked"] < 32 and r["err"] == 11, r                                      # RLIMIT_NPROC: zəif icrada 100 uşaq açılırdı
    assert res.survivors_killed >= 1 and worst < 1.0
    time.sleep(0.3)
    assert procs_with_uid(uid) == [], procs_with_uid(uid)                            # setsid uşaqları sessiya bitəndə ÖLDÜRÜLÜB
    def alive(pid):
        try:
            return not open(f"/proc/{pid}/status").read().split("State:")[1].lstrip().startswith("Z")
        except OSError:
            return False
    assert not any(alive(pid) for pid in r["pids"])                                   # ölüdürlər (zombi = ölü; konteyner init-i götürməyə bilər)
    assert res.killed == "" and res.exit_code == 0                                    # işçi təbii çıxdı; uşaqlar uid süpürgəsi ilə öldürüldü
    assert uid not in gw._quarantine and uid not in gw._used_uids                    # uid təmiz buraxıldı


@needs_root
def test_H4_a_survivor_blocks_uid_reuse_instead_of_leaking_into_the_next_session(prod_iso):
    gw = prod_iso.stack.agents
    a = agent(prod_iso)
    seen = set()
    for _ in range(3):
        res = gw.run_session(a.agent_id, program(prod_iso, "forker.py", FORKER), timeout_s=20)
        seen.add(res.survivors_killed >= 1)
    assert seen == {True}


PROBE_FS = '''
import json, os
def main(c):
    work = os.getcwd()
    base = os.path.dirname(os.path.dirname(work))
    r = {}
    for name, path in (("list_base", base), ("other_session", c.args[0]), ("other_work", os.path.join(c.args[0], "work"))):
        try:
            r[name] = sorted(os.listdir(path))
        except Exception as e:
            r[name] = type(e).__name__
    try:
        open(os.path.join(c.args[0], "program.py")).read(); r["other_program"] = "READABLE"
    except Exception as e:
        r["other_program"] = type(e).__name__
    print(json.dumps(r))
'''


@needs_root
def test_H4_sessions_cannot_list_or_read_each_others_directories(prod_iso):
    p = prod_iso
    a, b = agent(p), agent(p, "researcher")
    gw = p.stack.agents
    v = gw.open_session(a.agent_id, program(p, "victim.py", VICTIM), timeout_s=30)
    for _ in range(10):
        gw.pump(0.05)
    other_dir = str(gw._sessions[v].tmp)
    res = gw.run_session(b.agent_id, program(p, "fs.py", PROBE_FS), args=[other_dir], timeout_s=20)
    r = json.loads(res.stdout.strip().splitlines()[-1])
    assert r == {"list_base": "PermissionError", "other_session": "PermissionError", "other_work": "PermissionError", "other_program": "PermissionError"}, r
    drive(gw, v, 30)


SECCOMP = '''
import ctypes, json, os, threading
libc = ctypes.CDLL(None, use_errno=True)
def sc(nr, *a):
    r = libc.syscall(nr, *[ctypes.c_long(x) for x in a])
    return ctypes.get_errno() if r == -1 else 0
def main(c):
    out = {"ptrace": sc(101, 0, 0, 0, 0), "mount": sc(165, 0, 0, 0, 0, 0), "setns": sc(308, -1, 0), "bpf": sc(321, 0, 0, 0), "io_uring_setup": sc(425, 1, 0),
           "clone_newuser": sc(56, 0x10000000 | 17, 0, 0, 0, 0), "clone_newnet": sc(56, 0x40000000 | 17, 0, 0, 0, 0)}
    try:
        os.unshare(os.CLONE_NEWUSER); out["unshare_user"] = 0
    except OSError as e:
        out["unshare_user"] = e.errno
    try:
        os.unshare(os.CLONE_NEWNET); out["unshare_net"] = 0
    except OSError as e:
        out["unshare_net"] = e.errno
    ok = {}
    f = os.path.join(os.getcwd(), "f.txt")
    open(f, "w").write("x"); ok["file"] = open(f).read() == "x"
    t = []; th = threading.Thread(target=lambda: t.append(1)); th.start(); th.join(); ok["thread"] = t == [1]
    ok["info"] = c.info()["agent_id"].startswith("agt_")
    out["ok"] = ok
    print(json.dumps(out))
'''


def test_H4_seccomp_blocks_namespace_ptrace_mount_and_friends_but_normal_work_runs(prod):
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "sc.py", SECCOMP), timeout_s=20)
    assert res.exit_code == 0 and res.killed == "", res
    r = out(res)
    EPERM = 1
    assert {k: r[k] for k in ("ptrace", "mount", "setns", "bpf", "io_uring_setup", "clone_newuser", "clone_newnet", "unshare_user", "unshare_net")} == {
        k: EPERM for k in ("ptrace", "mount", "setns", "bpf", "io_uring_setup", "clone_newuser", "clone_newnet", "unshare_user", "unshare_net")}
    assert r["ok"] == {"file": True, "thread": True, "info": True}
    assert res.isolation["seccomp_filter"] is True and res.isolation["no_new_privs"] is True        # server /proc ilə təsdiqlədi


LIMITS_PROBE = '''
import json, resource
def main(c):
    print(json.dumps({n: resource.getrlimit(getattr(resource, n)) for n in ("RLIMIT_CORE", "RLIMIT_NOFILE", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_AS", "RLIMIT_NPROC")}))
'''


def test_H4_resource_limits_are_enforced_and_verified_by_the_server_same_uid_mode(prod):
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "lim.py", LIMITS_PROBE), timeout_s=15)
    r = out(res)
    assert r["RLIMIT_CORE"] == [0, 0] and r["RLIMIT_NOFILE"] == [64, 64] and r["RLIMIT_CPU"] == [60, 60] and r["RLIMIT_FSIZE"] == [10 * 1024 * 1024] * 2
    assert r["RLIMIT_NPROC"][0] != 32                              # same-uid rejimində NPROC qoyulmur (bütün istifadəçini sayardı)
    assert res.isolation["rlimits"] is True


@needs_root
def test_H4_resource_limits_including_nproc_are_enforced_and_verified_with_unique_uid(prod_iso):
    a = agent(prod_iso)
    res = prod_iso.stack.agents.run_session(a.agent_id, program(prod_iso, "lim.py", LIMITS_PROBE), timeout_s=15)
    r = out(res)
    assert r["RLIMIT_CORE"] == [0, 0] and r["RLIMIT_NOFILE"] == [64, 64] and r["RLIMIT_CPU"] == [60, 60] and r["RLIMIT_NPROC"] == [32, 32]
    assert res.isolation["rlimits"] is True and res.security_boundary is True


# ======================================================================== M-1: OwnerConsole

def test_M1_owner_console_exposes_no_privileged_object_through_defaults_closures_or_attributes(prod):
    console = prod.stack.console
    found = sorted({type(o).__name__ for o in reach(console)})
    for bad in ("FactoryAdmin", "FactoryControl", "FactoryRuntime", "AgentFactory", "_Core", "_Privileged", "CompanyAdmin", "Company", "Connection", "Registry",
                "Keyring", "TrustedSigner", "DispatcherIssuer"):
        assert bad not in found, (bad, found)
    for name in CONSOLE_OPS:
        fn = getattr(OwnerConsole, name)
        assert fn.__defaults__ is None and fn.__kwdefaults__ is None and fn.__closure__ is None, name
        bound = getattr(console, name)
        assert bound.__self__ is console
    assert {s for s in OwnerConsole.__slots__} == {"_token", "__weakref__"}
    assert isinstance(console._token, str) and len(console._token) == 32


def test_M1_callers_cannot_supply_their_own_callable(prod):
    console = prod.stack.console
    called = []
    with pytest.raises(TypeError):
        console.create_agent("programmer", None, _fn=lambda *a, **k: called.append(1))          # zəif icrada "overridden" qaytarırdı
    with pytest.raises(AttributeError):
        console._call("__class__", (), {})
    with pytest.raises(AttributeError):
        console._call("not_an_op", (), {})
    assert called == []


def test_M1_console_backend_registry_entry_is_dropped_when_the_console_is_collected():
    class Dummy:
        def spawn(self, *a, **k):
            return "ok"
    c = OwnerConsole(Dummy())
    token = c._token
    assert c.spawn() == "ok" and token in _BACKENDS
    del c
    gc.collect()
    assert token not in _BACKENDS


# ======================================================================== M-3: select() FD_SETSIZE

def test_M3_gateway_works_when_the_server_has_more_than_1024_open_fds(prod):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 1400:
        pytest.skip(f"RLIMIT_NOFILE hard={hard} < 1400: yüksək fd ssenarisi BU mühitdə yoxlana bilmir")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1500), hard))
    a = agent(prod)
    hold = []
    try:
        for _ in range(1100):
            hold.append(os.open("/dev/null", os.O_RDONLY))
        res = prod.stack.agents.run_session(a.agent_id, program(prod, "ok.py", "def main(c):\n    c.info()\n"), timeout_s=15)
        assert res.exit_code == 0 and res.killed == "" and res.calls.get("info") == 1          # zəif icrada ValueError: filedescriptor out of range in select()
    finally:
        for fd in hold:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# ======================================================================== M-4: same-uid rejimi sərhəd DEYİL və defoltda bağlıdır

def test_M4_same_uid_mode_is_never_the_default_and_reports_no_security_boundary(prod, tmp_path):
    import inspect
    sig = inspect.signature(AgentGateway.__init__)
    assert sig.parameters["allow_insecure_same_uid"].default is False
    gw = AgentGateway(lambda i: None, lambda i: "active", program_root=tmp_path, uid_pool=None, harden_server=False)
    (tmp_path / "x.py").write_text("def main(c):\n    pass\n")
    with pytest.raises(IsolationError) as e:
        gw.open_session("agt_x", "x.py")
    assert "TƏHLÜKƏSİZLİK SƏRHƏDİ DEYİL" in str(e.value)
    gw.close()
    assert prod.stack.agents._insecure is True and prod.stack.agents._uid_pool is None      # bu fixture açıq seçib
    a = agent(prod)
    res = prod.stack.agents.run_session(a.agent_id, program(prod, "ok.py", "def main(c):\n    pass\n"), timeout_s=10)
    assert res.security_boundary is False and res.isolation["uid_unique"] is False and res.isolation["filesystem_isolated"] is False


def test_M4_production_bootstrap_defaults_to_the_isolated_mode_only(tmp_path):
    p = make_prod(tmp_path, drop=True)
    gw = p.stack.agents
    assert gw._insecure is False and gw._uid_pool == agent_gateway.DEFAULT_UID_POOL and gw._req_net is True and gw._req_seccomp is True
    a = agent(p)
    code = program(p, "d.py", "def main(c):\n    pass\n")
    if IS_ROOT:
        res = gw.run_session(a.agent_id, code, timeout_s=15)
        assert res.security_boundary is True or res.killed.startswith("isolation_unverified")   # root: ya təsdiqlənmiş sərhəd, ya qapalı
    else:
        with pytest.raises(IsolationError):
            gw.open_session(a.agent_id, code)
    gw.close()


def test_M4_documentation_states_same_uid_mode_is_not_a_boundary():
    root = Path(__file__).resolve().parent.parent
    doc = (root / "AGENT_FACTORY.md").read_text(encoding="utf-8")
    assert "allow_insecure_same_uid" in doc and "TƏHLÜKƏSİZLİK SƏRHƏDİ DEYİL" in doc
    assert "allow_insecure_same_uid" in (root / "runtime" / "agent_gateway.py").read_text(encoding="utf-8")
