"""Agent icrası. Bütün agent işləri Orkestrator -> Effect Broker -> Gate / Təsdiq / Ledger yolundan keçir (bu modul onları keçmir və birbaşa import etmir).

- `AgentLLMRunner`: Orkestratorun icra mərhələsində çağırılır; agentin sistem təlimatı və tapşırığı ilə `HybridRouter`-dən MƏTN alır (Cloud/Local/AUTO).
  Alət, fayl, şəbəkə, ödəniş, nəşr çağırışı YOXDUR: nəticə yalnız etibarsız mətn (qaralama/hesabat) olur.
- `AgentDispatcher`: tapşırığı atomik tutur (təkrar icra yoxdur), Orkestratora verir, nəticəni yoxlayır (ölçü, sirr, təlimat bənzərliyi), agentin
  hesabatını yazır; həssas icazə (publish və s.) Gate-də sahib təsdiqini gözləyir (`awaiting_approval`), təsdiqdən sonra EYNİ op_id ilə davam edir.
- Paralel generasiya (`prefetch`): müstəqil tapşırıqların AI mətni eyni anda alınır (yan təsirsiz); icra/qeyd HƏMİŞƏ ardıcıl və Broker üzərindədir.
  Eyni `request_id` ilə router nəticəni saxladığı üçün sonradan yenidən generasiya olmur.
"""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from agents.agent_factory import (AgentFactory, AgentRecord, AgentReport, DispatchAuthorization, ExchangeRejected, FactoryDenied, TaskRecord,
                                  validate_exchange)
from agents.factory_auth import SignedPrincipal
from agents.hybrid import HybridRouter, RouteResult
from agents.llm import SpendScope, spend_scope
from agents.memory import Actor, MemoryDenied, MemoryService
from agents.orchestrator import ExecutionError, Orchestrator, Task, TransientError
from agents.spec import SENSITIVE_PERMISSIONS

_TRANSIENT_STATUSES = frozenset({"all_unavailable", "cloud_unavailable", "local_unavailable", "in_progress"})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def approval_input(agent: AgentRecord, t: TaskRecord) -> dict:
    """Təsdiqə və icraya EYNİ giriş: Broker bunun SHA-256-nı təsdiqə bağlayır, icraçı isə yalnız bu girişdən istifadə edir (F-2).
    Mətnin özü (`sorgu`) və onun, agent təlimatının hash-ləri daxildir; icradan dərhal əvvəl yenidən yoxlanılır."""
    return {"sorgu": t.prompt, "task_id": t.task_id, "agent_id": agent.agent_id, "kind": t.kind, "prompt_sha256": _sha(t.prompt),
            "system_sha256": _sha(agent.system_instruction)}


class AgentLLMRunner:
    """Orkestratorun `runner(spec, input)` müqaviləsi. Yalnız broker-in icra mərhələsində çağırılır."""

    def __init__(self, factory: AgentFactory, router: HybridRouter, memory: Callable[[], Sequence[Any]] | None = None,
                 allow_simulated: bool = True, global_limit: Callable[[], float | None] | None = None) -> None:
        self.factory, self.router, self.memory, self.allow_simulated = factory, router, memory, allow_simulated
        self.global_limit = global_limit   # Ledger ümumi limiti (Orkestratorun siyasətindən); None: yalnız agent limiti

    def generate(self, agent: AgentRecord, task: TaskRecord, prompt: str | None = None) -> RouteResult:
        """`prompt` verilibsə (təsdiqlənmiş giriş) YALNIZ o istifadə olunur. Həssas icazəli tapşırıqda yaddaş konteksti əlavə edilmir ki,
        təsdiq olunan mətn ilə model-ə gedən mətn eyni olsun (F-2).
        Ödənişli Cloud: agent büdcəsi 0 isə mühərrik heç yoxlanmır; əks halda xərc Ledger-də agentin adına rezerv edilir (F-3)."""
        text = task.prompt if prompt is None else prompt
        context: list[Any] = []
        if self.memory is not None and task.permission not in SENSITIVE_PERMISSIONS:
            try:  # yaddaş yalnız OXUNUR; agentin oxuya biləcəyi kateqoriyalarla məhdudlaşır (Cloud-a ötürmə router-in məxfilik siyasətindədir)
                context = [r for r in self.memory() if getattr(r, "category", "") in agent.memory_policy.read_categories][:3]
            except Exception:  # noqa: BLE001
                context = []
        request_id = f"agt:{task.task_id}"
        budget = float(agent.budget)
        scope = SpendScope(agent.name, request_id, max(0.0, budget), self.global_limit() if self.global_limit is not None else None)
        with spend_scope(scope):
            return self.router.route(request_id, "chat", text, agent.system_instruction, context, allow_mock=self.allow_simulated,
                                     allow_paid=budget > 0.0)

    def __call__(self, spec: Any, data: dict) -> dict:
        agent = self.factory.view.find(spec.name)
        task = self.factory.view.task(str(data.get("task_id", "")))
        if agent is None or task is None or task.agent_id != agent.agent_id:
            raise ExecutionError("agent/tapşırıq uyğunsuzluğu", cost=0.0)
        text = data.get("sorgu")
        # F-2: icradan DƏRHAL əvvəl təsdiqlənmiş giriş yenidən yoxlanılır; hər uyğunsuzluq (mutasiya/dəyişdirmə) icranı dayandırır
        if not isinstance(text, str) or data.get("prompt_sha256") != _sha(text) or data.get("agent_id") != agent.agent_id \
                or task.prompt_sha != _sha(text) or _sha(task.prompt) != task.prompt_sha or data.get("kind") != task.kind \
                or data.get("system_sha256") != _sha(agent.system_instruction) or task.state != "running":
            raise ExecutionError("təsdiqlənmiş giriş icra olunacaq girişlə üst-üstə düşmür: icra dayandırıldı", cost=0.0)
        res = self.generate(agent, task, text)
        if res.status != "ok":
            exc = TransientError if res.status in _TRANSIENT_STATUSES else ExecutionError
            raise exc(f"AI mətni alınmadı: {res.status}: {res.reason}"[:300], cost=0.0)
        return {"netice": res.text, "engine": res.engine, "is_mock": res.is_mock}


@dataclass(frozen=True)
class DispatchResult:
    status: str                       # done | failed | retry | awaiting_approval | not_claimable
    task_id: str
    report: AgentReport | None = None
    engine: str = ""
    is_mock: bool = False
    approval_id: str = ""
    reason: str = ""
    attempts: int = 0


class AgentDispatcher:
    def __init__(self, factory: AgentFactory, orchestrator: Orchestrator, runner: AgentLLMRunner, *, principal: SignedPrincipal, approvals: Any = None,
                 max_attempts: int = 2, max_output_chars: int = 3500, max_workers: int = 4) -> None:
        """`principal`: `PrincipalAuthority.dispatcher()` ilə imzalanmış icra qatı şəxsiyyəti. Saxta/imzasız şəxsiyyət ilə heç bir tapşırıq tutulmur."""
        self.factory, self.orchestrator, self.runner, self.principal = factory, orchestrator, runner, principal
        self.approvals = approvals  # YALNIZ OXU üzü (`ApprovalReader`): gözləyən təsdiqi Broker-i çağırmadan yoxlamaq üçün
        self.max_attempts, self.max_output_chars, self.max_workers = max_attempts, max_output_chars, max_workers
        self._exec_lock = threading.Lock()   # Orkestrator/Broker sorğuları ARDICIL icra olunur (SQLite və audit bütövlüyü)

    # --- paralel generasiya ---
    def prefetch(self, task_ids: Sequence[str]) -> None:
        """Müstəqil tapşırıqların AI mətnini eyni anda alır. Həssas icazəli, dayandırılmış agentə aid və ya növbədə olmayan tapşırıqlar üçün generasiya olunmur."""
        view = self.factory.view
        jobs: list[tuple[AgentRecord, TaskRecord]] = []
        for tid in task_ids:
            t = view.task(tid)
            a = view.agent(t.agent_id) if t is not None else None
            if t is None or a is None or t.state != "queued" or a.status != "active":
                continue
            if t.permission in SENSITIVE_PERMISSIONS or t.permission not in a.permissions:
                continue
            jobs.append((a, t))
        if not jobs:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(self.max_workers, len(jobs)))) as pool:
            for fut in [pool.submit(self._safe_generate, a, t) for a, t in jobs]:
                fut.result()

    def _safe_generate(self, agent: AgentRecord, task: TaskRecord) -> None:
        try:
            self.runner.generate(agent, task)
        except Exception:  # noqa: BLE001 — generasiya uğursuzluğu icra mərhələsində yenidən yoxlanılır və hesabata düşür
            return

    # --- icra ---
    def execute(self, task_id: str) -> DispatchResult:
        rt = self.factory.runtime
        claimed = rt.claim(task_id, self.principal)
        t = claimed.task if claimed is not None else None
        if t is None:
            cur = self.factory.view.task(task_id)
            return DispatchResult("not_claimable", task_id, reason="tapşırıq tutula bilmədi (artıq icra olunub/olunur və ya agent aktiv deyil)",
                                  attempts=cur.attempts if cur else 0)
        agent = self.factory.view.agent(t.agent_id)
        assert agent is not None
        auth = claimed.authorization
        if _sha(t.prompt) != t.prompt_sha:   # F-2: saxlanmış mətn yaradılışdan sonra dəyişdirilib -> icra və ya təsdiq sorğusu yoxdur
            reason = "prompt_integrity: tapşırıq mətni dəyişdirilib"
            rt.fail(auth, reason, retry=False)
            return DispatchResult("failed", t.task_id, reason=reason, attempts=t.attempts)
        if t.approval_id and self.approvals is not None:
            ticket = self.approvals.get(t.approval_id)
            if ticket is not None and ticket.status == "pending":   # sahib hələ qərar verməyib: icra yoxdur, gözləmə davam edir
                rt.await_approval(auth, t.approval_id)
                return DispatchResult("awaiting_approval", t.task_id, approval_id=t.approval_id, reason="sahib təsdiqi gözlənilir", attempts=t.attempts)
        orch_task = Task(agent.department, approval_input(agent, t), t.permission, t.cost, t.title, op_id=t.run_op,
                         approval_id=t.approval_id, agent=agent.name)
        with self._exec_lock:
            res = self.orchestrator.execute(orch_task)
        if res.status == "pending_approval":
            rt.await_approval(auth, res.approval_id)
            return DispatchResult("awaiting_approval", t.task_id, approval_id=res.approval_id, reason=res.reason, attempts=t.attempts)
        if res.status == "ok":
            return self._finish(agent, t, auth, res.output, res.cost)
        retry = res.status == "error" and bool(res.transient) and t.attempts < self.max_attempts
        reason = f"{res.status}: {res.reason}"[:200]
        rt.fail(auth, reason, retry=retry)
        return DispatchResult("retry" if retry else "failed", t.task_id, reason=reason, attempts=t.attempts)

    def _finish(self, agent: AgentRecord, t: TaskRecord, auth: DispatchAuthorization, output: Any, cost: float | None) -> DispatchResult:
        rt = self.factory.runtime
        text = output.get("netice") if isinstance(output, dict) else None
        try:
            ex = validate_exchange(text, source=agent.name, max_chars=self.max_output_chars)
        except ExchangeRejected as exc:
            reason = f"failed_check:{exc.code}"
            rt.fail(auth, reason, retry=False)
            return DispatchResult("failed", t.task_id, reason=reason, attempts=t.attempts)
        try:
            report = self.factory.handle(agent.agent_id).submit_result(t.task_id, ex.text, cost=min(float(cost or 0.0), t.cost),
                                                                      authorization=auth)
        except FactoryDenied as exc:
            rt.fail(auth, f"report_rejected:{exc.code}", retry=False)
            return DispatchResult("failed", t.task_id, reason=f"report_rejected:{exc.code}", attempts=t.attempts)
        rt.note_success(auth)
        out = output if isinstance(output, dict) else {}
        return DispatchResult("done", t.task_id, report=report, engine=str(out.get("engine", "")), is_mock=bool(out.get("is_mock", False)),
                              attempts=t.attempts)

    def execute_with_retry(self, task_id: str) -> DispatchResult:
        r = self.execute(task_id)
        while r.status == "retry":
            r = self.execute(task_id)
        return r

    # --- yaddaş ---
    def remember(self, task_id: str, memory: MemoryService, project: str = "agents") -> bool:
        """Təsdiqlənmiş nəticəni mövcud nəzarətli yaddaşa YAZIR (agent kimi, yalnız `result`; təkrar və şübhəli nəticə yazılmır)."""
        t = self.factory.view.task(task_id)
        if t is None or t.state != "done" or t.suspicious:
            return False
        agent = self.factory.view.agent(t.agent_id)
        reports = self.factory.view.reports(task_id=task_id)
        if agent is None or not reports or "result" not in agent.memory_policy.write_categories:
            return False
        try:
            res = memory.write(Actor(agent.name, "agent"), "result", reports[0].summary, project=project, op_key=f"agent-task:{task_id}")
        except MemoryDenied:
            return False
        return bool(res.created)
