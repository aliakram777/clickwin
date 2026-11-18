"""Davamlı xərc kitabı (SQLite): rezerv / təsdiq / azad etmə, limitlər, təkrar-qoruma, audit uyğunluğu.

Status: reserved (rezerv tutulub; `unknown` işarəsi = faktiki xərc məlum deyil, rezerv qorunur),
committed (faktiki xərc yazılıb), released (rezerv azad edilib). Limitlər `amount` üzərində
hesablanır: reserved -> rezerv məbləği, committed -> faktiki xərc, released -> 0.
Rezervasiya BEGIN IMMEDIATE tranzaksiyasındadır: paralel bağlantılar limiti aşa bilməz.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

from safety.dbconn import LockedConnection

DEFAULT_LEDGER_PATH = Path("data/ledger.db")
_EPS = 1e-9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS costs (
    op_id       TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    amount      REAL NOT NULL,
    ts          TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'committed',
    reserved    REAL,
    unknown     INTEGER NOT NULL DEFAULT 0
)
"""
_COLS = "op_id, agent, amount, ts, description, status, reserved, unknown"


@dataclass(frozen=True)
class LedgerEntry:
    op_id: str
    agent: str
    amount: float
    ts: str
    description: str
    status: str = "committed"
    reserved: float = 0.0
    unknown: bool = False


@dataclass(frozen=True)
class Reservation:
    status: str  # reserved | duplicate | denied
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "reserved"


@dataclass(frozen=True)
class BudgetPolicy:
    """Orkestratorun tətbiq etdiyi limitlər. Agent limiti yalnız sıxlaşdıra bilər (spec limitindən aşağı)."""
    global_limit: float | None = None
    task_limit: float | None = None
    agent_limits: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in [("global_limit", self.global_limit), ("task_limit", self.task_limit),
                            *[(f"agent_limits[{k}]", v) for k, v in self.agent_limits.items()]]:
            _check_limit(name, value)


class CostLedger:
    def __init__(self, db_path: str | Path = DEFAULT_LEDGER_PATH) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = LockedConnection(str(db_path), isolation_level=None, timeout=30)
        self._conn.execute(_SCHEMA)
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(costs)")}
        if "status" not in cols:  # mərhələ 4 sxemi
            with self._tx():
                self._conn.execute("ALTER TABLE costs ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'")
                self._conn.execute("ALTER TABLE costs ADD COLUMN reserved REAL")
                self._conn.execute("ALTER TABLE costs ADD COLUMN unknown INTEGER NOT NULL DEFAULT 0")
                self._conn.execute("UPDATE costs SET reserved = amount WHERE reserved IS NULL")

    def _tx(self):
        """İpə-təhlükəsiz tranzaksiya: kilid BEGIN-dən COMMIT-ə qədər saxlanır (audit B-5/A-7: ipler arası çarpazlaşma yoxdur)."""
        return self._conn.transaction()

    # --- yazma ---
    def record(self, op_id: str, agent: str, amount: float, description: str = "") -> bool:
        """Dərhal təsdiqlənmiş xərc yazır. Eyni `op_id` təkrar yazılmır (False)."""
        _check_text("op_id", op_id)
        _check_text("agent", agent)
        _check_amount("məbləğ", amount)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO costs (op_id, agent, amount, ts, description, status, reserved, unknown)"
            " VALUES (?, ?, ?, ?, ?, 'committed', ?, 0)",
            (op_id, agent, float(amount), _now(), description, float(amount)),
        )
        return cur.rowcount == 1

    def reserve(
        self,
        op_id: str,
        agent: str,
        amount: float,
        *,
        agent_limit: float | None = None,
        global_limit: float | None = None,
        task_limit: float | None = None,
        group_prefix: str | None = None,
        group_limit: float | None = None,
        description: str = "",
    ) -> Reservation:
        """Limitləri yoxlayıb rezervi atomik tutur. Azad edilmiş `op_id` yenidən rezerv edilə bilər.
        `group_prefix` + `group_limit`: eyni prefiksli `op_id`-lərin (məs. `llm:`) ümumi xərc tavanı (agentdən asılı olmayaraq, eyni tranzaksiyada)."""
        _check_text("op_id", op_id)
        _check_text("agent", agent)
        _check_amount("məbləğ", amount)
        for name, value in (("agent_limit", agent_limit), ("global_limit", global_limit),
                            ("task_limit", task_limit), ("group_limit", group_limit)):
            _check_limit(name, value)
        if group_limit is not None:
            _check_text("group_prefix", group_prefix)
        amount = float(amount)
        with self._tx():
            row = self._conn.execute("SELECT status FROM costs WHERE op_id = ?", (op_id,)).fetchone()
            if row is not None and row[0] != "released":
                return Reservation("duplicate", f"təkrar əməliyyat: {op_id}")
            if task_limit is not None and amount > task_limit + _EPS:
                return Reservation("denied", f"tapşırıq limiti aşılır: {amount:.4f} > {task_limit:.4f}")
            if agent_limit is not None:
                used = self.agent_total(agent)
                if used + amount > agent_limit + _EPS:
                    return Reservation(
                        "denied", f"agent xərc limiti aşılır: {used + amount:.4f} > {agent_limit:.4f}")
            if group_limit is not None and group_prefix and op_id.startswith(group_prefix):
                used = float(self._conn.execute(
                    "SELECT COALESCE(SUM(amount), 0.0) FROM costs WHERE substr(op_id, 1, ?) = ?",
                    (len(group_prefix), group_prefix)).fetchone()[0])
                if used + amount > group_limit + _EPS:
                    return Reservation(
                        "denied", f"qrup xərc limiti aşılır: {used + amount:.4f} > {group_limit:.4f}")
            if global_limit is not None:
                used = self.total()
                if used + amount > global_limit + _EPS:
                    return Reservation(
                        "denied", f"ümumi xərc limiti aşılır: {used + amount:.4f} > {global_limit:.4f}")
            if row is None:
                self._conn.execute(
                    "INSERT INTO costs (op_id, agent, amount, ts, description, status, reserved, unknown)"
                    " VALUES (?, ?, ?, ?, ?, 'reserved', ?, 0)",
                    (op_id, agent, amount, _now(), description, amount),
                )
            else:
                self._conn.execute(
                    "UPDATE costs SET agent = ?, amount = ?, ts = ?, description = ?, status = 'reserved',"
                    " reserved = ?, unknown = 0 WHERE op_id = ?",
                    (agent, amount, _now(), description, amount, op_id),
                )
        return Reservation("reserved")

    def commit(self, op_id: str, actual: float) -> bool:
        """Faktiki xərci yazır (yalnız reserved-dən). İkinci dəfə False."""
        _check_amount("faktiki xərc", actual)
        cur = self._conn.execute(
            "UPDATE costs SET amount = ?, status = 'committed', unknown = 0"
            " WHERE op_id = ? AND status = 'reserved'", (float(actual), op_id))
        return cur.rowcount == 1

    def release(self, op_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE costs SET amount = 0, status = 'released', unknown = 0"
            " WHERE op_id = ? AND status = 'reserved'", (op_id,))
        return cur.rowcount == 1

    def mark_unknown(self, op_id: str) -> bool:
        """Faktiki xərc naməlumdur: rezerv qorunur və işarələnir."""
        cur = self._conn.execute(
            "UPDATE costs SET unknown = 1 WHERE op_id = ? AND status = 'reserved'", (op_id,))
        return cur.rowcount == 1

    def stale_reserved(self, older_than_s: float, now: datetime | None = None) -> list[LedgerEntry]:
        """`older_than_s`-dən köhnə, hələ nə təsdiqlənmiş, nə azad edilmiş, nə də naməlum işarələnmiş rezervlər (sızma namizədləri)."""
        cutoff = (now or datetime.now(timezone.utc)).timestamp() - float(older_than_s)
        out = []
        for e in self.entries():
            if e.status == "reserved" and not e.unknown:
                try:
                    if datetime.fromisoformat(e.ts).timestamp() <= cutoff:
                        out.append(e)
                except ValueError:
                    out.append(e)      # oxunmayan vaxt damğası: sızma sayılır (qapalı qalır)
        return out

    def expire_stale(self, older_than_s: float, now: datetime | None = None) -> list[str]:
        """Köhnə rezervləri AZAD ETMİR: naməlum işarələyir (məbləğ qorunur, sahib uzlaşdırması tələb olunur). İşarələnən op_id-ləri qaytarır."""
        ids = [e.op_id for e in self.stale_reserved(older_than_s, now)]
        return [i for i in ids if self.mark_unknown(i)]

    def resolve_unknown(self, op_id: str, actual: float | None, resolved_by: str) -> bool:
        """SAHİB uzlaşdırması (yalnız `unknown` rezerv üçün): `actual` verilibsə faktiki xərclə təsdiqlənir, `None` isə xərc olmadığı təsdiqlənərək azad edilir.
        Naməlum olmayan və ya olmayan op_id üçün False."""
        _check_text("resolved_by", resolved_by)
        with self._tx():
            row = self._conn.execute("SELECT 1 FROM costs WHERE op_id = ? AND status = 'reserved' AND unknown = 1", (op_id,)).fetchone()
            if row is None:
                return False
            if actual is None:
                self._conn.execute("UPDATE costs SET amount = 0, status = 'released', unknown = 0, description = description || ? WHERE op_id = ?",
                                   (f" [uzlaşdırıldı:{resolved_by}:azad]", op_id))
            else:
                _check_amount("faktiki xərc", actual)
                self._conn.execute("UPDATE costs SET amount = ?, status = 'committed', unknown = 0, description = description || ? WHERE op_id = ?",
                                   (float(actual), f" [uzlaşdırıldı:{resolved_by}]", op_id))
        return True

    # --- oxuma ---
    def has(self, op_id: str) -> bool:
        """Aktiv (reserved/committed) əməliyyat varmı; released sayılmır."""
        row = self._conn.execute(
            "SELECT 1 FROM costs WHERE op_id = ? AND status != 'released'", (op_id,)).fetchone()
        return row is not None

    def agent_total(self, agent: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM costs WHERE agent = ?", (agent,)).fetchone()
        return float(row[0])

    def total(self) -> float:
        return float(self._conn.execute("SELECT COALESCE(SUM(amount), 0.0) FROM costs").fetchone()[0])

    def reserved_total(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM costs WHERE status = 'reserved'").fetchone()
        return float(row[0])

    def by_agent(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT agent, SUM(amount) FROM costs WHERE status != 'released' GROUP BY agent ORDER BY agent"
        ).fetchall()
        return {a: float(s) for a, s in rows}

    def entries(self, agent: str | None = None) -> list[LedgerEntry]:
        sql = f"SELECT {_COLS} FROM costs"
        params: tuple = ()
        if agent is not None:
            sql += " WHERE agent = ?"
            params = (agent,)
        rows = self._conn.execute(sql + " ORDER BY ts, rowid", params).fetchall()
        return [_entry(r) for r in rows]

    def unknown_entries(self) -> list[LedgerEntry]:
        return [e for e in self.entries() if e.status == "reserved" and e.unknown]


@dataclass
class ReconcileReport:
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def reconcile(ledger: CostLedger, audit_path: str | Path) -> ReconcileReport:
    """Gate-in icazə verdiyi audit sətirlərini (say, rezerv məbləği) azad edilməmiş kitab sətirləri ilə
    agent üzrə müqayisə edir; faktiki xərcin rezervi aşdığı halları da bildirir."""
    rep = ReconcileReport()
    audit: dict[str, list[float]] = {}  # agent -> [say, cəm]
    path = Path(audit_path)
    if path.exists():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if "event" in row:  # təsdiq xidmətinin hadisələri (qərar sətri deyil)
                    continue
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
        if e.status == "released":
            continue
        acc = led.setdefault(e.agent, [0, 0.0])
        acc[0] += 1
        acc[1] += e.reserved
        if e.status == "committed" and e.amount > e.reserved + _EPS:
            rep.mismatches.append(
                f"{e.agent}: {e.op_id} faktiki xərc rezervi aşıb ({e.amount:.6f} > {e.reserved:.6f})")

    for agent in sorted(set(audit) | set(led)):
        a_n, a_sum = audit.get(agent, [0, 0.0])
        l_n, l_sum = led.get(agent, [0, 0.0])
        if a_n != l_n:
            rep.mismatches.append(f"{agent}: audit-də {a_n} əməliyyat, kitabda {l_n}")
        elif abs(a_sum - l_sum) > _EPS:
            rep.mismatches.append(f"{agent}: audit məbləği {a_sum:.6f}, kitab {l_sum:.6f}")
    return rep


def log_denial(audit_path: str | Path, agent: str, permission: str, cost: float,
               description: str, reason: str, source: str) -> None:
    """Gate-ə çatmayan rədd qərarını (büdcə, stop) audit jurnalına yazır. `allowed: false` olduğu üçün
    reconcile() onu saymır."""
    row = {
        "ts": _now(), "agent": agent, "permission": permission, "cost": cost,
        "description": description, "allowed": False, "reason": reason, "source": source,
    }
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def log_budget_denial(audit_path: str | Path, agent: str, permission: str, cost: float,
                      description: str, reason: str) -> None:
    log_denial(audit_path, agent, permission, cost, description, reason, "budget")


def _entry(r: tuple) -> LedgerEntry:
    return LedgerEntry(r[0], r[1], float(r[2]), r[3], r[4], r[5],
                       float(r[2] if r[6] is None else r[6]), bool(r[7]))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} boş olmayan sətir olmalıdır")


def _check_amount(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} sonlu və mənfi olmayan rəqəm olmalıdır")


def _check_limit(name: str, value: object) -> None:
    if value is not None:
        _check_amount(name, value)
