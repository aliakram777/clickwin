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


def _harden(opts: dict) -> dict:
    iso = {"uid_dropped": False, "network_namespace": False, "no_new_privs": False, "dumpable_off": False, "rlimits": False, "filesystem_isolated": False}
    if opts.get("net_isolate") and hasattr(os, "unshare"):
        try:
            os.unshare(os.CLONE_NEWNET)
            iso["network_namespace"] = True
        except Exception:  # noqa: BLE001
            pass
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
                            ("RLIMIT_FSIZE", int(lim.get("fsize", 10 * 1024 * 1024))), ("RLIMIT_AS", int(lim.get("as_bytes", 1024 * 1024 * 1024)))):
            resource.setrlimit(getattr(resource, name), (value, value))
        iso["rlimits"] = True
    except Exception:  # noqa: BLE001
        pass
    os.umask(0o077)
    wd = opts.get("workdir")
    if wd:
        os.chdir(wd)
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
