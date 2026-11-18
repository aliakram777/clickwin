"""API-nin standart yığılması: bütün sistemlər yerli SQLite/fayl ilə, LLM və səs MOCK. Real LLM, real səs, real agent icraçısı
və xərc limiti burada QOŞULMUR (sahib qərarı və ayrıca mərhələ). Mühit dəyişəni oxunmur, şəbəkə çağırışı yoxdur."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.ceo import CEO, RulePlanner
from agents.company import Company
from agents.durable import DurableRunner, WorkflowStore
from agents.hybrid import AiStore, CloudEngine, HybridRouter, LocalEngine, RoutedLLM
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
from api.chat import ChatService, ConversationStore, HybridReplyEngine, RuleAssistant
from api.events import ApprovalEventChannel
from api.jobs import JobService
from safety.approvals import ApprovalService
from runtime.brokered_llm import BrokeredRouter
from runtime.factory_bootstrap import FactoryStack, TrustAnchor
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
    router: HybridRouter
    data_dir: Path
    factory: FactoryStack | None = None   # yalnız etibarlı server kodu; agentlərə verilmir


def build_app(data_dir: str | Path, *, owner_id: str = "owner", global_limit: float = 0.0,
              https: bool = False, cloud_engine: Any = None, local_engine: Any = None, trust: TrustAnchor | None = None,
              agent_isolation: dict | None = None) -> Services:
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)   # DB/audit/açar faylları yalnız server istifadəçisinə: agent prosesi (başqa uid) onlara çata bilməz
    audit, stop = d / "audit.jsonl", d / "STOP"
    reg = Registry(d / "naymera.db")
    company = Company(reg, d / "company.db", audit, owners={owner_id}, verifier=trust.verifier if trust is not None else None)
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
    # Hibrid AI: Cloud (burada MOCK: real provayder/açar qoşulmayıb) və Local (model yoxdur: NOT CONFIGURED). Rejim: AUTO.
    router = HybridRouter(cloud_engine if cloud_engine is not None else CloudEngine(llm),
                          local_engine if local_engine is not None else LocalEngine(None), AiStore(d / "ai.db"), stop_path=stop)
    llm_router = BrokeredRouter(router, orch.broker)     # A-8: söhbət/plan LLM çağırışları da Effect Broker (Gate/Ledger) üzərindən
    jobs = JobService(LLMPlanner(RoutedLLM(llm_router), company.view.department_costs()), durable, d / "jobs.db")
    rules = RuleAssistant(lambda: {"pending_approvals": len(approvals.reader.pending())})
    chat = ChatService(ConversationStore(d / "chat.db"), HybridReplyEngine(
        llm_router, lambda: memory.reader.search("", category="note", limit=3), rules, allow_simulated=False))
    auth = AuthService(d / "auth.db", audit, owner_id=owner_id)
    app = ApiApp(auth=auth, chat=chat, events=events, stop_path=stop, gateway=approvals.gateway,
                 reader=approvals.reader, memory=memory, company=company.view, proactive=proactive, jobs=jobs,
                 voice=MockVoiceSynthesizer(), owner_id=owner_id, audit_path=audit, llm_is_mock=True, https=https, ai=router)
    stack = None
    if trust is not None:   # etibarlı bootstrap: fabrik qurulur, yalnız dar imkanlar paylanır (agent icraçısı/real provayder yenə də ayrıca qoşulur)
        stack = trust.build_factory(
            company=company, auth=auth, owners={owner_id}, db_path=d / "factory.db", audit_path=audit, workspace_root=d / "workspaces",
            team_db=d / "team.db", router=router, approvals_reader=approvals.reader, memory=lambda: memory.reader.search("", category="note", limit=3), agent_isolation=agent_isolation,
            global_limit=lambda: policy.global_limit,
            orchestrator_factory=lambda runner: Orchestrator(reg, runner, audit_path=audit, stop_path=stop, errors_path=d / "errors.jsonl",
                                                             ledger=ledger, approvals=approvals.client, policy=policy))
    return Services(app, auth, approvals, company, memory, events, router, d, stack)
