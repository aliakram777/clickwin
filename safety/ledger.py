"""Davamlı xərc kitabı (SQLite): agent üzrə xərc, təkrar-qoruma, audit ilə uyğunluq."""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER_PATH = Path("data/ledger.db")
_EPS = 1e-9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS costs (
    op_id       TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    amount      REAL NOT NULL,
    ts          TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
)
"""


@dataclass(frozen=True)
class LedgerEntry:
    op_id: str
    agent: str
    amount: float
    ts: str
    description: str


class CostLedger:
    def __init__(self, db_path: str | Path = DEFAULT_LEDGER_PATH) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record(self, op_id: str, agent: str, amount: float, description: str = "") -> bool:
        """Xərci yazır. Eyni `op_id` ikinci dəfə yazılmır: təkrar üçün False qaytarır."""
        _check_text("op_id", op_id)
        _check_text("agent", agent)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
                or not math.isfinite(amount) or amount < 0:
            raise ValueError("məbləğ sonlu və mənfi olmayan rəqəm olmalıdır")
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO costs (op_id, agent, amount, ts, description)"
            " VALUES (?, ?, ?, ?, ?)",
            (op_id, agent, float(amount), datetime.now(timezone.utc).isoformat(), description),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def has(self, op_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM costs WHERE op_id = ?", (op_id,)).fetchone()
        return row is not None

    def agent_total(self, agent: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM costs WHERE agent = ?", (agent,)
        ).fetchone()
        return float(row[0])

    def total(self) -> float:
        return float(self._conn.execute("SELECT COALESCE(SUM(amount), 0.0) FROM costs").fetchone()[0])

    def by_agent(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT agent, SUM(amount) FROM costs GROUP BY agent ORDER BY agent"
        ).fetchall()
        return {a: float(s) for a, s in rows}

    def entries(self, agent: str | None = None) -> list[LedgerEntry]:
        sql = "SELECT op_id, agent, amount, ts, description FROM costs"
        params: tuple = ()
        if agent is not None:
            sql += " WHERE agent = ?"
            params = (agent,)
        rows = self._conn.execute(sql + " ORDER BY ts, rowid", params).fetchall()
        return [LedgerEntry(*r) for r in rows]


@dataclass
class ReconcileReport:
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def reconcile(ledger: CostLedger, audit_path: str | Path) -> ReconcileReport:
    """İcazə verilmiş audit sətirləri (say və məbləğ) ilə kitabı agent üzrə müqayisə edir."""
    rep = ReconcileReport()
    audit: dict[str, list[float]] = {}  # agent -> [say, cəm]
    path = Path(audit_path)
    if path.exists():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if row["allowed"] is True:
                    cost = row["cost"]
                    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
                        raise ValueError("cost rəqəm deyil")
                    acc = audit.setdefault(row["agent"], [0, 0.0])
                    acc[0] += 1
                    acc[1] += float(cost)
            except (ValueError, KeyError, TypeError):
                rep.mismatches.append(f"audit sətri {n} oxunmur")

    led: dict[str, list[float]] = {}
    for e in ledger.entries():
        acc = led.setdefault(e.agent, [0, 0.0])
        acc[0] += 1
        acc[1] += e.amount

    for agent in sorted(set(audit) | set(led)):
        a_n, a_sum = audit.get(agent, [0, 0.0])
        l_n, l_sum = led.get(agent, [0, 0.0])
        if a_n != l_n:
            rep.mismatches.append(f"{agent}: audit-də {a_n} əməliyyat, kitabda {l_n}")
        elif abs(a_sum - l_sum) > _EPS:
            rep.mismatches.append(f"{agent}: audit məbləği {a_sum:.6f}, kitab {l_sum:.6f}")
    return rep


def _check_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} boş olmayan sətir olmalıdır")
