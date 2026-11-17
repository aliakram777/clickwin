"""Agent fabriki: ixtisaslaşmış agentlərin yaradılması, idarəsi (dayandır/yenidən başlat/arxivlə/sil), tapşırıq və ötürmə qeydi, nəticə hesabatı.

Təhlükəsizlik modeli (mövcud `Company` qatının üzərində, onu keçmir):
- Agent səlahiyyəti/büdcəsi yalnız `Company` vasitəsilə dəyişir və GENİŞLƏNMƏ yalnız sahibdədir. NAYMERA (sistem) yalnız MƏHDUD agent yarada bilər
  (`read_files`, büdcə 0 AZN, ötürmə yox); agent öz icazəsini artıra bilməz, yalnız sorğu yaza bilər (`request_permission`), sorğunu yalnız sahib həll edir.
- Fabrik heç nə İCRA ETMİR: tapşırıq yalnız qeyd olunur. İcra `agent_runtime` -> Orkestrator -> Effect Broker/Gate/Təsdiq/Ledger yolundadır.
- Agentlərarası məlumat (ötürmə payload-ı, nəticə) ETİBARSIZDIR: təmizlənir, sirr rədd edilir, təlimat bənzərliyi işarələnir və
  `<untrusted_agent_output>` blokuna bükülür; heç vaxt təlimat kimi icra olunmur.
- Hər əməliyyat (qəbul və rədd) audit jurnalına yazılır; sirr jurnala düşmür.
- Büdcə: defolt 0 AZN; qlobal tavan (`global_budget_cap`) yalnız sahib tərəfindən qaldırılır.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from agents.agent_roles import ROLE_CATALOG, MemoryPolicy, RoleTemplate
from agents.company import DEPARTMENTS, Authority, Company, CompanyDenied
from agents.memory import detect_secrets
from agents.orchestrator import Task
from agents.research import detect_injection
from agents.spec import KNOWN_PERMISSIONS, AgentSpec
from safety.approvals import Principal

SPEC_COST_CEILING = 5.0   # spec tavanı: real büdcə Company-də (defolt 0.0) və qlobal tavanla məhdudlaşır
_OP_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_ART_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,120}$")


class FactoryDenied(ValueError):
    def __init__(self, message: str, code: str = "denied") -> None:
        super().__init__(message)
        self.code = code


class ExchangeRejected(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# --- agentlərarası məlumat ---

@dataclass(frozen=True)
class Exchange:
    text: str
    wrapped: str
    suspicious: bool
    flags: tuple[str, ...] = ()


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def validate_exchange(text: Any, *, source: str, max_chars: int = 8000) -> Exchange:
    """Agentlər arası mətni yoxlayır: ölçü, sirr (rədd), idarəedici simvollar (təmizlənir), təlimat bənzərliyi (işarələnir). Nəticə həmişə
    etibarsız məlumat blokuna bükülür."""
    if not isinstance(text, str) or not text.strip():
        raise ExchangeRejected("mətn boşdur", "empty")
    if len(text) > max_chars:
        raise ExchangeRejected(f"mətn çox uzundur (maks. {max_chars})", "too_large")
    clean = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C")).strip()
    if detect_secrets(clean):
        raise ExchangeRejected("mətn sirr bənzər məzmun ehtiva edir", "secret")
    flags = tuple(detect_injection(clean))
    suspicious = bool(flags)
    wrapped = f'<untrusted_agent_output agent="{_esc(source)}" suspicious="{"true" if suspicious else "false"}">{_esc(clean)}</untrusted_agent_output>'
    return Exchange(clean, wrapped, suspicious, flags)


# --- siyasət və qeydlər ---

@dataclass(frozen=True)
class FactoryPolicy:
    max_agents: int = 16
    max_per_role: int = 3
    max_queue: int = 50
    global_budget_cap: float = 0.0      # 0 AZN prioritet: tavanı yalnız sahib qaldırır
    autonomous_creation: bool = True    # NAYMERA-nın (sistem) MƏHDUD agent yaratmasına icazə
    auto_stop_after_failures: int = 3
    max_prompt_chars: int = 6000
    max_handoff_depth: int = 5


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    name: str
    role_id: str
    title: str
    specialization: str
    system_instruction: str
    department: str
    workspace: str
    memory_policy: MemoryPolicy
    status: str              # active | stopped | archived | deleted
    stopped_by: str
    restart_count: int
    failures: int
    created_by: str
    created_at: float
    updated_at: float
    permissions: frozenset[str] = frozenset()   # Company-dən canlı
    budget: float = 0.0


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    agent_id: str
    title: str
    prompt: str
    kind: str
    permission: str
    cost: float
    state: str               # queued | running | awaiting_approval | done | failed | cancelled
    op_id: str
    parent_task_id: str
    created_by: str
    attempts: int
    approval_id: str
    summary: str
    error: str
    suspicious: bool
    created_at: float
    updated_at: float
    run_op: str = ""


@dataclass(frozen=True)
class AgentReport:
    task_id: str
    agent_id: str
    status: str
    summary: str
    artifacts: tuple[str, ...]
    wrapped: str
    suspicious: bool
    cost: float
    ts: float


@dataclass(frozen=True)
class PermissionRequest:
    request_id: str
    agent_id: str
    permission: str
    reason: str
    state: str   # pending | approved | denied
    ts: float


@dataclass(frozen=True)
class EventRecord:
    ts: float
    event: str
    agent_id: str
    detail: dict


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS factory_agents (agent_id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, role_id TEXT NOT NULL, title TEXT NOT NULL, "
    "specialization TEXT NOT NULL, system_instruction TEXT NOT NULL, department TEXT NOT NULL, workspace TEXT NOT NULL, memory_policy_json TEXT NOT NULL, "
    "status TEXT NOT NULL, stopped_by TEXT NOT NULL DEFAULT '', restart_count INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, "
    "created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS factory_tasks (task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, title TEXT NOT NULL, prompt TEXT NOT NULL, kind TEXT NOT NULL, "
    "permission TEXT NOT NULL, cost REAL NOT NULL, state TEXT NOT NULL, op_id TEXT UNIQUE NOT NULL, op_sha TEXT NOT NULL, parent_task_id TEXT NOT NULL DEFAULT '', "
    "created_by TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, approval_id TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
    "error TEXT NOT NULL DEFAULT '', suspicious INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, run_op TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS factory_reports (task_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL, "
    "artifacts_json TEXT NOT NULL, wrapped TEXT NOT NULL, suspicious INTEGER NOT NULL, cost REAL NOT NULL, ts REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS factory_requests (request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, permission TEXT NOT NULL, reason TEXT NOT NULL, "
    "state TEXT NOT NULL, ts REAL NOT NULL, resolved_by TEXT NOT NULL DEFAULT '', resolved_ts REAL NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS factory_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL, agent_id TEXT NOT NULL, detail_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS factory_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_OPEN_TASK_STATES = ("queued", "running", "awaiting_approval")


def _finite(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0


class _Core:
    def __init__(self, company: Company, db_path: str | Path, audit_path: str | Path, workspace_root: str | Path, owners: Iterable[str],
                 allow_mock: bool, clock: Callable[[], float], policy: FactoryPolicy | None) -> None:
        self.company, self.clock, self.allow_mock = company, clock, bool(allow_mock)
        self.owners = frozenset(owners)
        if not self.owners or not all(isinstance(o, str) and o.strip() for o in self.owners):
            raise ValueError("fabrik üçün boş olmayan sahib siyahısı tələb olunur")
        self.audit_path = Path(audit_path)
        self.root = Path(workspace_root)
        self.root.mkdir(parents=True, exist_ok=True)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        for stmt in _SCHEMA:
            self.conn.execute(stmt)
        if policy is not None and self._setting("policy") is None:
            self._save_policy(policy)
        self.system = Principal("agent-factory", "system", "factory")

    # --- siyasət ---
    def _setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM factory_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _save_policy(self, p: FactoryPolicy) -> None:
        self.conn.execute("INSERT INTO factory_settings (key, value) VALUES ('policy', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (json.dumps(p.__dict__),))

    @property
    def policy(self) -> FactoryPolicy:
        raw = self._setting("policy")
        return FactoryPolicy(**json.loads(raw)) if raw else FactoryPolicy()

    # --- audit ---
    def audit(self, event: str, agent_id: str = "", **fields: Any) -> None:
        now = float(self.clock())
        row = {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(), "event": event}
        if agent_id:
            row["agent_id"] = agent_id
        row.update(fields)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        with self.lock:
            self.conn.execute("INSERT INTO factory_events (ts, event, agent_id, detail_json) VALUES (?, ?, ?, ?)",
                              (now, event, agent_id, json.dumps(fields, ensure_ascii=False, default=str)))

    def refuse(self, op: str, code: str, reason: str, principal: Principal | None = None, target: str = "") -> FactoryDenied:
        """Rədd audit jurnalına yazılır (sirr və ya mətn YOX; `agent_id` açarı yalnız uğurlu əməliyyatlarda istifadə olunur)."""
        fields: dict[str, Any] = {"op": op, "code": code, "reason": reason}
        if target:
            fields["target"] = target
        if isinstance(principal, Principal):
            fields.update(principal_id=principal.id, principal_kind=principal.kind)
        self.audit("factory_denied", **fields)
        return FactoryDenied(reason, code)

    # --- icazə ---
    def who(self, principal: Principal, op: str, *, owner_only: bool = False) -> str:
        if not isinstance(principal, Principal):
            raise self.refuse(op, "unauthorized", "principal tələb olunur")
        if principal.kind == "agent":
            raise self.refuse(op, "self_grant", "agent bu əməliyyatı edə bilməz", principal)
        if principal.kind == "mock" and not self.allow_mock:
            raise self.refuse(op, "mock_rejected", "mock principal real sahib kimi qəbul olunmur", principal)
        if principal.kind == "system":
            if owner_only:
                raise self.refuse(op, "owner_only", "bu əməliyyat yalnız sahibə məxsusdur", principal)
            return f"system:{principal.id}"
        if principal.id not in self.owners:
            raise self.refuse(op, "unauthorized", "əməliyyatı edən sahib siyahısında deyil", principal)
        return f"owner:{principal.id}"

    # --- oxuma ---
    def row(self, agent_id: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM factory_agents WHERE agent_id = ?", (agent_id,)).fetchone()

    def agent(self, row: sqlite3.Row) -> AgentRecord:
        ca = self.company.view.get(row["name"])
        mp = json.loads(row["memory_policy_json"])
        return AgentRecord(row["agent_id"], row["name"], row["role_id"], row["title"], row["specialization"], row["system_instruction"],
                           row["department"], row["workspace"], MemoryPolicy(tuple(mp["read_categories"]), tuple(mp["write_categories"]), bool(mp["share_with_cloud"])),
                           row["status"], row["stopped_by"], row["restart_count"], row["failures"], row["created_by"], row["created_at"], row["updated_at"],
                           ca.authority.permissions if (ca is not None and ca.status == "active") else frozenset(),
                           float(ca.budget) if ca is not None else 0.0)

    @staticmethod
    def task(row: sqlite3.Row | None) -> TaskRecord | None:
        if row is None:
            return None
        return TaskRecord(row["task_id"], row["agent_id"], row["title"], row["prompt"], row["kind"], row["permission"], float(row["cost"]), row["state"],
                          row["op_id"], row["parent_task_id"], row["created_by"], row["attempts"], row["approval_id"], row["summary"], row["error"],
                          bool(row["suspicious"]), row["created_at"], row["updated_at"], row["run_op"])

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self.lock:
            return self.task(self.conn.execute("SELECT * FROM factory_tasks WHERE task_id = ?", (task_id,)).fetchone())

    def must_agent(self, agent_id: str, op: str, principal: Principal | None = None) -> AgentRecord:
        row = self.row(agent_id) if isinstance(agent_id, str) else None
        if row is None:
            raise self.refuse(op, "unknown_agent", "agent tapılmadı", principal, str(agent_id)[:40])
        return self.agent(row)

    def committed(self, agent_id: str) -> float:
        with self.lock:
            r = self.conn.execute("SELECT COALESCE(SUM(cost), 0) AS c FROM factory_tasks WHERE agent_id = ? AND state IN ('queued','running','awaiting_approval','done')",
                                  (agent_id,)).fetchone()
        return float(r["c"])

    def _set_status(self, agent_id: str, status: str, **extra: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in extra)
        self.conn.execute(f"UPDATE factory_agents SET status = ?, updated_at = ?{', ' + cols if cols else ''} WHERE agent_id = ?",
                          (status, float(self.clock()), *extra.values(), agent_id))

    def wrap_company(self, op: str, principal: Principal, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except CompanyDenied as exc:
            raise self.refuse(op, exc.code, str(exc), principal) from None

    # --- yaradılış ---
    def check_caps(self, op: str, role_id: str, principal: Principal, system: bool) -> None:
        p = self.policy
        if system and not p.autonomous_creation:
            raise self.refuse(op, "autonomy_disabled", "avtonom agent yaradılışı sahib tərəfindən söndürülüb", principal)
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) AS n FROM factory_agents WHERE status IN ('active','stopped')").fetchone()["n"]
            per_role = self.conn.execute("SELECT COUNT(*) AS n FROM factory_agents WHERE status IN ('active','stopped') AND role_id = ?", (role_id,)).fetchone()["n"]
        if total >= p.max_agents:
            raise self.refuse(op, "agent_cap", f"agent sayı limiti: {p.max_agents}", principal)
        if per_role >= p.max_per_role:
            raise self.refuse(op, "role_cap", f"rol üzrə agent limiti: {p.max_per_role}", principal)

    def new_agent(self, role: RoleTemplate, principal: Principal, created_by: str, authority: Authority | None, budget: float) -> AgentRecord:
        agent_id = "agt_" + secrets.token_hex(4)
        name = f"{role.role_id}_{secrets.token_hex(3)}"
        spec = AgentSpec(name=name, purpose=role.specialization, tools=[], permissions=sorted(set(role.default_permissions) | set(role.requestable_permissions) | set(role.sensitive_requestable)),
                         inputs=["sorgu"], outputs=["netice"], cost_limit=SPEC_COST_CEILING)
        op = "create_agent"
        if authority is None:
            self.wrap_company(op, principal, lambda: self.company.admin.register_baseline_agent(spec, role.department, role.title_az, principal))
        else:
            self.wrap_company(op, principal, lambda: self.company.admin.register_agent(spec, role.department, role.title_az, authority, budget, principal))
        ws = (self.root / agent_id).resolve()
        if self.root.resolve() not in ws.parents:
            raise self.refuse(op, "workspace", "iş sahəsi yolu yanlışdır", principal)
        ws.mkdir(parents=True, exist_ok=False)
        now = float(self.clock())
        with self.lock:
            self.conn.execute(
                "INSERT INTO factory_agents (agent_id, name, role_id, title, specialization, system_instruction, department, workspace, memory_policy_json, "
                "status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (agent_id, name, role.role_id, role.title_az, role.specialization, role.system_instruction, role.department, str(ws),
                 json.dumps(role.memory_policy.__dict__ | {"read_categories": list(role.memory_policy.read_categories),
                                                           "write_categories": list(role.memory_policy.write_categories)}),
                 created_by, now, now))
        rec = self.agent(self.row(agent_id))  # type: ignore[arg-type]
        self.audit("factory_agent_created", agent_id, name=name, role_id=role.role_id, permissions=sorted(rec.permissions), budget=rec.budget,
                   created_by=created_by, workspace=str(ws))
        return rec

    def handoff_check(self, from_name: str, to_department: str, task: Task) -> Any:
        return self.company.view.handoff(from_name, to_department, task)

    # --- tapşırıq ---
    def assign(self, op: str, agent: AgentRecord, title: Any, prompt: Any, *, kind: str | None, permission: str, cost: Any, op_id: str | None,
               parent_task_id: str, created_by: str, principal: Principal | None, trusted_prompt: bool, suspicious: bool = False) -> TaskRecord:
        pol = self.policy
        if agent.status != "active":
            raise self.refuse(op, "agent_not_active", "agent aktiv deyil", principal, agent.agent_id)
        role = ROLE_CATALOG[agent.role_id]
        kind = kind or role.task_kinds[0]
        if kind not in role.task_kinds:
            raise self.refuse(op, "kind_not_allowed", f"bu rol üçün tapşırıq növü icazəli deyil: {kind}", principal, agent.agent_id)
        if permission not in KNOWN_PERMISSIONS:
            raise self.refuse(op, "unknown_permission", "naməlum icazə", principal, agent.agent_id)
        if permission not in agent.permissions:
            raise self.refuse(op, "permission_not_held", "agentdə bu icazə yoxdur (genişləndirmə yalnız sahibdən)", principal, agent.agent_id)
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise self.refuse(op, "bad_title", "başlıq yanlışdır", principal, agent.agent_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise self.refuse(op, "empty", "tapşırıq mətni boşdur", principal, agent.agent_id)
        if len(prompt) > pol.max_prompt_chars:
            raise self.refuse(op, "too_large", f"tapşırıq mətni çox uzundur (maks. {pol.max_prompt_chars})", principal, agent.agent_id)
        if detect_secrets(prompt) or detect_secrets(title):
            raise self.refuse(op, "secret", "tapşırıq sirr bənzər məzmun ehtiva edir", principal, agent.agent_id)
        if not _finite(cost):
            raise self.refuse(op, "bad_cost", "xərc sonlu və mənfi olmayan rəqəm olmalıdır", principal, agent.agent_id)
        cost = float(cost)
        if op_id is not None and (not isinstance(op_id, str) or not _OP_RE.match(op_id)):
            raise self.refuse(op, "bad_op_id", "op_id yanlışdır", principal, agent.agent_id)
        op_id = op_id or "op_" + secrets.token_hex(8)
        sha = hashlib.sha256(json.dumps([agent.agent_id, kind, permission, title, prompt, cost], ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            existing = self.conn.execute("SELECT task_id, op_sha FROM factory_tasks WHERE op_id = ?", (op_id,)).fetchone()
        if existing is not None:
            if existing["op_sha"] != sha:
                raise self.refuse(op, "op_conflict", "op_id başqa məzmunla artıq istifadə olunub", principal, agent.agent_id)
            return self.get_task(existing["task_id"])  # type: ignore[return-value]
        ca = self.company.view.get(agent.name)
        if ca is None or cost > ca.authority.max_task_cost:
            raise self.refuse(op, "task_cost_limit", "tapşırıq xərci agentin maksimumunu aşır", principal, agent.agent_id)
        if self.committed(agent.agent_id) + cost > agent.budget + 1e-9:
            raise self.refuse(op, "budget_exceeded", "agentin büdcəsi çatmır", principal, agent.agent_id)
        with self.lock:
            open_n = self.conn.execute("SELECT COUNT(*) AS n FROM factory_tasks WHERE agent_id = ? AND state IN ('queued','running','awaiting_approval')",
                                       (agent.agent_id,)).fetchone()["n"]
            if open_n >= pol.max_queue:
                raise self.refuse(op, "queue_full", "agentin növbəsi doludur", principal, agent.agent_id)
            task_id = "tsk_" + secrets.token_hex(5)
            now = float(self.clock())
            self.conn.execute(
                "INSERT INTO factory_tasks (task_id, agent_id, title, prompt, kind, permission, cost, state, op_id, op_sha, parent_task_id, created_by, "
                "suspicious, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                (task_id, agent.agent_id, " ".join(title.split()), prompt, kind, permission, cost, op_id, sha, parent_task_id, created_by, int(suspicious), now, now))
        self.audit("factory_task_assigned", agent.agent_id, task_id=task_id, kind=kind, permission=permission, cost=cost, op_id=op_id,
                   parent_task_id=parent_task_id, created_by=created_by, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        return self.get_task(task_id)  # type: ignore[return-value]


# --- fasadlar ---

class FactoryView:
    """Yalnız oxu."""

    def __init__(self, core: _Core) -> None:
        self._c = core

    @property
    def policy(self) -> FactoryPolicy:
        return self._c.policy

    def agent(self, agent_id: str) -> AgentRecord | None:
        row = self._c.row(agent_id) if isinstance(agent_id, str) else None
        return self._c.agent(row) if row is not None else None

    def find(self, name: str) -> AgentRecord | None:
        with self._c.lock:
            row = self._c.conn.execute("SELECT * FROM factory_agents WHERE name = ?", (name,)).fetchone()
        return self._c.agent(row) if row is not None else None

    def agents(self, status: str | None = None, role_id: str | None = None) -> list[AgentRecord]:
        sql, params = "SELECT * FROM factory_agents", []
        where = ["status = ?" if status else "status != 'deleted'"]
        if status:
            params.append(status)
        if role_id:
            where.append("role_id = ?")
            params.append(role_id)
        with self._c.lock:
            rows = self._c.conn.execute(f"{sql} WHERE {' AND '.join(where)} ORDER BY created_at, agent_id", params).fetchall()
        return [self._c.agent(r) for r in rows]

    def task(self, task_id: str) -> TaskRecord | None:
        return self._c.get_task(task_id)

    def tasks(self, agent_id: str | None = None, state: str | None = None) -> list[TaskRecord]:
        sql, params, where = "SELECT * FROM factory_tasks", [], []
        for col, val in (("agent_id", agent_id), ("state", state)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        with self._c.lock:
            rows = self._c.conn.execute(sql + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, task_id", params).fetchall()
        return [self._c.task(r) for r in rows]  # type: ignore[misc]

    def reports(self, agent_id: str | None = None, task_id: str | None = None) -> list[AgentReport]:
        sql, params, where = "SELECT * FROM factory_reports", [], []
        for col, val in (("agent_id", agent_id), ("task_id", task_id)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        with self._c.lock:
            rows = self._c.conn.execute(sql + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts, task_id", params).fetchall()
        return [AgentReport(r["task_id"], r["agent_id"], r["status"], r["summary"], tuple(json.loads(r["artifacts_json"])), r["wrapped"],
                            bool(r["suspicious"]), float(r["cost"]), r["ts"]) for r in rows]

    def requests(self, state: str | None = None) -> list[PermissionRequest]:
        with self._c.lock:
            rows = self._c.conn.execute("SELECT * FROM factory_requests" + (" WHERE state = ?" if state else "") + " ORDER BY ts, request_id",
                                        (state,) if state else ()).fetchall()
        return [PermissionRequest(r["request_id"], r["agent_id"], r["permission"], r["reason"], r["state"], r["ts"]) for r in rows]

    def events(self, agent_id: str | None = None, limit: int = 200) -> list[EventRecord]:
        with self._c.lock:
            rows = self._c.conn.execute("SELECT * FROM factory_events" + (" WHERE agent_id = ?" if agent_id else "") + " ORDER BY id LIMIT ?",
                                        ((agent_id, limit) if agent_id else (limit,))).fetchall()
        return [EventRecord(r["ts"], r["event"], r["agent_id"], json.loads(r["detail_json"])) for r in rows]

    def remaining_budget(self, agent_id: str) -> float:
        a = self.agent(agent_id)
        return 0.0 if a is None else max(0.0, a.budget - self._c.committed(agent_id))


class FactoryControl:
    """NAYMERA (sistem) və sahib üçün: MƏHDUD agent yaratma, həyat dövrü, tapşırıq təyini. Səlahiyyət genişləndirmə imkanı YOXDUR."""

    def __init__(self, core: _Core) -> None:
        self._c = core

    def spawn(self, role_id: str, principal: Principal, *, purpose: str = "") -> AgentRecord:
        c, op = self._c, "spawn"
        created_by = c.who(principal, op)
        role = ROLE_CATALOG.get(role_id) if isinstance(role_id, str) else None
        if role is None:
            raise c.refuse(op, "unknown_role", f"naməlum rol: {str(role_id)[:30]}", principal)
        c.check_caps(op, role.role_id, principal, system=principal.kind == "system")
        return c.new_agent(role, principal, created_by, None, 0.0)

    def assign(self, agent_id: str, title: str, prompt: str, principal: Principal, *, kind: str | None = None, permission: str = "read_files",
               cost: float = 0.0, op_id: str | None = None, parent_task_id: str = "") -> TaskRecord:
        c, op = self._c, "assign_task"
        created_by = c.who(principal, op)
        agent = c.must_agent(agent_id, op, principal)
        return c.assign(op, agent, title, prompt, kind=kind, permission=permission, cost=cost, op_id=op_id, parent_task_id=parent_task_id,
                        created_by=created_by, principal=principal, trusted_prompt=True)

    def cancel_task(self, task_id: str, principal: Principal) -> TaskRecord:
        c, op = self._c, "cancel_task"
        c.who(principal, op)
        with c.lock:
            cur = c.conn.execute("UPDATE factory_tasks SET state = 'cancelled', updated_at = ? WHERE task_id = ? AND state IN ('queued','awaiting_approval')",
                                 (float(c.clock()), task_id))
        t = c.get_task(task_id)
        if t is None or cur.rowcount != 1:
            raise c.refuse(op, "not_cancellable", "tapşırıq tapılmadı və ya ləğv oluna bilməz", principal, task_id)
        c.audit("factory_task_cancelled", t.agent_id, task_id=task_id)
        return t

    def stop(self, agent_id: str, principal: Principal, reason: str = "") -> AgentRecord:
        c, op = self._c, "stop"
        c.who(principal, op)
        a = c.must_agent(agent_id, op, principal)
        if a.status != "active":
            raise c.refuse(op, f"agent_{a.status}", "yalnız aktiv agent dayandırıla bilər", principal, agent_id)
        c.wrap_company(op, principal, lambda: c.company.admin.suspend(a.name, principal))
        with c.lock:
            c._set_status(agent_id, "stopped", stopped_by="owner" if principal.kind in ("human", "mock") else "system")
            c.conn.execute("UPDATE factory_tasks SET state = 'failed', error = 'agent dayandırıldı', updated_at = ? WHERE agent_id = ? AND state = 'running'",
                           (float(c.clock()), agent_id))
        c.audit("factory_agent_stopped", agent_id, by=f"{principal.kind}:{principal.id}", reason=" ".join(str(reason).split())[:120])
        return c.agent(c.row(agent_id))  # type: ignore[arg-type]

    def restart(self, agent_id: str, principal: Principal) -> AgentRecord:
        c, op = self._c, "restart"
        c.who(principal, op)
        a = c.must_agent(agent_id, op, principal)
        if a.status != "stopped":
            raise c.refuse(op, f"agent_{a.status}" if a.status != "active" else "not_stopped", "yalnız dayandırılmış agent yenidən başladıla bilər", principal, agent_id)
        if a.stopped_by == "owner" and principal.kind not in ("human", "mock"):
            raise c.refuse(op, "owner_stopped", "sahibin dayandırdığı agenti yalnız sahib yenidən başlada bilər", principal, agent_id)
        c.wrap_company(op, principal, lambda: c.company.admin.resume(a.name, principal))
        with c.lock:
            c._set_status(agent_id, "active", stopped_by="", restart_count=a.restart_count + 1, failures=0)
        c.audit("factory_agent_restarted", agent_id, by=f"{principal.kind}:{principal.id}", restart_count=a.restart_count + 1)
        return c.agent(c.row(agent_id))  # type: ignore[arg-type]

    def archive(self, agent_id: str, principal: Principal) -> AgentRecord:
        c, op = self._c, "archive"
        c.who(principal, op)
        a = c.must_agent(agent_id, op, principal)
        if a.status not in ("active", "stopped"):
            raise c.refuse(op, f"agent_{a.status}", "agent artıq arxivlənib və ya silinib", principal, agent_id)
        c.wrap_company(op, principal, lambda: c.company.admin.retire(a.name, principal))
        with c.lock:
            c._set_status(agent_id, "archived")
            c.conn.execute("UPDATE factory_tasks SET state = 'cancelled', error = 'agent arxivləndi', updated_at = ? WHERE agent_id = ? AND state IN ('queued','running','awaiting_approval')",
                           (float(c.clock()), agent_id))
        c.audit("factory_agent_archived", agent_id, by=f"{principal.kind}:{principal.id}")
        return c.agent(c.row(agent_id))  # type: ignore[arg-type]


class FactoryAdmin(FactoryControl):
    """YALNIZ sahib/kanal adapterləri üçün. CEO-ya, agentlərə və LLM-ə verilmir."""

    def create_agent(self, role_id: str, principal: Principal, *, permissions: Sequence[str] = ("read_files",), sensitive_ok: bool = False,
                     delegate_to: Sequence[str] = (), budget: float = 0.0, max_task_cost: float = 0.0) -> AgentRecord:
        c, op = self._c, "create_agent"
        created_by = c.who(principal, op, owner_only=True)
        role = ROLE_CATALOG.get(role_id) if isinstance(role_id, str) else None
        if role is None:
            raise c.refuse(op, "unknown_role", f"naməlum rol: {str(role_id)[:30]}", principal)
        perms = set(permissions)
        if not perms <= KNOWN_PERMISSIONS:
            raise c.refuse(op, "unknown_permission", "naməlum icazə", principal)
        if not perms <= set(role.default_permissions) | set(role.requestable_permissions) | set(role.sensitive_requestable):
            raise c.refuse(op, "permission_not_requestable", "icazə bu rol şablonunda verilə bilməz", principal)
        if not set(delegate_to) <= set(DEPARTMENTS):
            raise c.refuse(op, "unknown_department", "naməlum departament", principal)
        if not _finite(budget) or budget > c.policy.global_budget_cap + 1e-9:
            raise c.refuse(op, "budget_cap", "büdcə qlobal tavanı aşır (tavanı yalnız sahib qaldırır)", principal)
        c.check_caps(op, role.role_id, principal, system=False)
        authority = Authority(frozenset(perms), bool(sensitive_ok), frozenset(delegate_to), float(max_task_cost))
        return c.new_agent(role, principal, created_by, authority, float(budget))

    def grant(self, agent_id: str, principal: Principal, *, permissions: Sequence[str], sensitive_ok: bool = False,
              delegate_to: Sequence[str] = (), max_task_cost: float = 0.0) -> AgentRecord:
        c, op = self._c, "grant"
        c.who(principal, op, owner_only=True)
        a = c.must_agent(agent_id, op, principal)
        role = ROLE_CATALOG[a.role_id]
        if not set(permissions) <= set(role.default_permissions) | set(role.requestable_permissions) | set(role.sensitive_requestable):
            raise c.refuse(op, "permission_not_requestable", "icazə bu rol şablonunda verilə bilməz", principal, agent_id)
        ca = c.company.view.get(a.name)
        auth = Authority(frozenset(permissions), bool(sensitive_ok), frozenset(delegate_to) | (ca.authority.delegate_to if ca else frozenset()),
                         float(max_task_cost))
        c.wrap_company(op, principal, lambda: c.company.admin.set_authority(a.name, auth, principal))
        c.audit("factory_authority_granted", agent_id, permissions=sorted(permissions), by=f"{principal.kind}:{principal.id}")
        return c.agent(c.row(agent_id))  # type: ignore[arg-type]

    def set_budget(self, agent_id: str, budget: float, principal: Principal) -> AgentRecord:
        c, op = self._c, "set_budget"
        c.who(principal, op, owner_only=True)
        a = c.must_agent(agent_id, op, principal)
        if not _finite(budget) or budget > c.policy.global_budget_cap + 1e-9:
            raise c.refuse(op, "budget_cap", "büdcə qlobal tavanı aşır (tavanı yalnız sahib qaldırır)", principal, agent_id)
        c.wrap_company(op, principal, lambda: c.company.admin.set_budget(a.name, float(budget), principal))
        c.audit("factory_budget_changed", agent_id, before=a.budget, after=float(budget), by=f"{principal.kind}:{principal.id}")
        return c.agent(c.row(agent_id))  # type: ignore[arg-type]

    def set_policy(self, principal: Principal, **changes: Any) -> FactoryPolicy:
        c, op = self._c, "set_policy"
        c.who(principal, op, owner_only=True)
        cur = c.policy
        for k, v in changes.items():
            if k not in cur.__dict__:
                raise c.refuse(op, "unknown_field", f"naməlum siyasət sahəsi: {k}", principal)
            want = type(getattr(cur, k))
            ok = isinstance(v, bool) if want is bool else (_finite(v) if want is float else (isinstance(v, int) and not isinstance(v, bool) and v >= 1))
            if not ok:
                raise c.refuse(op, "bad_value", f"{k} dəyəri yanlışdır", principal)
            changes[k] = float(v) if want is float else v
        new = replace(cur, **changes)
        with c.lock:
            c._save_policy(new)
        c.audit("factory_policy_changed", changes=changes, by=f"{principal.kind}:{principal.id}")
        return new

    def resolve_request(self, request_id: str, approve: bool, principal: Principal) -> PermissionRequest:
        c, op = self._c, "resolve_request"
        c.who(principal, op, owner_only=True)
        with c.lock:
            row = c.conn.execute("SELECT * FROM factory_requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is None or row["state"] != "pending":
                raise c.refuse(op, "not_pending", "sorğu tapılmadı və ya artıq həll olunub", principal, str(request_id)[:40])
        a = c.must_agent(row["agent_id"], op, principal)
        if approve:
            ca = c.company.view.get(a.name)
            perms = (ca.authority.permissions if ca else frozenset()) | {row["permission"]}
            auth = Authority(frozenset(perms), ca.authority.sensitive_ok if ca else False, ca.authority.delegate_to if ca else frozenset(),
                             ca.authority.max_task_cost if ca else 0.0)
            c.wrap_company(op, principal, lambda: c.company.admin.set_authority(a.name, auth, principal))
        with c.lock:
            c.conn.execute("UPDATE factory_requests SET state = ?, resolved_by = ?, resolved_ts = ? WHERE request_id = ?",
                           ("approved" if approve else "denied", f"{principal.kind}:{principal.id}", float(c.clock()), request_id))
        c.audit("factory_permission_resolved", a.agent_id, request_id=request_id, permission=row["permission"], approved=bool(approve),
                by=f"{principal.kind}:{principal.id}")
        return next(r for r in FactoryView(c).requests() if r.request_id == request_id)

    def delete(self, agent_id: str, principal: Principal) -> AgentRecord:
        c, op = self._c, "delete"
        c.who(principal, op, owner_only=True)
        a = c.must_agent(agent_id, op, principal)
        if a.status != "archived":
            raise c.refuse(op, "not_archived", "yalnız arxivlənmiş agent silinə bilər", principal, agent_id)
        with c.lock:
            c._set_status(agent_id, "deleted")
        c.audit("factory_agent_deleted", agent_id, by=f"{principal.kind}:{principal.id}")
        return c.agent(c.row(agent_id))  # type: ignore[arg-type]


class AgentHandle:
    """Agentin ÖZ əməliyyatları: nəticə təqdim etmək, tapşırıq ötürmək, icazə SORĞUSU yazmaq. Səlahiyyət/büdcə dəyişdirmə imkanı YOXDUR."""

    def __init__(self, core: _Core, agent_id: str) -> None:
        self._k = core
        self.agent_id = agent_id

    def info(self) -> AgentRecord:
        return self._k.must_agent(self.agent_id, "info")

    def request_permission(self, permission: str, reason: str) -> PermissionRequest:
        k, op = self._k, "request_permission"
        a = k.must_agent(self.agent_id, op)
        if permission not in KNOWN_PERMISSIONS:
            raise k.refuse(op, "unknown_permission", "naməlum icazə", None, a.agent_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            raise k.refuse(op, "bad_reason", "səbəb boş və ya çox uzundur", None, a.agent_id)
        if detect_secrets(reason):
            raise k.refuse(op, "secret", "səbəb sirr bənzər məzmun ehtiva edir", None, a.agent_id)
        with k.lock:
            row = k.conn.execute("SELECT * FROM factory_requests WHERE agent_id = ? AND permission = ? AND state = 'pending'", (a.agent_id, permission)).fetchone()
            if row is not None:
                return PermissionRequest(row["request_id"], row["agent_id"], row["permission"], row["reason"], row["state"], row["ts"])
            rid, now = "prq_" + secrets.token_hex(4), float(k.clock())
            k.conn.execute("INSERT INTO factory_requests (request_id, agent_id, permission, reason, state, ts) VALUES (?, ?, ?, ?, 'pending', ?)",
                           (rid, a.agent_id, permission, " ".join(reason.split()), now))
        k.audit("factory_permission_requested", a.agent_id, request_id=rid, permission=permission)
        return PermissionRequest(rid, a.agent_id, permission, reason, "pending", now)

    def submit_result(self, task_id: str, summary: str, artifacts: Sequence[str] = (), cost: float = 0.0) -> AgentReport:
        k, op = self._k, "submit_result"
        t = k.get_task(task_id)
        if t is None:
            raise k.refuse(op, "unknown_task", "tapşırıq tapılmadı", None, str(task_id)[:40])
        if t.agent_id != self.agent_id:
            raise k.refuse(op, "not_your_task", "tapşırıq bu agentə təyin edilməyib", None, t.task_id)
        if t.state != "running":
            raise k.refuse(op, "task_not_running", "tapşırıq icra mərhələsində deyil", None, t.task_id)
        a = k.must_agent(self.agent_id, op)
        try:
            ex = validate_exchange(summary, source=a.name)
        except ExchangeRejected as exc:
            raise k.refuse(op, exc.code, str(exc), None, t.task_id) from None
        arts = tuple(artifacts)
        if len(arts) > 10 or any(not isinstance(x, str) or not _ART_RE.match(x) or ".." in x or x.startswith("/") for x in arts):
            raise k.refuse(op, "bad_artifact", "artefakt adları yanlışdır", None, t.task_id)
        if not _finite(cost) or cost > t.cost + 1e-9:
            raise k.refuse(op, "cost_over_estimate", "faktiki xərc təyin edilmiş xərci aşır", None, t.task_id)
        now = float(k.clock())
        with k.lock:
            cur = k.conn.execute("UPDATE factory_tasks SET state = 'done', summary = ?, suspicious = ?, updated_at = ?, cost = ? WHERE task_id = ? AND state = 'running'",
                                 (ex.text[:2000], int(ex.suspicious), now, float(cost), t.task_id))
            if cur.rowcount != 1:
                raise k.refuse(op, "task_not_running", "tapşırıq icra mərhələsində deyil", None, t.task_id)
            k.conn.execute("INSERT INTO factory_reports (task_id, agent_id, status, summary, artifacts_json, wrapped, suspicious, cost, ts) VALUES (?, ?, 'done', ?, ?, ?, ?, ?, ?)",
                           (t.task_id, a.agent_id, ex.text, json.dumps(list(arts)), ex.wrapped, int(ex.suspicious), float(cost), now))
        k.audit("factory_task_done", a.agent_id, task_id=t.task_id, suspicious=ex.suspicious, cost=float(cost))
        return AgentReport(t.task_id, a.agent_id, "done", ex.text, arts, ex.wrapped, ex.suspicious, float(cost), now)

    def handoff(self, to_agent_id: str, title: str, payload: dict, *, permission: str = "read_files", cost: float = 0.0, op_id: str | None = None,
                parent_task_id: str = "") -> TaskRecord:
        k, op = self._k, "handoff"
        src = k.must_agent(self.agent_id, op)
        if src.status != "active":
            raise k.refuse(op, "agent_not_active", "agent aktiv deyil", None, src.agent_id)
        dst = k.must_agent(to_agent_id, op)
        if dst.agent_id == src.agent_id:
            raise k.refuse(op, "self_handoff", "agent tapşırığı özünə ötürə bilməz", None, src.agent_id)
        if dst.status != "active":
            raise k.refuse(op, "target_not_active", "hədəf agent aktiv deyil", None, src.agent_id)
        chain: list[TaskRecord] = []
        if parent_task_id:
            p = k.get_task(parent_task_id)
            if p is None or p.agent_id != src.agent_id:
                raise k.refuse(op, "parent_mismatch", "ana tapşırıq bu agentə aid deyil", None, src.agent_id)
            cur: TaskRecord | None = p
            while cur is not None and len(chain) < 64:
                chain.append(cur)
                cur = k.get_task(cur.parent_task_id) if cur.parent_task_id else None
            if len(chain) > k.policy.max_handoff_depth:
                raise k.refuse(op, "handoff_depth", "ötürmə zənciri çox dərindir", None, src.agent_id)
            if dst.agent_id in {t.agent_id for t in chain}:
                raise k.refuse(op, "handoff_cycle", "ötürmə dövrə yaradır", None, src.agent_id)
        if not isinstance(payload, dict) or not payload or len(payload) > 20 or not all(isinstance(x, str) and 0 < len(x) <= 40 for x in payload):
            raise k.refuse(op, "bad_payload", "payload yanlışdır", None, src.agent_id)
        text = "\n".join(f"{key}: {value}" for key, value in payload.items())
        try:
            ex = validate_exchange(text, source=src.name, max_chars=k.policy.max_prompt_chars)
        except ExchangeRejected as exc:
            raise k.refuse(op, exc.code, str(exc), None, src.agent_id) from None
        h = k.handoff_check(src.name, dst.department, Task(dst.department, {"sorgu": ex.text}, permission, cost if _finite(cost) else -1.0, title if isinstance(title, str) else ""))
        if not h.ok:
            raise k.refuse(op, h.code or "handoff_refused", h.reason, None, src.agent_id)
        return k.assign(op, dst, title, ex.wrapped, kind=None, permission=permission, cost=cost, op_id=op_id, parent_task_id=parent_task_id,
                        created_by=f"agent:{src.agent_id}", principal=None, trusted_prompt=False, suspicious=ex.suspicious)


class FactoryRuntime:
    """Icra qatı üçün (dispatcher): tapşırığı tutmaq, uğursuzluğu/təsdiq gözləməni qeyd etmək. Agentlərə və CEO-ya verilmir; icraçı özü sistemdir."""

    def __init__(self, core: _Core) -> None:
        self._c = core

    def claim(self, task_id: str) -> TaskRecord | None:
        """Atomik: queued/awaiting_approval/failed(təkrar) -> running. Agent aktiv deyilsə və ya tapşırıq başqa vəziyyətdədirsə None (təkrar icra yoxdur)."""
        c = self._c
        t = c.get_task(task_id)
        if t is None:
            return None
        a = c.row(t.agent_id)
        if a is None or a["status"] != "active":
            return None
        with c.lock:
            cur = c.conn.execute("UPDATE factory_tasks SET state = 'running', attempts = attempts + 1, updated_at = ? WHERE task_id = ? AND state IN ('queued','awaiting_approval')",
                                 (float(c.clock()), task_id))
        if cur.rowcount != 1:
            return None
        with c.lock:  # icra zənciri üçün sabit op_id: təsdiqdən sonra davam edəndə eyni op_id istifadə olunur
            c.conn.execute("UPDATE factory_tasks SET run_op = ? WHERE task_id = ? AND run_op = ''", (f"{t.op_id}:a{t.attempts + 1}", task_id))
        c.audit("factory_task_started", t.agent_id, task_id=task_id, attempt=t.attempts + 1)
        return c.get_task(task_id)

    def await_approval(self, task_id: str, approval_id: str) -> None:
        c = self._c
        with c.lock:
            c.conn.execute("UPDATE factory_tasks SET state = 'awaiting_approval', approval_id = ?, updated_at = ? WHERE task_id = ? AND state = 'running'",
                           (approval_id, float(c.clock()), task_id))
        t = c.get_task(task_id)
        c.audit("factory_task_awaiting_approval", t.agent_id if t else "", task_id=task_id, approval_id=approval_id)

    def fail(self, task_id: str, reason: str, *, retry: bool = False) -> TaskRecord | None:
        """running -> failed (retry=True: queued, növbəti cəhd üçün). Ardıcıl uğursuzluq həddində agent sistem tərəfindən dayandırılır."""
        c = self._c
        with c.lock:
            c.conn.execute("UPDATE factory_tasks SET state = ?, error = ?, updated_at = ?, run_op = '' WHERE task_id = ? AND state = 'running'",
                           ("queued" if retry else "failed", " ".join(str(reason).split())[:200], float(c.clock()), task_id))
        t = c.get_task(task_id)
        if t is None:
            return None
        c.audit("factory_task_failed", t.agent_id, task_id=task_id, retry=retry, reason=" ".join(str(reason).split())[:120])
        if not retry:
            self.note_failure(t.agent_id)
        return t

    def note_failure(self, agent_id: str) -> None:
        c = self._c
        with c.lock:
            c.conn.execute("UPDATE factory_agents SET failures = failures + 1 WHERE agent_id = ?", (agent_id,))
        row = c.row(agent_id)
        if row is not None and row["status"] == "active" and row["failures"] >= c.policy.auto_stop_after_failures:
            FactoryControl(c).stop(agent_id, c.system, "ardıcıl uğursuzluq: avtomatik dayandırıldı")

    def note_success(self, agent_id: str) -> None:
        c = self._c
        with c.lock:
            c.conn.execute("UPDATE factory_agents SET failures = 0 WHERE agent_id = ?", (agent_id,))


class AgentFactory:
    def __init__(self, company: Company, db_path: str | Path, audit_path: str | Path, workspace_root: str | Path, *, owners: Iterable[str],
                 allow_mock: bool = False, clock: Callable[[], float] = time.time, policy: FactoryPolicy | None = None) -> None:
        self._core = _Core(company, db_path, audit_path, workspace_root, owners, allow_mock, clock, policy)
        self.view = FactoryView(self._core)
        self.control = FactoryControl(self._core)
        self.admin = FactoryAdmin(self._core)
        self.runtime = FactoryRuntime(self._core)
        self.system = self._core.system   # NAYMERA-nın (sistem) principal-ı: yalnız `control` əməliyyatları üçün (səlahiyyət genişləndirmə imkanı yoxdur)

    def handle(self, agent_id: str) -> AgentHandle:
        return AgentHandle(self._core, agent_id)
