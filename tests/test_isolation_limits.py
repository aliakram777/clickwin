"""Agent izolyasiyasının BİLİNƏN HƏDLƏRİ (sənədləşmiş): bu testlər həddi İDDİA EDİR, "təhlükəsizdir" demir.

Fayl sistemi izolyasiyası TAM DEYİL: mount/PID namespace yoxdur. Unikal uid server məlumat qovluğunu (0700), `/proc/<server>/environ|mem` və siqnalları bağlayır, amma agent prosesi
hostdakı BÜTÜN dünya-oxunaqlı faylları oxuya bilir. Əgər gələcəkdə mount namespace əlavə olunarsa, bu test bilərəkdən uğursuz olacaq və sənəd/iddia yenilənməlidir."""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from tests.test_agent_isolation import IS_ROOT, agent, make_prod, needs_root, out, program

READ_WORLD_READABLE = '''
import json, os
def main(c):
    r = {}
    for name, path in (("world_file", c.args[0]), ("server_cmdline", f"/proc/{os.getppid()}/cmdline")):
        try:
            r[name] = "READ:" + open(path, "rb").read(12).decode("latin-1")
        except Exception as e:
            r[name] = "DENIED:" + type(e).__name__
    for name, path in (("server_environ", f"/proc/{os.getppid()}/environ"), ("data_dir", c.args[2])):
        try:
            r[name] = "READ:" + str(os.listdir(path) if os.path.isdir(path) else open(path, "rb").read(8))[:20]
        except Exception as e:
            r[name] = "DENIED:" + type(e).__name__
    print(json.dumps(r))
'''


@needs_root
def test_KNOWN_LIMITATION_agents_can_read_world_readable_host_files_but_not_server_secrets(tmp_path):
    prod = make_prod(tmp_path, drop=True)
    shared = tempfile.mkdtemp(dir="/var/tmp")
    try:
        os.chmod(shared, 0o755)
        f = os.path.join(shared, "world.txt")
        with open(f, "w") as fh:
            fh.write("world-readable-secret-like-data")
        os.chmod(f, 0o644)
        a = agent(prod)
        repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "STATUS.md")
        r = out(prod.stack.agents.run_session(a.agent_id, program(prod, "rd.py", READ_WORLD_READABLE), args=[f, repo, str(tmp_path / "data")], timeout_s=20))
        assert r["world_file"].startswith("READ:world-reada")            # HƏD: fayl sistemi izolyasiyası YOXDUR
        # (depo mənbəyinin oxunması mühitdən asılıdır: runner-də ev qovluğu 0750 ola bilər; ona görə burada İDDİA EDİLMİR)
        assert r["server_cmdline"].startswith("READ:")                  # HƏD: /proc/<server>/cmdline oxunur (gizli məlumat komanda sətrinə YAZILMAMALIDIR)
        assert r["server_environ"].startswith("DENIED")                 # qorunur: server mühiti (açarlar)
        assert r["data_dir"].startswith("DENIED")                       # qorunur: server məlumat qovluğu (0700)
    finally:
        shutil.rmtree(shared, ignore_errors=True)
        prod.stack.agents.close()
