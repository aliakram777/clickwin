"""Virtual şirkət: departamentlər, agent rolu/səlahiyyəti/büdcəsi, agentlər arası ötürmə, nəticələrin birləşdirilməsi.

Qaydalar:
- Agentin faktiki icazələri = spec icazələri ∩ səlahiyyət (`Authority`); reyestrə YALNIZ bu "effektiv" spec yazılır,
  ona görə Orchestrator/Broker/Gate əlavə dəyişiklik olmadan səlahiyyət sərhədini tətbiq edir.
- Səlahiyyət/büdcə GENİŞLƏNDİRMƏSİ yalnız sahib (human, sahib siyahısında; mock yalnız `allow_mock` ilə, işarələnir)
  tərəfindən; sistem yalnız daralda bilər; agent heç nəyi dəyişə bilməz (özünə səlahiyyət verə bilməz).
- Ötürmə (`handoff`) yalnız tapşırıq yaradır, icra etmir: icra Orchestrator -> Effect Broker -> Gate/Təsdiq yolundadır.
  Maliyyə/xarici/nəşr tapşırıqları (həssas icazələr) təsdiq tələb etməyə davam edir.
- Bütün qərarlar (qəbul və rədd) audit jurnalına yazılır (`event` açarı ilə; reconcile onları saymır).
- İki üz: `view` (oxu + ötürmə) CEO/orkestrasiya üçün; `admin` yalnız kanal adapterlərinə (sahib) verilir.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from agents.ceo import JobReport, StepReport
from agents.factory_auth import CredentialError, Keyring
from agents.memory import Actor, MemoryDenied, MemoryService, WriteResult, detect_secrets
from agents.orchestrator import Task
from agents.registry import Registry, RegistryError
from agents.spec import KNOWN_PERMISSIONS, SENSITIVE_PERMISSIONS, AgentSpec
from safety.approvals import Principal
from safety.ledger import BudgetPolicy
from safety.dbconn import connect

DEFAULT_DB = Path("data/company.db")
DEFAULT_AUDIT = Path("logs/audit.jsonl")
DEFAULT_STEP_COST = 0.5
# Sahib etibarnaməsinin əhatə əməliyyatı (`scope_op`) Company əməliyyatına UYĞUN olmalıdır (B-3). Dəqiq hədəf/parametr-hash fabrik sərhədində yoxlanır (Company-nin hədəf adı
# fabrikdəki `agent_id`/rol-dan fərqlənə bilər); Company əməliyyat sinfini, imzanı/rolu/vaxtı/ləğvi və bir-dəfəlik istifadəni MÜSTƏQİL yoxlayır.
COMPANY_OP_SCOPES: dict[str, frozenset[str]] = {
    "register_agent": frozenset({"create_agent"}), "register_baseline_agent": frozenset({"spawn"}), "set_authority": frozenset({"grant", "resolve_request"}),
    "set_budget": frozenset({"set_budget"}), "suspend": frozenset({"stop"}), "resume": frozenset({"restart"}), "retire": frozenset({"archive"}),
    "set_department_cost": frozenset({"set_department_cost"}),
}
DEPARTMENTS = {
    "research": "Research", "development": "Development", "finance": "Finance", "security": "Security",
    "marketing": "Marketing", "sales": "Sales", "operations": "Operations", "support": "Support",
}
SYSTEM_ACTOR = Actor("company", "system")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS company_agents (
        name TEXT PRIMARY KEY, department TEXT NOT NULL, role TEXT NOT NULL, spec_yaml TEXT NOT NULL,
        permissions_json TEXT NOT NULL, sensitive_ok INTEGER NOT NULL, delegate_json TEXT NOT NULL,
        max_task_cost REAL NOT NULL, budget REAL NOT NULL, status TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS company_departments (department TEXT PRIMARY KEY, step_cost REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS company_used_credentials (nonce TEXT PRIMARY KEY, op TEXT NOT NULL, used_at REAL NOT NULL)",
]


class CompanyDenied(ValueError):
    def __init__(self, message: str, code: str = "denied") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Authority:
    permissions: frozenset[str]
    sensitive_ok: bool = False
    delegate_to: frozenset[str] = frozenset()
    max_task_cost: float = 0.0

    def __post_init__(self) -> None:
        if not self.permissions <= KNOWN_PERMISSIONS:
            raise ValueError("naməlum icazə")
        if not set(self.delegate_to) <= set(DEPARTMENTS):
            raise ValueError("naməlum departament")
        if isinstance(self.max_task_cost, bool) or not isinstance(self.max_task_cost, (int, float)) \
                or not math.isfinite(self.max_task_cost) or self.max_task_cost < 0:
            raise ValueError("max_task_cost sonlu və mənfi olmayan rəqəm olmalıdır")


@dataclass(frozen=True)
class CompanyAgent:
    name: str
    department: str
    role: str
    authority: Authority
    budget: float
    status: str  # active | retired
    spec: AgentSpec  # orijinal (səlahiyyətsiz) təsvir
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class HandoffResult:
    ok: bool
    reason: str = ""
    task: Task | None = None
    code: str = ""


def _canon_num(v: Any) -> Any:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else repr(v)


def _canon_authority(a: Any) -> dict:
    try:
        return {"permissions": sorted(str(x) for x in a.permissions), "sensitive_ok": bool(a.sensitive_ok), "delegate_to": sorted(str(x) for x in a.delegate_to),
                "max_task_cost": _canon_num(a.max_task_cost)}
    except Exception:  # noqa: BLE001 — yanlış tipli giriş: heç bir etibarnamənin əhatəsinə uyğun gəlməyən sabit dəyər
        return {"invalid_authority": True}


def _canon_spec(spec: Any) -> dict:
    """Agent adı (sistem tərəfindən yaradılan təsadüfi sonluq) daxil DEYİL: sahib onu əvvəlcədən bilə bilməz; qalan hər şey kanonik hash-dədir."""
    try:
        return {"purpose": str(spec.purpose), "tools": [str(x) for x in spec.tools], "permissions": sorted(str(x) for x in spec.permissions),
                "inputs": [str(x) for x in spec.inputs], "outputs": [str(x) for x in spec.outputs], "cost_limit": _canon_num(spec.cost_limit)}
    except Exception:  # noqa: BLE001
        return {"invalid_spec": True}


def company_scope(op: str, *args: Any) -> str:
    """Şirkət əməliyyatının DƏQİQ kanonik hash-i: SHA-256({op, subject, params}). Sahibin step-up ilə imzaladığı `scope_company` bununla müqayisə olunur (B-3).
    Argumentlər `CompanyAdmin` metoduna verilənlərlə eynidir (`principal` olmadan)."""
    if op == "register_agent":
        spec, department, role, authority, budget = args
        subject, params = str(department), {"role": _clean_role(role), "authority": _canon_authority(authority), "budget": _canon_num(budget), "spec": _canon_spec(spec)}
    elif op == "register_baseline_agent":
        spec, department, role = args
        subject, params = str(department), {"role": _clean_role(role), "spec": _canon_spec(spec)}
    elif op in ("suspend", "resume", "retire"):
        (name,) = args
        subject, params = str(name), {}
    elif op == "set_authority":
        name, authority = args
        subject, params = str(name), {"authority": _canon_authority(authority)}
    elif op == "set_budget":
        name, budget = args
        subject, params = str(name), {"budget": _canon_num(budget)}
    elif op == "set_department_cost":
        department, cost = args
        subject, params = str(department), {"cost": _canon_num(cost)}
    else:
        raise ValueError(f"naməlum şirkət əməliyyatı: {str(op)[:30]}")
    body = json.dumps({"op": op, "subject": subject, "params": params}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)
    return hashlib.sha256(body.encode("ascii")).hexdigest()


def _clean_role(role: Any) -> str:
    return " ".join(str(role).split())[:120] if isinstance(role, str) else ""


def _valid_money(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0


class _Core:
    def __init__(self, registry: Registry, db_path: str | Path, audit_path: str | Path, owners: Iterable[str],
                 allow_mock: bool, clock: Callable[[], float], verifier: Keyring | None = None) -> None:
        self.verifier = verifier   # YALNIZ yoxlama (açıq açarlar): şəxsi açar/imza imkanı yoxdur
        self.owners = frozenset(owners)
        if not self.owners or not all(isinstance(o, str) and o.strip() for o in self.owners):
            raise ValueError("şirkət üçün boş olmayan sahib siyahısı tələb olunur")
        self.registry = registry
        self.allow_mock = bool(allow_mock)
        self.audit_path = Path(audit_path)
        self.clock = clock
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(str(db_path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        for stmt in _SCHEMA:
            self.conn.execute(stmt)
        for dept in DEPARTMENTS:
            self.conn.execute("INSERT OR IGNORE INTO company_departments (department, step_cost) VALUES (?, ?)",
                              (dept, DEFAULT_STEP_COST))

    # --- audit və icazə ---
    def audit(self, event: str, **fields: Any) -> None:
        row = {"ts": datetime.fromtimestamp(float(self.clock()), timezone.utc).isoformat(), "event": event}
        row.update(fields)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def reject(self, op: str, code: str, reason: str, principal: Principal | None = None, **extra: Any) -> CompanyDenied:
        fields = dict(op=op, code=code, reason=reason, **extra)
        if principal is not None:
            fields.update(principal_id=principal.id, principal_kind=principal.kind)
        self.audit("company_denied", **fields)
        return CompanyDenied(reason, code)

    def identify(self, principal: Principal, op: str) -> None:
        """Kimlik yoxlaması (agent/rol/imza/vaxt/ləğv). Hər imtiyazlı əməliyyatın ƏVVƏLİNDƏ çağırılır: autentifikasiyasız çağıran agentin mövcudluğunu belə öyrənmir."""
        if not isinstance(principal, Principal):
            raise self.reject(op, "unauthorized", "principal tələb olunur")
        if principal.kind == "agent":
            raise self.reject(op, "self_grant", "agent səlahiyyət/büdcə təyin edə və ya dəyişə bilməz", principal)
        if self.verifier is None:
            raise self.reject(op, "owner_auth_unavailable", "etibarlı autentifikasiya qurulmayıb: imtiyazlı əməliyyat bağlıdır", principal)
        try:
            self.verifier.check_principal(principal, "owner", "system", "mock_owner")
        except CredentialError as exc:
            raise self.reject(op, exc.code, str(exc), principal) from None

    def authorize(self, principal: Principal, op: str, expansion: bool, scope: str = "") -> None:
        """Hər imtiyazlı əməliyyat üçün Company şəxsiyyəti MÜSTƏQİL yoxlayır: yalnız etibarlı açarla imzalanmış, vaxtı keçməmiş, ləğv olunmamış
        `owner`/`system`/`mock_owner` etibarnaməsi qəbul olunur. Özü yaradılmış `Principal` heç vaxt səlahiyyət vermir; yoxlayıcı yoxdursa əməliyyat bağlıdır."""
        self.identify(principal, op)
        if principal.kind == "system" and expansion:
            raise self.reject(op, "system_cannot_expand", "sistem səlahiyyəti genişləndirə bilməz (yalnız sahib)",
                            principal)
        if principal.kind == "system":
            return
        if principal.kind == "mock" and not self.allow_mock:
            raise self.reject(op, "mock_rejected", "mock təsdiqi real sahib təsdiqi deyil və qəbul olunmur", principal)
        if principal.id not in self.owners:
            raise self.reject(op, "unauthorized", "əməliyyatı edən sahib siyahısında deyil", principal)
        if getattr(principal, "scope_op", "") not in COMPANY_OP_SCOPES.get(op, frozenset()):
            raise self.reject(op, "scope_mismatch", "sahib etibarnaməsinin əhatəsi bu əməliyyata uyğun deyil", principal)
        if not scope or getattr(principal, "scope_company", "") != scope:        # Company MÜSTƏQİL yoxlayır: əməliyyat + hədəf + kanonik parametr hash-i (fabrikin yoxlamasına güvənmir)
            raise self.reject(op, "scope_mismatch", "sahib etibarnaməsi bu dəqiq şirkət əməliyyatına/hədəfinə/parametrlərinə bağlı deyil", principal)
        try:                        # bir-dəfəlik: eyni sahib etibarnaməsi ikinci Company əməliyyatını təsdiqləyə bilmir
            self.conn.execute("INSERT INTO company_used_credentials (nonce, op, used_at) VALUES (?, ?, ?)",
                              (principal.nonce, op, float(self.clock())))  # type: ignore[attr-defined]
        except sqlite3.IntegrityError:
            raise self.reject(op, "credential_replayed", "sahib etibarnaməsi artıq istifadə olunub (bir-dəfəlik)", principal) from None

    @staticmethod
    def marks(principal: Principal) -> dict[str, Any]:
        return {"principal_id": principal.id, "source_kind": principal.kind, "human": principal.kind == "human"}

    # --- yardımçılar ---
    def _row(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM company_agents WHERE name = ?", (name,)).fetchone()

    @staticmethod
    def _agent(row: sqlite3.Row) -> CompanyAgent:
        auth = Authority(frozenset(json.loads(row["permissions_json"])), bool(row["sensitive_ok"]),
                         frozenset(json.loads(row["delegate_json"])), float(row["max_task_cost"]))
        return CompanyAgent(row["name"], row["department"], row["role"], auth, float(row["budget"]), row["status"],
                            AgentSpec.from_yaml(row["spec_yaml"]), float(row["created_at"]), float(row["updated_at"]))

    @staticmethod
    def effective(spec: AgentSpec, authority: Authority, budget: float, retired: bool = False) -> AgentSpec:
        perms = [] if retired else sorted(authority.permissions & set(spec.permissions))
        return AgentSpec(spec.name, spec.purpose, list(spec.tools), perms, list(spec.inputs), list(spec.outputs),
                         min(spec.cost_limit, budget))

    def validate(self, op: str, spec: AgentSpec, authority: Authority, budget: Any) -> None:
        if not authority.permissions <= set(spec.permissions):
            raise self.reject(op, "exceeds_spec", "səlahiyyət agentin spec icazələrindən artıqdır")
        if authority.permissions & SENSITIVE_PERMISSIONS and not authority.sensitive_ok:
            raise self.reject(op, "sensitive_flag", "həssas icazə üçün sensitive_ok tələb olunur")
        if not _valid_money(budget) or budget > spec.cost_limit:
            raise self.reject(op, "budget", "büdcə yanlışdır və ya agentin spec xərc limitindən çoxdur")
        if authority.max_task_cost > budget:
            raise self.reject(op, "max_task_cost", "tapşırıq üçün maksimal xərc büdcədən çoxdur")

    @staticmethod
    def expands(old: Authority, new: Authority) -> bool:
        return bool(new.permissions - old.permissions or (new.sensitive_ok and not old.sensitive_ok)
                    or new.delegate_to - old.delegate_to or new.max_task_cost > old.max_task_cost)

    def now(self) -> float:
        return float(self.clock())


class CompanyView:
    """CEO/orkestrasiya üçün: oxu və ötürmə. Səlahiyyət dəyişdirmə imkanı YOXDUR."""

    def __init__(self, core: _Core) -> None:
        self._core = core

    def get(self, name: str) -> CompanyAgent | None:
        row = self._core._row(name) if isinstance(name, str) else None
        return self._core._agent(row) if row is not None else None

    def agents(self, department: str | None = None, include_retired: bool = False) -> list[CompanyAgent]:
        sql, params = "SELECT * FROM company_agents", []
        where = []
        if department is not None:
            where.append("department = ?")
            params.append(department)
        if not include_retired:
            where.append("status = 'active'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._core.conn.execute(sql + " ORDER BY name", params).fetchall()
        return [self._core._agent(r) for r in rows]

    def department_costs(self) -> dict[str, float]:
        """Aktiv agenti olan departamentlərin deterministik addım xərcləri (planlaşdırıcı üçün)."""
        rows = self._core.conn.execute(
            "SELECT d.department, d.step_cost FROM company_departments d WHERE EXISTS "
            "(SELECT 1 FROM company_agents a WHERE a.department = d.department AND a.status = 'active') "
            "ORDER BY d.department").fetchall()
        return {r["department"]: float(r["step_cost"]) for r in rows}

    def budget_policy(self, global_limit: float | None = None, task_limit: float | None = None) -> BudgetPolicy:
        return BudgetPolicy(global_limit=global_limit, task_limit=task_limit,
                            agent_limits={a.name: a.budget for a in self.agents()})

    def handoff(self, from_agent: str, to_department: str, task: Task, reason: str = "") -> HandoffResult:
        c = self._core
        digest = hashlib.sha256(str(getattr(task, "description", "")).encode("utf-8")).hexdigest()
        base = dict(from_agent=str(from_agent), to_department=str(to_department),
                    permission=getattr(task, "permission", ""), cost=getattr(task, "cost", 0.0),
                    description_sha256=digest)

        def refuse(code: str, msg: str) -> HandoffResult:
            c.audit("company_handoff_denied", code=code, reason=msg, **base)
            return HandoffResult(False, msg, None, code)

        if not isinstance(task, Task):
            return refuse("bad_task", "tapşırıq Task olmalıdır")
        agent = self.get(from_agent)
        if agent is None:
            return refuse("unknown_agent", "ötürən agent tapılmadı")
        if agent.status != "active":
            return refuse("agent_retired", "ötürən agent fəaliyyətdən çıxarılıb")
        if to_department not in DEPARTMENTS:
            return refuse("unknown_department", "naməlum departament")
        if not self.agents(to_department):
            return refuse("no_active_agents", "hədəf departamentdə aktiv agent yoxdur")
        a = agent.authority
        if to_department not in a.delegate_to:
            return refuse("not_delegable", "agentin bu departamentə tapşırıq ötürmək səlahiyyəti yoxdur")
        if task.permission not in a.permissions:
            return refuse("permission_not_held", "agent özündə olmayan icazəni ötürə bilməz")
        if not _valid_money(task.cost) or task.cost > a.max_task_cost:
            return refuse("cost_over_limit", "tapşırıq xərci agentin maksimumunu aşır")
        if task.permission in SENSITIVE_PERMISSIONS and not a.sensitive_ok:
            return refuse("sensitive_not_allowed", "həssas tapşırıq ötürmək üçün sensitive_ok yoxdur")
        out = replace(task, department=to_department, op_id=task.op_id or f"handoff:{uuid.uuid4().hex}")
        c.audit("company_handoff", op_id=out.op_id, reason_sha256=hashlib.sha256(
            str(reason).encode("utf-8")).hexdigest(), **base)
        return HandoffResult(True, "ötürmə təsdiqləndi", out)


def _serial(fn):
    """CompanyAdmin əməliyyatları (yoxla-sonra-yaz ardıcıllığı) bir kilid altında: ipler arası yarış yoxdur (audit B-5)."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._core.conn.lock:
            return fn(self, *a, **k)
    return wrapper



class CompanyAdmin:
    """Yalnız sahib/kanal adapterləri üçün. Agentlərə, CEO-ya və LLM-ə verilmir."""

    def __init__(self, core: _Core) -> None:
        self._core = core

    @_serial
    def register_agent(self, spec: AgentSpec, department: str, role: str, authority: Authority, budget: float,
                       principal: Principal) -> CompanyAgent:
        c = self._core
        op = "register_agent"
        c.authorize(principal, op, expansion=True, scope=company_scope(op, spec, department, role, authority, budget))
        if department not in DEPARTMENTS:
            raise c.reject(op, "unknown_department", f"naməlum şöbə: {department}", principal)
        role = _clean_role(role)
        if not role:
            raise c.reject(op, "role", "rol boş ola bilməz", principal)
        if c._row(spec.name) is not None:
            raise c.reject(op, "exists", "agent artıq şirkətdə mövcuddur", principal)
        c.validate(op, spec, authority, budget)
        eff = c.effective(spec, authority, budget)
        try:
            c.registry.add(eff, department)
        except RegistryError as exc:
            raise c.reject(op, "exists", str(exc), principal) from None
        now = c.now()
        c.conn.execute(
            "INSERT INTO company_agents (name, department, role, spec_yaml, permissions_json, sensitive_ok, "
            "delegate_json, max_task_cost, budget, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (spec.name, department, role, spec.to_yaml(), json.dumps(sorted(authority.permissions)),
             int(authority.sensitive_ok), json.dumps(sorted(authority.delegate_to)), float(authority.max_task_cost),
             float(budget), now, now))
        c.audit("company_agent_registered", agent=spec.name, department=department, role=role,
                permissions=sorted(authority.permissions), budget=float(budget), **c.marks(principal))
        return CompanyView(c).get(spec.name)

    @_serial
    def register_baseline_agent(self, spec: AgentSpec, department: str, role: str, principal: Principal) -> CompanyAgent:
        """MƏHDUD agent qeydiyyatı: səlahiyyət HƏMİŞƏ {read_files} ∩ spec, həssas icazə yox, ötürmə yox, tapşırıq/büdcə 0. Sistem (NAYMERA) və sahib
        çağıra bilər; agent çağıra bilməz. Genişləndirmə yalnız sahib tərəfindən (`set_authority`/`set_budget`)."""
        c = self._core
        op = "register_baseline_agent"
        c.authorize(principal, op, expansion=False, scope=company_scope(op, spec, department, role))
        if department not in DEPARTMENTS:
            raise c.reject(op, "unknown_department", f"naməlum şöbə: {department}", principal)
        role = _clean_role(role)
        if not role:
            raise c.reject(op, "role", "rol boş ola bilməz", principal)
        if c._row(spec.name) is not None:
            raise c.reject(op, "exists", "agent artıq şirkətdə mövcuddur", principal)
        authority = Authority(frozenset({"read_files"} & set(spec.permissions)), False, frozenset(), 0.0)
        c.validate(op, spec, authority, 0.0)
        eff = c.effective(spec, authority, 0.0)
        try:
            c.registry.add(eff, department)
        except RegistryError as exc:
            raise c.reject(op, "exists", str(exc), principal) from None
        now = c.now()
        c.conn.execute(
            "INSERT INTO company_agents (name, department, role, spec_yaml, permissions_json, sensitive_ok, "
            "delegate_json, max_task_cost, budget, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, '[]', 0.0, 0.0, 'active', ?, ?)",
            (spec.name, department, role, spec.to_yaml(), json.dumps(sorted(authority.permissions)), now, now))
        c.audit("company_baseline_registered", agent=spec.name, department=department, role=role,
                permissions=sorted(authority.permissions), budget=0.0, **c.marks(principal))
        return CompanyView(c).get(spec.name)

    @_serial
    def suspend(self, name: str, principal: Principal) -> CompanyAgent:
        """Geri qaytarıla bilən dayandırma: effektiv icazələr boşaldılır, səlahiyyət/büdcə saxlanılır. Sistem və ya sahib (azaltma)."""
        c = self._core
        op = "suspend"
        c.identify(principal, op)
        row = c._row(name)
        if row is None:
            raise c.reject(op, "unknown_agent", "agent tapılmadı", principal)
        c.authorize(principal, op, expansion=False, scope=company_scope(op, name))
        agent = c._agent(row)
        if agent.status != "active":
            raise c.reject(op, "not_active", "yalnız aktiv agent dayandırıla bilər", principal)
        status = "suspended_owner" if principal.kind in ("human", "mock") else "suspended_system"
        c.registry.update(c.effective(agent.spec, agent.authority, agent.budget, retired=True))
        c.conn.execute("UPDATE company_agents SET status = ?, updated_at = ? WHERE name = ?", (status, c.now(), name))
        c.audit("company_agent_suspended", agent=name, status=status, **c.marks(principal))
        return CompanyView(c).get(name)

    @_serial
    def resume(self, name: str, principal: Principal) -> CompanyAgent:
        """Əvvəlki səlahiyyəti BƏRPA edir (yeni səlahiyyət vermir). Sahibin dayandırdığını yalnız sahib davam etdirə bilər."""
        c = self._core
        op = "resume"
        c.identify(principal, op)
        row = c._row(name)
        if row is None:
            raise c.reject(op, "unknown_agent", "agent tapılmadı", principal)
        c.authorize(principal, op, expansion=False, scope=company_scope(op, name))
        agent = c._agent(row)
        if agent.status not in ("suspended_system", "suspended_owner"):
            raise c.reject(op, "not_suspended", "agent dayandırılmayıb (və ya fəaliyyətdən çıxarılıb)", principal)
        if agent.status == "suspended_owner" and principal.kind not in ("human", "mock"):
            raise c.reject(op, "owner_suspended", "sahibin dayandırdığı agenti yalnız sahib davam etdirə bilər", principal)
        c.registry.update(c.effective(agent.spec, agent.authority, agent.budget))
        c.conn.execute("UPDATE company_agents SET status = 'active', updated_at = ? WHERE name = ?", (c.now(), name))
        c.audit("company_agent_resumed", agent=name, **c.marks(principal))
        return CompanyView(c).get(name)

    @_serial
    def set_authority(self, name: str, authority: Authority, principal: Principal) -> CompanyAgent:
        c = self._core
        op = "set_authority"
        c.identify(principal, op)
        row = c._row(name)
        if row is None:
            raise c.reject(op, "unknown_agent", "agent tapılmadı", principal)
        agent = c._agent(row)
        c.authorize(principal, op, expansion=c.expands(agent.authority, authority), scope=company_scope(op, name, authority))
        if agent.status != "active":
            raise c.reject(op, "agent_retired", "fəaliyyətdən çıxarılmış agentin səlahiyyəti dəyişdirilmir", principal)
        c.validate(op, agent.spec, authority, agent.budget)
        c.registry.update(c.effective(agent.spec, authority, agent.budget))
        c.conn.execute(
            "UPDATE company_agents SET permissions_json = ?, sensitive_ok = ?, delegate_json = ?, "
            "max_task_cost = ?, updated_at = ? WHERE name = ?",
            (json.dumps(sorted(authority.permissions)), int(authority.sensitive_ok),
             json.dumps(sorted(authority.delegate_to)), float(authority.max_task_cost), c.now(), name))
        c.audit("company_authority_changed", agent=name, before=sorted(agent.authority.permissions),
                after=sorted(authority.permissions), **c.marks(principal))
        return CompanyView(c).get(name)

    @_serial
    def set_budget(self, name: str, budget: float, principal: Principal) -> CompanyAgent:
        c = self._core
        op = "set_budget"
        c.identify(principal, op)
        row = c._row(name)
        if row is None:
            raise c.reject(op, "unknown_agent", "agent tapılmadı", principal)
        agent = c._agent(row)
        c.authorize(principal, op, expansion=_valid_money(budget) and budget > agent.budget, scope=company_scope(op, name, budget))
        if agent.status != "active":
            raise c.reject(op, "agent_retired", "fəaliyyətdən çıxarılmış agentin büdcəsi dəyişdirilmir", principal)
        c.validate(op, agent.spec, agent.authority, budget)
        c.registry.update(c.effective(agent.spec, agent.authority, budget))
        c.conn.execute("UPDATE company_agents SET budget = ?, updated_at = ? WHERE name = ?",
                       (float(budget), c.now(), name))
        c.audit("company_budget_changed", agent=name, before=agent.budget, after=float(budget), **c.marks(principal))
        return CompanyView(c).get(name)

    @_serial
    def retire(self, name: str, principal: Principal) -> CompanyAgent:
        c = self._core
        op = "retire"
        c.identify(principal, op)
        row = c._row(name)
        if row is None:
            raise c.reject(op, "unknown_agent", "agent tapılmadı", principal)
        c.authorize(principal, op, expansion=False, scope=company_scope(op, name))  # azaltma: sahib və ya sistem
        agent = c._agent(row)
        c.registry.update(c.effective(agent.spec, agent.authority, agent.budget, retired=True))
        c.conn.execute("UPDATE company_agents SET status = 'retired', updated_at = ? WHERE name = ?", (c.now(), name))
        c.audit("company_agent_retired", agent=name, **c.marks(principal))
        return CompanyView(c).get(name)

    @_serial
    def set_department_cost(self, department: str, cost: float, principal: Principal) -> None:
        c = self._core
        op = "set_department_cost"
        c.authorize(principal, op, expansion=True, scope=company_scope(op, department, cost))  # planlaşdırma xərc cədvəli: yalnız sahib
        if department not in DEPARTMENTS:
            raise c.reject(op, "unknown_department", f"naməlum şöbə: {department}", principal)
        if not _valid_money(cost):
            raise c.reject(op, "cost", "xərc sonlu və mənfi olmayan rəqəm olmalıdır", principal)
        c.conn.execute("UPDATE company_departments SET step_cost = ? WHERE department = ?", (float(cost), department))
        c.audit("company_department_cost_changed", department=department, cost=float(cost), **c.marks(principal))


class Company:
    def __init__(self, registry: Registry, db_path: str | Path = DEFAULT_DB,
                 audit_path: str | Path = DEFAULT_AUDIT, owners: Iterable[str] = (), allow_mock: bool = False,
                 clock: Callable[[], float] = time.time, verifier: Keyring | None = None) -> None:
        self._core = _Core(registry, db_path, audit_path, owners, allow_mock, clock, verifier)
        self.view = CompanyView(self._core)
        self.admin = CompanyAdmin(self._core)


# --- nəticələrin birləşdirilməsi və yaddaşa yazılması ---

@dataclass(frozen=True)
class StepResult:
    step_id: str
    department: str
    agent: str
    status: str
    output: Any
    reason: str
    op_id: str
    approval_id: str
    attempts: int
    restored: bool
    needs_reconciliation: bool


@dataclass(frozen=True)
class ResultBundle:
    goal: str
    status: str
    steps: tuple[StepResult, ...]

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def failures(self) -> list[StepResult]:
        return [s for s in self.steps if s.status != "ok"]

    @property
    def pending_approvals(self) -> list[StepResult]:
        return [s for s in self.steps if s.status == "pending_approval"]

    @property
    def needs_reconciliation(self) -> list[StepResult]:
        return [s for s in self.steps if s.needs_reconciliation]


def merge_results(report: JobReport) -> ResultBundle:
    """CEO nəticələrini mənbə (agent/şöbə/addım) və icra statusu ilə vahid paketdə birləşdirir."""
    steps = tuple(StepResult(s.step_id, s.department, s.agent, s.status, s.output, s.reason, s.op_id, s.approval_id,
                             s.attempts, s.restored, s.needs_reconciliation) for s in report.steps)
    return ResultBundle(report.goal, report.status, steps)


def _write(memory: MemoryService, category: str, content: str, **kw: Any) -> WriteResult:
    return memory.write(SYSTEM_ACTOR, category, content, **kw)


def record_job_results(memory: MemoryService, report: JobReport, *, job_id: str, goal: str = "",
                       project: str = "default") -> list[WriteResult]:
    """İş və addım nəticələrini yaddaşa yazır (mənbə, icra statusu ilə). Sirr aşkarlanan çıxış gizlədilir;
    təkrar çağırış (`op_key`) təkrar qeyd yaratmır."""
    out: list[WriteResult] = []
    ok = sum(s.status == "ok" for s in report.steps)
    head = f"iş {job_id}: {report.status}; {ok}/{len(report.steps)} addım uğurlu"
    goal_text = f"{head}; məqsəd: {goal[:200]}" if goal and not detect_secrets(goal) else head
    out.append(_write(memory, "task", goal_text, project=project, op_key=f"{job_id}:job",
                      meta={"job_id": job_id, "status": report.status}))
    for s in report.steps:
        raw = json.dumps(s.output, ensure_ascii=False, default=str) if s.status == "ok" else (s.reason or s.status)
        withheld = bool(detect_secrets(raw))
        body = "[çıxış gizlədildi: sirr aşkarlandı]" if withheld else raw[:4000]
        agent = s.agent or ""
        out.append(_write(
            memory, "result", f"[{s.step_id}] {body}", project=project, op_key=f"{job_id}:{s.step_id}:result",
            provenance="agent" if agent else "system", source=f"agent:{agent}" if agent else "",
            meta={"job_id": job_id, "step_id": s.step_id, "department": s.department, "status": s.status,
                  "attempts": s.attempts, "op_id": s.op_id, "approval_id": s.approval_id,
                  "needs_reconciliation": s.needs_reconciliation, "restored": s.restored,
                  "output_withheld": withheld}))
    return out


def record_plan_decision(memory: MemoryService, goal: str, proposal: Any, *, job_id: str,
                         project: str = "default") -> WriteResult:
    """Plan təklifinin QƏRARINI (xam model mətnini yox) yazır; mock və real LLM mənşəyi ayrı saxlanır."""
    mock = bool(getattr(proposal, "is_mock", False))
    steps = getattr(proposal, "steps", ())
    if getattr(proposal, "ok", False):
        content = f"plan təklifi ({job_id}): " + ", ".join(f"{s.id}={s.department}:{s.permission}" for s in steps)
    else:
        content = f"plan təklifi rədd edildi ({job_id}): {getattr(proposal, 'reason', '')[:300]}"
    provider = getattr(proposal, "provider", "") or "unknown"
    return _write(memory, "decision", content, project=project, provenance="llm_mock" if mock else "llm_real",
                  source=f"llm:{provider}", op_key=f"{job_id}:plan",
                  meta={"job_id": job_id, "is_mock": mock, "provider": provider,
                        "model": getattr(proposal, "model", ""), "ok": bool(getattr(proposal, "ok", False)),
                        "response_sha256": getattr(proposal, "response_sha256", "")})


def record_approval(memory: MemoryService, ticket: Any, *, project: str = "default") -> WriteResult:
    """Təsdiq tarixçəsi (səlahiyyət DEYİL): mock təsdiqi real sahib təsdiqi kimi işarələnmir."""
    kind = getattr(ticket, "approver_kind", "")
    prov = {"human": "owner", "mock": "mock"}.get(kind, "system")
    return _write(
        memory, "approval",
        f"təsdiq {ticket.status}: {ticket.permission} agent={ticket.agent} xərc={ticket.cost} op_id={ticket.op_id}",
        project=project, provenance=prov, source=f"approval:{ticket.approver_id or 'gözləyir'}",
        op_key=f"approval:{ticket.id}:{ticket.status}",
        meta={"approval_id": ticket.id, "status": ticket.status, "payload_hash": ticket.payload_hash,
              "approver_kind": kind, "approver_id": ticket.approver_id, "channel": ticket.channel,
              "human": bool(getattr(ticket, "human_approved", False))})
