"""Davamlı workflow: plan və addım nəticələri SQLite-da saxlanır; yarımçıq iş təhlükəsiz davam etdirilir.

Təhlükəsiz davam etdirmə qaydaları:
- Plan və məqsəd saxlanmış nüsxədən götürülür (davam zamanı dəyişdirilə bilməz); plan hash-i yoxlanır (`WorkflowTampered`).
- Tamamlanmış addımlar yenidən işləmir və yenidən hesablanmır (restored). İcra olunmuş uğursuz addımlar
  (`error/failed_check/duplicate`) YALNIZ `retry_failed=True` ilə yeni cəhd (yeni op_id) alır.
- Naməlum xərcli rezervi olan addım sahib uzlaşdırana qədər (`ledger.commit/release`) işləmir; rezerv avtomatik silinmir.
- Təsdiq gözləyən addım eyni op_id ilə yenidən göndərilir (təsdiq hash ilə tapılır, bir dəfə istifadə olunur).
- `lease`: eyni işin eyni anda iki dəfə davam etdirilməsi rədd edilir; çökmüş proses lease bitəndə bərpa olunur.
- Cəhd limiti (`max_retries`) davam etdirmələr arasında da qüvvədədir.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from agents.ceo import CEO, JobReport, ResumeState, StepReport
from agents.workflow import PlanError, PlanStep, execution_order

DEFAULT_DB = Path("data/workflows.db")
JOB_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_RESUMABLE = frozenset({"created", "interrupted", "partial", "failed", "rejected", "no_plan"})
_EXECUTED_FAILURES = frozenset({"error", "failed_check", "duplicate"})
_NOT_EXECUTED = frozenset({"pending_approval", "blocked", "unrouted"})

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS workflows (
        job_id TEXT PRIMARY KEY, goal TEXT NOT NULL, plan_json TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
        status TEXT NOT NULL, lease_expires REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS workflow_steps (
        job_id TEXT NOT NULL, step_id TEXT NOT NULL, department TEXT NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL, op_id TEXT NOT NULL, agent TEXT NOT NULL, output_json TEXT NOT NULL,
        reason TEXT NOT NULL, approval_id TEXT NOT NULL, needs_reconciliation INTEGER NOT NULL,
        updated_at REAL NOT NULL, PRIMARY KEY (job_id, step_id))""",
]


class WorkflowError(Exception):
    pass


class WorkflowExists(WorkflowError):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowBusy(WorkflowError):
    pass


class WorkflowTampered(WorkflowError):
    pass


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    department: str
    status: str
    attempts: int
    op_id: str
    agent: str
    output: Any
    reason: str
    approval_id: str
    needs_reconciliation: bool

    def to_report(self) -> StepReport:
        return StepReport(self.department, self.status, self.agent, self.output, self.reason, self.step_id,
                          self.attempts, self.op_id, self.needs_reconciliation, self.approval_id)


@dataclass(frozen=True)
class JobInfo:
    job_id: str
    status: str
    updated_at: float
    lease_expires: float


@dataclass(frozen=True)
class StoredWorkflow:
    job_id: str
    goal: str
    steps: tuple[PlanStep, ...]
    status: str
    records: dict[str, StepRecord]
    plan_sha256: str


def _plan_json(goal: str, steps: Sequence[PlanStep]) -> str:
    return json.dumps({"goal": goal, "steps": [
        {"id": s.id, "department": s.department, "permission": s.permission, "cost": float(s.cost),
         "depends_on": list(s.depends_on), "description": s.description} for s in steps]},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class WorkflowStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB, clock: Callable[[], float] = time.time,
                 lease_s: float = 300.0) -> None:
        if isinstance(lease_s, bool) or not isinstance(lease_s, (int, float)) or lease_s <= 0:
            raise ValueError("lease_s müsbət rəqəm olmalıdır")
        self.clock = clock
        self.lease_s = float(lease_s)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        for stmt in _SCHEMA:
            self._conn.execute(stmt)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def create(self, job_id: str, goal: str, steps: Sequence[PlanStep]) -> None:
        if not isinstance(job_id, str) or not JOB_RE.match(job_id):
            raise ValueError("job_id yanlışdır")
        plan = _plan_json(goal, steps)
        now = float(self.clock())
        try:
            self._conn.execute(
                "INSERT INTO workflows (job_id, goal, plan_json, plan_sha256, status, lease_expires, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, 'created', 0, ?, ?)",
                (job_id, goal, plan, hashlib.sha256(plan.encode("utf-8")).hexdigest(), now, now))
        except sqlite3.IntegrityError:
            raise WorkflowExists(f"iş artıq mövcuddur: {job_id}") from None

    def _load(self, job_id: str) -> StoredWorkflow:
        row = self._conn.execute("SELECT * FROM workflows WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound(f"iş tapılmadı: {job_id}")
        if hashlib.sha256(row["plan_json"].encode("utf-8")).hexdigest() != row["plan_sha256"]:
            raise WorkflowTampered("saxlanmış plan pozulub (hash uyğun deyil)")
        data = json.loads(row["plan_json"])
        steps = tuple(PlanStep(s["id"], s["department"], s["permission"], float(s["cost"]), tuple(s["depends_on"]),
                               s["description"]) for s in data["steps"])
        recs = {}
        for r in self._conn.execute("SELECT * FROM workflow_steps WHERE job_id = ?", (job_id,)).fetchall():
            recs[r["step_id"]] = StepRecord(r["step_id"], r["department"], r["status"], int(r["attempts"]),
                                            r["op_id"], r["agent"], json.loads(r["output_json"]), r["reason"],
                                            r["approval_id"], bool(r["needs_reconciliation"]))
        return StoredWorkflow(job_id, data["goal"], steps, row["status"], recs, row["plan_sha256"])

    def load(self, job_id: str) -> StoredWorkflow:
        return self._load(job_id)

    def list_jobs(self, statuses: set[str] | None = None) -> list[JobInfo]:
        """İşlərin yüngül siyahısı (plan və nəticələr yüklənmir): təhlil/bildiriş üçün."""
        rows = self._conn.execute("SELECT job_id, status, updated_at, lease_expires FROM workflows "
                                  "ORDER BY updated_at, job_id").fetchall()
        return [JobInfo(r["job_id"], r["status"], float(r["updated_at"]), float(r["lease_expires"]))
                for r in rows if statuses is None or r["status"] in statuses]

    def acquire(self, job_id: str) -> StoredWorkflow:
        """İşi davam etdirmək üçün lease alır (atomik). Başqası işləyirsə WorkflowBusy."""
        with self._tx():
            wf = self._load(job_id)
            row = self._conn.execute("SELECT status, lease_expires FROM workflows WHERE job_id = ?",
                                     (job_id,)).fetchone()
            now = float(self.clock())
            if row["status"] == "running" and row["lease_expires"] > now:
                raise WorkflowBusy("iş başqa proses tərəfindən icra olunur (lease bitməyib)")
            if row["status"] not in _RESUMABLE and row["status"] != "running":
                raise WorkflowBusy(f"iş bu vəziyyətdə davam etdirilə bilməz: {row['status']}")
            self._conn.execute("UPDATE workflows SET status = 'running', lease_expires = ?, updated_at = ? "
                               "WHERE job_id = ?", (now + self.lease_s, now, job_id))
        return wf

    def record_step(self, job_id: str, step: PlanStep, report: StepReport) -> None:
        out = json.dumps(report.output, ensure_ascii=False, default=str)
        now = float(self.clock())
        # "skipped" (asılılıq/stop/uzlaşdırma) həqiqi icra qeydini ASLA əzmir: cəhd sayı və uğursuzluq itmir.
        if report.status == "skipped":
            conflict = "ON CONFLICT(job_id, step_id) DO NOTHING"
        else:
            conflict = ("ON CONFLICT(job_id, step_id) DO UPDATE SET department = excluded.department, "
                        "status = excluded.status, attempts = excluded.attempts, op_id = excluded.op_id, "
                        "agent = excluded.agent, output_json = excluded.output_json, reason = excluded.reason, "
                        "approval_id = excluded.approval_id, "
                        "needs_reconciliation = excluded.needs_reconciliation, updated_at = excluded.updated_at")
        self._conn.execute(
            "INSERT INTO workflow_steps (job_id, step_id, department, status, attempts, op_id, agent, output_json, "
            "reason, approval_id, needs_reconciliation, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            + conflict,
            (job_id, step.id, step.department, report.status, report.attempts, report.op_id, report.agent, out,
             report.reason, report.approval_id, int(report.needs_reconciliation), now))

    def finish(self, job_id: str, status: str) -> None:
        self._conn.execute("UPDATE workflows SET status = ?, lease_expires = 0, updated_at = ? WHERE job_id = ?",
                           (status, float(self.clock()), job_id))

    def mark_interrupted(self, job_id: str) -> None:
        self.finish(job_id, "interrupted")

    def report(self, job_id: str) -> JobReport:
        wf = self._load(job_id)
        steps = []
        for s in execution_order(wf.steps):
            rec = wf.records.get(s.id)
            if rec is not None:
                steps.append(StepReport(**{**rec.to_report().__dict__, "restored": True}))
        return JobReport(wf.goal, wf.status, tuple(steps))


class DurableRunner:
    def __init__(self, ceo: CEO, store: WorkflowStore) -> None:
        self.ceo = ceo
        self.store = store

    def start(self, goal: str, steps: Sequence[PlanStep], job_id: str) -> JobReport:
        try:
            execution_order(steps)
        except PlanError as exc:  # etibarsız plan saxlanmır
            return JobReport(goal, "rejected", reason=str(exc))
        self.store.create(job_id, goal, steps)
        return self._run(job_id, retry_failed=False, fresh=True)

    def resume(self, job_id: str, retry_failed: bool = False) -> JobReport:
        wf = self.store.load(job_id)
        if wf.status == "ok":
            return self.store.report(job_id)  # artıq bitib: heç nə icra olunmur
        return self._run(job_id, retry_failed=retry_failed, fresh=False)

    def _run(self, job_id: str, *, retry_failed: bool, fresh: bool) -> JobReport:
        wf = self.store.acquire(job_id)
        state = None if fresh else self._state(wf, retry_failed)
        try:
            report = self.ceo.run_workflow(wf.goal, wf.steps, job_id, resume=state,
                                           on_step=lambda s, r: self.store.record_step(job_id, s, r))
        except BaseException:
            self.store.mark_interrupted(job_id)
            raise
        self.store.finish(job_id, report.status)
        return report

    def _state(self, wf: StoredWorkflow, retry_failed: bool) -> ResumeState:
        ledger = self.ceo.orchestrator.ledger
        unknown = {e.op_id for e in ledger.unknown_entries()} if ledger is not None else set()
        completed: dict[str, StepReport] = {}
        terminal: dict[str, StepReport] = {}
        attempts: dict[str, int] = {}
        blocked: dict[str, str] = {}
        for step in wf.steps:
            rec = wf.records.get(step.id)
            prefix = f"{wf.job_id}:{step.id}:a"
            held = sorted(o for o in unknown if o.startswith(prefix))
            if rec is not None and rec.status == "ok":
                completed[step.id] = rec.to_report()
            elif rec is not None and rec.status in _EXECUTED_FAILURES and not retry_failed:
                terminal[step.id] = rec.to_report()
            elif held:
                blocked[step.id] = (f"naməlum xərc: əl ilə uzlaşdırma lazımdır ({', '.join(held)}); "
                                    "ledger.commit/release ilə həll edin")
            elif rec is None or rec.status == "skipped":
                attempts[step.id] = 0
            elif rec.status in _NOT_EXECUTED:
                attempts[step.id] = max(rec.attempts - 1, 0)
            else:  # icra olunmuş uğursuzluq, retry_failed=True
                attempts[step.id] = rec.attempts
        return ResumeState(completed, terminal, attempts, blocked)
