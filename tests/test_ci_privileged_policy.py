"""CI siyasəti: `NAYMERA_REQUIRE_PRIVILEGED=1` ilə izolyasiya testləri səssizcə atlana bilməz (conftest.py)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def run(extra_env, *args):
    env = {**os.environ, **extra_env}
    env.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)


def test_require_privileged_mode_refuses_to_run_without_root_instead_of_skipping():
    res = run({"NAYMERA_REQUIRE_PRIVILEGED": "1"}, "--co", "tests/test_agent_isolation.py")
    if IS_ROOT:
        assert res.returncode == 0, res.stdout[-400:] + res.stderr[-400:]                   # root: toplama işləyir
    else:
        assert res.returncode == 2 and "root tələb edir" in (res.stdout + res.stderr)       # root deyil: DAYANIR (atlama yox)


def test_default_mode_prints_an_explicit_warning_when_isolation_tests_are_skipped():
    if IS_ROOT:
        return                                                                               # root-da atlanma olmur: xəbərdarlıq mənasızdır
    res = run({"NAYMERA_REQUIRE_PRIVILEGED": ""}, "tests/test_agent_isolation.py", "-k", "reach or import_repo")
    assert "izolyasiya testi ATLANDI" in res.stdout and "YOXLANMAYIB" in res.stdout, res.stdout[-600:]


def test_ci_workflow_runs_a_separate_privileged_job_with_the_require_flag():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "isolation-privileged" in text and "run_privileged_isolation_tests.sh" in text
    script = (ROOT / "tools" / "run_privileged_isolation_tests.sh").read_text(encoding="utf-8")
    assert "NAYMERA_REQUIRE_PRIVILEGED=1" in script and "test_agent_isolation.py" in script and "test_agent_isolation_hardening.py" in script
