"""Agent icrası. Bütün agent işləri Orkestrator -> Effect Broker -> Gate / Təsdiq / Ledger yolundan keçir (bu modul onları keçmir və birbaşa import etmir).

- `AgentLLMRunner`: Orkestratorun icra mərhələsində çağırılır; agentin sistem təlimatı və tapşırığı ilə `HybridRouter`-dən MƏTN alır (Cloud/Local/AUTO).
  Alət, fayl, şəbəkə, ödəniş, nəşr çağırışı YOXDUR: nəticə yalnız etibarsız mətn (qaralama/hesabat) olur.
- `AgentDispatcher`: tapşırığı atomik tutur (təkrar icra yoxdur), Orkestratora verir, nəticəni yoxlayır (ölçü, sirr, təlimat bənzərliyi), agentin
  hesabatını yazır; həssas icazə (publish və s.) Gate-də sahib təsdiqini gözləyir (`awaiting_approval`), təsdiqdən sonra EYNİ op_id ilə davam edir.
- Paralel generasiya (`prefetch`): müstəqil tapşırıqların AI mətni eyni anda alınır (yan təsirsiz); icra/qeyd HƏMİŞƏ ardıcıl və Broker üzərindədir.
  Eyni `request_id` ilə router nəticəni saxladığı üçün sonradan yenidən generasiya olmur.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from agents.agent_factory import AgentFactory, AgentRecord, AgentReport, ExchangeRejected, FactoryDenied, TaskRecord, validate_exchange
from agents.hybrid import HybridRouter, RouteResult
from agents.memory import Actor, MemoryDenied, MemoryService
from agents.orchestrator import ExecutionError, Orchestrator, Task, TransientError
from agents.spec import SENSITIVE_PERMISSIONS

_TRANSIENT_STATUSES = frozenset({"all_unavailable", "cloud_unavailable", "local_unavailable", "in_progress"})


class AgentLLMRunner:
    """Orkestratorun `runner(spec, input)` müqaviləsi. Yalnız broker-in icra mərhələsində çağırılır."""

    def __init__(self, factory: AgentFactory, router: HybridRouter, memory: Callable[[], Sequence[Any]] | None = None,
                 allow_simulated: bool = True) -> None:
        self.factory, self.router, self.memory, self.allow_simulated = factory, router, memory, allow_simulated

    def generate(self, agent: AgentRecord, task: TaskRecord) -> RouteResult:
        context: list[Any] = []
        if self.memory is not None:
            try:  # yaddaş yalnız OXUNUR; agentin oxuya biləcəyi kateqoriyalarla məhdudlaşır (Cloud-a ötürmə router-in məxfilik siyasətindədir)
                context = [r for r in self.memory() if getattr(r, "category", "") in agent.memory_policy.read_categories][:3]
            except Exception:  # noqa: BLE001
                context = []
        return self.router.route(f"agt:{task.task_id}", "chat", task.prompt, agent.system_instruction, context, allow_mock=self.allow_simulated)

    def __call__(self, spec: Any, data: dict) -> dict:
        agent = self.factory.view.find(spec.name)
        task = self.factory.view.task(str(data.get("task_id", "")))
        if agent is None or task is None or task.agent_id != agent.agent_id:
            raise ExecutionError("agent/tapşırıq uyğunsuzluğu", cost=0.0)
        res = self.generate(agent, task)
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
    def __init__(self, factory: AgentFactory, orchestrator: Orchestrator, runner: AgentLLMRunner, *, approvals: Any = None,
                 max_attempts: int = 2, max_output_chars: int = 3500, max_workers: int = 4) -> None:
        self.factory, self.orchestrator, self.runner = factory, orchestrator, runner
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
        t = rt.claim(task_id)
        if t is None:
            cur = self.factory.view.task(task_id)
            return DispatchResult("not_claimable", task_id, reason="tapşırıq tutula bilmədi (artıq icra olunub/olunur və ya agent aktiv deyil)",
                                  attempts=cur.attempts if cur else 0)
        agent = self.factory.view.agent(t.agent_id)
        assert agent is not None
        if t.approval_id and self.approvals is not None:
            ticket = self.approvals.get(t.approval_id)
            if ticket is not None and ticket.status == "pending":   # sahib hələ qərar verməyib: icra yoxdur, gözləmə davam edir
                rt.await_approval(t.task_id, t.approval_id)
                return DispatchResult("awaiting_approval", t.task_id, approval_id=t.approval_id, reason="sahib təsdiqi gözlənilir", attempts=t.attempts)
        orch_task = Task(agent.department, {"sorgu": t.prompt, "task_id": t.task_id}, t.permission, t.cost, t.title, op_id=t.run_op,
                         approval_id=t.approval_id, agent=agent.name)
        with self._exec_lock:
            res = self.orchestrator.execute(orch_task)
        if res.status == "pending_approval":
            rt.await_approval(t.task_id, res.approval_id)
            return DispatchResult("awaiting_approval", t.task_id, approval_id=res.approval_id, reason=res.reason, attempts=t.attempts)
        if res.status == "ok":
            return self._finish(agent, t, res.output, res.cost)
        retry = res.status == "error" and bool(res.transient) and t.attempts < self.max_attempts
        reason = f"{res.status}: {res.reason}"[:200]
        rt.fail(t.task_id, reason, retry=retry)
        return DispatchResult("retry" if retry else "failed", t.task_id, reason=reason, attempts=t.attempts)

    def _finish(self, agent: AgentRecord, t: TaskRecord, output: Any, cost: float | None) -> DispatchResult:
        rt = self.factory.runtime
        text = output.get("netice") if isinstance(output, dict) else None
        try:
            ex = validate_exchange(text, source=agent.name, max_chars=self.max_output_chars)
        except ExchangeRejected as exc:
            reason = f"failed_check:{exc.code}"
            rt.fail(t.task_id, reason, retry=False)
            return DispatchResult("failed", t.task_id, reason=reason, attempts=t.attempts)
        try:
            report = self.factory.handle(agent.agent_id).submit_result(t.task_id, ex.text, cost=min(float(cost or 0.0), t.cost))
        except FactoryDenied as exc:
            rt.fail(t.task_id, f"report_rejected:{exc.code}", retry=False)
            return DispatchResult("failed", t.task_id, reason=f"report_rejected:{exc.code}", attempts=t.attempts)
        rt.note_success(agent.agent_id)
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
