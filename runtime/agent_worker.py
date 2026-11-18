"""Agent kodunun işlədiyi AYRI proses (yalnız stdlib; deposdan HEÇ nə idxal etmir). Server (`runtime/agent_gateway.py`) bunu `python -I -S -B` ilə, boş mühit,
boş iş qovluğu və yalnız iki irsi fayl deskriptoru (socketpair uc + sessiya açarı borusu) ilə başladır.

Başlanğıc (imtiyazlı ola bilən hissə yalnız bu etibarlı kodu icra edir; agent proqramı DAHA SONRA işləyir):
  1. sessiya açarını oxuyur, borunu bağlayır;
  2. (istəyə görə) şəbəkə namespace-i (`unshare(CLONE_NEWNET)`), sonra uid/gid endirmə (setgroups([]) -> setgid -> setuid; geri qayıtmanın mümkünsüzlüyü yoxlanır);
  3. `PR_SET_NO_NEW_PRIVS`, `PR_SET_DUMPABLE=0`, rlimit-lər (CORE=0, NOFILE, CPU, FSIZE, AS), umask 077, boş iş qovluğu;
  4. server ilə `hello` (əldə edilən izolyasiya səviyyəsini DÜRÜST bildirir) və agent proqramının `main(client)` funksiyasını çağırır.
Agent yalnız `AgentClient` ilə danışır: serverin heç bir obyekti bu prosesdə mövcud deyil. Bu OS səviyyəsində tam sandbox DEYİL: fayl sistemi/şəbəkə/sistem çağırışı
məhdudiyyəti yalnız uid endirmə və (mümkündürsə) şəbəkə namespace-i qədərdir; qalanı operator səviyyəsində (konteyner/bwrap/seccomp) təmin olunmalıdır.
"""
from __future__ import annotations

import json
import os
import runpy
import socket
import sys
import traceback

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import agent_wire  # noqa: E402  (eyni şəxsi qovluqdan)


class RpcError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class AgentClient:
    """Agent proqramının gördüyü YEGANƏ interfeys: üç icazəli əməliyyat + ümumi `call` (rədd olunan əməliyyatları sınamaq üçün də)."""

    def __init__(self, sock: socket.socket, key: bytes, args: list, isolation: dict) -> None:
        self._sock, self._w, self._r = sock, agent_wire.Writer(key, agent_wire.C2S), agent_wire.Reader(key, agent_wire.S2C)
        self._id = 0
        self.args = tuple(args)
        self.isolation = dict(isolation)

    def call(self, op: str, **args):
        self._id += 1
        self._sock.sendall(self._w.pack({"id": self._id, "op": op, "args": args}))
        while True:
            data = self._sock.recv(65536)
            if not data:
                raise RpcError("closed", "server sessiyanı bağladı")
            for msg in self._r.feed(data):
                if msg.get("id") != self._id:
                    raise RpcError("protocol", "gözlənilməz cavab")
                if msg.get("ok") is True:
                    return msg.get("result")
                err = msg.get("error") or {}
                raise RpcError(str(err.get("code", "error")), str(err.get("message", "")))

    def info(self) -> dict:
        return self.call("info")

    def request_permission(self, permission: str, reason: str) -> dict:
        return self.call("request_permission", permission=permission, reason=reason)

    def handoff(self, to_agent_id: str, title: str, payload: dict, permission: str = "read_files", cost: float = 0.0, op_id: str | None = None,
                parent_task_id: str = "") -> dict:
        a = {"to_agent_id": to_agent_id, "title": title, "payload": payload, "permission": permission, "cost": cost, "parent_task_id": parent_task_id}
        if op_id is not None:
            a["op_id"] = op_id
        return self.call("handoff", **a)


def _prctl(option: int, arg: int) -> bool:
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(option, arg, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001
        return False


# --- seccomp (yalnız x86_64: cədvəl bu arxitekturada sınanıb; başqa arxitekturada ƏLÇATAN DEYİL -> server `require_seccomp` ilə sessiyanı rədd edir) ---
_X86_64_DENY = {"ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311, "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161,
                "unshare": 272, "setns": 308, "bpf": 321, "perf_event_open": 298, "keyctl": 250, "add_key": 248, "request_key": 249, "kexec_load": 246,
                "kexec_file_load": 320, "init_module": 175, "finit_module": 313, "delete_module": 176, "reboot": 169, "swapon": 167, "swapoff": 168,
                "acct": 163, "quotactl": 179, "open_by_handle_at": 304, "userfaultfd": 323, "iopl": 172, "ioperm": 173, "syslog": 103, "mknod": 133,
                "mknodat": 259, "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427, "open_tree": 428, "move_mount": 429,
                "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442}
_CLONE, _CLONE3 = 56, 435
_NS_FLAGS = 0x7E020000   # CLONE_NEWNS|NEWCGROUP|NEWUTS|NEWIPC|NEWUSER|NEWPID|NEWNET


def build_seccomp_program() -> bytes | None:
    """BPF: arxitektura/x32 yoxlaması, təhlükəli sistem çağırışları EPERM, `clone` ad-sahəsi bayraqları EPERM, `clone3` ENOSYS (glibc `clone`-a keçir); qalanı icazəli."""
    import platform
    import struct
    if platform.machine() != "x86_64":
        return None
    ins: list[bytes] = []

    def op(code: int, jt: int, jf: int, k: int) -> None:
        ins.append(struct.pack("<HBBI", code, jt, jf, k))
    ERRNO = lambda e: 0x00050000 | e  # noqa: E731
    op(0x20, 0, 0, 4)                       # ld arch
    op(0x15, 1, 0, 0xC000003E)              # jeq x86_64 -> atla
    op(0x06, 0, 0, 0x80000000)              # KILL_PROCESS
    op(0x20, 0, 0, 0)                       # ld nr
    op(0x45, 0, 1, 0x40000000)              # x32 ABI bitı varsa ...
    op(0x06, 0, 0, 0x80000000)              # ... KILL_PROCESS
    for nr in _X86_64_DENY.values():
        op(0x15, 0, 1, nr)
        op(0x06, 0, 0, ERRNO(1))            # EPERM
    op(0x15, 0, 1, _CLONE3)
    op(0x06, 0, 0, ERRNO(38))               # ENOSYS
    op(0x15, 0, 4, _CLONE)                  # clone deyilsə 4 əmri atla
    op(0x20, 0, 0, 16)                      # ld args[0] (aşağı söz)
    op(0x45, 0, 1, _NS_FLAGS)               # ad-sahəsi bayrağı varsa ...
    op(0x06, 0, 0, ERRNO(1))                # ... EPERM
    op(0x20, 0, 0, 0)                       # ld nr (yenidən)
    op(0x06, 0, 0, 0x7FFF0000)              # ALLOW
    return b"".join(ins)


def _install_seccomp() -> bool:
    try:
        import ctypes
        prog = build_seccomp_program()
        if prog is None:
            return False
        n = len(prog) // 8
        buf = ctypes.create_string_buffer(prog, len(prog))

        class SockFprog(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
        fprog = SockFprog(n, ctypes.addressof(buf))
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(22, 2, ctypes.byref(fprog), 0, 0) == 0     # PR_SET_SECCOMP, SECCOMP_MODE_FILTER
    except Exception:  # noqa: BLE001
        return False


def _harden(opts: dict) -> dict:
    """Server TƏRƏFDƏN `/proc` ilə MÜSTƏQİL yoxlanır; buradakı bayraqlar yalnız diaqnostikadır (etibar olunmur)."""
    iso = {"uid_dropped": False, "network_namespace": False, "no_new_privs": False, "dumpable_off": False, "rlimits": False, "seccomp": False}
    if opts.get("net_isolate"):
        ok = False
        if hasattr(os, "unshare"):
            try:
                os.unshare(os.CLONE_NEWNET)
                ok = True
            except Exception:  # noqa: BLE001
                pass
        iso["network_namespace"] = ok
        if not ok:
            os._exit(72)          # tələb olunan şəbəkə izolyasiyası alınmadı: agent kodu HEÇ VAXT başlamır (server də ayrıca yoxlayır)
    uid, gid = opts.get("uid"), opts.get("gid")
    if uid is not None:
        os.setgroups([])
        os.setgid(int(gid))
        os.setuid(int(uid))
        if os.getuid() != uid or os.geteuid() != uid or os.getgid() != gid:
            os._exit(70)
        try:
            os.setuid(0)
            os._exit(71)    # geri qayıtmaq mümkündürsə dərhal dayan
        except PermissionError:
            pass
        iso["uid_dropped"] = True
    iso["no_new_privs"] = _prctl(38, 1)
    iso["dumpable_off"] = _prctl(4, 0)
    try:
        import resource
        lim = opts.get("limits") or {}
        for name, value in (("RLIMIT_CORE", 0), ("RLIMIT_NOFILE", int(lim.get("nofile", 64))), ("RLIMIT_CPU", int(lim.get("cpu_s", 60))),
                            ("RLIMIT_FSIZE", int(lim.get("fsize", 10 * 1024 * 1024))), ("RLIMIT_AS", int(lim.get("as_bytes", 1024 * 1024 * 1024))),
                            ("RLIMIT_NPROC", int(lim["nproc"])) if "nproc" in lim else (None, None)):
            if name is not None:
                resource.setrlimit(getattr(resource, name), (value, value))
        iso["rlimits"] = True
    except Exception:  # noqa: BLE001
        pass
    os.umask(0o077)
    wd = opts.get("workdir")
    if wd:
        os.chdir(wd)
    if opts.get("seccomp"):
        iso["seccomp"] = _install_seccomp()     # NNP-dən sonra, agent kodundan ƏVVƏL
    return iso


def main(argv: list[str]) -> int:
    sock_fd, key_fd, program, opts = int(argv[1]), int(argv[2]), argv[3], json.loads(argv[4])
    key = b""
    while len(key) < 32:
        chunk = os.read(key_fd, 32 - len(key))
        if not chunk:
            return 64
        key += chunk
    os.close(key_fd)
    iso = _harden(opts)
    sock = socket.socket(fileno=sock_fd)
    client = AgentClient(sock, key, opts.get("args") or [], iso)
    client.call("hello", proto=agent_wire.PROTO, isolation=iso)
    try:
        ns = runpy.run_path(program, run_name="agent_program")
        entry = ns.get("main")
        if not callable(entry):
            sys.stderr.write("agent proqramında main(client) yoxdur\n")
            return 65
        entry(client)
    except RpcError as exc:
        sys.stderr.write(f"RpcError: {exc}\n")
        return 3
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
