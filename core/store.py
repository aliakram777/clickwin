"""GoalStore (Mərhələ 18.2-B): `kernel.Goal`/`kernel.Plan` üçün davamlı SQLite saxlancı. Heç nə İCRA ETMİR.

Dizayn:
- Mövcud 18.0 müqavilələri olduğu kimi istifadə olunur (`kernel/` dəyişdirilmir): sənəd JSON-u + hash saxlanır, oxuma zamanı yenidən doğrulanır və hash ilə müqayisə olunur (dəyişdirilmiş sətir aşkarlanır).
- İKİ ÜZ: `EngineGoalStore` (döngünün gördüyü; məqsədi aktivləşdirə/tamamlaya/ləğv edə və ya dayandırılmış plana davam edə BİLMİR) və `OwnerGoalStore` (yalnız sahib qapısı tərəfi). Bu proses-daxili
  üz ayrılığıdır, OS sərhədi DEYİL (`LedgerPort` ilə eyni dürüst qeyd). Sahibin həqiqiliyi (cihaz imzası/step-up) bu saxlancın deyil, sahib qapısının məsuliyyətidir; `approval_ref` yalnız ona istinaddır.
- İdempotentlik: hər yazıcı əməliyyat `op_id` ilə (eyni op_id + eyni məzmun = dublikat; fərqli məzmun = ziddiyyət). Optimistik versiya: `expected_version`.
- Hadisələr: goal/plan dəyişiklikləri `outbox`-a EYNİ tranzaksiyada yazılır, sonra `flush_outbox(EventLog)` ilə (idempotent) kernel jurnalına köçürülür (qəza arasında itki yoxdur).
  Addım/döngü hadisələri öz append-only `journal` cədvəlindədir (UPDATE/DELETE trigger ilə bloklanır).
- Addımlar: `begin_step` atomik CAS + lease; lease bitmiş RUNNING addım `recover_expired` ilə UNKNOWN olur (avtomatik TƏKRAR YOXDUR: yan təsir baş vermiş ola bilər; yalnız sahib həll edir).
- Sirr/idarəedici simvol: mətnlər `kernel.common.clean_text` ilə; icraçı çıxışı yalnız qısa xülasə kimi, sirr aşkarlanarsa REDAKTƏ olunur (dəyər saxlanmır)."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from core.ports import ALLOWED_STEP_PERMISSIONS, Actor, StepRecord, StepState  # noqa: F401
from kernel.common import ContractError, InvalidTransition, SecretDetected, UnsupportedVersion, canonical_json, clean_text, fmt_dt, sha256_hex
from kernel.eventlog import EventLog
from kernel.events import EntityRef, Event
from kernel.goal import Goal, GoalStatus
from kernel.plan import Plan, PlanStatus, PlanStepSpec
from safety.dbconn import LockedConnection

STORE_SCHEMA_VERSION = 1
_OP = re.compile(r"^[a-z0-9][a-z0-9_\-:.]{2,95}$")
_APPROVAL_REF = re.compile(r"^[A-Za-z0-9_.:\-]{8,96}$")
_ACTIVE_PLAN = (PlanStatus.DRAFT, PlanStatus.READY, PlanStatus.EXECUTING, PlanStatus.PAUSED)
SUMMARY_MAX = 500

ENGINE_GOAL = frozenset({(GoalStatus.ACTIVE, GoalStatus.BLOCKED), (GoalStatus.ACTIVE, GoalStatus.PAUSED), (GoalStatus.ACTIVE, GoalStatus.FAILED)})
ENGINE_PLAN = frozenset({(PlanStatus.DRAFT, PlanStatus.READY), (PlanStatus.DRAFT, PlanStatus.SUPERSEDED), (PlanStatus.READY, PlanStatus.EXECUTING),
                         (PlanStatus.EXECUTING, PlanStatus.COMPLETED), (PlanStatus.EXECUTING, PlanStatus.FAILED), (PlanStatus.EXECUTING, PlanStatus.PAUSED)})


class StoreError(ContractError):
    pass


class StaleVersion(StoreError):
    def __init__(self, message: str = "versiya köhnəlib (başqa yazıcı dəyişib)") -> None:
        super().__init__(message, "stale_version")


class OpConflict(StoreError):
    def __init__(self, message: str = "op_id artıq FƏRQLİ məzmunla istifadə olunub") -> None:
        super().__init__(message, "op_conflict")


class NotPermitted(StoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "not_permitted")


class StoreIntegrityError(StoreError):
    def __init__(self, message: str = "saxlancda sətir dəyişdirilib və ya pozulub") -> None:
        super().__init__(message, "store_integrity")


@dataclass(frozen=True)
class OpResult:
    duplicate: bool
    version: int


_DDL = """
CREATE TABLE IF NOT EXISTS goals (goal_id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL, created_at TEXT NOT NULL, doc TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ops (op_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, content_hash TEXT NOT NULL, result_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS plans (plan_id TEXT NOT NULL, version INTEGER NOT NULL, goal_id TEXT NOT NULL, status TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0, doc TEXT NOT NULL,
                                  content_hash TEXT NOT NULL, PRIMARY KEY (plan_id, version));
CREATE INDEX IF NOT EXISTS idx_plans_goal ON plans(goal_id);
CREATE TABLE IF NOT EXISTS steps (plan_id TEXT NOT NULL, plan_version INTEGER NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, op_id TEXT NOT NULL DEFAULT '',
                                  lease_until REAL, summary TEXT NOT NULL DEFAULT '', output_sha256 TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                                  updated_at TEXT NOT NULL, PRIMARY KEY (plan_id, plan_version, step_id));
CREATE TABLE IF NOT EXISTS journal (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, goal_id TEXT NOT NULL, plan_id TEXT NOT NULL, step_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL, ts TEXT NOT NULL, flushed INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
"""


def _op(op_id: Any) -> str:
    if not isinstance(op_id, str) or not _OP.match(op_id):
        raise ContractError("op_id: kiçik hərf/rəqəm/_-:. (3-96) tələb olunur", "bad_op_id")
    return op_id


def safe_summary(text: Any) -> tuple[str, str]:
    """İcraçı çıxışı ETİBARSIZDIR: (xülasə, xam çıxışın sha256-sı). Sirr/yanlış simvol ⇒ redaksiya; dəyər HEÇ VAXT saxlanmır."""
    raw = text if isinstance(text, str) else ""
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest() if raw else ""
    if not raw:
        return "", digest
    try:
        return clean_text("summary", raw[:SUMMARY_MAX], max_len=SUMMARY_MAX, multiline=True), digest
    except SecretDetected:
        return "[redaktə olundu: sirr bənzər məzmun]", digest
    except ContractError:
        return "[redaktə olundu: yararsız simvollar]", digest


class _Backend:
    def __init__(self, conn: LockedConnection, clock: Callable[[], datetime] | None = None) -> None:
        self._db, self._clock = conn, clock or (lambda: datetime.now(timezone.utc))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > STORE_SCHEMA_VERSION:
            raise UnsupportedVersion("goal store daha yeni sxem versiyasındadır")
        with conn.transaction():
            for stmt in [s.strip() for s in _DDL.split(";\n") if s.strip()]:
                conn.execute(stmt if not stmt.upper().startswith("CREATE TRIGGER") else stmt + ";")
            conn.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ ortaq
    def now(self) -> datetime:
        t = self._clock()
        if t.tzinfo is None:
            raise ContractError("saat timezone-aware olmalıdır", "naive_datetime")
        return t.astimezone(timezone.utc)

    def _ts(self) -> str:
        return fmt_dt(self.now())

    def _journal(self, kind: str, goal_id: str = "", plan_id: str = "", step_id: str = "", **detail: Any) -> None:
        text = canonical_json({k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:300]) for k, v in detail.items()})
        self._db.execute("INSERT INTO journal (ts, goal_id, plan_id, step_id, kind, detail) VALUES (?,?,?,?,?,?)", (self._ts(), goal_id, plan_id, step_id, kind, text))

    def _outbox(self, event_type: str, kind: str, entity_id: str, payload: dict[str, Any], op_id: str, correlation_id: str) -> None:
        body = canonical_json({"event_type": event_type, "entity": {"kind": kind, "entity_id": entity_id}, "payload": payload, "op_id": op_id, "correlation_id": correlation_id})
        self._db.execute("INSERT OR IGNORE INTO outbox (op_id, body, ts) VALUES (?,?,?)", (op_id, body, self._ts()))

    def _op_check(self, op_id: str, kind: str, subject: str, content_hash: str) -> OpResult | None:
        row = self._db.execute("SELECT kind, subject, content_hash, result_version FROM ops WHERE op_id = ?", (op_id,)).fetchone()
        if row is None:
            return None
        if (row[0], row[1], row[2]) != (kind, subject, content_hash):
            raise OpConflict()
        return OpResult(True, int(row[3]))

    def _op_put(self, op_id: str, kind: str, subject: str, content_hash: str, version: int) -> None:
        self._db.execute("INSERT INTO ops (op_id, kind, subject, content_hash, result_version) VALUES (?,?,?,?,?)", (op_id, kind, subject, content_hash, version))

    # ------------------------------------------------------------------ goals
    def _load_goal(self, goal_id: str) -> tuple[Goal, int]:
        row = self._db.execute("SELECT version, doc, content_hash FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        if row is None:
            raise StoreError("məqsəd tapılmadı", "not_found")
        try:
            goal = Goal.from_json(row[1])
        except ContractError as exc:
            raise StoreIntegrityError() from exc
        if goal.content_hash != row[2] or goal.goal_id != goal_id:
            raise StoreIntegrityError()
        return goal, int(row[0])

    def create_goal(self, goal: Goal, op_id: str, actor: Actor) -> OpResult:
        _op(op_id)
        if not isinstance(goal, Goal) or goal.status is not GoalStatus.DRAFT:
            raise NotPermitted("yeni məqsəd yalnız DRAFT ola bilər (aktivləşdirmə yalnız sahib)")
        with self._lock, self._db.transaction():
            dup = self._op_check(op_id, "goal.create", goal.goal_id, goal.content_hash)
            if dup is not None:
                return dup
            if self._db.execute("SELECT 1 FROM goals WHERE goal_id = ?", (goal.goal_id,)).fetchone():
                raise OpConflict("goal_id artıq mövcuddur")
            self._db.execute("INSERT INTO goals (goal_id, version, status, priority, created_at, doc, content_hash) VALUES (?,?,?,?,?,?,?)",
                             (goal.goal_id, 1, goal.status.value, goal.priority.value, fmt_dt(goal.created_at), goal.to_json(), goal.content_hash))
            self._op_put(op_id, "goal.create", goal.goal_id, goal.content_hash, 1)
            self._outbox("goal.created", "goal", goal.goal_id, {"title": goal.title}, f"goal.created:{goal.goal_id}", goal.goal_id)
            self._journal("goal_created", goal.goal_id, actor=actor.value)
            return OpResult(False, 1)

    def get_goal(self, goal_id: str) -> tuple[Goal, int] | None:
        try:
            return self._load_goal(goal_id)
        except StoreError as exc:
            if exc.code == "not_found":
                return None
            raise

    def list_goals(self, status: GoalStatus | None = None, limit: int = 100) -> list[tuple[Goal, int]]:
        lim = max(1, min(int(limit), 500))
        rows = self._db.execute(f"SELECT goal_id FROM goals {'WHERE status = ?' if status else ''} ORDER BY created_at, goal_id LIMIT ?",
                                ((status.value, lim) if status else (lim,))).fetchall()
        return [self._load_goal(r[0]) for r in rows]

    def transition_goal(self, goal_id: str, to: GoalStatus, actor: Actor, op_id: str, expected_version: int, reason: str = "") -> OpResult:
        _op(op_id)
        to = GoalStatus(to)
        reason = clean_text("reason", reason, max_len=300) if reason else ""
        with self._lock, self._db.transaction():
            dup = self._op_check(op_id, "goal.transition", goal_id, sha256_hex(f"{to.value}:{expected_version}"))
            if dup is not None:
                return dup
            goal, version = self._load_goal(goal_id)
            if version != expected_version:
                raise StaleVersion()
            if actor is Actor.ENGINE and (goal.status, to) not in ENGINE_GOAL:
                raise NotPermitted(f"mühərrik bu keçidi edə bilməz: {goal.status.value} -> {to.value} (yalnız sahib)")
            new = goal.transition(to, at=self.now())                       # kernel keçid qaydası (InvalidTransition)
            self._db.execute("UPDATE goals SET version = ?, status = ?, doc = ?, content_hash = ? WHERE goal_id = ?", (version + 1, new.status.value, new.to_json(), new.content_hash, goal_id))
            self._op_put(op_id, "goal.transition", goal_id, sha256_hex(f"{to.value}:{expected_version}"), version + 1)
            payload = {"from_status": goal.status.value, "to_status": to.value}
            if reason:
                payload["reason"] = reason
            self._outbox("goal.status_changed", "goal", goal_id, payload, f"goal.status:{goal_id}:v{version + 1}", goal_id)
            self._journal("goal_transition", goal_id, actor=actor.value, from_status=goal.status.value, to_status=to.value, reason=reason)
            return OpResult(False, version + 1)

    # ------------------------------------------------------------------ plans
    def _load_plan(self, plan_id: str, version: int | None = None) -> tuple[Plan, int, bool]:
        sql = "SELECT version, doc, content_hash, superseded FROM plans WHERE plan_id = ?"
        row = (self._db.execute(sql + " AND version = ?", (plan_id, version)) if version is not None else self._db.execute(sql + " ORDER BY version DESC LIMIT 1", (plan_id,))).fetchone()
        if row is None:
            raise StoreError("plan tapılmadı", "not_found")
        try:
            plan = Plan.from_json(row[1])
        except ContractError as exc:
            raise StoreIntegrityError() from exc
        if plan.content_hash != row[2] or plan.plan_id != plan_id or plan.version != row[0]:
            raise StoreIntegrityError()
        return plan, int(row[0]), bool(row[3])

    def _insert_plan(self, plan: Plan) -> None:
        self._db.execute("INSERT INTO plans (plan_id, version, goal_id, status, doc, content_hash) VALUES (?,?,?,?,?,?)",
                         (plan.plan_id, plan.version, plan.goal_id, plan.status.value, plan.to_json(), plan.content_hash))
        for s in plan.steps:
            self._db.execute("INSERT INTO steps (plan_id, plan_version, step_id, state, updated_at) VALUES (?,?,?,?,?)", (plan.plan_id, plan.version, s.step_id, StepState.PENDING.value, self._ts()))

    def add_plan(self, plan: Plan, op_id: str, actor: Actor) -> OpResult:
        _op(op_id)
        if not isinstance(plan, Plan) or plan.status is not PlanStatus.DRAFT or plan.version != 1:
            raise NotPermitted("yeni plan yalnız DRAFT və versiya 1 ola bilər")
        with self._lock, self._db.transaction():
            dup = self._op_check(op_id, "plan.add", plan.plan_id, plan.content_hash)
            if dup is not None:
                return dup
            goal, _ = self._load_goal(plan.goal_id)
            if goal.status is not GoalStatus.ACTIVE:
                raise NotPermitted("plan yalnız ACTIVE məqsəd üçün əlavə olunur")
            if self._db.execute("SELECT 1 FROM plans WHERE plan_id = ?", (plan.plan_id,)).fetchone():
                raise OpConflict("plan_id artıq mövcuddur")
            for p in self._goal_plans(plan.goal_id):
                if p.status in _ACTIVE_PLAN:
                    raise NotPermitted("məqsədin artıq aktiv planı var (əvvəlcə SUPERSEDED/CANCELLED olmalıdır)")
            self._insert_plan(plan)
            self._op_put(op_id, "plan.add", plan.plan_id, plan.content_hash, 1)
            self._outbox("plan.created", "plan", plan.plan_id, {"goal_id": plan.goal_id, "version": 1, "step_count": len(plan.steps)}, f"plan.created:{plan.plan_id}:v1", plan.goal_id)
            self._journal("plan_added", plan.goal_id, plan.plan_id, actor=actor.value, origin=plan.origin.value, steps=len(plan.steps))
            return OpResult(False, 1)

    def _goal_plans(self, goal_id: str) -> list[Plan]:
        ids = [r[0] for r in self._db.execute("SELECT DISTINCT plan_id FROM plans WHERE goal_id = ? ORDER BY plan_id", (goal_id,)).fetchall()]
        return [self._load_plan(i)[0] for i in ids]

    def get_plan(self, plan_id: str) -> tuple[Plan, int] | None:
        try:
            p, v, _ = self._load_plan(plan_id)
            return p, v
        except StoreError as exc:
            if exc.code == "not_found":
                return None
            raise

    def plans_for_goal(self, goal_id: str) -> list[Plan]:
        return self._goal_plans(goal_id)

    def current_plan(self, goal_id: str) -> Plan | None:
        actives = [p for p in self._goal_plans(goal_id) if p.status in _ACTIVE_PLAN]
        return actives[0] if actives else None

    def revise_plan(self, plan_id: str, steps: tuple[PlanStepSpec, ...], actor: Actor, op_id: str, expected_version: int) -> OpResult:
        _op(op_id)
        if actor is not Actor.OWNER:
            raise NotPermitted("plan yeniləməsi yalnız sahib tərəfindən")
        with self._lock, self._db.transaction():
            plan, version, _ = self._load_plan(plan_id)
            new = plan.revise(tuple(steps), at=self.now())
            dup = self._op_check(op_id, "plan.revise", plan_id, new.content_hash)
            if dup is not None:
                return dup
            if version != expected_version:
                raise StaleVersion()
            self._db.execute("UPDATE plans SET superseded = 1 WHERE plan_id = ? AND version = ?", (plan_id, version))
            self._insert_plan(new)
            self._op_put(op_id, "plan.revise", plan_id, new.content_hash, new.version)
            self._outbox("plan.created", "plan", plan_id, {"goal_id": plan.goal_id, "version": new.version, "step_count": len(new.steps)}, f"plan.created:{plan_id}:v{new.version}", plan.goal_id)
            self._journal("plan_revised", plan.goal_id, plan_id, actor=actor.value, version=new.version)
            return OpResult(False, new.version)

    def transition_plan(self, plan_id: str, to: PlanStatus, actor: Actor, op_id: str, expected_version: int, *, validated_hash: str | None = None, reason: str = "") -> OpResult:
        _op(op_id)
        to = PlanStatus(to)
        reason = clean_text("reason", reason, max_len=300) if reason else ""
        with self._lock, self._db.transaction():
            plan, version, _ = self._load_plan(plan_id)
            subject = sha256_hex(f"{to.value}:{expected_version}:{validated_hash or ''}")
            dup = self._op_check(op_id, "plan.transition", plan_id, subject)
            if dup is not None:
                return dup
            if version != expected_version:
                raise StaleVersion()
            if actor is Actor.ENGINE and (plan.status, to) not in ENGINE_PLAN:
                raise NotPermitted(f"mühərrik bu plan keçidini edə bilməz: {plan.status.value} -> {to.value}")
            if to is PlanStatus.READY and actor is Actor.ENGINE and validated_hash != plan.content_hash:
                raise NotPermitted("READY üçün planın CARİ məzmun hash-i ilə yoxlama nəticəsi tələb olunur")
            if to is PlanStatus.COMPLETED:
                rows = self._db.execute("SELECT state, verified FROM steps WHERE plan_id = ? AND plan_version = ?", (plan_id, version)).fetchall()
                if not rows or any(r[0] != StepState.SUCCEEDED.value for r in rows) or (actor is Actor.ENGINE and any(not r[1] for r in rows)):
                    raise NotPermitted("plan yalnız bütün addımlar uğurlu (mühərrik üçün həm də yoxlanmış) olduqda tamamlana bilər")
            new = plan.transition(to, at=self.now())                       # kernel keçid qaydası
            self._db.execute("UPDATE plans SET status = ?, doc = ?, content_hash = ? WHERE plan_id = ? AND version = ?", (new.status.value, new.to_json(), new.content_hash, plan_id, version))
            self._op_put(op_id, "plan.transition", plan_id, subject, version)
            payload = {"from_status": plan.status.value, "to_status": to.value}
            if reason:
                payload["reason"] = reason
            self._outbox("plan.status_changed", "plan", plan_id, payload, f"plan.status:{plan_id}:v{version}:{to.value}", plan.goal_id)
            self._journal("plan_transition", plan.goal_id, plan_id, actor=actor.value, from_status=plan.status.value, to_status=to.value, reason=reason)
            return OpResult(False, version)

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _rec(r: Any) -> StepRecord:
        return StepRecord(r[0], int(r[1]), r[2], StepState(r[3]), int(r[4]), r[5], r[6], r[7], r[8], bool(r[9]), r[10], r[11])

    _STEP_COLS = "plan_id, plan_version, step_id, state, attempt, op_id, lease_until, summary, output_sha256, verified, error, updated_at"

    def steps(self, plan_id: str) -> list[StepRecord]:
        _, version, _ = self._load_plan(plan_id)
        rows = self._db.execute(f"SELECT {self._STEP_COLS} FROM steps WHERE plan_id = ? AND plan_version = ? ORDER BY rowid", (plan_id, version)).fetchall()
        return [self._rec(r) for r in rows]

    def mark_awaiting(self, plan_id: str, version: int, step_id: str) -> bool:
        with self._lock, self._db.transaction():
            cur = self._db.execute("UPDATE steps SET state = ?, updated_at = ? WHERE plan_id = ? AND plan_version = ? AND step_id = ? AND state IN (?, ?)",
                                   (StepState.AWAITING_APPROVAL.value, self._ts(), plan_id, version, step_id, StepState.PENDING.value, StepState.AWAITING_APPROVAL.value))
            return cur.rowcount == 1

    def begin_step(self, plan_id: str, version: int, step_id: str, op_id: str, attempt: int, lease_s: float) -> bool:
        """Atomik CAS: PENDING/AWAITING_APPROVAL -> RUNNING yalnız plan CARİ versiya və READY/EXECUTING olduqda, attempt = köhnə+1. Lease bitəndə `recover_expired` UNKNOWN edir."""
        _op(op_id)
        with self._lock, self._db.transaction():
            plan, cur_version, _ = self._load_plan(plan_id)
            if cur_version != version or plan.status not in (PlanStatus.READY, PlanStatus.EXECUTING):
                return False
            row = self._db.execute("SELECT state, attempt FROM steps WHERE plan_id = ? AND plan_version = ? AND step_id = ?", (plan_id, version, step_id)).fetchone()
            if row is None or row[0] not in (StepState.PENDING.value, StepState.AWAITING_APPROVAL.value) or int(row[1]) + 1 != attempt:
                return False
            self._db.execute("UPDATE steps SET state = ?, attempt = ?, op_id = ?, lease_until = ?, updated_at = ? WHERE plan_id = ? AND plan_version = ? AND step_id = ?",
                             (StepState.RUNNING.value, attempt, op_id, self.now().timestamp() + float(lease_s), self._ts(), plan_id, version, step_id))
            self._journal("step_started", plan.goal_id, plan_id, step_id, attempt=attempt, op_id=op_id)
            return True

    def finish_step(self, plan_id: str, version: int, step_id: str, op_id: str, state: StepState, summary: str, output_sha: str, verified: bool, error: str) -> bool:
        """Yalnız RUNNING və eyni op_id olduqda (UNKNOWN-a keçmiş addımın gec nəticəsi YAZILMIR: sahib həll edir)."""
        state = StepState(state)
        if state not in (StepState.SUCCEEDED, StepState.FAILED, StepState.BLOCKED, StepState.UNKNOWN):
            raise ContractError("finish_step: yanlış son vəziyyət", "bad_state")
        summary, digest = safe_summary(summary)
        output_sha = output_sha if isinstance(output_sha, str) and re.fullmatch(r"[0-9a-f]{64}", output_sha) else digest        # icraçının verdiyi hash da ETİBARSIZDIR
        error = clean_text("error", error[:200], max_len=200) if isinstance(error, str) and error else ""
        with self._lock, self._db.transaction():
            plan, _, _ = self._load_plan(plan_id, version)
            cur = self._db.execute("UPDATE steps SET state = ?, summary = ?, output_sha256 = ?, verified = ?, error = ?, lease_until = NULL, updated_at = ? "
                                   "WHERE plan_id = ? AND plan_version = ? AND step_id = ? AND state = ? AND op_id = ?",
                                   (state.value, summary, output_sha, 1 if verified else 0, error, self._ts(), plan_id, version, step_id, StepState.RUNNING.value, op_id))
            self._journal("step_finished" if cur.rowcount == 1 else "step_late_result_ignored", plan.goal_id, plan_id, step_id, state=state.value, verified=verified, error=error)
            return cur.rowcount == 1

    def block_step(self, plan_id: str, version: int, step_id: str, reason: str) -> bool:
        reason = clean_text("reason", reason[:200], max_len=200) if reason else ""
        with self._lock, self._db.transaction():
            plan, _, _ = self._load_plan(plan_id, version)
            cur = self._db.execute("UPDATE steps SET state = ?, error = ?, updated_at = ? WHERE plan_id = ? AND plan_version = ? AND step_id = ? AND state IN (?, ?)",
                                   (StepState.BLOCKED.value, reason, self._ts(), plan_id, version, step_id, StepState.PENDING.value, StepState.AWAITING_APPROVAL.value))
            if cur.rowcount == 1:
                self._journal("step_blocked", plan.goal_id, plan_id, step_id, reason=reason)
            return cur.rowcount == 1

    def renew_lease(self, plan_id: str, version: int, step_id: str, op_id: str, lease_s: float) -> bool:
        """Lease yenilənməsi (heartbeat): yalnız RUNNING və eyni op_id olduqda. UNKNOWN-a keçmiş addımın lease-i YENİLƏNMİR (False)."""
        with self._lock, self._db.transaction():
            cur = self._db.execute("UPDATE steps SET lease_until = ?, updated_at = ? WHERE plan_id = ? AND plan_version = ? AND step_id = ? AND state = ? AND op_id = ?",
                                   (self.now().timestamp() + float(lease_s), self._ts(), plan_id, version, step_id, StepState.RUNNING.value, op_id))
            return cur.rowcount == 1

    def health_counts(self) -> dict[str, int]:
        """Əməliyyat sağlamlığı üçün yalnız SAYLAR (məzmun yoxdur)."""
        counts = {s.value: 0 for s in StepState}
        for state, n in self._db.execute("SELECT state, COUNT(*) FROM steps GROUP BY state").fetchall():
            counts[state] = int(n)
        counts["outbox_pending"] = self.pending_outbox()
        return counts

    def recover_expired(self) -> list[tuple[str, str]]:
        """Lease-i bitmiş RUNNING addımlar UNKNOWN olur (avtomatik təkrar YOXDUR). Qaytarır: [(plan_id, step_id)]."""
        now = self.now().timestamp()
        out: list[tuple[str, str]] = []
        with self._lock, self._db.transaction():
            rows = self._db.execute("SELECT plan_id, plan_version, step_id FROM steps WHERE state = ? AND lease_until IS NOT NULL AND lease_until < ?", (StepState.RUNNING.value, now)).fetchall()
            for plan_id, ver, step_id in rows:
                self._db.execute("UPDATE steps SET state = ?, updated_at = ? WHERE plan_id = ? AND plan_version = ? AND step_id = ? AND state = ?",
                                 (StepState.UNKNOWN.value, self._ts(), plan_id, ver, step_id, StepState.RUNNING.value))
                plan, _, _ = self._load_plan(plan_id, ver)
                self._journal("step_unknown_after_crash", plan.goal_id, plan_id, step_id)
                out.append((plan_id, step_id))
        return out

    def resolve_step(self, plan_id: str, step_id: str, resolution: str, actor: Actor, approval_ref: str, note: str = "") -> bool:
        """YALNIZ sahib: UNKNOWN/BLOCKED/FAILED addımı həll edir (retry -> PENDING; failed; succeeded=yoxlanmamış). `approval_ref` sahib qapısının sübutuna istinaddır."""
        if actor is not Actor.OWNER:
            raise NotPermitted("addımı yalnız sahib həll edə bilər")
        if resolution not in ("retry", "failed", "succeeded") or not isinstance(approval_ref, str) or not _APPROVAL_REF.match(approval_ref):
            raise ContractError("resolve_step: yanlış həll və ya approval_ref", "bad_resolution")
        note = clean_text("note", note, max_len=300) if note else ""
        new_state = {"retry": StepState.PENDING, "failed": StepState.FAILED, "succeeded": StepState.SUCCEEDED}[resolution]
        with self._lock, self._db.transaction():
            plan, version, _ = self._load_plan(plan_id)
            cur = self._db.execute("UPDATE steps SET state = ?, verified = 0, lease_until = NULL, error = ?, updated_at = ? WHERE plan_id = ? AND plan_version = ? AND step_id = ? AND state IN (?, ?, ?)",
                                   (new_state.value, note, self._ts(), plan_id, version, step_id, StepState.UNKNOWN.value, StepState.BLOCKED.value, StepState.FAILED.value))
            if cur.rowcount == 1:
                self._journal("step_resolved_by_owner", plan.goal_id, plan_id, step_id, resolution=resolution, approval_ref=approval_ref, note=note)
            return cur.rowcount == 1

    # ------------------------------------------------------------------ jurnal / outbox
    def record(self, kind: str, goal_id: str = "", plan_id: str = "", step_id: str = "", **detail: Any) -> None:
        with self._lock, self._db.transaction():
            self._journal(kind[:60], goal_id, plan_id, step_id, **detail)

    def journal_entries(self, goal_id: str | None = None, limit: int = 200, after_seq: int = 0) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 1000))
        after = max(0, int(after_seq))
        rows = (self._db.execute("SELECT seq, ts, goal_id, plan_id, step_id, kind, detail FROM journal WHERE goal_id = ? AND seq > ? ORDER BY seq LIMIT ?", (goal_id, after, lim)) if goal_id
                else self._db.execute("SELECT seq, ts, goal_id, plan_id, step_id, kind, detail FROM journal WHERE seq > ? ORDER BY seq LIMIT ?", (after, lim))).fetchall()
        return [{"seq": r[0], "ts": r[1], "goal_id": r[2], "plan_id": r[3], "step_id": r[4], "kind": r[5], "detail": json.loads(r[6])} for r in rows]

    def flush_outbox(self, log: EventLog, limit: int = 200) -> int:
        """Gözləyən goal/plan hadisələrini kernel `EventLog`-a (idempotent) köçürür; xətada qalanlar növbəti flush-a qalır."""
        from kernel.common import Origin, parse_dt
        done = 0
        with self._lock:
            rows = self._db.execute("SELECT seq, body, ts FROM outbox WHERE flushed = 0 ORDER BY seq LIMIT ?", (max(1, min(int(limit), 1000)),)).fetchall()
        for seq, body, ts in rows:
            d = json.loads(body)
            ev = Event.create(d["event_type"], EntityRef(d["entity"]["kind"], d["entity"]["entity_id"]), d["payload"], op_id=d["op_id"], correlation_id=d["correlation_id"],
                              origin=Origin.SYSTEM, timestamp=parse_dt("ts", ts))
            log.append(ev)
            with self._lock, self._db.transaction():
                self._db.execute("UPDATE outbox SET flushed = 1 WHERE seq = ?", (seq,))
            done += 1
        return done

    def pending_outbox(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM outbox WHERE flushed = 0").fetchone()[0])


class EngineGoalStore:
    """Döngünün görən üzü: məqsədi AKTİVLƏŞDİRƏ/TAMAMLAYA/LƏĞV EDƏ/DAVAM ETDİRƏ BİLMİR, planı yeniləyə və addımı sahib adından həll edə bilmir."""
    __slots__ = ("_b",)

    def __init__(self, backend: _Backend) -> None:
        object.__setattr__(self, "_b", backend)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")

    def now(self) -> datetime: return self._b.now()
    def create_goal(self, goal: Goal, op_id: str) -> OpResult: return self._b.create_goal(goal, op_id, Actor.ENGINE)
    def get_goal(self, goal_id: str): return self._b.get_goal(goal_id)
    def list_goals(self, status: GoalStatus | None = None, limit: int = 100): return self._b.list_goals(status, limit)
    def transition_goal(self, goal_id: str, to: GoalStatus, op_id: str, expected_version: int, reason: str = "") -> OpResult:
        return self._b.transition_goal(goal_id, to, Actor.ENGINE, op_id, expected_version, reason)
    def add_plan(self, plan: Plan, op_id: str) -> OpResult: return self._b.add_plan(plan, op_id, Actor.ENGINE)
    def get_plan(self, plan_id: str): return self._b.get_plan(plan_id)
    def plans_for_goal(self, goal_id: str): return self._b.plans_for_goal(goal_id)
    def current_plan(self, goal_id: str): return self._b.current_plan(goal_id)
    def transition_plan(self, plan_id: str, to: PlanStatus, op_id: str, expected_version: int, *, validated_hash: str | None = None, reason: str = "") -> OpResult:
        return self._b.transition_plan(plan_id, to, Actor.ENGINE, op_id, expected_version, validated_hash=validated_hash, reason=reason)
    def steps(self, plan_id: str): return self._b.steps(plan_id)
    def mark_awaiting(self, plan_id: str, version: int, step_id: str) -> bool: return self._b.mark_awaiting(plan_id, version, step_id)
    def begin_step(self, plan_id: str, version: int, step_id: str, op_id: str, attempt: int, lease_s: float) -> bool: return self._b.begin_step(plan_id, version, step_id, op_id, attempt, lease_s)
    def finish_step(self, *a: Any, **k: Any) -> bool: return self._b.finish_step(*a, **k)
    def block_step(self, plan_id: str, version: int, step_id: str, reason: str) -> bool: return self._b.block_step(plan_id, version, step_id, reason)
    def recover_expired(self): return self._b.recover_expired()
    def renew_lease(self, plan_id: str, version: int, step_id: str, op_id: str, lease_s: float) -> bool: return self._b.renew_lease(plan_id, version, step_id, op_id, lease_s)
    def health_counts(self) -> dict[str, int]: return self._b.health_counts()
    def record(self, kind: str, goal_id: str = "", plan_id: str = "", step_id: str = "", **detail: Any) -> None: self._b.record(kind, goal_id, plan_id, step_id, **detail)
    def journal_entries(self, goal_id: str | None = None, limit: int = 200, after_seq: int = 0): return self._b.journal_entries(goal_id, limit, after_seq)
    def flush_outbox(self, log: EventLog) -> int: return self._b.flush_outbox(log)


class OwnerGoalStore:
    """YALNIZ sahib qapısı tərəfi (API/Android step-up arxasında). Bu obyekt döngüyə/agentlərə/modellərə VERİLMİR."""
    __slots__ = ("_b",)

    def __init__(self, backend: _Backend) -> None:
        object.__setattr__(self, "_b", backend)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")

    def now(self) -> datetime: return self._b.now()
    def create_goal(self, goal: Goal, op_id: str) -> OpResult: return self._b.create_goal(goal, op_id, Actor.OWNER)
    def get_goal(self, goal_id: str): return self._b.get_goal(goal_id)
    def list_goals(self, status: GoalStatus | None = None, limit: int = 100): return self._b.list_goals(status, limit)
    def transition_goal(self, goal_id: str, to: GoalStatus, op_id: str, expected_version: int, reason: str = "") -> OpResult:
        return self._b.transition_goal(goal_id, to, Actor.OWNER, op_id, expected_version, reason)
    def add_plan(self, plan: Plan, op_id: str) -> OpResult: return self._b.add_plan(plan, op_id, Actor.OWNER)
    def revise_plan(self, plan_id: str, steps: tuple[PlanStepSpec, ...], op_id: str, expected_version: int) -> OpResult:
        return self._b.revise_plan(plan_id, steps, Actor.OWNER, op_id, expected_version)
    def get_plan(self, plan_id: str): return self._b.get_plan(plan_id)
    def current_plan(self, goal_id: str): return self._b.current_plan(goal_id)
    def transition_plan(self, plan_id: str, to: PlanStatus, op_id: str, expected_version: int, reason: str = "") -> OpResult:
        return self._b.transition_plan(plan_id, to, Actor.OWNER, op_id, expected_version, reason=reason)
    def steps(self, plan_id: str): return self._b.steps(plan_id)
    def resolve_step(self, plan_id: str, step_id: str, resolution: str, approval_ref: str, note: str = "") -> bool:
        return self._b.resolve_step(plan_id, step_id, resolution, Actor.OWNER, approval_ref, note)
    def journal_entries(self, goal_id: str | None = None, limit: int = 200, after_seq: int = 0): return self._b.journal_entries(goal_id, limit, after_seq)
    def health_counts(self) -> dict[str, int]: return self._b.health_counts()


@dataclass(frozen=True)
class StoreFaces:
    engine: EngineGoalStore
    owner: OwnerGoalStore


def build_store(conn: LockedConnection, clock: Callable[[], datetime] | None = None) -> StoreFaces:
    """Bir saxlanc, iki üz. `engine` döngüyə, `owner` yalnız sahib qapısına verilməlidir."""
    b = _Backend(conn, clock)
    return StoreFaces(EngineGoalStore(b), OwnerGoalStore(b))
