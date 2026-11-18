"""Agent izolyasiya sərhədi: agent kodu AYRI OS prosesində işləyir; etibarlı server ilə yalnız minimal, autentifikasiyalı RPC danışır (audit B-1/B-2, H-1..H-4, M-1..M-4).

Nə var:
- `AgentGateway` (etibarlı server tərəfi): işçi prosesi (`runtime/agent_worker.py`) boş mühit/boş iş qovluğu/irsi iki fd ilə başladır. Server root-dursa HƏR SESSİYAYA unikal uid/gid verilir
  (`uid_pool`); beləliklə sessiyalar bir-birini siqnal/ptrace/fayl ilə əlçatan ola bilmir və sessiya bitəndə həmin uid-nin BÜTÜN prosesləri (fork/setsid qaçışı daxil) öldürülür.
- İzolyasiya xassələri işçinin iddiasına DEYİL, serverin `/proc` yoxlamasına əsaslanır: uid/gid, boş qrup, sıfır capability, NoNewPrivs, seccomp filtri, şəbəkə namespace-i
  (serverinkindən fərqli inode + yalnız `lo`), dumpable=0 (proc qeydi root-a məxsus), rlimit-lər. Hər hansı tələb olunan xassə təsdiqlənmirsə agent kodu HEÇ VAXT işə düşmür (hello cavabsız, proses öldürülür).
- RPC: yalnız `hello`, `info`, `request_permission`, `handoff`. Agent id sessiyaya SERVER tərəfindən bağlanır. Çərçivə HMAC + sıra nömrəsi + ölçü və iç-içəlik limiti (ayrışdırmadan əvvəl).
- Giriş/çıxış tamamilə qeyri-bloklayıcıdır (`selectors`, yazma buferi limitli); hər `pump()` çağırışı ödəniş müddətlərini tətbiq edir; proses təmizliyi `pump()`-ı bloklamır.

Nə YOXDUR (dürüst məhdudiyyətlər): tam OS sandbox DEYİL. Mount/PID namespace, cgroup, fayl sistemi pivot_root YOXDUR: agent dünyaca oxunan yollara çata bilir. seccomp yalnız x86_64-də (başqa arxitekturada
`require_seccomp=True` ilə sessiya açılmır). `allow_insecure_same_uid=True` rejimi (root olmayan server) HEÇ BİR təhlükəsizlik sərhədi DEYİL: agent serverlə EYNİ istifadəçidir (onu öldürə, DB fayllarını
oxuya/yaza, şəbəkəyə çıxa bilər); defolt olaraq BAĞLIDIR və nəticədə `security_boundary=False` göstərilir.
Server bir-iplikli sorğu emalı edir (`pump()` DB bağlantılarını yaradan iplikdən çağırılmalıdır).
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import pwd
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from agents.agent_factory import FactoryDenied
from runtime import agent_wire

ALLOWED_OPS = ("hello", "info", "request_permission", "handoff")
DEFAULT_UID_POOL = (62000, 62256)       # NAYMERA-ya AYRILMIŞ uid aralığı [başlanğıc, son): sessiya başına bir uid/gid; passwd-da olan və ya başqa proses işlədən uid-lər atlanır
DEFAULT_LIMITS = {"nofile": 64, "cpu_s": 60, "fsize": 10 * 1024 * 1024, "as_bytes": 1024 * 1024 * 1024, "nproc": 32}
CLOSE_GRACE_S = 10.0
EOF_GRACE_S = 2.0              # soket bağlandıqdan sonra proses təbii çıxsın deyə qısa möhlət; sonra öldürülür
_PROGRAM_RE = re.compile(r"^[A-Za-z0-9_\-]{1,60}\.py$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_PERM_RE = re.compile(r"^[a-z_]{1,40}$")
_WORKER = Path(__file__).resolve().parent / "agent_worker.py"
_WIRE = Path(__file__).resolve().parent / "agent_wire.py"
_OUT_CAP = 16 * 1024


class IsolationError(RuntimeError):
    """Tələb olunan izolyasiya təmin edilə bilmir (qapalı qalır) və ya sessiya parametrləri yanlışdır."""


class _BadArgs(ValueError):
    pass


@dataclass(frozen=True)
class SessionResult:
    agent_id: str
    exit_code: int | None
    stdout: str                  # ETİBARSIZ mətn (agent yazıb)
    stderr: str
    calls: dict
    denied: tuple
    violations: int
    killed: str
    isolation: dict              # SERVER tərəfindən /proc ilə yoxlanmış xassələr (işçinin iddiası deyil)
    security_boundary: bool      # yalnız izolasiya rejimində və bütün tələb olunan xassələr təsdiqlənibsə True
    survivors_killed: int        # sessiya bitəndən sonra öldürülən qalıq proseslər (fork/setsid cəhdləri)
    reported: dict = field(default_factory=dict)   # işçinin öz bayraqları: yalnız diaqnostika, etibar olunmur


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
    uid: int | None
    gid: int | None
    mode: str
    limits: dict
    hello: bool = False
    requests: int = 0
    violations: int = 0
    calls: dict = field(default_factory=dict)
    denied: list = field(default_factory=list)
    killed: str = ""
    isolation: dict = field(default_factory=dict)
    reported: dict = field(default_factory=dict)
    boundary: bool = False
    outbuf: bytearray = field(default_factory=bytearray)
    closing: bool = False
    close_started: float = 0.0
    eof_at: float = 0.0
    survivors: set = field(default_factory=set)
    done: bool = False
    result: SessionResult | None = None


def harden_server_process() -> bool:
    """Serveri `PR_SET_DUMPABLE=0` edir: eyni-uid (root olmayan) proseslər `/proc/<pid>/mem`-i oxuya və ptrace edə bilmir. Linux-a xasdır, best-effort."""
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001
        return False


# --- /proc yardımçıları (server tərəfdən müstəqil yoxlama) ---

def _proc_status(pid: int) -> dict[str, str] | None:
    try:
        out = {}
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            k, _, v = line.partition(":")
            out[k] = v.strip()
        return out
    except (OSError, ValueError):
        return None


def _uid_procs(uid: int, exclude: int = 0, zombies: bool = False) -> list[int]:
    """Real uid-i `uid` olan bütün proseslər (`/proc` skanı; dumpable=0 proseslərin `/proc/<pid>` sahibi root olduğu üçün `status` oxunur)."""
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for n in names:
        if not n.isdigit() or int(n) == exclude:
            continue
        st = _proc_status(int(n))
        if st and st.get("Uid", "").split()[:1] == [str(uid)] and (zombies or not st.get("State", "").startswith("Z")):   # zombi (ölü, init-in götürməsini gözləyən) sağ qalan sayılmır
            found.append(int(n))
    return found


def _parse_limits(pid: int) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    try:
        for line in Path(f"/proc/{pid}/limits").read_text().splitlines()[1:]:
            m = re.match(r"^(Max [a-z ]+?)\s{2,}(\S+)\s+(\S+)", line)
            if m:
                out[m.group(1).strip()] = (m.group(2), m.group(3))
    except OSError:
        pass
    return out


def _lim_ok(pair: tuple[str, str] | None, cap: int) -> bool:
    if pair is None:
        return False
    try:
        return int(pair[0]) <= cap and int(pair[1]) <= cap
    except ValueError:
        return False            # "unlimited"


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
                 python: str | None = None, uid_pool: tuple[int, int] | None = DEFAULT_UID_POOL, allow_insecure_same_uid: bool = False,
                 require_net_isolation: bool = True, require_seccomp: bool = True, harden_server: bool = True, limits: dict | None = None,
                 max_sessions: int = 4, max_requests: int = 200, max_violations: int = 3, max_outbuf: int = 128 * 1024,
                 clock: Callable[[], float] = time.monotonic, tmp_dir: str | Path | None = None) -> None:
        """`ports_for(agent_id)` və `status_of(agent_id)` etibarlı bootstrap-ın verdiyi funksiyalardır (bağlı metod DEYİL).
        İzolyasiya rejimi: server root + `uid_pool`. Əks halda sessiya AÇILMIR (qapalı qalır), YALNIZ `allow_insecure_same_uid=True` açıq seçilməsə:
        onda agent serverlə EYNİ uid-də işləyir, bu TƏHLÜKƏSİZLİK SƏRHƏDİ DEYİL və nəticədə `security_boundary=False` olur."""
        self._ports_for, self._status_of = ports_for, status_of
        self._root = Path(program_root).resolve()
        self._python = python or sys.executable
        self._uid_pool = uid_pool
        self._insecure, self._req_net, self._req_seccomp = bool(allow_insecure_same_uid), bool(require_net_isolation), bool(require_seccomp)
        self._limits = {**DEFAULT_LIMITS, **(limits or {})}
        self._max_sessions, self._max_requests, self._max_violations, self._max_outbuf, self._clock = max_sessions, max_requests, max_violations, max_outbuf, clock
        self._sessions: dict[str, _Session] = {}
        self._used_uids: set[int] = set()
        self._quarantine: set[int] = set()
        self._sel = selectors.DefaultSelector()
        self.server_dumpable_off = harden_server_process() if harden_server else False
        base_dir = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.gettempdir())
        self._sweep_stale(base_dir)
        self._base = Path(tempfile.mkdtemp(prefix=f"naymera-agents-{os.getpid()}-", dir=str(base_dir)))
        os.chmod(self._base, 0o711)           # sessiya qovluqlarına yalnız dəqiq yolla girmək olar; siyahı almaq olmaz
        weakref.finalize(self, shutil.rmtree, str(self._base), True)       # GC/proses çıxışında baza qovluğu silinir (close() çağırılmasa da)

    # --- bərpa ---
    @staticmethod
    def _sweep_stale(base_dir: Path) -> None:
        """Ölü server proseslərindən qalan sessiya baza qovluqlarını silir (`naymera-agents-<pid>-*`)."""
        try:
            for p in base_dir.glob("naymera-agents-*"):
                m = re.match(r"^naymera-agents-(\d+)-", p.name)
                if not m or int(m.group(1)) == os.getpid():
                    continue
                try:
                    os.kill(int(m.group(1)), 0)
                    continue                  # server hələ yaşayır
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass

    # --- rejim və uid ---
    def _mode(self) -> str:
        if hasattr(os, "geteuid") and os.geteuid() == 0 and self._uid_pool is not None:
            return "isolated"
        if self._insecure:
            return "insecure_same_uid"
        raise IsolationError("izolyasiya təmin edilə bilmir (server root deyil və ya uid_pool yoxdur): sessiya açılmır. `allow_insecure_same_uid=True` TƏHLÜKƏSİZLİK SƏRHƏDİ DEYİL")

    def _alloc_uid(self) -> int:
        lo, hi = self._uid_pool  # type: ignore[misc]
        for uid in range(lo, hi):
            if uid in self._used_uids or uid in self._quarantine:
                continue
            try:
                pwd.getpwuid(uid)
                continue                      # sistem istifadəçisi
            except KeyError:
                pass
            if _uid_procs(uid, zombies=True):
                continue                      # başqa proses (və ya götürülməmiş zombi: RLIMIT_NPROC-u tutur) bu uid-i işlədir: toxunmuruq
            self._used_uids.add(uid)
            return uid
        raise IsolationError("boş sessiya uid-i yoxdur (uid_pool tükənib və ya qalıq proseslər var)")

    # --- yaşam dövrü ---
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
        mode = self._mode()
        isolated = mode == "isolated"
        uid = self._alloc_uid() if isolated else None
        gid = uid
        sid = os.urandom(8).hex()
        sdir = self._base / sid
        sock_p = sock_c = None
        out = err = None
        try:
            sdir.mkdir(mode=0o750)
            if isolated:
                os.chown(sdir, 0, gid)        # yalnız bu sessiyanın qrupu girə/oxuya bilər
            for p in (_WORKER, _WIRE):
                shutil.copyfile(p, sdir / p.name)
            shutil.copyfile(src, sdir / "program.py")
            for f in ("agent_worker.py", "agent_wire.py", "program.py"):
                os.chmod(sdir / f, 0o640)
                if isolated:
                    os.chown(sdir / f, 0, gid)
            work = sdir / "work"
            work.mkdir(mode=0o700)
            if isolated:
                os.chown(work, uid, gid)
            key = os.urandom(32)
            sock_p, sock_c = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            kr, kw = os.pipe()
            os.write(kw, key)
            os.close(kw)
            out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
            lims = dict(self._limits)
            if not isolated:
                lims.pop("nproc", None)       # same-uid rejimində RLIMIT_NPROC bütün istifadəçinin proseslərini sayır: sessiyaya aid ola bilməz (yalnız unikal uid-də mənalıdır)
            opts = {"args": [str(a)[:200] for a in args][:16], "workdir": str(work), "limits": lims, "net_isolate": bool(isolated and self._req_net),
                    "seccomp": bool(self._req_seccomp or not isolated), "uid": uid, "gid": gid}
            proc = subprocess.Popen([self._python, "-I", "-S", "-B", str(sdir / "agent_worker.py"), str(sock_c.fileno()), str(kr), str(sdir / "program.py"),
                                     json.dumps(opts)], pass_fds=(sock_c.fileno(), kr), close_fds=True, env={}, cwd=str(sdir), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)
            os.close(kr)
            sock_c.close()
            sock_c = None
            sock_p.setblocking(False)
        except BaseException:
            for x in (sock_p, sock_c, out, err):
                try:
                    x and x.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(sdir, ignore_errors=True)
            if uid is not None:
                self._used_uids.discard(uid)
            raise
        s = _Session(sid, agent_id, proc, sock_p, agent_wire.Reader(key, agent_wire.C2S), agent_wire.Writer(key, agent_wire.S2C), sdir, out, err,
                     self._clock() + float(timeout_s), uid, gid, mode, lims)
        self._sessions[sid] = s
        self._sel.register(sock_p, selectors.EVENT_READ, s)
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

    def close_all(self, wait_s: float = 15.0) -> None:
        """Bütün sessiyaları bağlayır (açıq teardown çağırışı; `pump()` özü heç vaxt bloklanmır)."""
        for s in list(self._sessions.values()):
            self._begin_close(s, "closed")
        end = time.monotonic() + wait_s
        while any(not s.done for s in self._sessions.values()) and time.monotonic() < end:
            self.pump(0.02)

    def close(self) -> None:
        self.close_all()
        try:
            self._sel.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self._base, ignore_errors=True)

    # --- sorğu emalı (bir-iplikli, qeyri-bloklayıcı) ---
    def pump(self, timeout: float = 0.1) -> None:
        if any(not s.done and not s.closing and not s.eof_at for s in self._sessions.values()):
            for key, mask in self._sel.select(max(0.0, timeout)):
                s = key.data
                if s.done or s.closing:
                    continue
                if mask & selectors.EVENT_READ:
                    self._on_readable(s)
                if mask & selectors.EVENT_WRITE and not s.done and not s.closing:
                    self._flush(s)
        else:
            time.sleep(min(max(0.0, timeout), 0.01))
        now = self._clock()
        for s in list(self._sessions.values()):
            if s.done:
                continue
            if not s.closing and not s.eof_at and s.proc.poll() is not None:
                for _ in range(16):                  # işçi çıxdı, lakin uşaq proseslər soketi açıq saxlaya bilər: qalıq çərçivələri boşalt, sonra bağla
                    if s.closing or not self._on_readable(s):
                        break
                if not s.closing and not s.eof_at:
                    self._begin_close(s, "")
            if s.eof_at and not s.closing:
                if s.proc.poll() is not None:
                    self._begin_close(s, "")
                elif now - s.eof_at > EOF_GRACE_S:
                    self._begin_close(s, "socket_closed")
            if not s.closing and now > s.deadline:
                self._begin_close(s, "timeout")
            if s.closing:
                self._progress_close(s, now)

    def _on_readable(self, s: _Session) -> bool:
        """Oxunan bayt olubsa True (qalıq çərçivələri boşaltmaq üçün)."""
        try:
            data = s.sock.recv(65536)
        except BlockingIOError:
            return False
        except OSError:
            data = b""
        if not data:
            if not s.eof_at:                   # agent çıxdı/soketi bağladı: qısa möhlət, sonra (lazım olsa) öldürülür; qalıq proseslər HƏR HALDA təmizlənir
                s.eof_at = self._clock()
                try:
                    self._sel.unregister(s.sock)
                except (KeyError, ValueError, OSError):
                    pass
            return False
        try:
            msgs = s.reader.feed(data)
        except agent_wire.WireError as exc:
            self._begin_close(s, f"wire:{exc.code}")
            return True
        except Exception:  # noqa: BLE001 — gözlənilməz ayrışdırma xətası serveri çökdürməməlidir
            self._begin_close(s, "wire:internal")
            return True
        for msg in msgs:
            if s.done or s.closing:
                break
            try:
                self._dispatch(s, msg)
            except Exception:  # noqa: BLE001
                self._begin_close(s, "internal_error")
                break
        return True

    # --- yazma (limitli, qeyri-bloklayıcı) ---
    def _reply(self, s: _Session, mid: Any, ok: bool, payload: dict) -> None:
        body = {"id": mid, "ok": ok, ("result" if ok else "error"): payload}
        try:
            frame = s.writer.pack(body)
        except agent_wire.WireError:
            self._begin_close(s, "reply_too_large")
            return
        if len(s.outbuf) + len(frame) > self._max_outbuf:
            self._begin_close(s, "backpressure")      # agent cavabları oxumur: yığılma limitini aşdı
            return
        s.outbuf += frame
        self._flush(s)

    def _flush(self, s: _Session) -> None:
        while s.outbuf:
            try:
                n = s.sock.send(s.outbuf)
            except BlockingIOError:
                break
            except OSError:
                self._begin_close(s, "reply_failed")
                return
            del s.outbuf[:n]
        try:
            self._sel.modify(s.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if s.outbuf else 0), s)
        except (KeyError, ValueError, OSError):
            pass

    def _violation(self, s: _Session, why: str) -> None:
        s.violations += 1
        if s.violations >= self._max_violations:
            self._begin_close(s, f"violations:{why}")

    def _dispatch(self, s: _Session, msg: dict) -> None:
        mid, op, args = msg.get("id"), msg.get("op"), msg.get("args")
        if isinstance(mid, bool) or not isinstance(mid, int) or not isinstance(op, str) or not isinstance(args, dict) or set(msg) - {"id", "op", "args"}:
            self._begin_close(s, "bad_request")
            return
        s.requests += 1
        s.calls[op] = s.calls.get(op, 0) + 1
        if s.requests > self._max_requests:
            self._begin_close(s, "rate_limit")
            return
        if op not in ALLOWED_OPS:
            s.denied.append((op, "unknown_op"))
            self._reply(s, mid, False, {"code": "unknown_op", "message": "əməliyyat icazəli deyil"})
            self._violation(s, "unknown_op")
            return
        if self._status_of(s.agent_id) != "active":
            s.denied.append((op, "agent_not_active"))
            self._reply(s, mid, False, {"code": "agent_not_active", "message": "agent aktiv deyil"})
            self._begin_close(s, "agent_not_active")
            return
        if op == "hello":
            self._hello(s, mid, args)
            return
        if not s.hello:
            self._begin_close(s, "no_hello")
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

    # --- server tərəfli izolyasiya yoxlaması ---
    def _verify(self, s: _Session) -> tuple[dict, list[str]]:
        """Bütün xassələr `/proc`-dan SERVER tərəfindən oxunur (işçinin hello bayraqlarına baxılmır)."""
        pid = s.proc.pid
        v: dict[str, bool] = {}
        st = _proc_status(pid) or {}
        v["parent_is_server"] = st.get("PPid") == str(os.getpid())
        ids = lambda k: st.get(k, "").split()  # noqa: E731
        v["uid_unique"] = s.uid is not None and ids("Uid") == [str(s.uid)] * 4 and ids("Gid") == [str(s.gid)] * 4
        v["groups_empty"] = st.get("Groups", "x").strip() == ""
        v["caps_zero"] = all(st.get(k, "1").strip("0") == "" for k in ("CapInh", "CapPrm", "CapEff", "CapAmb"))
        v["no_new_privs"] = st.get("NoNewPrivs") == "1"
        v["seccomp_filter"] = st.get("Seccomp") == "2"
        try:
            mine, theirs = os.readlink("/proc/self/ns/net"), os.readlink(f"/proc/{pid}/ns/net")
            ifaces = [ln.split(":")[0].strip() for ln in Path(f"/proc/{pid}/net/dev").read_text().splitlines()[2:] if ":" in ln]
            v["network_namespace"] = theirs != mine and set(ifaces) <= {"lo"}
        except OSError:
            v["network_namespace"] = False
        try:
            v["dumpable_off"] = os.stat(f"/proc/{pid}/environ").st_uid == 0   # dumpable=0 prosesin `environ` və s. fayllarının sahibi root olur (qovluğun özünün yox)
        except OSError:
            v["dumpable_off"] = False
        lim = _parse_limits(pid)
        L = s.limits
        v["rlimits"] = (lim.get("Max core file size") in (("0", "0"),) and _lim_ok(lim.get("Max open files"), int(L["nofile"])) and _lim_ok(lim.get("Max cpu time"), int(L["cpu_s"]))
                        and _lim_ok(lim.get("Max file size"), int(L["fsize"])) and _lim_ok(lim.get("Max address space"), int(L["as_bytes"]))
                        and (s.mode != "isolated" or _lim_ok(lim.get("Max processes"), int(L["nproc"]))))
        v["filesystem_isolated"] = False        # mount namespace/pivot_root YOXDUR (sənədləşdirilib)
        v["pid_namespace"] = False
        required: list[str] = []
        if s.mode == "isolated":
            required = ["parent_is_server", "uid_unique", "groups_empty", "caps_zero", "no_new_privs", "dumpable_off", "rlimits"]
            if self._req_net:
                required.append("network_namespace")
            if self._req_seccomp:
                required.append("seccomp_filter")
        return v, [k for k in required if not v.get(k)]

    def _hello(self, s: _Session, mid: Any, args: dict) -> None:
        if s.hello or args.get("proto") != agent_wire.PROTO or not isinstance(args.get("isolation"), dict):
            self._begin_close(s, "bad_hello")
            return
        s.reported = {k: bool(x) for k, x in args["isolation"].items() if isinstance(k, str) and len(k) < 40}
        verified, failed = self._verify(s)
        s.isolation = verified
        if failed:                                   # cavab VERİLMİR: işçi hello-da gözləyir, agent kodu heç vaxt başlamır
            self._begin_close(s, "isolation_unverified:" + ",".join(failed))
            return
        s.boundary = s.mode == "isolated"
        s.hello = True
        self._reply(s, mid, True, {"proto": agent_wire.PROTO})

    # --- bağlama (bloklamayan) ---
    def _kill_tree(self, s: _Session) -> None:
        try:
            os.killpg(s.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                s.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if s.uid is not None:
            for pid in (p for p in _uid_procs(s.uid) if p != s.proc.pid):
                s.survivors.add(pid)
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _begin_close(self, s: _Session, why: str) -> None:
        if s.closing or s.done:
            return
        if not why and s.proc.poll() is None:
            why = "socket_closed"
        s.killed = s.killed or why
        s.closing = True
        s.close_started = self._clock()
        try:
            self._sel.unregister(s.sock)
        except (KeyError, ValueError, OSError):
            pass
        self._kill_tree(s)

    def _progress_close(self, s: _Session, now: float) -> None:
        rc = s.proc.poll()
        left = [p for p in _uid_procs(s.uid) if p != s.proc.pid] if s.uid is not None else []
        for pid in left:
            s.survivors.add(pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if rc is None:
            try:
                os.killpg(s.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if rc is not None and not left:
            self._complete(s, clean=True)
        elif now - s.close_started > CLOSE_GRACE_S:
            self._complete(s, clean=False)         # uid karantinaya alınır, pump bloklanmır

    def _complete(self, s: _Session, clean: bool) -> None:
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
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            s.sock.close()
        except OSError:
            pass
        shutil.rmtree(s.tmp, ignore_errors=True)
        if s.uid is not None:
            self._used_uids.discard(s.uid)
            if not clean:
                self._quarantine.add(s.uid)
        s.done = True
        killed = s.killed
        if not s.hello and not killed:                       # hello-ya çatmadan çıxdı (məs. tələb olunan şəbəkə izolyasiyası alınmadı): agent kodu işləməyib
            killed = f"no_hello:exit={s.proc.returncode}"
        killed += "" if clean else (";" if killed else "") + "cleanup_incomplete"
        s.result = SessionResult(s.agent_id, s.proc.returncode, out, err, dict(s.calls), tuple(s.denied), s.violations, killed, dict(s.isolation),
                                 bool(s.boundary and clean), len(s.survivors), dict(s.reported))
