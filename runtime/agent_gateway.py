"""Agent izolyasiya sərhədi: agent kodu AYRI OS prosesində işləyir; etibarlı server ilə yalnız minimal, autentifikasiyalı RPC danışır (audit B-1/B-2).

Nə var:
- `AgentGateway` (etibarlı server tərəfi): işçi prosesi (`runtime/agent_worker.py`) boş mühit/boş iş qovluğu/irsi iki fd ilə başladır; (root isə) uid/gid endirir
  (`require_os_drop=True` olduqda endirmə mümkün deyilsə sessiyanı AÇMIR: qapalı qalır), şəbəkə namespace-i cəhd edir, NO_NEW_PRIVS/DUMPABLE=0/rlimit tətbiq edir;
  serverin özü `PR_SET_DUMPABLE=0` edir (eyni-uid proseslərin `/proc/<pid>/mem`/ptrace ilə oxumasına qarşı).
- RPC: yalnız `hello`, `info`, `request_permission`, `handoff`. Agent id sessiyaya SERVER tərəfindən bağlanır (müştəri göndərə bilmir). Hər sorğu: çərçivə HMAC + sıra nömrəsi,
  əməliyyat allowlist-i, ciddi arqument sxemi, agent hələ `active`-dirmi, sorğu limiti. Pozuntu (naməlum əməliyyat, pis çərçivə, limit) -> sessiya öldürülür.
- Nəticə yalnız JSON proyeksiyadır; heç bir obyekt/istinad (fabrik, Company, Keyring, DB bağlantısı, imza obyekti) sərhədi keçmir.

Nə YOXDUR (dürüst məhdudiyyətlər): tam OS sandbox DEYİL. Fayl sistemi qoruması yalnız uid endirmə + serverin 0700/0600 icazələri qədərdir; sistem çağırışı filtri (seccomp), mount/pid
namespace, cgroup yoxdur (operator konteyner/bwrap ilə əlavə etməlidir). `drop` olmadan (root olmayan mühit) agent EYNİ istifadəçidir və DB fayllarını oxuya/yaza bilər.
Server bir-iplikli sorğu emalı edir (`pump()` DB bağlantılarını yaradan iplikdən çağırılmalıdır).
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from runtime import agent_wire
from agents.agent_factory import FactoryDenied

ALLOWED_OPS = ("hello", "info", "request_permission", "handoff")
_PROGRAM_RE = re.compile(r"^[A-Za-z0-9_\-]{1,60}\.py$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_PERM_RE = re.compile(r"^[a-z_]{1,40}$")
_WORKER = Path(__file__).resolve().parent / "agent_worker.py"
_WIRE = Path(__file__).resolve().parent / "agent_wire.py"
_OUT_CAP = 16 * 1024
NOBODY = (65534, 65534)


class IsolationError(RuntimeError):
    """Tələb olunan izolyasiya təmin edilə bilmir (qapalı qalır) və ya sessiya parametrləri yanlışdır."""


class _BadArgs(ValueError):
    pass


@dataclass(frozen=True)
class SessionResult:
    agent_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    calls: dict
    denied: tuple
    violations: int
    killed: str
    isolation: dict


@dataclass
class _Session:
    session_id: str
    agent_id: str
    proc: subprocess.Popen
    sock: socket.socket
    reader: agent_wire.Reader
    writer: agent_wire.Writer
    tmp: Path
    out: Any
    err: Any
    deadline: float
    expect_uid: int | None
    hello: bool = False
    requests: int = 0
    violations: int = 0
    calls: dict = field(default_factory=dict)
    denied: list = field(default_factory=list)
    killed: str = ""
    isolation: dict = field(default_factory=dict)
    done: bool = False
    result: SessionResult | None = None


def harden_server_process() -> bool:
    """Serveri `PR_SET_DUMPABLE=0` edir: eyni-uid (root olmayan) proseslər `/proc/<pid>/mem`-i oxuya və ptrace edə bilmir. Linux-a xasdır, best-effort."""
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001
        return False


def _text(args: dict, key: str, max_len: int, *, regex: re.Pattern | None = None, optional: bool = False, default: str = "") -> str:
    if optional and args.get(key) in (None, ""):
        return default
    v = args.get(key)
    if not isinstance(v, str) or not v or len(v) > max_len or (regex is not None and not regex.match(v)):
        raise _BadArgs(key)
    return v


def _number(args: dict, key: str) -> float:
    v = args.get(key, 0.0)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > 1e6:
        raise _BadArgs(key)
    return float(v)


def _only(args: dict, allowed: set[str]) -> None:
    if set(args) - allowed:
        raise _BadArgs("extra")


def _op_info(gw: "AgentGateway", s: _Session, args: dict) -> dict:
    _only(args, set())
    a = gw._ports_for(s.agent_id).info()
    return {"agent_id": a.agent_id, "name": a.name, "role_id": a.role_id, "title": a.title, "department": a.department, "status": a.status,
            "permissions": sorted(a.permissions)}


def _op_request_permission(gw: "AgentGateway", s: _Session, args: dict) -> dict:
    _only(args, {"permission", "reason"})
    permission, reason = _text(args, "permission", 40, regex=_PERM_RE), _text(args, "reason", 300)
    r = gw._ports_for(s.agent_id).request_permission(permission, reason)
    return {"request_id": r.request_id, "permission": r.permission, "state": r.state}


def _op_handoff(gw: "AgentGateway", s: _Session, args: dict) -> dict:
    _only(args, {"to_agent_id", "title", "payload", "permission", "cost", "op_id", "parent_task_id"})
    to = _text(args, "to_agent_id", 80, regex=_ID_RE)
    title = _text(args, "title", 120)
    permission = _text(args, "permission", 40, regex=_PERM_RE, optional=True, default="read_files")
    op_id = _text(args, "op_id", 80, regex=_ID_RE, optional=True, default="") or None
    parent = _text(args, "parent_task_id", 80, regex=_ID_RE, optional=True, default="")
    payload = args.get("payload")
    if not isinstance(payload, dict) or not payload or len(payload) > 20:
        raise _BadArgs("payload")
    clean: dict[str, Any] = {}
    for k, v in payload.items():
        if not isinstance(k, str) or not 0 < len(k) <= 40 or isinstance(v, (dict, list)) or v is None or (isinstance(v, str) and len(v) > 2000):
            raise _BadArgs("payload")
        if isinstance(v, float) and not math.isfinite(v):
            raise _BadArgs("payload")
        clean[k] = v
    t = gw._ports_for(s.agent_id).handoff(to, title, clean, permission=permission, cost=_number(args, "cost"), op_id=op_id, parent_task_id=parent)
    return {"task_id": t.task_id, "agent_id": t.agent_id, "state": t.state}


_HANDLERS: dict[str, Callable[["AgentGateway", _Session, dict], dict]] = {"info": _op_info, "request_permission": _op_request_permission, "handoff": _op_handoff}


class AgentGateway:
    def __init__(self, ports_for: Callable[[str], Any], status_of: Callable[[str], str | None], *, program_root: str | Path,
                 python: str | None = None, drop_to: tuple[int, int] | None = NOBODY, require_os_drop: bool = True, net_isolate: bool = True,
                 harden_server: bool = True, limits: dict | None = None, max_sessions: int = 4, max_requests: int = 200, max_violations: int = 3,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """`ports_for(agent_id)` ve `status_of(agent_id)` etibarlı bootstrap-ın verdiyi funksiyalardır (bağlı metod DEYİL: fabrikə istinad sərhədi keçmir).
        `require_os_drop=True` (defolt): uid endirmə mümkün deyilsə (root olmayan server) HEÇ BİR sessiya açılmır."""
        self._ports_for, self._status_of = ports_for, status_of
        self._root = Path(program_root).resolve()
        self._python = python or sys.executable
        self._drop_to, self._require_drop, self._net_isolate = drop_to, bool(require_os_drop), bool(net_isolate)
        self._limits = dict(limits or {})
        self._max_sessions, self._max_requests, self._max_violations, self._clock = max_sessions, max_requests, max_violations, clock
        self._sessions: dict[str, _Session] = {}
        self.server_dumpable_off = harden_server_process() if harden_server else False

    # --- yaşam dövrü ---
    def _can_drop(self) -> bool:
        return self._drop_to is not None and hasattr(os, "geteuid") and os.geteuid() == 0

    def open_session(self, agent_id: str, program: str, *, args: Sequence[str] = (), timeout_s: float = 30.0) -> str:
        if not isinstance(agent_id, str) or self._status_of(agent_id) != "active":
            raise IsolationError("agent aktiv deyil və ya tapılmadı")
        if not isinstance(program, str) or not _PROGRAM_RE.match(program):
            raise IsolationError("proqram adı yanlışdır")
        src = (self._root / program)
        if src.is_symlink() or not src.is_file() or src.resolve().parent != self._root:
            raise IsolationError("proqram program_root daxilində adi fayl olmalıdır")
        if len([x for x in self._sessions.values() if not x.done]) >= self._max_sessions:
            raise IsolationError("açıq sessiya limiti")
        drop = self._can_drop()
        if self._require_drop and not drop:
            raise IsolationError("uid endirmə mümkün deyil (server root deyil və ya drop_to yoxdur): izolyasiya təmin edilmədiyi üçün sessiya açılmır")
        tmp = Path(tempfile.mkdtemp(prefix="naymera-agent-"))
        sock_p = sock_c = None
        out = err = None
        try:
            os.chmod(tmp, 0o755)
            for p in (_WORKER, _WIRE):
                shutil.copyfile(p, tmp / p.name)
                os.chmod(tmp / p.name, 0o644)
            shutil.copyfile(src, tmp / "program.py")
            os.chmod(tmp / "program.py", 0o644)
            work = tmp / "work"
            work.mkdir(mode=0o700)
            if drop:
                os.chown(work, self._drop_to[0], self._drop_to[1])  # type: ignore[index]
            key = os.urandom(32)
            sock_p, sock_c = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            kr, kw = os.pipe()
            os.write(kw, key)
            os.close(kw)
            out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
            opts = {"args": [str(a)[:200] for a in args][:16], "workdir": str(work), "limits": self._limits, "net_isolate": self._net_isolate,
                    "uid": self._drop_to[0] if drop else None, "gid": self._drop_to[1] if drop else None}  # type: ignore[index]
            proc = subprocess.Popen([self._python, "-I", "-S", "-B", str(tmp / "agent_worker.py"), str(sock_c.fileno()), str(kr), str(tmp / "program.py"),
                                     json.dumps(opts)], pass_fds=(sock_c.fileno(), kr), close_fds=True, env={}, cwd=str(tmp), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)
            os.close(kr)
            sock_c.close()
        except BaseException:
            for x in (sock_p, sock_c, out, err):
                try:
                    x and x.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        sid = os.urandom(8).hex()
        sock_p.setblocking(True)
        self._sessions[sid] = _Session(sid, agent_id, proc, sock_p, agent_wire.Reader(key, agent_wire.C2S), agent_wire.Writer(key, agent_wire.S2C), tmp, out, err,
                                       self._clock() + float(timeout_s), self._drop_to[0] if drop else None)  # type: ignore[index]
        return sid

    def run_session(self, agent_id: str, program: str, *, args: Sequence[str] = (), timeout_s: float = 30.0) -> SessionResult:
        sid = self.open_session(agent_id, program, args=args, timeout_s=timeout_s)
        while not self._sessions[sid].done:
            self.pump(0.05)
        return self.result(sid)

    def result(self, sid: str) -> SessionResult:
        s = self._sessions[sid]
        assert s.result is not None
        return s.result

    def close_all(self) -> None:
        for s in list(self._sessions.values()):
            if not s.done:
                self._kill(s, "closed")
                self._finalize(s)

    # --- sorğu emalı (bir-iplikli) ---
    def pump(self, timeout: float = 0.1) -> None:
        live = {s.sock: s for s in self._sessions.values() if not s.done}
        if live:
            ready, _, _ = select.select(list(live), [], [], timeout)
            for sock in ready:
                s = live[sock]
                try:
                    data = sock.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    s.proc.poll()
                    self._finalize(s)
                    continue
                self._feed(s, data)
        else:
            time.sleep(min(timeout, 0.01))
        now = self._clock()
        for s in [x for x in self._sessions.values() if not x.done]:
            if now > s.deadline:
                self._kill(s, "timeout")
                self._finalize(s)
            elif s.proc.poll() is not None and not select.select([s.sock], [], [], 0)[0]:
                self._finalize(s)

    def _feed(self, s: _Session, data: bytes) -> None:
        try:
            msgs = s.reader.feed(data)
        except agent_wire.WireError as exc:
            self._kill(s, f"wire:{exc.code}")
            self._finalize(s)
            return
        for msg in msgs:
            if s.done:
                break
            self._dispatch(s, msg)

    def _reply(self, s: _Session, mid: Any, ok: bool, payload: dict) -> None:
        body = {"id": mid, "ok": ok, ("result" if ok else "error"): payload}
        try:
            s.sock.sendall(s.writer.pack(body))
        except (OSError, agent_wire.WireError):
            self._kill(s, "reply_failed")

    def _violation(self, s: _Session, why: str) -> None:
        s.violations += 1
        if s.violations >= self._max_violations:
            self._kill(s, f"violations:{why}")
            self._finalize(s)

    def _dispatch(self, s: _Session, msg: dict) -> None:
        mid, op, args = msg.get("id"), msg.get("op"), msg.get("args")
        if isinstance(mid, bool) or not isinstance(mid, int) or not isinstance(op, str) or not isinstance(args, dict) or set(msg) - {"id", "op", "args"}:
            self._kill(s, "bad_request")
            self._finalize(s)
            return
        s.requests += 1
        s.calls[op] = s.calls.get(op, 0) + 1
        if s.requests > self._max_requests:
            self._kill(s, "rate_limit")
            self._finalize(s)
            return
        if op not in ALLOWED_OPS:
            s.denied.append((op, "unknown_op"))
            self._reply(s, mid, False, {"code": "unknown_op", "message": "əməliyyat icazəli deyil"})
            self._violation(s, "unknown_op")
            return
        if self._status_of(s.agent_id) != "active":
            s.denied.append((op, "agent_not_active"))
            self._reply(s, mid, False, {"code": "agent_not_active", "message": "agent aktiv deyil"})
            self._kill(s, "agent_not_active")
            self._finalize(s)
            return
        if op == "hello":
            self._hello(s, mid, args)
            return
        if not s.hello:
            self._kill(s, "no_hello")
            self._finalize(s)
            return
        try:
            self._reply(s, mid, True, _HANDLERS[op](self, s, args))
        except _BadArgs as exc:
            s.denied.append((op, "bad_args"))
            self._reply(s, mid, False, {"code": "bad_args", "message": f"arqument yanlışdır: {exc}"})
            self._violation(s, "bad_args")
        except FactoryDenied as exc:
            s.denied.append((op, exc.code))
            self._reply(s, mid, False, {"code": exc.code, "message": str(exc)[:200]})
        except Exception:  # noqa: BLE001 — daxili xəta təfərrüatı sərhədi keçmir
            s.denied.append((op, "internal_error"))
            self._reply(s, mid, False, {"code": "internal_error", "message": "daxili xəta"})

    def _hello(self, s: _Session, mid: Any, args: dict) -> None:
        if s.hello or args.get("proto") != agent_wire.PROTO or not isinstance(args.get("isolation"), dict):
            self._kill(s, "bad_hello")
            self._finalize(s)
            return
        reported = {k: bool(v) for k, v in args["isolation"].items() if k in ("uid_dropped", "network_namespace", "no_new_privs", "dumpable_off", "rlimits",
                                                                              "filesystem_isolated")}
        uid_ok = self._proc_uid(s.proc.pid)
        if s.expect_uid is not None and (uid_ok != s.expect_uid or not reported.get("uid_dropped")):   # serverin müstəqil yoxlaması (/proc), işçinin iddiası kifayət deyil
            self._kill(s, "isolation_mismatch")
            self._finalize(s)
            return
        reported["server_verified_uid"] = uid_ok is not None and s.expect_uid is not None and uid_ok == s.expect_uid
        s.isolation, s.hello = reported, True
        self._reply(s, mid, True, {"proto": agent_wire.PROTO})

    @staticmethod
    def _proc_uid(pid: int) -> int | None:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("Uid:"):
                    real, eff, saved, fs = (int(x) for x in line.split()[1:5])
                    return real if real == eff == saved == fs else None
        except (OSError, ValueError):
            return None
        return None

    def _kill(self, s: _Session, why: str) -> None:
        s.killed = s.killed or why
        try:
            os.killpg(s.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                s.proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _finalize(self, s: _Session) -> None:
        if s.done:
            return
        try:
            s.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill(s, "wait_timeout")
            s.proc.wait()
        out = err = ""
        for f, name in ((s.out, "out"), (s.err, "err")):
            try:
                f.seek(0)
                text = f.read(_OUT_CAP).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                text = ""
            if name == "out":
                out = text
            else:
                err = text
            f.close()
        try:
            s.sock.close()
        except OSError:
            pass
        shutil.rmtree(s.tmp, ignore_errors=True)
        s.done = True
        s.result = SessionResult(s.agent_id, s.proc.returncode, out, err, dict(s.calls), tuple(s.denied), s.violations, s.killed, dict(s.isolation))
