"""Hash-zəncirli audit (Mərhələ 18.8): append-only SQLite; hər sətrin hash-i əvvəlkinin hash-inə bağlıdır. Dəyişdirmə, silmə, sıra pozulması `verify` ilə aşkarlanır; UPDATE/DELETE trigger ilə də bloklanır.

DÜRÜST HƏDD: DB faylına yazma imkanı olan tərəf BÜTÜN zənciri yenidən hesablaya bilər. Qorunma yalnız `anchor()` (seq + baş hash) sistemdən KƏNARDA (sahib cihazı/oflayn) saxlanıb `verify_anchor` ilə
yoxlanılanda tamdır. Xarici anker imzalama bu modulda YOXDUR (sahib açarı ilə, sonrakı iş). Sirr bənzər məzmun redaksiya olunur (hadisə düşmür, dəyər yazılmır)."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from kernel.common import ContractError, SecretDetected, canonical_json, clean_text, fmt_dt
from safety.dbconn import LockedConnection

GENESIS = "0" * 64
MAX_DETAIL = 2000
_DDL = """
CREATE TABLE IF NOT EXISTS chain (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL, detail TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mirror_state (source TEXT PRIMARY KEY, last_seq INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS chain_no_update BEFORE UPDATE ON chain BEGIN SELECT RAISE(ABORT, 'audit chain is append-only'); END;
CREATE TRIGGER IF NOT EXISTS chain_no_delete BEFORE DELETE ON chain BEGIN SELECT RAISE(ABORT, 'audit chain is append-only'); END;
"""


def _hash(seq: int, ts: str, kind: str, source: str, detail: str, prev: str) -> str:
    return hashlib.sha256("|".join([str(seq), ts, kind, source, detail, prev]).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Anchor:
    seq: int
    head_hash: str
    ts: str


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    count: int
    broken_at: int = 0
    reason: str = ""


class AuditChain:
    def __init__(self, conn: LockedConnection, clock: Callable[[], datetime] | None = None) -> None:
        self._db, self._clock, self._lock = conn, clock or (lambda: datetime.now(timezone.utc)), threading.Lock()
        with conn.transaction():
            for stmt in [s.strip() for s in _DDL.split(";\n") if s.strip()]:
                conn.execute(stmt if not stmt.upper().startswith("CREATE TRIGGER") else stmt + ";")

    def append(self, kind: str, source: str, detail: dict[str, Any] | None = None) -> int:
        kind = clean_text("kind", kind, max_len=60, min_len=1)
        source = clean_text("source", source, max_len=60, min_len=1)
        text = canonical_json(detail or {})
        try:
            clean_text("detail", text, max_len=MAX_DETAIL)
        except SecretDetected:
            text = canonical_json({"redacted": "secret_like_content"})
        except ContractError:
            text = canonical_json({"redacted": "invalid_or_too_long", "sha256": hashlib.sha256(text.encode()).hexdigest()})
        ts = fmt_dt(self._clock().astimezone(timezone.utc))
        with self._lock, self._db.transaction():
            row = self._db.execute("SELECT seq, hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (int(row[0]) + 1, row[1]) if row else (1, GENESIS)
            self._db.execute("INSERT INTO chain (seq, ts, kind, source, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)", (seq, ts, kind, source, text, prev, _hash(seq, ts, kind, source, text, prev)))
            return seq

    def verify(self) -> VerifyReport:
        prev, expected, n = GENESIS, 1, 0
        for seq, ts, kind, source, detail, p, h in self._db.execute("SELECT seq, ts, kind, source, detail, prev_hash, hash FROM chain ORDER BY seq").fetchall():
            if seq != expected:
                return VerifyReport(False, n, expected, "missing_or_reordered_row")
            if p != prev or h != _hash(seq, ts, kind, source, detail, p):
                return VerifyReport(False, n, seq, "hash_mismatch")
            prev, expected, n = h, expected + 1, n + 1
        return VerifyReport(True, n)

    def anchor(self) -> Anchor | None:
        row = self._db.execute("SELECT seq, hash, ts FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        return Anchor(int(row[0]), row[1], row[2]) if row else None

    def verify_anchor(self, anchor: Anchor) -> bool:
        """Sistemdən kənarda saxlanmış ankerlə müqayisə: həmin seq-də hash eynidirsə və zəncir bütövdürsə True (silinmə/yenidən yazma aşkarlanır)."""
        row = self._db.execute("SELECT hash FROM chain WHERE seq = ?", (anchor.seq,)).fetchone()
        return bool(row) and row[0] == anchor.head_hash and self.verify().ok

    def entries(self, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT seq, ts, kind, source, detail, hash FROM chain WHERE seq > ? ORDER BY seq LIMIT ?", (max(0, int(after_seq)), max(1, min(int(limit), 1000)))).fetchall()
        return [{"seq": r[0], "ts": r[1], "kind": r[2], "source": r[3], "detail": json.loads(r[4]), "hash": r[5]} for r in rows]

    def _cursor(self, source: str) -> int:
        row = self._db.execute("SELECT last_seq FROM mirror_state WHERE source = ?", (source,)).fetchone()
        return int(row[0]) if row else 0


class AuditMirror:
    """Kernel Loop jurnalını (append-only `journal`) zəncirə köçürür; kursor davamlıdır, təkrar köçürmə yoxdur. Yalnız metadata (id/vəziyyət/səbəb); icraçı çıxışı jurnalda zatən yoxdur."""

    def __init__(self, chain: AuditChain, journal_reader, source: str = "core_journal") -> None:
        self._chain, self._reader, self._source = chain, journal_reader, source

    def sync(self, limit: int = 500) -> int:
        n = 0
        with self._chain._lock:                                  # kursor və əlavə eyni anda
            cursor = self._chain._cursor(self._source)
        entries = self._reader.journal_entries(None, limit, cursor)
        for e in entries:
            self._chain.append(e["kind"], self._source, {"seq": e["seq"], "goal": e["goal_id"], "plan": e["plan_id"], "step": e["step_id"], **{k: v for k, v in e["detail"].items() if k in ("action", "state", "to_status", "from_status", "reason", "attempt")}})
            with self._chain._lock, self._chain._db.transaction():
                self._chain._db.execute("INSERT INTO mirror_state (source, last_seq) VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET last_seq = excluded.last_seq", (self._source, e["seq"]))
            n += 1
        return n
