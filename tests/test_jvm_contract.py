"""Opt-in (NAYMERA_RUN_JVM=1): Kotlin data/security qatını JVM-də KOMPİLYASİYA edir və HƏQİQİ Python backend ilə (loopback) sınayır.
Gradle və Maven Central tələb edir; CI-də defolt söndürülüb. Bu, APK build DEYİL və Android API-lərini (Keystore, BiometricPrompt) yoxlamır."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(os.environ.get("NAYMERA_RUN_JVM") != "1", reason="opt-in: NAYMERA_RUN_JVM=1 (Gradle + Maven Central lazımdır)")
def test_kotlin_core_against_the_real_backend():
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "jvm_contract.py")], capture_output=True, text=True,
                         timeout=900, cwd=ROOT)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    assert "BUILD SUCCESSFUL" in out.stdout + out.stderr
