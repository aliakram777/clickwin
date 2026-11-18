"""Event jurnalı (Mərhələ 18.0): SQLite, yalnız-əlavə (append-only), idempotent. Eyni `op_id` + eyni məzmun = dublikat (yeni sətir yazılmır); eyni `op_id` + fərqli məzmun = ziddiyyət (rədd).
UPDATE/DELETE trigger ilə bloklanır. Oxuma zamanı sənəd yenidən doğrulanır və saxlanmış hash ilə müqayisə olunur (dəyişdirilmiş sətir aşkarlanır). Jurnal heç nə icra etmir/icazə vermir; yalnız qeydədir.
Mənbə kodu `safety/*`-ə yalnız `safety.dbconn.LockedConnection` ilə bağlıdır (iplik-təhlükəsiz SQLite); şəbəkə/env yoxdur."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from kernel.common import ContractError, UnsupportedVersion
from kernel.events import EntityRef, Event
from safety.dbconn import LockedConnection

LOG_SCHEMA_VERSION = 1


class IdempotencyConflict(ContractError):
    def __init__(self, message: str = "op_id artıq FƏRQLİ məzmunla istifadə olunub") -> None:
        super().__init__(message, "idempotency_conflict")


class LogIntegrityError(ContractError):
    def __init__(self, message: str = "jurnal sətri dəyişdirilib və ya pozulub") -> None:
        super().__init__(message, "log_integrity")


@dataclass(frozen=True)
class AppendResult:
    event: Event          # yazılmış (və ya əvvəl yazılmış) hadisə
    seq: int              # jurnal sırası
    duplicate: bool       # True: eyni op_id+məzmun əvvəl yazılıb, yeni sətir yoxdur


_DDL = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    op_id TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_kind, entity_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_corr ON events(correlation_id, seq);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
"""


class EventLog:
    def __init__(self, conn: LockedConnection) -> None:
        self._db = conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > LOG_SCHEMA_VERSION:
            raise UnsupportedVersion("event jurnalı daha yeni sxem versiyasındadır; köhnə kod onu oxumur/yazmır")
        with conn.transaction():
            for stmt in [s.strip() for s in _DDL.split(";\n") if s.strip()]:
                conn.execute(stmt.rstrip(";") if not stmt.upper().startswith("CREATE TRIGGER") else stmt + ";")
            conn.execute(f"PRAGMA user_version = {LOG_SCHEMA_VERSION}")

    def append(self, event: Event) -> AppendResult:
        if not isinstance(event, Event):
            raise ContractError("Event tələb olunur", "bad_type")
        with self._db.transaction():
            row = self._db.execute("SELECT seq, content_hash, document FROM events WHERE op_id = ?", (event.op_id,)).fetchone()
            if row is not None:
                if row[1] != event.content_hash:
                    raise IdempotencyConflict()
                return AppendResult(self._load(row[0], row[1], row[2]), int(row[0]), True)
            if self._db.execute("SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)).fetchone() is not None:
                raise IdempotencyConflict("event_id artıq başqa op_id ilə istifadə olunub")
            try:
                cur = self._db.execute(
                    "INSERT INTO events (event_id, op_id, content_hash, event_type, entity_kind, entity_id, correlation_id, timestamp, document) VALUES (?,?,?,?,?,?,?,?,?)",
                    (event.event_id, event.op_id, event.content_hash, event.event_type, event.entity.kind.value, event.entity.entity_id, event.correlation_id,
                     event.to_dict()["timestamp"], event.to_json()))
            except sqlite3.IntegrityError as exc:                        # yarış: başqa yazıcı eyni anda daxil edib
                raise IdempotencyConflict("op_id/event_id yarışı: təkrar yoxlayın") from exc
            return AppendResult(event, int(cur.lastrowid), False)

    def _load(self, seq: int, content_hash: str, document: str) -> Event:
        try:
            ev = Event.from_json(document)
        except ContractError as exc:
            raise LogIntegrityError() from exc
        if ev.content_hash != content_hash:
            raise LogIntegrityError()
        return ev

    def get_by_op_id(self, op_id: str) -> Event | None:
        row = self._db.execute("SELECT seq, content_hash, document FROM events WHERE op_id = ?", (op_id,)).fetchone()
        return self._load(*row) if row else None

    def for_entity(self, entity: EntityRef, *, limit: int = 100) -> list[Event]:
        return self._query("WHERE entity_kind = ? AND entity_id = ?", (entity.kind.value, entity.entity_id), limit)

    def for_correlation(self, correlation_id: str, *, limit: int = 100) -> list[Event]:
        return self._query("WHERE correlation_id = ?", (correlation_id,), limit)

    def after(self, cursor: int = 0, *, limit: int = 100) -> list[tuple[int, Event]]:
        """Kursor (seq) əsaslı oxuma: yalnız `cursor`-dan sonrakı hadisələr, artan sıra ilə."""
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ContractError("cursor: mənfi olmayan tam ədəd tələb olunur", "bad_cursor")
        lim = max(1, min(int(limit), 500))
        rows = self._db.execute("SELECT seq, content_hash, document FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, lim)).fetchall()
        return [(int(r[0]), self._load(r[0], r[1], r[2])) for r in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def _query(self, where: str, params: tuple[Any, ...], limit: int) -> list[Event]:
        lim = max(1, min(int(limit), 500))
        rows = self._db.execute(f"SELECT seq, content_hash, document FROM events {where} ORDER BY seq LIMIT ?", (*params, lim)).fetchall()   # where sabit mətndir, dəyərlər parametrdir
        return [self._load(r[0], r[1], r[2]) for r in rows]
