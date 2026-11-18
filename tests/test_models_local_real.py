"""Mərhələ 18.6: REAL lokal model — `local_resource_use` razılığı + cihaz resursları (RAM/batareya/istilik) + lokal server qeydiyyatı + mətn generatoru. Şəbəkə YOXDUR (nəqliyyat monkeypatch)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.llm import Secret
from agents.local_models import DeviceState, ResourceLimits
from agents.providers import HttpResponse
from agents.spec import AgentSpec
from kernel.common import ContractError, Evidence
from models import DataClass, ModelCapability, ModelRegistry, RegistryError, ResourceSnapshot, RouteRequest, plan_route
from models.adapters.fixtures import FixtureTransport, openai_ok
from models.adapters.local import FixtureLocalRuntime, LocalRuntimeProvider
from models.adapters.openai_compat import OpenAICompatProvider
from models.local_server import catalog_min_ram_mb, register_local_server
from models.router import CONSENT_LOCAL, CONSENT_PAID
from runtime.brokered_models import BrokeredModelRunner, ModelRunRequest, make_text_generator
from runtime.transport import UrllibTransport
from safety.broker import EffectBroker
from safety.ledger import CostLedger

EV = (Evidence("test", "tests/test_models_local_real.py", datetime(2026, 1, 1, tzinfo=timezone.utc)),)
CAPS = (ModelCapability.TEXT_GENERATION, ModelCapability.CHAT)
GOOD = ResourceSnapshot(8000, 80, False, True)


def real_local_registry(min_ram_mb=0, **kw):
    r = ModelRegistry()
    r.register(OpenAICompatProvider("http://127.0.0.1:8080", "local-m", None, UrllibTransport(kind="private"), kind="private"), model_id="local_real", provider="local",
               capabilities=CAPS, context_tokens=8000, max_output_tokens=1000, max_data_class=DataClass.SENSITIVE, evidence=EV, min_ram_mb=min_ram_mb, **kw)
    return r


def req(**kw):
    base = dict(texts=("salam",), max_output_tokens=100, data_class=DataClass.SENSITIVE)
    base.update(kw)
    return RouteRequest(**base)


def reasons(plan):
    return next(x.reasons for x in plan.rejected if x.model_id == "local_real")


def test_real_local_model_requires_local_resource_use_consent():
    r = real_local_registry()
    d = r.descriptor("local_real")
    assert (d.locality.value, d.reality.value, d.payment.value) == ("local", "real", "free")                 # əvvəl razılıqsız seçilirdi (boşluq)
    p = plan_route(r, req(), resources=GOOD)
    assert p.status == "refused" and f"consent_missing:{CONSENT_LOCAL}" in reasons(p)
    for bad in (frozenset({CONSENT_PAID}), frozenset({"local_model_download"}), "local_resource_use", None, ("x",)):
        assert plan_route(r, req(granted_consents=bad), resources=GOOD).status == "refused", bad            # type: ignore[arg-type]
    ok = plan_route(r, req(granted_consents=frozenset({CONSENT_LOCAL})), resources=GOOD)
    assert ok.status == "selected" and ok.selected.required_consents == (CONSENT_LOCAL,) and ok.selected.reality == "real" and not ok.selected.simulated


def test_simulated_mock_and_remote_rules_are_unchanged():
    r = ModelRegistry()
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "sim"), model_id="sim_local", provider="p", capabilities=CAPS, context_tokens=8000, max_output_tokens=1000, max_data_class=DataClass.SENSITIVE)
    p = plan_route(r, req(allow_simulated=True))
    assert p.status == "selected" and p.selected.required_consents == ()                                      # simulyasiya: real resurs işlətmir ⇒ razılıq/resurs tələb olunmur


@pytest.mark.parametrize("snap,fragment", [(None, "resources_unknown"), (ResourceSnapshot(), "resources_unknown"), (ResourceSnapshot(8000, 80, False, None), "resources_unknown"),
                                           (ResourceSnapshot("8000", 80, False, True), "resources_unknown"), (ResourceSnapshot(float("nan"), 80, False, True), "resources_unknown"),
                                           (ResourceSnapshot(8000, 150, False, True), "resources_unknown"), (ResourceSnapshot(8000, True, False, True), "resources_unknown"),
                                           (ResourceSnapshot(500, 80, False, True), "ram_low:500<1024"), (ResourceSnapshot(8000, 10, False, True), "battery_low:10<20"),
                                           (ResourceSnapshot(8000, 80, False, False), "thermal_throttled"), ("ok", "resources_unknown")])
def test_resource_guard_blocks_unknown_low_ram_low_battery_and_thermal(snap, fragment):
    p = plan_route(real_local_registry(), req(granted_consents=frozenset({CONSENT_LOCAL})), resources=snap)                           # type: ignore[arg-type]
    assert p.status == "refused" and fragment in reasons(p)


def test_charging_waives_the_battery_limit_and_model_ram_requirement_is_enforced():
    c = req(granted_consents=frozenset({CONSENT_LOCAL}))
    assert plan_route(real_local_registry(), c, resources=ResourceSnapshot(8000, 5, True, True)).status == "selected"                 # şarj olunur
    r = real_local_registry(min_ram_mb=6000)
    assert plan_route(r, c, resources=ResourceSnapshot(5000, 80, False, True)).status == "refused" and "ram_low:5000<6000" in reasons(plan_route(r, c, resources=ResourceSnapshot(5000, 80, False, True)))
    assert plan_route(r, c, resources=ResourceSnapshot(6000, 80, False, True)).status == "selected"
    tight = ResourceLimits(min_free_ram_mb=9000)
    assert "ram_low:8000<9000" in reasons(plan_route(real_local_registry(), c, resources=GOOD, limits=tight))


def test_secret_and_class_rules_still_apply_to_real_local_models():
    secret = "sk-" + "Mn7B" * 10
    p = plan_route(real_local_registry(), req(texts=("salam " + secret,), data_class=DataClass.PUBLIC, granted_consents=frozenset({CONSENT_LOCAL})), resources=GOOD)
    assert p.status == "refused" and p.refusal_code == "secret_data_not_allowed"


# ------------------------------------------------------------------ lokal server qeydiyyatı
def test_register_local_server_is_private_real_only_and_uses_the_catalog_ram_estimate():
    r = ModelRegistry()
    d = register_local_server(r, model_id="phi", base_url="http://127.0.0.1:8080", model_name="phi3-mini", transport=UrllibTransport(kind="private"), capabilities=CAPS, context_tokens=4096,
                              max_output_tokens=512, catalog_id="phi3-mini-q4", evidence=EV)
    assert d.reality.value == "real" and d.min_ram_mb == catalog_min_ram_mb("phi3-mini-q4") == 4200 and d.availability.value == "available"
    with pytest.raises(ValueError):                                                                           # ictimai host lokal ola bilməz
        register_local_server(ModelRegistry(), model_id="x", base_url="https://api.example.com", model_name="m", transport=UrllibTransport(kind="private"), capabilities=CAPS, context_tokens=4096,
                              max_output_tokens=512, evidence=EV)
    r2 = ModelRegistry()
    with pytest.raises(RegistryError) as ei:                                                                  # fixture nəqliyyat REAL deyil ⇒ rədd (səssiz SIMULATED yox)
        register_local_server(r2, model_id="fake", base_url="http://127.0.0.1:8080", model_name="m", transport=FixtureTransport(), capabilities=CAPS, context_tokens=4096, max_output_tokens=512, evidence=EV)
    assert ei.value.code == "not_real" and r2.descriptor("fake").availability.value == "disabled"
    assert register_local_server(ModelRegistry(), model_id="fake2", base_url="http://127.0.0.1:8080", model_name="m", transport=FixtureTransport(), capabilities=CAPS, context_tokens=4096,
                                 max_output_tokens=512, evidence=EV, require_real=False).reality.value == "simulated"
    with pytest.raises(ContractError):
        catalog_min_ram_mb("ghost-model")


# ------------------------------------------------------------------ icra: Broker + razılıq + resurs + mətn generatoru
class Consents:
    def __init__(self, *g):
        self.g = set(g)

    def granted(self, scope):
        return scope in self.g


def state(ram=8000, bat=80, chg=False, thermal=True):
    return DeviceState(16000, ram, 50000, bat, chg, thermal, False)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(UrllibTransport, "__call__", lambda self, r: (calls.append(r), HttpResponse(200, openai_ok("lokal cavab ğüş", model="local-m").body))[1])    # YALNIZ test: şəbəkə yoxdur
    ledger = CostLedger(tmp_path / "l.db", global_limit=5.0)
    broker = EffectBroker(tmp_path / "a.jsonl", tmp_path / "STOP", ledger=ledger)
    spec = AgentSpec(name="agent_loc", purpose="p", tools=[], permissions=["read_files"], inputs=["x"], outputs=["y"], cost_limit=0.0)
    box = {"consents": Consents(CONSENT_LOCAL), "state": state()}
    runner = BrokeredModelRunner(real_local_registry(), broker, agent=spec, consents=type("C", (), {"granted": lambda s, sc: box["consents"].granted(sc)})(), log_dir=tmp_path,
                                 device_state=lambda: box["state"])
    return runner, calls, box, ledger


def run(runner, op, **kw):
    base = dict(op_id=op, prompt="salam", data_class=DataClass.SENSITIVE)
    base.update(kw)
    return runner.run(ModelRunRequest(**base))


def test_runner_calls_a_real_local_model_only_with_consent_and_good_resources(world):
    runner, calls, box, ledger = world
    ok = run(runner, "op-ok-1")
    assert ok.status == "ok" and ok.text == "lokal cavab ğüş" and (ok.locality, ok.simulated, ok.payment) == ("local", False, "free") and len(calls) == 1 and ledger.total() == 0.0
    box["consents"] = Consents()                                                                              # razılıq geri çəkildi
    assert run(runner, "op-nc-2").status == "refused" and len(calls) == 1
    box["consents"] = Consents(CONSENT_LOCAL)
    for bad in (state(ram=300), state(bat=5), state(thermal=False)):
        box["state"] = bad                                                                                    # resurs hər cəhddə təzə oxunur
        assert run(runner, f"op-res-{len(str(bad))}-{bad.free_ram_mb}-{bad.battery_pct}-{bad.thermal_ok}").status == "refused" and len(calls) == 1
    box["state"] = state(bat=5, chg=True)
    assert run(runner, "op-chg-3").status == "ok" and len(calls) == 2


def test_runner_without_a_device_state_source_never_runs_a_real_local_model(tmp_path, world):
    runner, calls, box, ledger = world
    blind = BrokeredModelRunner(runner._registry, runner._broker, agent=runner._agent, consents=runner._consents, log_dir=tmp_path / "b")
    assert run(blind, "op-blind-1").status == "refused" and calls == []
    broken = BrokeredModelRunner(runner._registry, runner._broker, agent=runner._agent, consents=runner._consents, log_dir=tmp_path / "c", device_state=lambda: 1 / 0)
    assert run(broken, "op-broken-1").status == "refused" and calls == []


def test_text_generator_returns_only_ok_text_and_raises_otherwise(world):
    runner, calls, box, ledger = world
    gen = make_text_generator(runner, prefix="plan", data_class=DataClass.SENSITIVE)
    assert gen("sistem", "salam") == "lokal cavab ğüş" and gen("sistem", "salam 2") == "lokal cavab ğüş" and len(calls) == 2                   # hər çağırış unikal op_id
    box["consents"] = Consents()
    with pytest.raises(RuntimeError):
        gen("sistem", "salam 3")
    with pytest.raises(ValueError):
        make_text_generator(runner, prefix="bad prefix!")
    default = make_text_generator(runner, prefix="d")                                                                     # defolt: təsnif edilməmiş ⇒ lokal, uzaq yox
    box["consents"] = Consents(CONSENT_LOCAL)
    assert default("s", "p") == "lokal cavab ğüş"
