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

from api.server import DEFAULT_HOST, DEFAULT_PORT, create_server  # noqa: E402
from runtime.factory_bootstrap import BootstrapError  # noqa: E402
from runtime.production import build_production  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/api")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--global-limit", type=float, default=0.0, help="ümumi xərc tavanı (AZN); 0 = heç bir ödənişli xərc")
    ap.add_argument("--tls-cert")
    ap.add_argument("--tls-key")
    a = ap.parse_args()
    try:
        svc, engines, trust = build_production(a.data_dir, os.environ, global_limit=a.global_limit, https=bool(a.tls_cert))
    except BootstrapError as exc:   # qismən/zəif/yanlış açar: açıq xəta, səssiz davam yoxdur
        print(f"imza konfiqurasiyası rədd edildi: {exc}", file=sys.stderr)
        return 2
    for note in engines.notes:
        print(note)
    print("fabrik/Company imtiyazlı əməliyyatları: " + ("imza açarı yüklənib (qısa ömürlü imzalı etibarnamələr)" if trust else "BAĞLI (NAYMERA_SIGNING_KEY qurulmayıb)"))
    print(f"ümumi xərc tavanı (bütün ödənişli çağırışlar üçün TƏK kitab): {a.global_limit}")
    httpd = create_server(svc, a.host, a.port, a.tls_cert, a.tls_key)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
