#!/usr/bin/env python3
"""Compose tip-yoxlaması (APK DEYİL): tətbiqin HƏQİQİ Kotlin/Compose mənbələrini JVM-də Compose Multiplatform 1.7.0 qarşısında kompilyasiya edir
(`android/compose-check`; Android-a məxsus simvollar stub-dadır). Yalnız Maven Central lazımdır; real APK üçün Android SDK + Google Maven (dl.google.com) lazımdır.
İstifadə: python3 tools/compose_check.py   (NAYMERA_GRADLE_OFFLINE=1 ilə `--offline`)"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cmd = ["gradle", *(["--offline"] if os.environ.get("NAYMERA_GRADLE_OFFLINE") else []), "--no-daemon", "-p", str(ROOT / "android" / "compose-check"), "compileKotlin"]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
