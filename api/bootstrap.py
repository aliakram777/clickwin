"""API-nin standart yığılması: bütün sistemlər yerli SQLite/fayl ilə, LLM və səs MOCK. Real LLM, real səs, real agent icraçısı
və xərc limiti burada QOŞULMUR (sahib qərarı və ayrıca mərhələ). Mühit dəyişəni oxunmur, şəbəkə çağırışı yoxdur."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.ceo import CEO, RulePlanner
from agents.company import Company
from agents.durable import DurableRunner, WorkflowStore
from agents.llm import GuardedLLM, LLMConfig, MockProvider
from agents.llm_planner import LLMPlanner
from agents.memory import MemoryService
from agents.mobile import EventStore, MockVoiceSynthesizer
from agents.notify_store import NotificationStore
from agents.orchestrator import Orchestrator
from agents.proactive import NotificationPolicy, ProactiveAnalyzer, ProactiveEngine
from agents.registry import Registry
from api.app import ApiApp
from api.auth import AuthService
from api.chat import ChatService, ConversationStore, RuleAssistant
from api.events import ApprovalEventChannel
from api.jobs import JobService
from safety.approvals import ApprovalService
from safety.ledger import CostLedger


def _no_runner(spec: Any, data: dict) -> dict:
    raise RuntimeError("agent icraçısı qoşulmayıb")


@dataclass
class Services:
    app: ApiApp
    auth: AuthService
    approvals: ApprovalService
    company: Company
    memory: MemoryService
    events: EventStore
    data_dir: Path


def build_app(data_dir: str | Path, *, owner_id: str = "owner", global_limit: float = 0.0,
              https: bool = False) -> Services:
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    audit, stop = d / "audit.jsonl", d / "STOP"
    reg = Registry(d / "naymera.db")
    company = Company(reg, d / "company.db", audit, owners={owner_id})
    events = EventStore(d / "events.db")
    ledger = CostLedger(d / "ledger.db")
    approvals = ApprovalService(d / "approvals.db", audit, owners={owner_id}, channels=[ApprovalEventChannel(events)])
    policy = company.view.budget_policy(global_limit=global_limit)
    orch = Orchestrator(reg, _no_runner, audit_path=audit, stop_path=stop, errors_path=d / "errors.jsonl",
                        ledger=ledger, approvals=approvals.client, policy=policy)
    workflows = WorkflowStore(d / "workflows.db")
    durable = DurableRunner(CEO(orch, RulePlanner([]), budget=global_limit), workflows)
    memory = MemoryService(d / "memory.db", d / "memory_audit.jsonl")
    proactive = ProactiveEngine(
        ProactiveAnalyzer(ledger=ledger, budget=policy, approvals=approvals.reader, workflows=workflows,
                          memory=memory.reader, stop_path=stop),
        NotificationStore(d / "notifications.db"), NotificationPolicy())
    llm = GuardedLLM(MockProvider(default="Mock cavab: real LLM qoşulmayıb."), LLMConfig(provider="mock"),
                     log_path=d / "llm.jsonl", stop_path=stop)
    jobs = JobService(LLMPlanner(llm, company.view.department_costs()), durable, d / "jobs.db")
    chat = ChatService(ConversationStore(d / "chat.db"),
                       RuleAssistant(lambda: {"pending_approvals": len(approvals.reader.pending())}))
    auth = AuthService(d / "auth.db", audit, owner_id=owner_id)
    app = ApiApp(auth=auth, chat=chat, events=events, stop_path=stop, gateway=approvals.gateway,
                 reader=approvals.reader, memory=memory, company=company.view, proactive=proactive, jobs=jobs,
                 voice=MockVoiceSynthesizer(), owner_id=owner_id, audit_path=audit, llm_is_mock=True, https=https)
    return Services(app, auth, approvals, company, memory, events, d)
