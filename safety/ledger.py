"""Davamlı xərc kitabı (SQLite): rezerv / təsdiq / azad etmə, limitlər, təkrar-qoruma, audit uyğunluğu.

Status: reserved (rezerv tutulub; `unknown` işarəsi = faktiki xərc məlum deyil, rezerv qorunur),
committed (faktiki xərc yazılıb), released (rezerv azad edilib). Limitlər `amount` üzərində
hesablanır: reserved -> rezerv məbləği, committed -> faktiki xərc, released -> 0.
Rezervasiya BEGIN IMMEDIATE tranzaksiyasındadır: paralel bağlantılar limiti aşa bilməz.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

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


class ResolutionDenied(Exception):
    """`resolve_unknown` üçün sahib təsdiqi qəbul olunmadı (`code`: owner_auth_unavailable, forged_principal, role_not_allowed, scope_mismatch, credential_replayed, ...)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def resolution_scope(op_id: str, actual: float | None) -> tuple[str, str]:
    """Sahibin step-up ilə imzalayacağı DƏQİQ əhatə: `resolve_unknown` + rezerv id-si (subject) + nəticə (`release`/`commit`) və məbləğin SHA-256-sı. `(subject, hash)` qaytarır."""
    if actual is None:
        params: dict = {"outcome": "release", "amount": 0.0}
    else:
        _check_amount("faktiki xərc", actual)
        params = {"outcome": "commit", "amount": float(actual)}
    body = json.dumps({"op": "resolve_unknown", "subject": str(op_id), "params": params}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return str(op_id), hashlib.sha256(body.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Settlement:
    """`settle()` nəticəsi. `ok`: rezerv həll olundu (köhnə `commit()` bool müqaviləsi). Faktiki xərc HƏMİŞƏ dəqiq yazılır (kəsilmir/azaldılmır). `overrun`: faktiki - rezerv (>0 isə hesablama
    rezervi aşıb; DÜRÜST tokenizer fərqi ola bilər); `over_cap`: yazıldıqdan sonra ümumi xərc tavanı aşıb (yalnız bu halda MALİYYƏ kəsicisi açılır: `breaker_tripped`);
    `ceiling_exceeded`: provayder bildirdiyi çıxış tokenləri elan olunmuş `max_output_tokens`-i aşıb (AYRI provayder təhlükəsizlik kəsicisi: `provider_breaker_tripped`)."""
    ok: bool
    reserved: float = 0.0
    actual: float = 0.0
    overrun: float = 0.0
    over_cap: bool = False
    breaker_tripped: bool = False
    ceiling_exceeded: bool = False
    provider_breaker_tripped: bool = False

    def __bool__(self) -> bool:
        return self.ok

    @property
    def blocking(self) -> bool:
        """Çağıran nəticəni "uğur" kimi qəbul etməməlidir: tavan aşılıb və ya provayder çıxış tavanını pozub."""
        return self.ok and (self.over_cap or self.ceiling_exceeded)

    @property
    def soft_overrun(self) -> bool:
        """Faktiki xərc rezervi aşdı, lakin ümumi tavan qorunur və çıxış tavanı pozulmayıb: dəqiq yazılır, auditə düşür, KƏSİCİ AÇILMIR."""
        return self.ok and self.overrun > 0.0 and not self.blocking

    @property
    def overran(self) -> bool:      # köhnə ad: indi yalnız `blocking`
        return self.blocking


def clear_provider_breaker_scope(provider: str, breaker_id: int) -> tuple[str, str]:
    """Provayder təhlükəsizlik kəsicisini təmizləmək üçün DƏQİQ əhatə: `clear_provider_breaker` + `provider:<ad>:<kəsici id>`."""
    subject = f"provider:{str(provider)[:64]}:{int(breaker_id)}"
    body = json.dumps({"op": "clear_provider_breaker", "subject": subject, "params": {}}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return subject, hashlib.sha256(body.encode("ascii")).hexdigest()


def clear_breaker_scope(breaker_id: int) -> tuple[str, str]:
    """Sahibin step-up ilə imzalayacağı DƏQİQ əhatə: `clear_cap_breaker` + konkret kəsici id-si (subject). `(subject, hash)` qaytarır; təsdiq yalnız həmin kəsicini təmizləyir."""
    subject = f"breaker:{int(breaker_id)}"
    body = json.dumps({"op": "clear_cap_breaker", "subject": subject, "params": {}}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return subject, hashlib.sha256(body.encode("ascii")).hexdigest()


def ledger_backend(obj: Any) -> "CostLedger | None":
    """`CostLedger` və ya onun `LedgerPort`-undan arxa uc kitabı çıxarır (başqa obyekt üçün None). `build_app` mühərrikin kitabını bununla yoxlayır."""
    if isinstance(obj, CostLedger):
        return obj
    if isinstance(obj, LedgerPort):
        return _PORT_BACKENDS.get(obj._token)
    return None


class CostLedger:
    def __init__(self, db_path: str | Path = DEFAULT_LEDGER_PATH, *, verifier: Any = None, owners: Iterable[str] = (), clock: Callable[[], float] = time.time,
                 global_limit: float | None = None) -> None:
        """`verifier` (yalnız yoxlama: `check_principal`) və `owners` yalnız naməlum rezervlərin sahib təsdiqi ilə həllində (`resolve_unknown`) istifadə olunur; verilməyibsə həll BAĞLIDIR.
        `global_limit`: bu kitabın ÜMUMİ xərc tavanı (dəyişməzdir). Hər `reserve` (Broker, GuardedLLM, agent və sistem çağırışları) çağıranın verdiyi limitlə bu tavanın KİÇİYİ ilə yoxlanılır,
        ona görə heç bir çağıran (limit verməyi unudan sistem söhbəti daxil) tavanı aşa bilməz."""
        if global_limit is not None:
            _check_limit("global_limit", global_limit)
        self._global_cap = None if global_limit is None else float(global_limit)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._verifier, self._owners, self._clock = verifier, frozenset(owners), clock
        self._conn = LockedConnection(str(db_path), isolation_level=None, timeout=30)
        self._conn.execute(_SCHEMA)
        self._conn.execute("CREATE TABLE IF NOT EXISTS ledger_used_credentials (nonce TEXT PRIMARY KEY, op_id TEXT NOT NULL, used_at REAL NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")                       # davamlı "tavan aşıldı" kəsicisi
        self._conn.execute("CREATE TABLE IF NOT EXISTS ledger_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL, "
                           "op_id TEXT NOT NULL, data TEXT NOT NULL)")                                                                   # audit: overrun/breaker_tripped/breaker_cleared
        self._migrate()

    @property
    def global_limit(self) -> float | None:
        return self._global_cap

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
        with self._tx():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO costs (op_id, agent, amount, ts, description, status, reserved, unknown)"
                " VALUES (?, ?, ?, ?, ?, 'committed', ?, 0)",
                (op_id, agent, float(amount), _now(), description, float(amount)),
            )
            if cur.rowcount == 1:
                self._guard_cap(op_id, 0.0, float(amount))      # faktiki xərc yazıldı: tavanı aşırsa kəsici açılır (xərc dəqiq saxlanır)
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
        provider: str | None = None,
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
        if self._global_cap is not None:
            global_limit = self._global_cap if global_limit is None else min(float(global_limit), self._global_cap)
        with self._tx():
            tripped = self._breaker_row()
            if tripped is not None:      # davamlı kəsici: SIFIR xərcli rezervlər də daxil, HƏR yeni rezerv rədd edilir (yalnız sahib təmizləyir)
                return Reservation("denied", f"xərc tavanı aşılıb (kəsici #{tripped['id']}, op={tripped['op_id']}): yeni rezerv sahib təmizləyənə qədər bağlıdır")
            if provider is not None:     # provayder çıxış tavanını pozub: həmin provayder üçün HƏR yeni rezerv (sahib təmizləyənə qədər) rədd edilir
                pb = self._provider_breaker_row(str(provider))
                if pb is not None:
                    return Reservation("denied", f"provayder təhlükəsizlik kəsicisi açıqdır (#{pb['id']}, {pb['provider']}: çıxış tavanı pozuldu): sahib təmizləyənə qədər bağlıdır")
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
        """Köhnə bool müqaviləsi: `settle(...).ok`. Faktiki xərc dəqiq yazılır; rezervi/tavanı aşma `settle` ilə eyni qaydada qeydə alınır və kəsici açılır."""
        return self.settle(op_id, actual).ok

    def settle(self, op_id: str, actual: float, *, provider: str | None = None, output_tokens: int | None = None, max_output_tokens: int | None = None) -> Settlement:
        """Faktiki (provayderin bildirdiyi) xərci DƏQİQ yazır (YALNIZ normal `reserved`-dən; naməlum rezervi təsdiqləməz). Hesablama və aşma yoxlaması BİR tranzaksiyadadır:
        faktiki > rezerv audit cədvəlinə yazılır (tokenizer fərqi dürüst ola bilər: kəsici AÇILMIR); ümumi > tavan olarsa davamlı MALİYYƏ kəsicisi açılır (sonrakı HƏR rezerv rədd edilir).
        `output_tokens` > `max_output_tokens` olarsa `output_ceiling_violation` auditə yazılır və AYRI provayder kəsicisi açılır (maliyyə kəsicisi yalnız tavan aşılarsa). Xərc heç vaxt kəsilmir/azaldılmır.
        DÜRÜST QEYD: artıq baş vermiş provayder xərcini heç bir tavan geri qaytara bilməz; kəsici yalnız SONRAKI ödənişli çağırışları dayandırır."""
        _check_amount("faktiki xərc", actual)
        actual = float(actual)
        with self._tx():
            row = self._conn.execute("SELECT amount FROM costs WHERE op_id = ? AND status = 'reserved' AND unknown = 0", (op_id,)).fetchone()
            if row is None:
                return Settlement(False)
            reserved = float(row[0])
            self._conn.execute("UPDATE costs SET amount = ?, status = 'committed' WHERE op_id = ?", (actual, op_id))
            overrun, over_cap, tripped = self._guard_cap(op_id, reserved, actual)
            ceiling = output_tokens is not None and max_output_tokens is not None and int(output_tokens) > int(max_output_tokens)
            ptripped = False
            if ceiling:
                pname = str(provider if provider is not None else "unknown")[:64]
                data = {"provider": pname, "output_tokens": int(output_tokens), "max_output_tokens": int(max_output_tokens), "actual": actual, "reserved": reserved}
                eid = self._event("output_ceiling_violation", op_id, data)
                if self._provider_breaker_row(pname) is None:
                    self._conn.execute("INSERT INTO ledger_meta (key, value) VALUES (?, ?)",
                                       (f"provider_breaker:{pname}", json.dumps({"id": eid, "provider": pname, "op_id": op_id, **data, "ts": float(self._clock())})))
                    self._event("provider_breaker_tripped", op_id, data)
                    ptripped = True
        return Settlement(True, reserved, actual, overrun, over_cap, tripped, ceiling, ptripped)

    def _guard_cap(self, op_id: str, reserved: float, actual: float) -> tuple[float, bool, bool]:
        """Tranzaksiya DAXİLİNDƏ çağırılır. `(aşma, tavan_aşıldı, kəsici_açıldı)`."""
        overrun = actual - reserved
        overrun = overrun if overrun > _EPS else 0.0
        total = float(self._conn.execute("SELECT COALESCE(SUM(amount), 0.0) FROM costs").fetchone()[0])
        cap = self._global_cap
        over_cap = cap is not None and total > cap + _EPS
        tripped = False
        if overrun > 0.0 or over_cap:
            data = {"reserved": reserved, "actual": actual, "overrun": overrun, "total": total, "cap": cap, "over_cap": over_cap}
            self._event("overrun", op_id, data)                       # rezervi aşma HƏMİŞƏ auditə yazılır
            if over_cap and self._breaker_row() is None:               # MALİYYƏ kəsicisi YALNIZ ümumi tavan aşılanda (dürüst tokenizer fərqi tavan daxilində kəsici açmır)
                eid = self._event("breaker_tripped", op_id, data)
                self._conn.execute("INSERT INTO ledger_meta (key, value) VALUES ('breaker', ?)", (json.dumps({"id": eid, "op_id": op_id, **data, "ts": float(self._clock())}),))
                tripped = True
        return overrun, over_cap, tripped

    def _event(self, event: str, op_id: str, data: dict) -> int:
        cur = self._conn.execute("INSERT INTO ledger_events (ts, event, op_id, data) VALUES (?, ?, ?, ?)", (float(self._clock()), event, op_id, json.dumps(data, sort_keys=True)))
        return int(cur.lastrowid)

    def _breaker_row(self) -> dict | None:
        row = self._conn.execute("SELECT value FROM ledger_meta WHERE key = 'breaker'").fetchone()
        return json.loads(row[0]) if row is not None else None

    def breaker(self) -> dict | None:
        """Açıq "tavan aşıldı" kəsicisi (id, op_id, rezerv, faktiki, aşma, ümumi, tavan, vaxt) və ya None."""
        return self._breaker_row()

    def _provider_breaker_row(self, provider: str) -> dict | None:
        row = self._conn.execute("SELECT value FROM ledger_meta WHERE key = ?", (f"provider_breaker:{provider}",)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def provider_breaker(self, provider: str) -> dict | None:
        """Provayderin açıq təhlükəsizlik kəsicisi (çıxış tavanı pozuntusu) və ya None."""
        return self._provider_breaker_row(str(provider)[:64])

    def provider_breakers(self) -> list[dict]:
        rows = self._conn.execute("SELECT value FROM ledger_meta WHERE key LIKE 'provider_breaker:%' ORDER BY key").fetchall()
        return [json.loads(r[0]) for r in rows]

    def clear_provider_breaker(self, provider: str, approval: Any) -> bool:
        """Provayder kəsicisini YALNIZ həmin kəsicinin id-sinə bağlı, bir-dəfəlik sahib step-up təsdiqi ilə təmizləyir (auditli). Kəsici yoxdursa/id fərqlidirsə False."""
        provider = str(provider)[:64]
        current = self._provider_breaker_row(provider)
        if current is None:
            return False
        subject, digest = clear_provider_breaker_scope(provider, current["id"])
        self._verify_owner(approval, "clear_provider_breaker", subject, digest)
        with self._tx():
            row = self._provider_breaker_row(provider)
            if row is None or row["id"] != current["id"]:
                return False
            try:
                self._conn.execute("INSERT INTO ledger_used_credentials (nonce, op_id, used_at) VALUES (?, ?, ?)", (approval.nonce, subject, float(self._clock())))
            except sqlite3.IntegrityError:
                raise ResolutionDenied("sahib təsdiqi artıq istifadə olunub (bir-dəfəlik)", "credential_replayed") from None
            self._conn.execute("DELETE FROM ledger_meta WHERE key = ?", (f"provider_breaker:{provider}",))
            self._event("provider_breaker_cleared", row["op_id"], {"breaker_id": row["id"], "provider": provider, "by": approval.id})
        return True

    def events(self, limit: int = 100) -> list[dict]:
        """Audit hadisələri (overrun, breaker_tripped, breaker_cleared), köhnədən yeniyə."""
        rows = self._conn.execute("SELECT id, ts, event, op_id, data FROM ledger_events ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r[0], "ts": r[1], "event": r[2], "op_id": r[3], "data": json.loads(r[4])} for r in reversed(rows)]

    def clear_breaker(self, approval: Any) -> bool:
        """Kəsicini YALNIZ həmin kəsicin id-sinə bağlı, bir-dəfəlik sahib step-up təsdiqi ilə təmizləyir (auditə yazılır). Kəsici yoxdursa/id fərqlidirsə False (təsdiq yandırılmır).
        Təmizləmə tavanı yüksəltmir: ümumi xərc hələ tavandan çoxdursa yeni rezervlər tavan arifmetikası ilə rədd olunmaqda qalır."""
        current = self._breaker_row()
        if current is None:
            return False
        subject, digest = clear_breaker_scope(current["id"])
        self._verify_owner(approval, "clear_cap_breaker", subject, digest)
        with self._tx():
            row = self._breaker_row()
            if row is None or row["id"] != current["id"]:
                return False
            try:
                self._conn.execute("INSERT INTO ledger_used_credentials (nonce, op_id, used_at) VALUES (?, ?, ?)", (approval.nonce, subject, float(self._clock())))
            except sqlite3.IntegrityError:
                raise ResolutionDenied("sahib təsdiqi artıq istifadə olunub (bir-dəfəlik)", "credential_replayed") from None
            self._conn.execute("DELETE FROM ledger_meta WHERE key = 'breaker'")
            self._event("breaker_cleared", row["op_id"], {"breaker_id": row["id"], "by": approval.id})
        return True

    def release(self, op_id: str) -> bool:
        """Normal rezervi azad edir. NAMƏLUM rezervi azad etməz (False): yalnız `resolve_unknown`."""
        cur = self._conn.execute(
            "UPDATE costs SET amount = 0, status = 'released'"
            " WHERE op_id = ? AND status = 'reserved' AND unknown = 0", (op_id,))
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

    def _verify_resolution(self, op_id: str, actual: float | None, approval: Any) -> None:
        subject, digest = resolution_scope(op_id, actual)
        self._verify_owner(approval, "resolve_unknown", subject, digest)

    def _verify_owner(self, approval: Any, scope_op: str, subject: str, digest: str) -> None:
        if self._verifier is None:
            raise ResolutionDenied("etibarlı autentifikasiya qurulmayıb: naməlum rezervin həlli bağlıdır", "owner_auth_unavailable")
        try:
            self._verifier.check_principal(approval, "owner")           # imza, rol, ömür, ləğv (cihaz daxil)
        except Exception as exc:  # noqa: BLE001
            raise ResolutionDenied(str(exc)[:120], str(getattr(exc, "code", "forged_principal"))) from None
        if getattr(approval, "kind", "") != "human" or getattr(approval, "id", None) not in self._owners:
            raise ResolutionDenied("yalnız real sahib təsdiqi qəbul olunur", "unauthorized")
        if (getattr(approval, "scope_op", ""), getattr(approval, "scope_subject", ""), getattr(approval, "scope_hash", ""), getattr(approval, "scope_company", "")) \
                != (scope_op, subject, digest, ""):
            raise ResolutionDenied("sahib təsdiqi bu dəqiq əməliyyata/hədəfə/parametrə bağlı deyil", "scope_mismatch")

    def resolve_unknown(self, op_id: str, actual: float | None, approval: Any) -> bool:
        """Naməlum rezervin həlli: YALNIZ bu rezerv id-sinə + nəticəyə (`actual` verilibsə təsdiq, `None` isə xərc yoxdur: azad) + məbləğə bağlı, bir-dəfəlik SAHİB step-up təsdiqi ilə
        (`resolution_scope`; cihazın Keystore imzası). Təsdiq uyğunsuz/təkrar/ləğv olunmuş/etibarsızdırsa `ResolutionDenied`; naməlum olmayan/olmayan op_id üçün `False` (təsdiq yandırılmır)."""
        _check_text("op_id", op_id)
        self._verify_resolution(op_id, actual, approval)
        with self._tx():
            row = self._conn.execute("SELECT 1 FROM costs WHERE op_id = ? AND status = 'reserved' AND unknown = 1", (op_id,)).fetchone()
            if row is None:
                return False
            try:
                self._conn.execute("INSERT INTO ledger_used_credentials (nonce, op_id, used_at) VALUES (?, ?, ?)", (approval.nonce, op_id, float(self._clock())))
            except sqlite3.IntegrityError:
                raise ResolutionDenied("sahib təsdiqi artıq istifadə olunub (bir-dəfəlik)", "credential_replayed") from None
            tag = f" [uzlaşdırıldı:{approval.id}:{'azad' if actual is None else 'təsdiq'}]"
            if actual is None:
                self._conn.execute("UPDATE costs SET amount = 0, status = 'released', unknown = 0, description = description || ? WHERE op_id = ?", (tag, op_id))
            else:
                self._conn.execute("UPDATE costs SET amount = ?, status = 'committed', unknown = 0, description = description || ? WHERE op_id = ?", (float(actual), tag, op_id))
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


_PORT_BACKENDS: dict[str, "CostLedger"] = {}                 # modul reyestri: LedgerPort obyektində kitaba istinad YOXDUR
_PORT_METHODS = ("reserve", "commit", "settle", "release", "mark_unknown", "has", "agent_total", "total", "reserved_total", "by_agent", "entries", "unknown_entries", "stale_reserved",
                 "breaker", "events", "provider_breaker", "provider_breakers")        # `clear_breaker` (sahib təsdiqi), `record`, `expire_stale`, `resolve_unknown` YOXDUR


class LedgerPort:
    """İcra qatı (Broker, Orchestrator, GuardedLLM, proaktiv analiz) üçün DAR kitab səthi: yalnız rezerv/hesablaşma/oxuma. `record`, `expire_stale`, `resolve_unknown`, `close` və SQLite
    bağlantısı BU obyektdə YOXDUR; ApiApp-dan çatılan obyekt qrafında xam `CostLedger` olmur. Obyektdə kitaba istinad yoxdur (yalnız təsadüfi simvol açar).
    DÜRÜST QEYD: bu proses-daxili sarğıdır (arxa uc modul reyestrindədir), ixtiyari proses-daxili koda qarşı sərhəd DEYİL; yalnız səthi daraldır və səhvən genişlənməyə qarşı testlə qorunur."""
    __slots__ = ("_token", "__weakref__")

    def __init__(self, ledger: "CostLedger") -> None:
        token = secrets.token_hex(16)
        _PORT_BACKENDS[token] = ledger
        object.__setattr__(self, "_token", token)
        weakref.finalize(self, _PORT_BACKENDS.pop, token, None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")

    @property
    def global_limit(self) -> float | None:
        return _PORT_BACKENDS[self._token].global_limit


def _port_method(name: str) -> Callable[..., Any]:
    def method(self: LedgerPort, *a: Any, **k: Any) -> Any:
        return getattr(_PORT_BACKENDS[self._token], name)(*a, **k)
    method.__name__ = name
    return method


for _name in _PORT_METHODS:
    setattr(LedgerPort, _name, _port_method(_name))


_RECONCILER_BACKENDS: dict[str, "CostLedger"] = {}      # modul reyestri: reconciler obyektində kitaba istinad YOXDUR (yalnız təsadüfi simvol açar)


class LedgerReconciler:
    """Sahib uzlaşdırması üçün DAR səth (etibarlı server kodu): naməlum rezervləri göstərir, step-up əhatəsini hesablayır və YALNIZ sahib təsdiqi ilə həll edir.
    Obyektdə `CostLedger` istinadı və `reserve/commit/release/record` kimi yazma imkanı YOXDUR (yalnız təsadüfi simvol açar). DÜRÜST QEYD: bu proses-daxili sarğıdır, ixtiyari
    proses-daxili koda qarşı sərhəd DEYİL (arxa uc modul reyestrindədir)."""
    __slots__ = ("_token", "__weakref__")

    def __init__(self, ledger: CostLedger) -> None:
        token = secrets.token_hex(16)
        _RECONCILER_BACKENDS[token] = ledger
        object.__setattr__(self, "_token", token)
        weakref.finalize(self, _RECONCILER_BACKENDS.pop, token, None)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")

    def unknown(self) -> list[LedgerEntry]:
        return _RECONCILER_BACKENDS[self._token].unknown_entries()

    @staticmethod
    def scope(op_id: str, actual: float | None) -> tuple[str, str]:
        return resolution_scope(op_id, actual)

    def resolve(self, op_id: str, actual: float | None, approval: Any) -> bool:
        return _RECONCILER_BACKENDS[self._token].resolve_unknown(op_id, actual, approval)

    def breaker(self) -> dict | None:
        return _RECONCILER_BACKENDS[self._token].breaker()

    def events(self, limit: int = 100) -> list[dict]:
        return _RECONCILER_BACKENDS[self._token].events(limit)

    def clear_scope(self) -> tuple[str, str]:
        """Açıq kəsicini təmizləmək üçün sahibin step-up ilə imzalayacağı `(subject, hash)` (kəsici yoxdursa ValueError)."""
        b = _RECONCILER_BACKENDS[self._token].breaker()
        if b is None:
            raise ValueError("açıq kəsici yoxdur")
        return clear_breaker_scope(b["id"])

    def clear_breaker(self, approval: Any) -> bool:
        return _RECONCILER_BACKENDS[self._token].clear_breaker(approval)

    def provider_breakers(self) -> list[dict]:
        return _RECONCILER_BACKENDS[self._token].provider_breakers()

    def clear_provider_scope(self, provider: str) -> tuple[str, str]:
        b = _RECONCILER_BACKENDS[self._token].provider_breaker(provider)
        if b is None:
            raise ValueError("bu provayder üçün açıq kəsici yoxdur")
        return clear_provider_breaker_scope(provider, b["id"])

    def clear_provider_breaker(self, provider: str, approval: Any) -> bool:
        return _RECONCILER_BACKENDS[self._token].clear_provider_breaker(provider, approval)


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
