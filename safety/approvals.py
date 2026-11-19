"""Təsdiq xidməti: həssas Action-lar (pul, xarici hesab, nəşr) üçün sahibin təsdiqi.

Axın: broker `ApprovalClient.request` ilə pending təsdiq açır -> sahib kanal adapteri
(`ApprovalGateway.approve/deny`) ilə qərar verir -> broker eyni effekti yenidən göndərəndə təsdiq
`consume` ilə BİR DƏFƏ istifadə olunur. Təsdiq unikal ID və effektin SHA-256 hash-inə bağlıdır; hash,
vaxt (expiry), status və qeyd bütövlüyü yoxlanır; payload dəyişərsə təsdiq həmişəlik etibarsızlaşır.

İki üz: broker yalnız `ApprovalClient` (sorğu/yoxlama/istifadə) görür; təsdiq/rədd edən `ApprovalGateway`
yalnız kanal adapterlərinə verilir. Qeyd: bu, proses daxilində rolların ayrılmasıdır, təhlükəsizlik sərhədi
deyil — real sərhəd agentlərin sandbox-da (DB və fayllara çıxışsız) işləməsidir (mərhələ 9).

Defolt DENY: sahib siyahısı boş ola bilməz; mock təsdiqləyicilər defoltda rədd edilir və icazə verilsə
audit-də `source_kind: "mock"`, `human: false` kimi işarələnir (real istifadəçi təsdiqi kimi təqdim olunmur).
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol
from safety.dbconn import connect

DEFAULT_APPROVALS_DB = Path("data/approvals.db")
DEFAULT_AUDIT_PATH = Path("logs/audit.jsonl")
MAX_PAYLOAD_BYTES = 65536
MIN_TTL, MAX_TTL = 1, 86400
PRINCIPAL_KINDS = frozenset({"human", "mock", "agent", "system"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    payload_hash   TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    agent          TEXT NOT NULL,
    permission     TEXT NOT NULL,
    cost           REAL NOT NULL,
    op_id          TEXT NOT NULL,
    description    TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    decided_at     REAL,
    approver_id    TEXT,
    approver_kind  TEXT,
    channel        TEXT,
    decision_note  TEXT,
    used_at        REAL
)
"""
_COLS = ("id, payload_hash, payload_json, agent, permission, cost, op_id, description, status, "
         "created_at, expires_at, decided_at, approver_id, approver_kind, channel, decision_note, used_at")


# --- hash ---

@dataclass(frozen=True)
class Digest:
    json: str
    hash: str


def effect_digest(agent: str, permission: str, cost: float, op_id: str, description: str,
                  payload: Any) -> Digest:
    """Effektin kanonik JSON-u və SHA-256 hash-i. JSON-a çevrilməyən/çox böyük payload ValueError verir."""
    try:
        text = json.dumps(
            {"v": 1, "agent": agent, "permission": permission, "cost": float(cost), "op_id": op_id,
             "description": description, "payload": payload},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload təsdiq üçün JSON-a çevrilə bilmir: {exc}") from exc
    raw = text.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload təsdiq üçün çox böyükdür")
    return Digest(text, hashlib.sha256(raw).hexdigest())


# --- tiplər ---

@dataclass(frozen=True)
class Principal:
    """Qərarı verən tərəf. `kind="human"` yalnız kanal adapteri istifadəçini autentifikasiya etdikdən
    sonra yaradılmalıdır; testlər `kind="mock"` istifadə edir."""
    id: str
    kind: str
    channel: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("principal id boş ola bilməz")
        if self.kind not in PRINCIPAL_KINDS:
            raise ValueError(f"principal kind yanlışdır: {self.kind}")
        if not isinstance(self.channel, str) or not self.channel.strip():
            raise ValueError("principal channel boş ola bilməz")


@dataclass(frozen=True)
class Ticket:
    id: str
    payload_hash: str
    status: str  # pending | approved | denied | consumed | expired | invalidated
    agent: str
    permission: str
    cost: float
    op_id: str
    description: str
    payload: Any
    created_at: float
    expires_at: float
    approver_id: str = ""
    approver_kind: str = ""
    channel: str = ""

    @property
    def human_approved(self) -> bool:
        return self.approver_kind == "human"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str = ""
    ticket: Ticket | None = None


class ApprovalChannel(Protocol):
    """Web / Telegram / Android adapterləri bunu həyata keçirir (push bildiriş) və qərarı autentifikasiya
    olunmuş istifadəçi adından `ApprovalGateway.approve/deny` ilə qaytarır."""

    def notify(self, ticket: Ticket) -> None: ...


# --- nüvə ---

class _Core:
    def __init__(self, db_path: str | Path, audit_path: str | Path, owners: Iterable[str],
                 ttl_seconds: float, clock: Callable[[], float], allow_mock: bool,
                 channels: Iterable[ApprovalChannel]) -> None:
        self.owners = frozenset(owners)
        if not self.owners or not all(isinstance(o, str) and o.strip() for o in self.owners):
            raise ValueError("təsdiq xidməti üçün boş olmayan sahib siyahısı tələb olunur")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) \
                or not math.isfinite(ttl_seconds) or not MIN_TTL <= ttl_seconds <= MAX_TTL:
            raise ValueError(f"ttl_seconds {MIN_TTL}..{MAX_TTL} aralığında olmalıdır")
        self.ttl = float(ttl_seconds)
        self.clock = clock
        self.allow_mock = bool(allow_mock)
        self.channels = list(channels)
        self.audit_path = Path(audit_path)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(str(db_path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def now(self) -> float:
        return float(self.clock())

    # --- tranzaksiya + audit ---
    @contextmanager
    def _tx(self) -> Iterator[list]:
        events: list[dict] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield events
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        for ev in events:  # audit tranzaksiyadan sonra; yazıla bilməsə istisna yuxarı ötürülür
            self._write_audit(ev)

    def _write_audit(self, row: dict) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _ev(self, events: list, event: str, row: sqlite3.Row | None, code: str = "", reason: str = "",
            **extra: Any) -> None:
        d: dict[str, Any] = {
            "ts": datetime.fromtimestamp(self.now(), timezone.utc).isoformat(),
            "event": event, "approval_id": row["id"] if row else "",
            "agent": row["agent"] if row else "", "permission": row["permission"] if row else "",
            "cost": row["cost"] if row else None, "payload_hash": row["payload_hash"] if row else "",
            "code": code, "reason": reason,
        }
        if row is not None and row["approver_kind"]:
            d.update(decided_by=row["approver_id"], source_kind=row["approver_kind"],
                     channel=row["channel"], human=row["approver_kind"] == "human")
        d.update(extra)
        events.append(d)

    # --- yardımçılar ---
    def _fetch(self, approval_id: str) -> sqlite3.Row | None:
        if not isinstance(approval_id, str):
            return None
        return self.conn.execute(f"SELECT {_COLS} FROM approvals WHERE id = ?", (approval_id,)).fetchone()

    def _sweep(self, events: list) -> None:
        due = self.conn.execute(
            f"SELECT {_COLS} FROM approvals WHERE status IN ('pending', 'approved') AND expires_at <= ?",
            (self.now(),)).fetchall()
        for row in due:
            self.conn.execute("UPDATE approvals SET status = 'expired' WHERE id = ?", (row["id"],))
            self._ev(events, "approval_expired", row, "expired", "təsdiqin vaxtı bitdi")

    @staticmethod
    def _ticket(row: sqlite3.Row) -> Ticket:
        payload = json.loads(row["payload_json"]).get("payload")
        return Ticket(row["id"], row["payload_hash"], row["status"], row["agent"], row["permission"],
                      float(row["cost"]), row["op_id"], row["description"], payload,
                      float(row["created_at"]), float(row["expires_at"]), row["approver_id"] or "",
                      row["approver_kind"] or "", row["channel"] or "")

    def _reject(self, events: list, row: sqlite3.Row | None, code: str, reason: str) -> Outcome:
        self._ev(events, "approval_rejected", row, code, reason)
        return Outcome(False, reason, self._ticket(row) if row is not None else None)

    def _invalidate(self, events: list, row: sqlite3.Row, code: str, reason: str) -> Outcome:
        self.conn.execute("UPDATE approvals SET status = 'invalidated' WHERE id = ?", (row["id"],))
        self._ev(events, "approval_invalidated", row, code, reason)
        return self._reject(events, row, code, reason)

    def _validate_use(self, events: list, approval_id: str, payload_hash: str) -> tuple[Outcome | None,
                                                                                      sqlite3.Row | None]:
        row = self._fetch(approval_id)
        if row is None:
            return self._reject(events, None, "unknown_id", "təsdiq tapılmadı (saxta və ya yanlış ID)"), None
        terminal = {
            "consumed": ("replay", "təkrar istifadə: təsdiq artıq istifadə olunub"),
            "expired": ("expired", "təsdiqin vaxtı bitib"),
            "denied": ("denied", "təsdiq rədd edilib"),
            "invalidated": ("invalidated", "təsdiq etibarsız edilib"),
            "pending": ("not_approved", "təsdiq hələ verilməyib"),
        }
        if row["status"] in terminal:
            code, reason = terminal[row["status"]]
            return self._reject(events, row, code, reason), None
        if row["status"] != "approved":  # naməlum vəziyyət: DENY
            return self._reject(events, row, "bad_state", "təsdiq vəziyyəti etibarsızdır"), None
        if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != row["payload_hash"]:
            return self._invalidate(events, row, "tamper", "təsdiq qeydi pozulub"), None
        if row["payload_hash"] != payload_hash:
            return self._invalidate(events, row, "hash_mismatch",
                                    "payload dəyişib: təsdiq etibarsızdır"), None
        return None, row

    # --- broker tərəfi ---
    def request(self, agent: str, permission: str, cost: float, op_id: str, description: str,
                payload: Any) -> Ticket:
        d = effect_digest(agent, permission, cost, op_id, description, payload)
        with self._tx() as ev:
            self._sweep(ev)
            row = self.conn.execute(
                f"SELECT {_COLS} FROM approvals WHERE payload_hash = ? AND expires_at > ? "
                "AND status IN ('pending', 'approved', 'denied') ORDER BY created_at DESC LIMIT 1",
                (d.hash, self.now())).fetchone()
            created = row is None
            if created:
                now = self.now()
                aid = uuid.uuid4().hex
                self.conn.execute(
                    "INSERT INTO approvals (id, payload_hash, payload_json, agent, permission, cost, op_id,"
                    " description, status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (aid, d.hash, d.json, agent, permission, float(cost), op_id, description, now,
                     now + self.ttl))
                row = self._fetch(aid)
                self._ev(ev, "approval_requested", row, "requested", "təsdiq gözlənilir")
            ticket = self._ticket(row)
        if created:
            self._notify(ticket)
        return ticket

    def _notify(self, ticket: Ticket) -> None:
        for ch in self.channels:
            try:
                ch.notify(ticket)
            except Exception as exc:  # noqa: BLE001 — kanal xətası sorğunu pozmur, audit-ə yazılır
                with self._tx() as ev:
                    self._ev(ev, "approval_notify_failed", self._fetch(ticket.id), "notify_failed",
                             f"{type(exc).__name__}: {exc}")

    def check(self, approval_id: str, payload_hash: str) -> Outcome:
        with self._tx() as ev:
            self._sweep(ev)
            rejected, row = self._validate_use(ev, approval_id, payload_hash)
            return rejected or Outcome(True, "təsdiq etibarlıdır", self._ticket(row))

    def consume(self, approval_id: str, payload_hash: str) -> Outcome:
        with self._tx() as ev:
            self._sweep(ev)
            rejected, row = self._validate_use(ev, approval_id, payload_hash)
            if rejected:
                return rejected
            cur = self.conn.execute(
                "UPDATE approvals SET status = 'consumed', used_at = ? WHERE id = ? AND status = 'approved'",
                (self.now(), approval_id))
            if cur.rowcount != 1:  # BEGIN IMMEDIATE ilə mümkün deyil; yenə də DENY
                return self._reject(ev, row, "bad_state", "təsdiq istifadə oluna bilmədi")
            row = self._fetch(approval_id)
            self._ev(ev, "approval_consumed", row, "consumed", "təsdiq istifadə olundu")
            return Outcome(True, "təsdiq istifadə olundu", self._ticket(row))

    # --- kanal tərəfi ---
    def decide(self, approval_id: str, principal: Principal, approve: bool, note: str) -> Outcome:
        with self._tx() as ev:
            self._sweep(ev)
            row = self._fetch(approval_id)
            if row is None:
                return self._reject(ev, None, "unknown_id", "təsdiq tapılmadı (saxta və ya yanlış ID)")
            if principal.kind in ("agent", "system") or principal.id.casefold() == row["agent"].casefold():
                return self._reject(ev, row, "self_approval",
                                    "agent/sistem öz əməliyyatını təsdiqləyə bilməz")
            if principal.kind == "mock" and not self.allow_mock:
                return self._reject(ev, row, "mock_rejected",
                                    "mock təsdiqi real istifadəçi təsdiqi deyil və bu xidmətdə qəbul olunmur")
            if principal.id not in self.owners:
                return self._reject(ev, row, "unauthorized", "təsdiqləyici sahib siyahısında deyil")
            if row["status"] == "expired":
                return self._reject(ev, row, "expired", "təsdiqin vaxtı bitib")
            if row["status"] != "pending":
                return self._reject(ev, row, "not_pending", f"təsdiq {row['status']} vəziyyətindədir")
            status = "approved" if approve else "denied"
            self.conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, approver_id = ?, approver_kind = ?, "
                "channel = ?, decision_note = ? WHERE id = ? AND status = 'pending'",
                (status, self.now(), principal.id, principal.kind, principal.channel, note, approval_id))
            row = self._fetch(approval_id)
            self._ev(ev, f"approval_{status}", row, status, note or status)
            return Outcome(True, status, self._ticket(row))

    def get(self, approval_id: str) -> Ticket | None:
        with self._tx() as ev:
            self._sweep(ev)
            row = self._fetch(approval_id)
            return self._ticket(row) if row is not None else None

    def pending(self) -> list[Ticket]:
        with self._tx() as ev:
            self._sweep(ev)
            rows = self.conn.execute(
                f"SELECT {_COLS} FROM approvals WHERE status = 'pending' ORDER BY created_at, rowid").fetchall()
            return [self._ticket(r) for r in rows]


# --- üzlər ---

class ApprovalClient:
    """Broker tərəfi: sorğu aç, yoxla, istifadə et. Təsdiq/rəd etmək imkanı YOXDUR."""

    def __init__(self, core: _Core) -> None:
        self._core = core

    def request(self, agent: str, permission: str, cost: float, op_id: str, description: str,
                payload: Any) -> Ticket:
        return self._core.request(agent, permission, cost, op_id, description, payload)

    def check(self, approval_id: str, payload_hash: str) -> Outcome:
        return self._core.check(approval_id, payload_hash)

    def consume(self, approval_id: str, payload_hash: str) -> Outcome:
        return self._core.consume(approval_id, payload_hash)


class ApprovalGateway:
    """Kanal adapterləri (Web, Telegram, Android) üçün: gözləyənləri göstər, təsdiqlə və ya rədd et.
    Agentlərə və broker-ə verilmir."""

    def __init__(self, core: _Core) -> None:
        self._core = core

    def pending(self) -> list[Ticket]:
        return self._core.pending()

    def get(self, approval_id: str) -> Ticket | None:
        return self._core.get(approval_id)

    def approve(self, approval_id: str, principal: Principal, note: str = "") -> Outcome:
        return self._core.decide(approval_id, principal, True, note)

    def deny(self, approval_id: str, principal: Principal, note: str = "") -> Outcome:
        return self._core.decide(approval_id, principal, False, note)


class ApprovalReader:
    """YALNIZ OXU üzü (təhlil/bildiriş üçün): gözləyən təsdiqləri göstərir; təsdiq/rədd/istifadə imkanı yoxdur."""

    def __init__(self, core: _Core) -> None:
        self._core = core

    def pending(self) -> list[Ticket]:
        return self._core.pending()

    def get(self, approval_id: str) -> Ticket | None:
        return self._core.get(approval_id)


class ApprovalService:
    def __init__(
        self,
        db_path: str | Path = DEFAULT_APPROVALS_DB,
        audit_path: str | Path = DEFAULT_AUDIT_PATH,
        owners: Iterable[str] = (),
        ttl_seconds: float = 600,
        clock: Callable[[], float] = time.time,
        allow_mock: bool = False,
        channels: Iterable[ApprovalChannel] = (),
    ) -> None:
        self._core = _Core(db_path, audit_path, owners, ttl_seconds, clock, allow_mock, channels)
        self.client = ApprovalClient(self._core)
        self.gateway = ApprovalGateway(self._core)
        self.reader = ApprovalReader(self._core)

    def close(self) -> None:
        self._core.close()
