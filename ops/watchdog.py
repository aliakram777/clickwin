"""Watchdog və bərpa (Mərhələ 18.8): heartbeat, sağlamlıq hesabatı (yalnız saylar və bayraqlar), qəza sonrası bərpa. Heç nə icra etmir; bərpa yalnız mövcud təhlükəsiz əməliyyatlardır
(`recover_expired`: bitmiş lease ⇒ UNKNOWN, outbox flush). `UNKNOWN` addımlar avtomatik təkrar EDİLMİR, sahib həllini gözləyir."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from safety.dbconn import LockedConnection


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    issues: tuple[str, ...]
    details: dict


class Watchdog:
    def __init__(self, conn: LockedConnection, store, *, stop: Callable[[], bool], clock: Callable[[], datetime] | None = None, max_heartbeat_age_s: float = 120.0,
                 disk_path: str | Path | None = None, min_free_mb: int = 100, outbox_max: int = 1000, audit=None) -> None:
        self._db, self._store, self._stop = conn, store, stop
        self._clock, self._max_age, self._disk, self._min_free, self._outbox_max, self._audit = clock or (lambda: datetime.now(timezone.utc)), max_heartbeat_age_s, disk_path, min_free_mb, outbox_max, audit
        with conn.transaction():
            conn.execute("CREATE TABLE IF NOT EXISTS heartbeat (name TEXT PRIMARY KEY, ts REAL NOT NULL)")

    def _now(self) -> float:
        return self._clock().astimezone(timezone.utc).timestamp()

    def beat(self, name: str = "kernel_loop") -> None:
        with self._db.transaction():
            self._db.execute("INSERT INTO heartbeat (name, ts) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET ts = excluded.ts", (name, self._now()))

    def check(self, name: str = "kernel_loop") -> HealthReport:
        issues, details = [], {}
        row = self._db.execute("SELECT ts FROM heartbeat WHERE name = ?", (name,)).fetchone()
        age = None if row is None else self._now() - float(row[0])
        details["heartbeat_age_s"] = age
        if age is None:
            issues.append("no_heartbeat")
        elif age > self._max_age:
            issues.append("heartbeat_stale")
        counts = self._store.health_counts()
        details["steps"] = counts
        if counts.get("unknown", 0):
            issues.append("unknown_steps_need_owner")
        if counts.get("outbox_pending", 0) > self._outbox_max:
            issues.append("outbox_backlog")
        details["stop_active"] = bool(self._stop())
        if details["stop_active"]:
            issues.append("stop_active")
        if self._disk is not None:
            free_mb = shutil.disk_usage(self._disk).free // (1024 * 1024)
            details["disk_free_mb"] = int(free_mb)
            if free_mb < self._min_free:
                issues.append("low_disk")
        return HealthReport(not issues, tuple(issues), details)

    def recover(self, event_log=None) -> dict:
        """Qəza sonrası: bitmiş lease-ləri UNKNOWN edir, gözləyən hadisələri kernel jurnalına köçürür. Heç bir addımı yenidən İCRA ETMİR."""
        expired = self._store.recover_expired()
        flushed = self._store.flush_outbox(event_log) if event_log is not None else 0
        if self._audit is not None:
            self._audit.append("recovery", "watchdog", {"expired_to_unknown": len(expired), "outbox_flushed": flushed})
        return {"expired_to_unknown": len(expired), "outbox_flushed": flushed}
