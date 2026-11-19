"""Bildiriş vəziyyətinin davamlı saxlanması (SQLite): cooldown, limitlər, ack/snooze/dismiss, gündəlik xülasə növbəsi."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from safety.dbconn import connect

DEFAULT_DB = Path("data/notifications.db")
STATUSES = ("active", "acked", "snoozed", "dismissed")


@dataclass(frozen=True)
class KeyState:
    key: str
    status: str
    until: float
    last_importance: str
    last_sent: float
    sent_count: int


class NotificationStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect(str(db_path), isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        for stmt in (
            "CREATE TABLE IF NOT EXISTS notif_state (key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', "
            "until REAL NOT NULL DEFAULT 0, last_importance TEXT NOT NULL DEFAULT 'low', last_sent REAL NOT NULL "
            "DEFAULT 0, sent_count INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS notif_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
            "key TEXT NOT NULL, importance TEXT NOT NULL, counted INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS digest_queue (key TEXT PRIMARY KEY, importance TEXT NOT NULL, "
            "title TEXT NOT NULL, ts REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS digest_state (id INTEGER PRIMARY KEY CHECK (id = 1), last_digest REAL NOT NULL)",
        ):
            self._conn.execute(stmt)

    def close(self) -> None:
        self._conn.close()

    def state(self, key: str) -> KeyState | None:
        r = self._conn.execute("SELECT * FROM notif_state WHERE key = ?", (key,)).fetchone()
        return KeyState(r["key"], r["status"], r["until"], r["last_importance"], r["last_sent"],
                        r["sent_count"]) if r else None

    def record_sent(self, key: str, importance: str, ts: float, counted: bool = True) -> None:
        self._conn.execute("INSERT INTO notif_log (ts, key, importance, counted) VALUES (?, ?, ?, ?)",
                           (ts, key, importance, int(counted)))
        self._conn.execute(
            "INSERT INTO notif_state (key, status, until, last_importance, last_sent, sent_count) "
            "VALUES (?, 'active', 0, ?, ?, 1) ON CONFLICT(key) DO UPDATE SET status = 'active', until = 0, "
            "last_importance = excluded.last_importance, last_sent = excluded.last_sent, "
            "sent_count = sent_count + 1", (key, importance, ts))

    def sent_count(self, since: float, critical: bool) -> int:
        op = "=" if critical else "!="
        return int(self._conn.execute(
            f"SELECT COUNT(*) FROM notif_log WHERE ts >= ? AND counted = 1 AND importance {op} 'critical'",
            (since,)).fetchone()[0])

    def mark(self, key: str, status: str, until: float = 0.0) -> None:
        if status not in STATUSES:
            raise ValueError("status yanlışdır")
        self._conn.execute(
            "INSERT INTO notif_state (key, status, until) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "status = excluded.status, until = excluded.until", (key, status, until))

    # --- gündəlik xülasə ---
    def queue_digest(self, key: str, importance: str, title: str, ts: float) -> None:
        self._conn.execute("INSERT OR REPLACE INTO digest_queue (key, importance, title, ts) VALUES (?, ?, ?, ?)",
                           (key, importance, title, ts))

    def digest_items(self, limit: int = 5) -> list[tuple[str, str, str]]:
        rows = self._conn.execute("SELECT key, importance, title FROM digest_queue ORDER BY ts, key LIMIT ?",
                                  (limit,)).fetchall()
        return [(r["key"], r["importance"], r["title"]) for r in rows]

    def clear_digest(self) -> None:
        self._conn.execute("DELETE FROM digest_queue")

    def last_digest(self) -> float:
        r = self._conn.execute("SELECT last_digest FROM digest_state WHERE id = 1").fetchone()
        return float(r["last_digest"]) if r else 0.0

    def set_last_digest(self, ts: float) -> None:
        self._conn.execute("INSERT INTO digest_state (id, last_digest) VALUES (1, ?) ON CONFLICT(id) DO UPDATE "
                           "SET last_digest = excluded.last_digest", (ts,))
