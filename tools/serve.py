#!/usr/bin/env python3
"""Serveri mühit dəyişənlərindən qurulmuş mühərriklərlə işə salır (mühit dəyişənləri YALNIZ bu skriptdə/`runtime/`-də oxunur).
Heç bir açar verilməyibsə Cloud/Local `not_configured` qalır. İstifadə: `python3 tools/serve.py [--data-dir data/api] [--port 8765]`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.bootstrap import build_app  # noqa: E402
from api.server import DEFAULT_HOST, DEFAULT_PORT, create_server  # noqa: E402
from runtime.env import build_engines  # noqa: E402
from safety.ledger import CostLedger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/api")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--tls-cert")
    ap.add_argument("--tls-key")
    a = ap.parse_args()
    d = Path(a.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    engines = build_engines(os.environ, log_dir=d, stop_path=d / "STOP", ledger=CostLedger(d / "llm_ledger.db"))
    for note in engines.notes:
        print(note)
    svc = build_app(d, https=bool(a.tls_cert), cloud_engine=engines.cloud, local_engine=engines.local)
    httpd = create_server(svc, a.host, a.port, a.tls_cert, a.tls_key)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
