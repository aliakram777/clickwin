"""Scheduler (Mərhələ 18.8): sahibin yaratdığı dövri işlər. Yalnız QEYDLİ, dar iş adları (məs. `loop.tick`, `memory.purge`, `audit.mirror`, `watchdog.check`, `backup.run`); ixtiyari kod/komanda YOXDUR.
`loop.tick` yenə L1 təsdiq/Broker zəncirindən keçir: scheduler heç bir təsdiqi əvəz etmir. STOP bayrağı üstündür (STOP-da heç bir iş işləmir, vəziyyət dəyişmir).
Qaydalar: ardıcıl uğursuzluq ≥ `max_failures` ⇒ iş avtomatik dayandırılır (yalnız sahib yenidən aktiv edir); buraxılmış işlər üçün «yığılma» yoxdur (növbəti vaxt indidən hesablanır); bir çağırışda ≤10 iş.
HƏDD: iş eyni prosesdə, eyni ipdə işləyir (vaxt aşımı məcbur edilmir); dəqiq vaxt (cron) yoxdur, yalnız interval."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Callable, Mapping

from kernel.common import ContractError, SecretDetected, clean_text
from safety.dbconn import LockedConnection

_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,47}$")
_JOB = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,47}$")
MIN_EVERY_S, MAX_EVERY_S, MAX_PER_RUN = 1.0, 30 * 86400.0, 10
_DDL = """CREATE TABLE IF NOT EXISTS schedules (name TEXT PRIMARY KEY, job TEXT NOT NULL, every_s REAL NOT NULL, next_run REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    failures INTEGER NOT NULL DEFAULT 0, last_run REAL, last_status TEXT NOT NULL DEFAULT '')"""


class Scheduler:
    def __init__(self, conn: LockedConnection, jobs: Mapping[str, Callable[[], str]], stop: Callable[[], bool], clock: Callable[[], datetime] | None = None, max_failures: int = 5) -> None:
        if not callable(stop) or not all(callable(f) and _JOB.match(n) for n, f in jobs.items()):
            raise ValueError("jobs: {ad: çağırıla bilən}, stop çağırıla bilən olmalıdır")
        self._db, self._jobs, self._stop, self._lock = conn, dict(jobs), stop, threading.Lock()
        self._clock, self._max_failures = clock or (lambda: datetime.now(timezone.utc)), max(1, int(max_failures))
        with conn.transaction():
            conn.execute(_DDL)

    def _now(self) -> float:
        return self._clock().astimezone(timezone.utc).timestamp()

    # --- sahib tərəfi
    def add(self, name: str, job: str, every_s: float, *, start_in_s: float | None = None) -> None:
        if not _NAME.match(name or "") or job not in self._jobs:
            raise ContractError("schedule: yanlış ad və ya qeydsiz iş adı (yalnız qeydli işlər)", "bad_schedule")
        if isinstance(every_s, bool) or not isinstance(every_s, (int, float)) or not MIN_EVERY_S <= every_s <= MAX_EVERY_S:
            raise ContractError("every_s 1..2592000 olmalıdır", "bad_interval")
        with self._lock, self._db.transaction():
            if self._db.execute("SELECT 1 FROM schedules WHERE name = ?", (name,)).fetchone():
                raise ContractError("schedule artıq mövcuddur", "duplicate")
            self._db.execute("INSERT INTO schedules (name, job, every_s, next_run) VALUES (?,?,?,?)", (name, job, float(every_s), self._now() + (float(every_s) if start_in_s is None else float(start_in_s))))

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock, self._db.transaction():
            cur = self._db.execute("UPDATE schedules SET enabled = ?, failures = CASE WHEN ? THEN 0 ELSE failures END WHERE name = ?", (1 if enabled else 0, 1 if enabled else 0, name))
            return cur.rowcount == 1

    def list(self) -> list[dict]:
        rows = self._db.execute("SELECT name, job, every_s, next_run, enabled, failures, last_run, last_status FROM schedules ORDER BY name").fetchall()
        return [dict(zip(("name", "job", "every_s", "next_run", "enabled", "failures", "last_run", "last_status"), r)) for r in rows]

    # --- icra tərəfi
    def run_due(self) -> list[tuple[str, str]]:
        """STOP-da heç nə etmir. Vaxtı çatmış işləri (≤10) icra edir; qaytarır: [(schedule_adı, status)]."""
        if self._stop():
            return []
        now, out = self._now(), []
        with self._lock:
            due = self._db.execute("SELECT name, job, every_s, failures FROM schedules WHERE enabled = 1 AND next_run <= ? ORDER BY next_run, name LIMIT ?", (now, MAX_PER_RUN)).fetchall()
        for name, job, every_s, failures in due:
            if self._stop():                                   # işlər arasında STOP
                break
            try:
                status, failed = clean_text("status", str(self._jobs[job]())[:100], max_len=100), False
            except SecretDetected:
                status, failed = "ok (nəticə redaktə olundu)", False
            except Exception as exc:                           # noqa: BLE001
                status, failed = f"error:{type(exc).__name__}", True
            except BaseException:
                raise
            nf = failures + 1 if failed else 0
            with self._lock, self._db.transaction():
                self._db.execute("UPDATE schedules SET next_run = ?, last_run = ?, last_status = ?, failures = ?, enabled = ? WHERE name = ?",
                                 (self._now() + every_s, now, status, nf, 0 if nf >= self._max_failures else 1, name))
            out.append((name, status))
        return out
