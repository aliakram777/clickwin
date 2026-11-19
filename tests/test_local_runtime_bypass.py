"""LocalEngine/PersonalServerRuntime üzərindən ödənişli Cloud yan keçidi və test-bypass parametri.

`private` endpoint YALNIZ həqiqətən lokal hədəfdir (loopback, özəl IP, `localhost`, özəl şəkilçili ad, tək etiketli ad). İctimai DNS/IP (HTTPS ilə də) uzaq/ödənişli servis ola bilər: bunun yolu
`CloudEngine(GuardedLLM(OpenAICompatibleProvider))` + eyni paylaşılan `CostLedger`-dir. İxtiyari nəqliyyatlı `PersonalServerRuntime` `build_app`-da rədd edilir."""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

from agents.hybrid import CloudEngine, LocalEngine, PersonalServerRuntime
from agents.llm import LLMProviderError, LLMRequest
from agents.providers import EndpointError, OpenAICompatLocalRuntime, OpenAICompatibleProvider, TransportError, validate_endpoint
from api import bootstrap
from api.bootstrap import build_app
from runtime.env import build_engines
from runtime.production import build_production
from runtime.transport import UrllibTransport
from safety.ledger import CostLedger, LedgerPort
from agents.providers import HttpRequest
from tests.engine_support import trust_test_types  # noqa: F401  (fixture)
from tests.test_hybrid_router import Fake
from tests.test_providers import FakeTransport, _Handler, env as cloud_env, loopback, ok_body  # noqa: F401  (loopback: fixture)

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ["https://api.openai.com/v1", "https://api.example.com", "https://llm.provider.io", "https://8.8.8.8", "http://api.example.com", "https://u:p@api.example.com"]
LOCAL = ["http://127.0.0.1:11434", "http://localhost:8080", "http://[::1]:8000", "http://192.168.1.20:8000", "http://10.1.2.3:9000", "http://172.16.5.4:8000",
         "http://myserver.local:8000", "https://nas.lan:8443", "http://ollama:11434", "http://box.home.arpa:8080", "http://llm.internal:9000"]


def paid_remote_transport(calls):
    def transport(op, payload):
        calls.append((op, payload))                    # uzaq ödənişli servisə sorğu (kitabda izi YOXDUR)
        return {"text": "paid"}
    return transport


def make_app(tmp_path, **kw):
    return build_app(tmp_path / f"d{abs(hash(str(sorted(kw))))}", **kw)


# ============================================================================ (1) ictimai endpoint lokal sayılmır

@pytest.mark.parametrize("url", PUBLIC)
def test_public_dns_or_ip_endpoints_are_not_accepted_as_local(url):
    with pytest.raises(EndpointError):
        validate_endpoint(url, "private")
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(url, "m", None, FakeTransport([ok_body()]), kind="private")


@pytest.mark.parametrize("url", LOCAL)
def test_real_local_and_private_endpoints_remain_valid(url):
    validate_endpoint(url, "private")
    OpenAICompatibleProvider(url, "m", None, FakeTransport([ok_body()]), kind="private")


def test_env_configured_public_https_local_endpoint_is_not_configured_with_a_clear_note(tmp_path):
    for url in ("https://api.openai.com/v1", "https://api.example.com"):
        eng = build_engines({"NAYMERA_LOCAL_AI_URL": url, "NAYMERA_LOCAL_AI_MODEL": "m"}, log_dir=tmp_path, stop_path=tmp_path / "STOP", transport=FakeTransport([ok_body()]))
        assert eng.local.status().state == "not_configured" and eng.local.runtime is None
        assert any("local: qurulmayıb" in n and "Cloud provayder" in n for n in eng.notes)


def test_local_runtime_cannot_be_built_from_a_public_provider_even_by_mutation(tmp_path):
    t = FakeTransport([ok_body()])
    prov = OpenAICompatibleProvider("http://127.0.0.1:11434", "m", None, t, kind="private")
    prov.base_url = "https://api.openai.com/v1"                                          # atribut dəyişdirilib
    with pytest.raises(EndpointError):
        OpenAICompatLocalRuntime(prov)
    with pytest.raises(LLMProviderError) as e:                                           # sorğu anında da yoxlanılır
        prov.complete(LLMRequest("", "salam", 8, 2.0))
    assert e.value.charged is False and t.calls == []                                    # nəqliyyat HEÇ VAXT çağırılmayıb


def test_the_enforcing_transport_refuses_public_hosts_for_private_and_local_hosts_for_cloud_without_connecting(tmp_path):
    for kind, url in (("private", "https://api.openai.com/v1/chat/completions"), ("private", "https://8.8.8.8/x"), ("cloud", "http://127.0.0.1:11434/v1/chat/completions"),
                      ("cloud", "https://192.168.1.5/v1/chat/completions")):
        t = UrllibTransport(kind=kind)
        t._connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("qoşulma cəhdi olmamalıdır"))      # şəbəkəyə çıxış YOXDUR
        with pytest.raises(TransportError):
            t(HttpRequest("POST", url, {}, b"{}", 1.0))
    with pytest.raises(ValueError):
        UrllibTransport(kind="other")


# ============================================================================ build_app: lokal mühərrik/nəqliyyat fail-closed

def test_build_app_rejects_a_local_runtime_with_a_mutated_public_endpoint(tmp_path):
    eng = build_engines({"NAYMERA_LOCAL_AI_URL": "http://127.0.0.1:11434", "NAYMERA_LOCAL_AI_MODEL": "m"}, log_dir=tmp_path, stop_path=tmp_path / "STOP")
    assert isinstance(eng.local.runtime, OpenAICompatLocalRuntime) and isinstance(eng.local.runtime.provider._transport, UrllibTransport)
    make_app(tmp_path, local_engine=eng.local)                                           # həqiqi lokal: qəbul olunur
    eng.local.runtime.provider.base_url = "https://api.openai.com/v1"
    with pytest.raises(ValueError, match="həqiqətən lokal deyil"):
        make_app(tmp_path, local_engine=eng.local)


def test_build_app_rejects_a_local_runtime_whose_transport_does_not_enforce_the_private_policy(tmp_path):
    calls = []

    def remote(request):                                                                 # ictimai hosta gedə bilən ixtiyari nəqliyyat
        calls.append(request)
        raise AssertionError("çağırılmamalıdır")
    prov = OpenAICompatibleProvider("http://127.0.0.1:11434", "m", None, remote, kind="private")
    with pytest.raises(ValueError, match="nəqliyyatı etibarlı deyil"):
        make_app(tmp_path, local_engine=LocalEngine(OpenAICompatLocalRuntime(prov)))
    wrong_kind = OpenAICompatibleProvider("http://127.0.0.1:11434", "m", None, UrllibTransport(kind="cloud"), kind="private")
    with pytest.raises(ValueError, match="nəqliyyatı etibarlı deyil"):
        make_app(tmp_path, local_engine=LocalEngine(OpenAICompatLocalRuntime(wrong_kind)))
    unchecked = OpenAICompatibleProvider("http://127.0.0.1:11434", "m", None, UrllibTransport(), kind="private")      # kind=None: siyasət tətbiq etmir
    with pytest.raises(ValueError, match="nəqliyyatı etibarlı deyil"):
        make_app(tmp_path, local_engine=LocalEngine(OpenAICompatLocalRuntime(unchecked)))
    assert calls == []


def test_personal_server_runtime_with_an_arbitrary_transport_cannot_reach_the_router(tmp_path):
    calls = []
    rogue = LocalEngine(PersonalServerRuntime(paid_remote_transport(calls), name="private-server", model="gpt-x", is_mock=False), consent_scope=None)
    assert rogue.status().state == "ready" and rogue.status().is_mock is False          # (özü real görünür)
    with pytest.raises(ValueError, match="nəqliyyatlı PersonalServerRuntime"):
        make_app(tmp_path, local_engine=rogue)
    with pytest.raises(ValueError, match="nəqliyyatlı PersonalServerRuntime"):
        make_app(tmp_path, local_engine=LocalEngine(PersonalServerRuntime(paid_remote_transport(calls), is_mock=True)))        # mock bayrağı da etibarlı deyil
    assert calls == []                                                                   # uzaq/ödənişli servisə heç bir sorğu getməyib
    make_app(tmp_path, local_engine=LocalEngine(PersonalServerRuntime()))                # nəqliyyatsız: qəbul olunur


def test_a_paid_public_endpoint_needs_a_guarded_cloud_engine_and_the_identical_shared_ledger(tmp_path):
    e = cloud_env(NAYMERA_LLM_COST_LIMIT="5.0", NAYMERA_LLM_PRICE_IN_PER_1K="0.01", NAYMERA_LLM_PRICE_OUT_PER_1K="0.02")           # https://api.example.com (ictimai)
    shared = CostLedger(tmp_path / "s.db", global_limit=1.0)
    eng = build_engines(e, log_dir=tmp_path, stop_path=tmp_path / "STOP", transport=FakeTransport([ok_body()]), ledger=LedgerPort(shared))
    assert eng.cloud.status().state == "ready"
    with pytest.raises(ValueError, match="ledger="):
        make_app(tmp_path, global_limit=1.0, cloud_engine=eng.cloud)
    with pytest.raises(ValueError, match="EYNİ deyil"):
        make_app(tmp_path, global_limit=1.0, cloud_engine=eng.cloud, ledger=CostLedger(tmp_path / "o.db", global_limit=1.0))
    assert make_app(tmp_path, global_limit=1.0, cloud_engine=eng.cloud, ledger=shared).ledger is shared


# ============================================================================ həqiqi lokal serverlər (Ollama/llama.cpp tipli) işləyir

def test_a_real_loopback_openai_compatible_server_works_end_to_end_through_production_wiring(tmp_path, loopback):
    env = {"NAYMERA_LOCAL_AI_URL": loopback, "NAYMERA_LOCAL_AI_MODEL": "small"}
    svc, engines, _ = build_production(tmp_path / "p", env, global_limit=0.0)           # nəqliyyat verilmir: UrllibTransport(kind="private")
    assert engines.local.status().state == "ready" and engines.local.status().is_mock is False
    assert isinstance(engines.local.runtime.provider._transport, UrllibTransport) and engines.local.runtime.provider._transport.kind == "private"
    assert engines.local.generate("salam", "", "rid-1").text == "loopback cavabı"      # HƏQİQİ HTTP, ledger/razılıq tələb olunmur
    assert _Handler.seen and _Handler.seen[0][0] == "/v1/chat/completions"
    assert svc.ledger.total() == 0.0                                                    # lokal çağırış ödənişli deyil


@pytest.mark.parametrize("url", ["http://192.168.1.20:8000", "http://10.0.0.7:11434", "http://ollama:11434", "http://llama.local:8080"])
def test_private_network_servers_are_configured_by_env_and_accepted_by_build_app(tmp_path, url):
    eng = build_engines({"NAYMERA_LOCAL_AI_URL": url, "NAYMERA_LOCAL_AI_MODEL": "small"}, log_dir=tmp_path, stop_path=tmp_path / "STOP")
    assert eng.local.status().state == "ready"
    make_app(tmp_path, local_engine=eng.local)


def test_production_wiring_with_a_public_local_url_has_no_local_engine_and_never_builds_a_transport_to_it(tmp_path):
    svc, engines, _ = build_production(tmp_path / "p", {"NAYMERA_LOCAL_AI_URL": "https://api.openai.com/v1", "NAYMERA_LOCAL_AI_MODEL": "gpt"}, global_limit=0.0)
    assert engines.local.runtime is None and engines.local.status().state == "not_configured"


# ============================================================================ test-bypass parametri production API-sindən çıxarılıb

def test_build_app_has_no_bypass_parameter_and_unknown_engines_are_rejected(tmp_path):
    assert "allow_unaudited_engines" not in inspect.signature(build_app).parameters
    assert not any("unaudited" in name or "bypass" in name or "unsafe" in name for name in inspect.signature(build_app).parameters)
    with pytest.raises(TypeError):
        build_app(tmp_path / "x", allow_unaudited_engines=True)
    with pytest.raises(ValueError, match="naməlum mühərrik tipi"):
        make_app(tmp_path, cloud_engine=Fake("cloud", is_mock=True))
    p = inspect.signature(build_production).parameters
    assert not any("engine" in n for n in p)                                            # istehsal yığılması xaricdən mühərrik qəbul etmir


def test_test_registry_is_exact_type_only_and_is_refused_outside_pytest(tmp_path, trust_test_types, monkeypatch):
    class Other(Fake):
        pass
    trust_test_types(Fake)
    make_app(tmp_path, cloud_engine=Fake("cloud", is_mock=True))
    with pytest.raises(ValueError, match="naməlum mühərrik tipi"):                      # alt sinif/başqa tip qəbul olunmur
        make_app(tmp_path, cloud_engine=Other("cloud", is_mock=True))
    monkeypatch.delitem(sys.modules, "pytest")                                          # pytest-siz proses (istehsal) təqlidi
    with pytest.raises(RuntimeError, match="yalnız pytest daxilində"):
        make_app(tmp_path, cloud_engine=Fake("cloud", is_mock=True))


def test_no_production_module_references_the_bypass_or_the_test_registry():
    offenders = []
    for d in ("runtime", "tools", "api", "agents", "safety"):
        for p in (ROOT / d).rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            text = p.read_text(encoding="utf-8")
            if "allow_unaudited_engines" in text and rel != "api/bootstrap.py":
                offenders.append((rel, "allow_unaudited_engines"))
            if "_TEST_ONLY_TYPES" in text and rel != "api/bootstrap.py":
                offenders.append((rel, "_TEST_ONLY_TYPES"))
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and any(k.arg and "unaudited" in k.arg for k in node.keywords):
                    offenders.append((rel, "keyword"))
    assert offenders == [], offenders
    assert "allow_unaudited_engines" not in (ROOT / "api" / "bootstrap.py").read_text(encoding="utf-8").split("def build_app(")[1].split(") -> Services")[0]
    assert bootstrap._TEST_ONLY_TYPES == frozenset()                                    # defolt: boşdur
