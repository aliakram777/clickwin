"""CEO işləri: LLM yalnız TƏKLİF edir (`LLMPlanner`), icra yalnız sahibin açıq `run` sorğusu ilə və yalnız
`DurableRunner` -> CEO -> Orkestrator -> Broker/Gate/Approval/Ledger yolu ilə. Burada alət, broker və ya kitab çağırışı yoxdur."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from agents.durable import DurableRunner, WorkflowError
from agents.mobile import clean_text
from agents.memory import detect_secrets
from agents.workflow import PlanStep
from safety.dbconn import connect


class JobNotFound(KeyError):
    pass


class JobConflict(Exception):
    pass


def _preview(output: Any) -> dict[str, Any]:
    try:
        text = json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(output)
    text = clean_text(text, 200)
    return {"text": "[gizli]" if detect_secrets(text) else text, "untrusted": True}


def _step_dto(s: PlanStep) -> dict[str, Any]:
    return {"id": s.id, "department": s.department, "permission": s.permission, "cost": float(s.cost),
            "depends_on": list(s.depends_on), "description": s.description}


def _report_dto(report: Any) -> dict[str, Any]:
    steps = [{"id": s.step_id, "department": s.department, "status": s.status, "agent": s.agent,
              "reason": clean_text(s.reason, 200), "attempts": s.attempts, "approval_id": s.approval_id,
              "needs_reconciliation": bool(s.needs_reconciliation), "output_preview": _preview(s.output)}
             for s in report.steps]
    return {"status": report.status, "reason": clean_text(report.reason, 200), "steps": steps,
            "pending_approvals": [s["approval_id"] for s in steps if s["status"] == "pending_approval" and s["approval_id"]]}


class JobService:
    def __init__(self, planner: Any, durable: DurableRunner, db_path: str | Path,
                 clock: Callable[[], float] = time.time) -> None:
        self.planner, self.durable, self.clock = planner, durable, clock
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect(str(db_path), isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, "
            "ok INTEGER NOT NULL, reason TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, "
            "is_mock INTEGER NOT NULL, steps_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")

    def close(self) -> None:
        self._conn.close()

    def _row(self, job_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return row

    def _dto(self, row: sqlite3.Row, report: Any = None) -> dict[str, Any]:
        out = {"id": row["id"], "goal": row["goal"], "status": row["status"], "ok": bool(row["ok"]),
               "reason": row["reason"], "provider": row["provider"], "model": row["model"],
               "is_mock": bool(row["is_mock"]), "steps": json.loads(row["steps_json"]),
               "created_at": row["created_at"], "updated_at": row["updated_at"]}
        if report is not None:
            out["report"] = _report_dto(report)
        return out

    def propose(self, goal: str) -> dict[str, Any]:
        prop = self.planner.propose(goal)
        job_id, now = "job_" + secrets.token_hex(8), self.clock()
        steps = [_step_dto(s) for s in prop.steps]
        self._conn.execute(
            "INSERT INTO jobs (id, goal, status, ok, reason, provider, model, is_mock, steps_json, created_at, "
            "updated_at) VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, goal, int(prop.ok), clean_text(prop.reason, 300), prop.provider, prop.model, int(prop.is_mock),
             json.dumps(steps, ensure_ascii=False), now, now))
        return self._dto(self._row(job_id))

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._dto(r) for r in rows]

    def get(self, job_id: str) -> dict[str, Any]:
        row = self._row(job_id)
        report = None
        if row["status"] != "proposed":
            try:
                report = self.durable.store.report(job_id)
            except WorkflowError:
                report = None
        return self._dto(row, report)

    def run(self, job_id: str) -> dict[str, Any]:
        row = self._row(job_id)
        if not row["ok"]:
            raise JobConflict("təklif etibarsızdır, icra olunmur")
        try:
            if row["status"] == "proposed":
                steps = [PlanStep(s["id"], s["department"], s["permission"], float(s["cost"]),
                                  tuple(s["depends_on"]), s["description"]) for s in json.loads(row["steps_json"])]
                report = self.durable.start(row["goal"], steps, job_id)
            else:
                report = self.durable.resume(job_id)
        except WorkflowError as exc:
            raise JobConflict(type(exc).__name__) from None
        self._conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                           (report.status, self.clock(), job_id))
        return self._dto(self._row(job_id), report)
