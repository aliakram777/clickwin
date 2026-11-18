"""A-8: ödənişli (real) LLM çağırışının BÜTÜN istehsal giriş nöqtələri Effect Broker (Gate/Ledger) arxasındadır.

DÜRÜST QEYD: Broker konteksti eyni prosesdəki `ContextVar`-dır (`safety/execution_context.py`). Bu, səhv/ötürülmüş çağırışlara qarşı BÜTÖVLÜK yoxlamasıdır; prosesdə ixtiyari kod icra edə bilən
tərəfin onu özü qoymasının qarşısını ALMIR və OS təhlükəsizlik sərhədi DEYİL. Agent kodu bu prosesdə işləmir (AgentGateway: ayrı proses)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agents.hybrid import RoutedLLM
from api.bootstrap import build_app
from runtime.brokered_llm import BrokeredRouter
from safety.execution_context import broker_context, in_broker
from tests.engine_support import trust_test_types  # noqa: F401  (fixture)
from tests.test_agent_runtime import task, world  # noqa: F401  (fixture)
from tests.test_hybrid_router import Fake

ROOT = Path(__file__).resolve().parent.parent
CALL_NAMES = {"route", "complete", "transcribe", "synthesize"}
PROD_DIRS = ("agents", "api", "runtime", "safety", "tools")

# (fayl, sinif.funksiya, çağırış) -> mühafizə növü. Siyahıda olmayan YENİ giriş nöqtəsi testi uğursuz edir: yenidən baxılmalıdır.
ENTRY_POINTS = {
    ("runtime/brokered_llm.py", "BrokeredRouter.route", "route"): "broker: EffectBroker.run daxilində",
    ("agents/agent_runtime.py", "AgentLLMRunner.generate", "route"): "broker: yalnız Orchestrator->Broker icra mərhələsində çağırılır; prefetch ödənişliyi istəmir",
    ("api/chat.py", "HybridReplyEngine.reply", "route"): "broker: istehsalda router BrokeredRouter-dir (aşağıdakı test)",
    ("runtime/brokered_models.py", "BrokeredModelRunner._call", "complete"): "broker: yalnız BrokeredModelRunner._execute-dan (EffectBroker.run daxilində, in_broker yoxlanır); GuardedLLM + vahid LedgerPort",
    ("runtime/brokered_models.py", "_TrustedProviderView.complete", "complete"): "guard: yalnız runner silahlandırdıqda; hər çağırışda reyestr reverify; GuardedLLM-in arxasında",
    ("agents/hybrid.py", "RoutedLLM.complete", "route"): "broker: istehsalda router BrokeredRouter-dir (planlaşdırıcı)",
    ("agents/hybrid.py", "CloudEngine.generate", "complete"): "guard: GuardedLLM mock olmayan provayderi Broker konteksti olmadan bloklayır",
    ("agents/llm.py", "GuardedLLM.complete.target", "complete"): "guard: bu, Broker konteksti yoxlamasından SONRA provayder çağırışıdır",
    ("api/chat.py", "LLMReplyEngine.reply", "complete"): "guard: GuardedLLM (istehsalda istifadə olunmur; ödənişli olsa Broker olmadan bloklanır)",
    ("agents/llm_planner.py", "LLMPlanner.propose", "complete"): "broker: client=RoutedLLM(BrokeredRouter); birbaşa GuardedLLM olsa guard bloklayır",
    ("agents/llm_planner.py", "FactoryAdapter.complete", "complete"): "broker: eyni client",
    ("agents/factory.py", "create_spec", "complete"): "ödənişli deyil: LLM interfeysi yalnız MockLLM ilə; real provayder qoşulsa GuardedLLM guard-ı tətbiq olunur",
    ("agents/providers.py", "OpenAICompatLocalRuntime.generate", "complete"): "ödənişli Cloud deyil: LOKAL model çağırışı (local_resource_use razılığı, router)",
    ("agents/team.py", "TeamPlanner._llm_plan", "route"): "istehsalda əlçatmaz: TeamPlanner istehsal kodunda qurulmur (aşağıdakı test)",
    ("agents/voice.py", "VoiceRegistry.synthesize", "synthesize"): "LLM deyil: səs adapterləri yalnız MOCK/NOT CONFIGURED (real/ödənişli səs qoşulmayıb)",
    ("agents/voice.py", "VoiceRegistry.transcribe", "transcribe"): "LLM deyil: eyni",
    ("api/app.py", "ApiApp.voice_synthesize", "synthesize"): "LLM deyil: MockVoiceSynthesizer",
}


def _calls():
    found = set()
    for d in PROD_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))

            def walk(node, stack):
                for c in ast.iter_child_nodes(node):
                    if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        walk(c, stack + [c.name])
                    else:
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr in CALL_NAMES:
                            found.add((p.relative_to(ROOT).as_posix(), ".".join(stack), c.func.attr))
                        walk(c, stack)
            walk(tree, [])
    return found


def test_every_llm_or_provider_entry_point_is_known_and_classified():
    found = _calls()
    assert found - set(ENTRY_POINTS) == set(), f"yeni/yoxlanmamış LLM giriş nöqtəsi: {sorted(found - set(ENTRY_POINTS))}"
    assert set(ENTRY_POINTS) - found == set(), f"siyahıda olan, amma kodda olmayan giriş nöqtəsi: {sorted(set(ENTRY_POINTS) - found)}"


def test_team_planner_with_a_router_is_never_constructed_in_production_code():
    for d in PROD_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "TeamPlanner":
                    pytest.fail(f"{p}: TeamPlanner istehsalda qurulur — yoxlanmalıdır (router birbaşa çağırır)")


class SpyCloud(Fake):
    """Real (mock olmayan) Cloud mühərriki: hər çağırışda Broker kontekstində olub-olmadığını qeyd edir."""

    def __init__(self):
        super().__init__("cloud", script=["Cloud cavabı"], consent=())
        self.in_broker = []

    def generate(self, prompt, system, request_id):
        self.in_broker.append(in_broker())
        return super().generate(prompt, system, request_id)


def _svc(tmp_path, cloud, register):
    register(type(cloud), Fake)
    return build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, cloud_engine=cloud, local_engine=Fake("local", state="not_configured"))


def test_production_chat_reaches_the_engine_only_inside_the_broker_with_an_audit_trail(tmp_path, trust_test_types):
    cloud = SpyCloud()
    svc = _svc(tmp_path, cloud, trust_test_types)
    out = svc.app.chat.send("x" * 900)
    assert cloud.calls == 1 and cloud.in_broker == [True], out
    audit = [json.loads(x) for x in (tmp_path / "data" / "audit.jsonl").read_text().splitlines()]
    assert any(r.get("description") == "llm:chat" and r.get("allowed") is True for r in audit)       # Gate/audit izi
    (tmp_path / "data" / "STOP").write_text("stop")
    svc.app.chat.send("y" * 901)
    assert cloud.calls == 1                                                                           # stop bayrağı: mühərrikə çatmır


def test_production_chat_and_planner_use_the_brokered_router_not_the_raw_one(tmp_path, trust_test_types):
    svc = _svc(tmp_path, SpyCloud(), trust_test_types)
    planner = svc.app.jobs.planner
    assert isinstance(planner.client, RoutedLLM) and isinstance(planner.client.router, BrokeredRouter)
    chat_router = svc.app.chat.engine.router
    assert isinstance(chat_router, BrokeredRouter) and chat_router is planner.client.router
    assert svc.router is not chat_router                                                               # xam router yalnız etibarlı server koduna məxsusdur


def test_agent_tasks_reach_the_engine_inside_the_broker(world):
    w = world
    seen = []
    real = w.cloud.generate

    def spy(prompt, system, request_id):
        seen.append(in_broker())
        return real(prompt, system, request_id)
    w.cloud.generate = spy
    a, t = task(w)
    assert w.disp.execute(t.task_id).status == "done"
    assert seen == [True] and w.cloud.calls == 1                                                      # Orchestrator -> Broker icra mərhələsində
    assert in_broker() is False


def test_raw_router_cannot_reach_a_real_provider_outside_the_broker(tmp_path):
    from tests.test_remaining_hardening import _PaidFake, _paid_llm
    from safety.ledger import CostLedger
    prov, led = _PaidFake(), CostLedger(tmp_path / "l.db")
    llm = _paid_llm(tmp_path, prov, led)
    from agents.llm import LLMBlocked
    with pytest.raises(LLMBlocked):
        llm.complete("salam")
    assert prov.calls == 0 and led.entries() == []
    with broker_context():
        assert llm.complete("salam").text == "ok"
