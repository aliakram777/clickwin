#!/usr/bin/env python3
"""JVM kontrakt sınağı: HƏQİQİ Python backend (mock rejimi, loopback, müvəqqəti məlumat qovluğu) qurur və Kotlin qatını
`gradle test` ilə ona qarşı icra edir. APK build DEYİL. Heç bir real açar/hesab/ödəniş yoxdur; yalnız Maven Central lazımdır."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.bootstrap import build_app  # noqa: E402
from api.server import create_server  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        svc = build_app(tmp)
        codes = [svc.auth.create_pairing_code() for _ in range(14)]
        approvals = [svc.approvals.client.request("fin_agent", "spend_money", 0.5, f"jvm{i}", "JVM kontrakt sınağı",
                                                  {"i": i}).id for i in range(4)]
        httpd = create_server(svc, "127.0.0.1", 0, request_timeout=5)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        env = dict(os.environ, NAYMERA_BACKEND_URL=f"http://127.0.0.1:{httpd.server_address[1]}",
                   NAYMERA_PAIR_CODES=",".join(codes), NAYMERA_APPROVAL_IDS=",".join(approvals))
        try:
            return subprocess.run(["gradle", "--no-daemon", "-p", str(ROOT / "android" / "jvm-contract"), "cleanTest", "test"],
                                  env=env).returncode
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    # SQLite bağlantıları serverin ipliyində istifadə olunur (ardıcıl sorğular): iplik yoxlaması bu sınaq üçün söndürülür.
    import sqlite3
    _real = sqlite3.connect
    sqlite3.connect = lambda *a, **k: _real(*a, **{**k, "check_same_thread": False})  # type: ignore[assignment]
    raise SystemExit(main())
