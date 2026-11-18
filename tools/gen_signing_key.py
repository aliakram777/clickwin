#!/usr/bin/env python3
"""NAYMERA imza açarı yaradır: 32 təsadüfi bayt (`secrets`), `agents.factory_auth.validate_signing_seed` ilə yoxlanır.

İstifadə:
  python3 tools/gen_signing_key.py --out /etc/naymera/signing.key --key-id k-2026-11     # fayl 0600, `O_EXCL` (üzərinə yazmır); açar ekrana YAZILMIR
  python3 tools/gen_signing_key.py --key-id k-2026-11                                      # açarı stdout-a yazır (yalnız sirr meneceri/boru üçün)
Çıxışda yalnız AÇIQ açar (rotasiya: köhnə açarın `NAYMERA_VERIFY_KEYS`-ə əlavəsi üçün) və lazım olan mühit dəyişənləri göstərilir. Şəxsi açar repoya/loqa/APK-ya düşməməlidir.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.factory_auth import TrustedSigner, decode_signing_seed, generate_signing_seed  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-id", required=True)
    ap.add_argument("--out", help="açar faylı (0600, mövcud fayl üzərinə yazılmır)")
    a = ap.parse_args()
    seed_text = generate_signing_seed()
    signer = TrustedSigner(a.key_id, decode_signing_seed(seed_text))
    import base64
    pub = base64.urlsafe_b64encode(signer.public_key).decode().rstrip("=")
    if a.out:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(seed_text + "\n")
        print(f"şəxsi açar yazıldı: {a.out} (0600). Mühit: NAYMERA_SIGNING_KEY_FILE={a.out} NAYMERA_SIGNING_KEY_ID={a.key_id}", file=sys.stderr)
    else:
        print(seed_text)
        print(f"Mühit: NAYMERA_SIGNING_KEY=<yuxarıdakı> NAYMERA_SIGNING_KEY_ID={a.key_id}", file=sys.stderr)
    print(f"açıq açar (rotasiya üçün NAYMERA_VERIFY_KEYS={a.key_id}=...): {pub}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
