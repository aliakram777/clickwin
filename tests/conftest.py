"""Test siyasəti: root tələb edən izolyasiya testləri SƏSSİZCƏ atlanıb "izolyasiya yoxlanıb" kimi təqdim olunmasın.

- Adi (root olmayan) icra: root-xüsusi testlər atlanır, lakin xülasədə açıq xəbərdarlıq çıxır ("izolyasiya YOXLANMAYIB").
- `NAYMERA_REQUIRE_PRIVILEGED=1` (CI-nın `isolation-privileged` işi, `tools/run_privileged_isolation_tests.sh`): root deyilsə sessiya DƏRHAL dayanır (kod 2); root ilə isə
  `tests/test_agent_isolation*.py`-dəki HƏR atlanma sessiyanı uğursuz edir. Beləliklə bu rejimdə "yaşıl" = izolyasiya testləri həqiqətən işləyib.
Bu fayl heç bir təhlükəsizlik iddiasını zəiflətmir və testləri dəyişmir."""
from __future__ import annotations

import os

import pytest

REQUIRE = os.environ.get("NAYMERA_REQUIRE_PRIVILEGED") == "1"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_skipped_isolation: list[str] = []
_skipped_other: list[tuple[str, str]] = []


def pytest_sessionstart(session: pytest.Session) -> None:
    if REQUIRE and not IS_ROOT:
        pytest.exit("NAYMERA_REQUIRE_PRIVILEGED=1 root tələb edir (sudo ilə işə salın): izolyasiya testləri atlana bilməz", returncode=2)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        reason = str(report.longrepr[-1]) if isinstance(report.longrepr, tuple) else ""
        if "test_agent_isolation" in report.nodeid or "test_isolation_limits" in report.nodeid:
            _skipped_isolation.append(report.nodeid)
        else:
            _skipped_other.append((report.nodeid, reason))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if REQUIRE and _skipped_isolation and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # noqa: ANN001
    tr = terminalreporter
    if _skipped_isolation:
        level = "XƏTA" if REQUIRE else "XƏBƏRDARLIQ"
        tr.write_sep("=", f"{level}: {len(_skipped_isolation)} izolyasiya testi ATLANDI — agent izolyasiyası bu icrada YOXLANMAYIB", red=REQUIRE, yellow=not REQUIRE)
        tr.write_line("İzolyasiya (unikal uid, şəbəkə namespace-i, seccomp) yalnız root ilə yoxlanır: `tools/run_privileged_isolation_tests.sh` və ya CI-nın `isolation-privileged` işi.")
    if _skipped_other:
        tr.write_line(f"Digər atlanan testlər: {len(_skipped_other)} (səbəblər üçün `-rs`).")
