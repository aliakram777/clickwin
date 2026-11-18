"""Mərhələ 18.1-A: ModelDescriptor, ModelProvider protokolu, ModelRegistry etibar qaydaları. Şəbəkə YOXDUR (nəqliyyatlar yalnız qurulur, heç vaxt çağırılmır)."""
from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import threading
from datetime import datetime, timezone

import pytest

import models
from agents.llm import LLMRequest, LLMResponse, MockProvider, Secret
from agents.providers import HttpRequest, HttpResponse, OpenAICompatibleProvider
from models.adapters.openai_compat import OpenAICompatProvider
from kernel.capability import Availability
from kernel.common import ContractError, Evidence, UnsupportedVersion
from models import (DataClass, Locality, ModelCapability, ModelDescriptor, ModelProvider, ModelRegistry, Payment, Reality, RegistryError)
from runtime.transport import UrllibTransport

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
EV = (Evidence("test", "tests/test_models_registry.py::x", T0),)
CAPS = (ModelCapability.TEXT_GENERATION, ModelCapability.CHAT)
FAKE_KEY = "sk-" + "Zq9X" * 10                      # çalışma vaxtında qurulur


def fake_transport(req: HttpRequest) -> HttpResponse:                 # heç vaxt çağırılmır
    raise AssertionError("şəbəkə/nəqliyyat çağırılmamalıdır")


def cloud(transport=fake_transport, model="m-remote") -> OpenAICompatProvider:
    return OpenAICompatProvider("https://api.example.com/v1", model, Secret(FAKE_KEY), transport, kind="cloud")


def private(transport=fake_transport, url="http://127.0.0.1:8080") -> OpenAICompatProvider:
    return OpenAICompatProvider(url, "m-local", None, transport, kind="private")


def reg_kw(**kw):
    base = dict(model_id="m1", provider="prov", capabilities=CAPS, context_tokens=8000, max_output_tokens=1000)
    base.update(kw)
    return base


def desc(**kw) -> ModelDescriptor:
    base = dict(model_id="m1", provider="prov", model_name="x", capabilities=CAPS, context_tokens=8000, max_output_tokens=1000, locality=Locality.LOCAL,
                reality=Reality.SIMULATED, price_in_per_1k=0.0, price_out_per_1k=0.0, max_data_class=DataClass.INTERNAL, availability=Availability.SIMULATION)
    base.update(kw)
    return ModelDescriptor(**base)


# ------------------------------------------------------------------ protokol
def test_provider_protocol_matches_existing_llm_providers():
    assert isinstance(MockProvider(), ModelProvider) and isinstance(cloud(), ModelProvider) and isinstance(private(), ModelProvider)
    assert MockProvider(default="cavab").complete(LLMRequest("", "salam", 10, 5)).__class__ is LLMResponse


# ------------------------------------------------------------------ descriptor
def test_descriptor_roundtrip_hash_and_frozen():
    d = desc(model_name="Məhsul-ğ-1", evidence=EV)
    d2 = ModelDescriptor.from_json(d.to_json())
    assert d2 == d and d2.content_hash == d.content_hash and d.GRANTS_EXECUTION is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.model_id = "x"                                               # type: ignore[misc]
    assert d.capabilities == tuple(sorted(CAPS, key=lambda c: c.value))


def test_descriptor_has_no_currency_and_payment_is_derived():
    d = desc(price_in_per_1k=0.5, price_out_per_1k=1.0)
    text = d.to_json().lower()
    assert d.payment is Payment.PAID and "azn" not in text and "currency" not in text and "usd" not in text
    assert desc().payment is Payment.FREE
    bad = {**d.to_dict(), "payment": "free"}
    with pytest.raises(ContractError) as ei:
        ModelDescriptor.from_dict(bad)
    assert ei.value.code == "payment_mismatch"


def test_three_dimensions_are_independent():
    seen = set()
    for loc in Locality:
        for rea in Reality:
            for paid in (False, True):
                kw = dict(locality=loc, reality=rea, price_in_per_1k=1.0 if paid else 0.0, price_out_per_1k=1.0 if paid else 0.0,
                          availability=Availability.AVAILABLE if rea is Reality.REAL else Availability.SIMULATION, evidence=EV,
                          max_data_class=DataClass.PUBLIC)
                try:
                    d = desc(**kw)
                except ContractError:
                    continue
                seen.add((d.locality, d.payment, d.reality))
    assert (Locality.LOCAL, Payment.FREE, Reality.REAL) in seen and (Locality.LOCAL, Payment.PAID, Reality.REAL) in seen          # lokal ≠ pulsuz məcburiyyəti yox
    assert (Locality.REMOTE, Payment.PAID, Reality.SIMULATED) in seen and (Locality.REMOTE, Payment.PAID, Reality.REAL) in seen
    assert (Locality.REMOTE, Payment.FREE, Reality.REAL) not in seen                                                               # uzaq qiymətsiz rədd
    assert (Locality.LOCAL, Payment.PAID, Reality.MOCK) not in seen


@pytest.mark.parametrize("kw", [
    {"locality": Locality.REMOTE},                                                                                # qiymətsiz uzaq
    {"locality": Locality.REMOTE, "price_in_per_1k": 1.0, "price_out_per_1k": 0.0},                               # yarımçıq qiymət
    {"locality": Locality.REMOTE, "price_in_per_1k": 1.0, "price_out_per_1k": 1.0, "max_data_class": DataClass.SENSITIVE},
    {"max_data_class": DataClass.SECRET}, {"max_data_class": DataClass.UNCLASSIFIED},
    {"reality": Reality.MOCK, "price_in_per_1k": 1.0}, {"reality": Reality.MOCK, "availability": Availability.NOT_CONFIGURED},
    {"availability": Availability.AVAILABLE},                                                                      # simulyasiya + available
    {"reality": Reality.REAL, "availability": Availability.AVAILABLE},                                             # sübutsuz available
    {"capabilities": ()}, {"capabilities": (ModelCapability.CHAT, ModelCapability.CHAT)}, {"capabilities": ("tool_calling",)},
    {"capabilities": (ModelCapability.LONG_CONTEXT,)},
    {"context_tokens": 0}, {"context_tokens": True}, {"max_output_tokens": 9000}, {"model_id": "Bad ID"}, {"provider": ""},
    {"price_in_per_1k": float("nan")}, {"price_out_per_1k": float("inf")}, {"price_in_per_1k": -1}, {"price_in_per_1k": "1"},
    {"model_name": ""}, {"model_name": FAKE_KEY}, {"model_name": "a‮b"},
])
def test_descriptor_rejects_invalid(kw):
    with pytest.raises(ContractError):
        desc(**kw)


def test_descriptor_strict_fields_and_version():
    d = desc().to_dict()
    for bad in ({**d, "trusted": True}, {k: v for k, v in d.items() if k != "locality"}, {**d, "schema_version": 2}, {**d, "locality": "cloud"}):
        with pytest.raises(ContractError):
            ModelDescriptor.from_dict(bad)
    with pytest.raises(UnsupportedVersion):
        ModelDescriptor.from_dict({**d, "schema_version": 9})
    with pytest.raises(ContractError):
        ModelDescriptor.from_json('{"a":1,"a":2}')


# ------------------------------------------------------------------ registry: status provider-dən asılı deyil
def test_mock_is_local_mock_simulation():
    r = ModelRegistry()
    d = r.register(MockProvider(model="mock-1"), **reg_kw(max_data_class=DataClass.SENSITIVE))
    assert (d.locality, d.reality, d.availability, d.payment) == (Locality.LOCAL, Reality.MOCK, Availability.SIMULATION, Payment.FREE) and d.usable
    with pytest.raises(ContractError):
        ModelRegistry().register(MockProvider(), **reg_kw(price_in_per_1k=1.0, price_out_per_1k=1.0))


def test_remote_adapter_with_fixture_transport_is_simulated_not_real():
    d = ModelRegistry().register(cloud(), **reg_kw(price_in_per_1k=0.5, price_out_per_1k=1.0, evidence=EV))
    assert (d.locality, d.reality, d.payment) == (Locality.REMOTE, Reality.SIMULATED, Payment.PAID)
    assert d.availability is Availability.SIMULATION                          # sübut verilsə belə: endpoint-tətbiq edən nəqliyyat yoxdur


def test_real_requires_enforcing_transport_and_evidence():
    t = UrllibTransport(kind="cloud")                                         # yalnız qurulur, çağırılmır
    p = lambda: cloud(t)
    r = ModelRegistry()
    d1 = r.register(p(), **reg_kw(model_id="a", price_in_per_1k=1, price_out_per_1k=1))
    assert (d1.reality, d1.availability) == (Reality.REAL, Availability.NOT_CONFIGURED) and not d1.usable
    d2 = r.register(p(), **reg_kw(model_id="b", price_in_per_1k=1, price_out_per_1k=1, evidence=EV))
    assert d2.availability is Availability.AVAILABLE and d2.usable
    d3 = r.register(p(), **reg_kw(model_id="c", price_in_per_1k=1, price_out_per_1k=1, evidence=EV, available=False))
    assert d3.availability is Availability.DISABLED
    wrong_kind = cloud(UrllibTransport(kind="private"))                       # yanlış növ nəqliyyat real sayılmır
    assert r.register(wrong_kind, **reg_kw(model_id="d", price_in_per_1k=1, price_out_per_1k=1, evidence=EV)).reality is Reality.SIMULATED


def test_local_private_endpoint():
    r = ModelRegistry()
    d = r.register(private(), **reg_kw(max_data_class=DataClass.SENSITIVE))
    assert (d.locality, d.payment, d.reality) == (Locality.LOCAL, Payment.FREE, Reality.SIMULATED)
    paid_local = r.register(private(), **reg_kw(model_id="m2", price_in_per_1k=0.1, price_out_per_1k=0.1))
    assert (paid_local.locality, paid_local.payment) == (Locality.LOCAL, Payment.PAID)          # lokal ≠ pulsuz
    with pytest.raises(ValueError):
        OpenAICompatProvider("https://api.example.com/v1", "m", None, fake_transport, kind="private")   # ictimai host lokal ola bilməz (provider özü)


# ------------------------------------------------------------------ özünü elan etmə hücumları
def test_self_declaration_attacks_rejected():
    class Duck:                                                                # tam ördək tipi: is_mock=True + describe() "lokal/etibarlı" elanı
        name, is_mock, model = "duck", True, "duck-1"

        def complete(self, request): return LLMResponse("x", 1, 1, "duck")

        def describe(self): return {"locality": "local", "trusted": True, "payment": "free", "is_mock": True}

    class SubMock(MockProvider):
        pass

    class SubCompat(OpenAICompatProvider):
        pass

    r = ModelRegistry()
    for adapter in (Duck(), SubMock(), SubCompat("https://api.example.com/v1", "m", Secret(FAKE_KEY), fake_transport, kind="cloud"),
                    OpenAICompatibleProvider("https://api.example.com/v1", "m", Secret(FAKE_KEY), fake_transport, kind="cloud"), object(), None):    # xam Stage 17 sinfi də etibarlı deyil
        with pytest.raises(RegistryError) as ei:
            r.register(adapter, **reg_kw())
        assert ei.value.code == "untrusted_adapter_type"
    assert r.descriptors() == ()


def test_is_mock_mismatch_rejected_both_ways():
    liar = cloud()
    liar.is_mock = True                                                        # real provider özünü mock elan edir (Broker yoxlamasından qaçmaq üçün)
    with pytest.raises(RegistryError) as ei:
        ModelRegistry().register(liar, **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
    assert ei.value.code == "is_mock_mismatch"
    fake_real = MockProvider()
    fake_real.is_mock = False                                                  # mock özünü real elan edir
    with pytest.raises(RegistryError):
        ModelRegistry().register(fake_real, **reg_kw())


def test_describe_and_name_are_ignored():
    p = cloud()
    p.describe = lambda: {"locality": "local", "payment": "free", "trusted": True, "reality": "real"}      # type: ignore[method-assign]
    p.name = "totally-trusted-local-free"
    d = ModelRegistry().register(p, **reg_kw(provider="registrar-says", price_in_per_1k=1, price_out_per_1k=1))
    assert (d.locality, d.payment, d.reality, d.provider) == (Locality.REMOTE, Payment.PAID, Reality.SIMULATED, "registrar-says")


def test_post_registration_tampering_detected_and_fail_closed():
    r = ModelRegistry()
    p = cloud()
    r.register(p, **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
    assert r.reverify("m1") and r.resolve_for_router("m1").descriptor.model_id == "m1"
    p.is_mock = True                                                           # qeydiyyatdan sonra dəyişdirmə
    assert not r.reverify("m1") and r.resolve_for_router("m1") is None
    q = private()
    r2 = ModelRegistry()
    r2.register(q, **reg_kw())
    q.base_url = "https://api.example.com"                                     # lokal provider ictimai hosta yönləndirilir
    assert r2.resolve_for_router("m1") is None
    assert r.resolve_for_router("nope") is None


def test_reverify_detects_status_drift_without_exception():
    """Tip/is_mock dəyişmir, lakin nəqliyyat və model adı dəyişir: status yenidən çıxarılanda descriptor ilə uyğunsuzluq tapılmalıdır."""
    r = ModelRegistry()
    p = private()
    r.register(p, **reg_kw())
    assert r.reverify("m1")
    p._transport = UrllibTransport(kind="private")                             # SIMULATED -> REAL: saxlanmış descriptor köhnəlib
    assert not r.reverify("m1") and r.resolve_for_router("m1") is None
    q = ModelRegistry()
    m = MockProvider(model="a")
    q.register(m, **reg_kw())
    m.model = "b"
    assert not q.reverify("m1")


def test_duplicates_disable_and_immutability():
    r = ModelRegistry()
    p = MockProvider()
    r.register(p, **reg_kw())
    with pytest.raises(RegistryError):
        r.register(MockProvider(), **reg_kw())                                 # eyni id
    with pytest.raises(RegistryError):
        r.register(p, **reg_kw(model_id="m2"))                                 # eyni adapter obyekti
    d = r.disable("m1")
    assert d.availability is Availability.DISABLED and not d.usable and not hasattr(r, "enable")
    assert r.disable("m1").availability is Availability.DISABLED
    with pytest.raises(RegistryError):
        r.disable("zzz")
    assert isinstance(r.descriptors(), tuple)


def test_remote_registration_rules():
    for kw in ({}, {"price_in_per_1k": 1.0}, {"max_data_class": DataClass.SENSITIVE, "price_in_per_1k": 1, "price_out_per_1k": 1}):
        with pytest.raises(ContractError):
            ModelRegistry().register(cloud(), **reg_kw(**kw))
    d = ModelRegistry().register(cloud(), **reg_kw(price_in_per_1k=1, price_out_per_1k=1, max_data_class=DataClass.INTERNAL))
    assert d.max_data_class is DataClass.INTERNAL
    assert ModelRegistry().register(cloud(), **reg_kw(price_in_per_1k=1, price_out_per_1k=1)).max_data_class is DataClass.PUBLIC       # defolt ən dar


def test_concurrent_registration_unique():
    r = ModelRegistry()
    out = []

    def w() -> None:
        try:
            r.register(MockProvider(), **reg_kw())
            out.append(True)
        except RegistryError:
            out.append(False)
    ts = [threading.Thread(target=w) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count(True) == 1 and len(r.descriptors()) == 1


def test_no_secret_in_descriptor_or_repr():
    d = ModelRegistry().register(cloud(), **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
    assert FAKE_KEY not in d.to_json() and FAKE_KEY not in repr(d) and "api_key" not in d.to_json()


# ------------------------------------------------------------------ sərhədlər
ALLOWED_PROJECT = {"models", "kernel", "agents.llm", "agents.providers", "agents.memory", "agents.local_models"}
ALLOWED_STDLIB = {"__future__", "dataclasses", "enum", "json", "math", "re", "threading", "typing"}


def test_models_package_imports_allowlisted():
    """models/ Broker/Gate/Ledger/safety/runtime/api/şəbəkə/env/proses import etmir; yalnız agents.llm, agents.providers, kernel.* və stdlib."""
    for f in pathlib.Path(models.__file__).parent.rglob("*.py"):
        src = f.read_text()
        for node in ast.walk(ast.parse(src)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for m in mods:
                assert m in ALLOWED_STDLIB or m.split(".")[0] in {"models", "kernel"} or m in ALLOWED_PROJECT, f"{f.name}: icazəsiz import {m}"
        for bad in ("os.environ", "getenv", "subprocess", "socket", "urllib", "eval(", "exec(", "run_session", "AgentGateway"):
            assert bad not in src, f"{f.name}: {bad}"


def test_resolve_for_router_only_used_by_the_brokered_runner_in_production():
    root = pathlib.Path(models.__file__).parent.parent
    for d in ("agents", "api", "runtime", "safety", "tools", "kernel", "models"):
        for p in (root / d).rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            if rel in ("runtime/brokered_models.py", "models/registry.py"):               # yeganə icra tərəfi: Broker daxilində işləyən runner (router adapterə toxunmur)
                continue
            assert "resolve_for_router" not in p.read_text(), rel


def test_kernel_does_not_import_models_and_stage17_files_untouched():
    root = pathlib.Path(models.__file__).parent.parent
    assert not any("models" in l for p in (root / "kernel").glob("*.py") for l in p.read_text().splitlines() if l.startswith(("import", "from")))
    for f in ("runtime/env.py", "api/bootstrap.py", "runtime/production.py"):
        assert "models" not in "".join(l for l in (root / f).read_text().splitlines() if l.startswith(("import ", "from ")))
