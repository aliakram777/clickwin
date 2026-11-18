"""API-nin standart yığılması: bütün sistemlər yerli SQLite/fayl ilə, LLM və səs MOCK. Real LLM, real səs, real agent icraçısı
və xərc limiti burada QOŞULMUR (sahib qərarı və ayrıca mərhələ). Mühit dəyişəni oxunmur, şəbəkə çağırışı yoxdur."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from agents.ceo import CEO, RulePlanner
from agents.company import Company
from agents.durable import DurableRunner, WorkflowStore
from agents.hybrid import AiStore, CloudEngine, HybridRouter, LocalEngine, MockLocalRuntime, PersonalServerRuntime, RoutedLLM
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
from agents.providers import EndpointError, OpenAICompatibleProvider, OpenAICompatLocalRuntime, enforces_endpoint_kind, validate_endpoint
from safety.ledger import CostLedger, LedgerPort, LedgerReconciler, ledger_backend


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
    reconciler: LedgerReconciler | None = None   # naməlum rezervlərin sahib təsdiqi ilə həlli (yalnız etibarlı server kodu)
    ledger: CostLedger | None = None             # TƏK paylaşılan xərc kitabı (Broker, büdcə siyasəti, Cloud LLM, uzlaşdırma): etibarlı server kodu; ApiApp-a verilmir


# YALNIZ TEST: pytest daxilində testlərin ördək tipli saxta mühərrikləri/nəqliyyatları üçün dəqiq tip siyahısı (`monkeypatch.setattr(bootstrap, "_TEST_ONLY_TYPES", frozenset({Fake}))`).
# `build_app` API-sinin parametri DEYİL; istehsal kodu bunu təyin etmir (AST testi) və pytest yüklənməyibsə qeyd edilmiş tip varsa `RuntimeError` verilir (istehsalda aktivləşə bilməz).
_TEST_ONLY_TYPES: frozenset = frozenset()


def _test_only(obj: Any) -> bool:
    if not _TEST_ONLY_TYPES:
        return False
    if "pytest" not in sys.modules:
        raise RuntimeError("_TEST_ONLY_TYPES yalnız pytest daxilində istifadə oluna bilər (istehsalda qadağandır)")
    return type(obj) in _TEST_ONLY_TYPES


def _trusted_local_transport(transport: Any) -> bool:
    """Lokal runtime-ın provayderi YALNIZ özəl hədəf siyasətini hər sorğuda məcburi tətbiq edən qeydli nəqliyyatla (`kind="private"`) qəbul olunur; ixtiyari callable/alt sinif → rədd."""
    return enforces_endpoint_kind(transport, "private") or _test_only(transport)


def _check_local_runtime(label: str, rt: Any) -> None:
    kind = type(rt)
    if rt is None or kind is MockLocalRuntime:
        return
    if kind is PersonalServerRuntime:
        # İxtiyari nəqliyyat (callable) istənilən (ödənişli) uzaq servisə qoşulub kitabı yan keçə bilər: yalnız qurulmamış (transport=None) PersonalServerRuntime qəbul olunur.
        # Həqiqi lokal server (Ollama/llama.cpp) üçün OpenAICompatLocalRuntime (yoxlanmış özəl endpoint) istifadə edin.
        if rt.transport is not None:
            raise ValueError(f"{label}: nəqliyyatlı PersonalServerRuntime rədd edildi (ixtiyari nəqliyyat uzaq/ödənişli servisə qoşula bilər); OpenAICompatLocalRuntime + özəl endpoint istifadə edin")
        return
    if kind is OpenAICompatLocalRuntime:
        provider = rt.provider
        if type(provider) is not OpenAICompatibleProvider or provider.kind != "private":
            raise ValueError(f"{label}: lokal runtime yalnız özəl OpenAICompatibleProvider ilə qəbul olunur")
        try:
            validate_endpoint(provider.base_url, "private")
        except EndpointError as exc:
            raise ValueError(f"{label}: lokal runtime endpoint-i həqiqətən lokal deyil ({exc})") from None
        if not _trusted_local_transport(provider._transport):
            raise ValueError(f"{label}: lokal runtime nəqliyyatı etibarlı deyil (endpoint siyasətini məcburi tətbiq edən qeydli nəqliyyat, kind='private', tələb olunur)")
        return
    raise ValueError(f"{label}: naməlum lokal icra mühiti tipi rədd edildi: {kind.__name__}")


def _check_engines(ledger: Any, engines: Sequence[tuple[str, Any]]) -> CostLedger | None:
    """FAIL-CLOSED mühərrik yoxlaması (ödənişli yolun bölünmüş kitab/yan keçid riskinə qarşı). Yalnız TAM tipi etibarlı olan mühərriklər qəbul olunur (alt siniflər/ördək tipli obyektlər YOX):
    - `None`; `CloudEngine(None)` (qurulmayıb); `CloudEngine(GuardedLLM(MockProvider))` (mock: provayderin özü-bəyan etdiyi `is_mock` bayrağına GÜVƏNİLMİR, konkret tip yoxlanılır);
    - `CloudEngine(GuardedLLM(OpenAICompatibleProvider))` real ödənişli (ictimai/uzaq endpoint daxil): açıq `ledger=` TƏLƏB OLUNUR və mühərrikin kitabı EYNİ arxa uc olmalıdır;
    - `LocalEngine(None | MockLocalRuntime | nəqliyyatsız PersonalServerRuntime | OpenAICompatLocalRuntime(özəl endpoint + endpoint-tətbiq edən qeydli nəqliyyat))`.
    Hər şey başqa `ValueError`. Test fake-ləri yalnız pytest daxilində `_TEST_ONLY_TYPES` ilə (parametr YOXDUR)."""
    raw = None
    if ledger is not None:
        raw = ledger_backend(ledger)
        if raw is None:
            raise ValueError("ledger CostLedger (və ya LedgerPort) olmalıdır")
    for label, eng in engines:
        if eng is None:
            continue
        kind = type(eng)
        if kind is CloudEngine:
            client = eng.client
            if client is None:
                continue
            if type(client) is not GuardedLLM:
                raise ValueError(f"{label}: CloudEngine yalnız GuardedLLM müştərisini qəbul edir (naməlum müştəri tipi: {type(client).__name__})")
            ptype = type(client.provider)
            if ptype is MockProvider:
                continue
            if ptype is not OpenAICompatibleProvider:
                raise ValueError(f"{label}: naməlum provayder tipi rədd edildi: {ptype.__name__} (özü-bəyan etdiyi is_mock bayrağına güvənilmir)")
            if raw is None:
                raise ValueError(f"{label}: real ödənişli mühərrik üçün açıq `ledger=` tələb olunur (build_app ikinci kitab yaratmır); `runtime.production.build_production` istifadə edin")
            backend = ledger_backend(client.ledger)
            if backend is None:
                raise ValueError(f"{label}: real ödənişli mühərrikin xərc kitabı yoxdur: ödənişli çağırış kitabsız qurula bilməz")
            if backend is not raw:
                raise ValueError(f"{label}: real ödənişli mühərrikin kitabı build_app kitabı ilə EYNİ deyil (bölünmüş kitab): bir paylaşılan CostLedger istifadə edin")
        elif kind is LocalEngine:
            _check_local_runtime(label, eng.runtime)
        elif not _test_only(eng):
            raise ValueError(f"{label}: naməlum mühərrik tipi ({kind.__name__}) rədd edildi: yalnız CloudEngine/LocalEngine (etibarlı müştəri/provayder/icra mühiti ilə) qəbul olunur")
    return raw


def build_app(data_dir: str | Path, *, owner_id: str = "owner", global_limit: float = 0.0,
              https: bool = False, cloud_engine: Any = None, local_engine: Any = None, trust: TrustAnchor | None = None,
              agent_isolation: dict | None = None, ledger: CostLedger | None = None) -> Services:
    """`ledger`: istehsalda Cloud mühərriki ilə PAYLAŞILAN tək kitab (`runtime/production.py`). Verilibsə onun ümumi tavanı `global_limit`-ə bərabər və doğrulayıcısı `trust.verifier` olmalıdır:
    əks halda ikinci/uyğunsuz kitab səssizcə qəbul olunmur (`ValueError`). Verilməyibsə `ledger.db` burada yaradılır."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)   # DB/audit/açar faylları yalnız server istifadəçisinə: agent prosesi (başqa uid) onlara çata bilməz
    audit, stop = d / "audit.jsonl", d / "STOP"
    reg = Registry(d / "naymera.db")
    company = Company(reg, d / "company.db", audit, owners={owner_id}, verifier=trust.verifier if trust is not None else None)
    events = EventStore(d / "events.db")
    verifier = trust.verifier if trust is not None else None
    ledger = _check_engines(ledger, (('cloud_engine', cloud_engine), ('local_engine', local_engine)))
    if ledger is None:
        ledger = CostLedger(d / "ledger.db", verifier=verifier, owners={owner_id}, global_limit=global_limit)
    else:
        if ledger.global_limit != float(global_limit):
            raise ValueError("paylaşılan kitabın ümumi tavanı build_app global_limit ilə eyni olmalıdır")
        if getattr(ledger, "_verifier", None) is not verifier:
            raise ValueError("paylaşılan kitabın doğrulayıcısı etibar çıpasının doğrulayıcısı olmalıdır (uzlaşdırma eyni açarlarla yoxlanır)")
    approvals = ApprovalService(d / "approvals.db", audit, owners={owner_id}, channels=[ApprovalEventChannel(events)])
    raw_ledger, ledger = ledger, LedgerPort(ledger)   # icra qatı yalnız DAR `LedgerPort` görür; xam kitab (record/expire_stale/resolve_unknown) yalnız etibarlı server kodundadır
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
    return Services(app, auth, approvals, company, memory, events, router, d, stack, LedgerReconciler(raw_ledger), raw_ledger)
