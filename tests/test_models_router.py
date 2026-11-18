"""Mərhələ 18.1-C: ModelRouter.plan_route — yalnız qərar planı (çağırış/şəbəkə/xərc/rezerv/fallback icrası YOXDUR). Real API/açar YOXDUR."""
from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import pathlib
import random
import socket
from datetime import datetime, timezone

import pytest

import models
from agents.llm import _INPUT_TOKEN_OVERHEAD, Secret
from kernel.common import Evidence
from models import (BudgetSnapshot, DataClass, HealthTracker, ModelCapability, ModelRegistry, RouteRequest, plan_route)
from models.adapters.anthropic import AnthropicProvider
from models.adapters.fixtures import FixtureTransport
from models.adapters.gemini import GeminiProvider
from models.adapters.local import FixtureLocalRuntime, LocalRuntimeProvider
from models.adapters.openai_compat import OpenAICompatProvider
from models import ResourceSnapshot
from models.router import CONSENT_LOCAL, CONSENT_PAID, CONSENT_TRANSFER
from runtime.transport import UrllibTransport

KEY = "sk-" + "Hn4P" * 10                                  # çalışma vaxtında qurulur
EV = (Evidence("test", "tests/test_models_router.py::x", datetime(2026, 1, 1, tzinfo=timezone.utc)),)
C = ModelCapability
BASE = (C.TEXT_GENERATION, C.CHAT)
CONSENTS = frozenset({CONSENT_PAID, CONSENT_TRANSFER})
CONSENTS_LR = CONSENTS | {CONSENT_LOCAL}                  # + REAL lokal model razılığı
GOOD_RES = ResourceSnapshot(8000, 80, False, True)


def spy_transport() -> FixtureTransport:
    return FixtureTransport()


def reg(**kw) -> dict:
    base = dict(provider="prov", capabilities=BASE, context_tokens=32000, max_output_tokens=2000)
    base.update(kw)
    return base


def build(*names: str, order=None) -> tuple[ModelRegistry, dict]:
    """Ad → (id, adapter/transport) xəritəsi ilə hazır reyestr."""
    r, spies = ModelRegistry(), {}
    makers = {
        "local_free": lambda: (LocalRuntimeProvider(rt := FixtureLocalRuntime(), "lf"), rt, dict(max_data_class=DataClass.SENSITIVE)),
        "local_paid": lambda: (OpenAICompatProvider("http://127.0.0.1:9", "lp", None, (t := spy_transport()), kind="private"), t,
                               dict(max_data_class=DataClass.SENSITIVE, price_in_per_1k=0.01, price_out_per_1k=0.02)),
        "remote_a": lambda: (AnthropicProvider("ra", Secret(KEY), (t := spy_transport())), t, dict(max_data_class=DataClass.INTERNAL, price_in_per_1k=0.5, price_out_per_1k=1.0)),
        "remote_b": lambda: (GeminiProvider("rb", Secret(KEY), (t := spy_transport())), t, dict(max_data_class=DataClass.INTERNAL, price_in_per_1k=0.2, price_out_per_1k=0.4)),
        "remote_c": lambda: (OpenAICompatProvider("https://api.example.com/v1", "rc", Secret(KEY), (t := spy_transport()), kind="cloud"), t,
                             dict(max_data_class=DataClass.PUBLIC, price_in_per_1k=0.2, price_out_per_1k=0.4)),
        "real_remote": lambda: (OpenAICompatProvider("https://api.example.com/v1", "rr", Secret(KEY), UrllibTransport(kind="cloud"), kind="cloud"), None,
                                dict(max_data_class=DataClass.INTERNAL, price_in_per_1k=0.3, price_out_per_1k=0.6, evidence=EV)),
        "real_local": lambda: (OpenAICompatProvider("http://127.0.0.1:9", "rl", None, UrllibTransport(kind="private"), kind="private"), None,
                               dict(max_data_class=DataClass.SENSITIVE, evidence=EV)),
    }
    for name in (names if order is None else order):
        adapter, spy, extra = makers[name]()
        r.register(adapter, model_id=name, **reg(**extra))
        spies[name] = (adapter, spy)
    return r, spies


def rq(**kw) -> RouteRequest:
    base = dict(texts=("salam",), max_output_tokens=100, data_class=DataClass.PUBLIC, allow_simulated=True)
    base.update(kw)
    return RouteRequest(**base)


FUNDS = BudgetSnapshot(ledger_headroom=100.0)


def reasons(plan, model_id) -> tuple[str, ...]:
    return next(x.reasons for x in plan.rejected if x.model_id == model_id)


# ------------------------------------------------------------------ determinizm
def test_same_inputs_same_plan_and_id_regardless_of_registration_order():
    names = ["local_free", "local_paid", "remote_a", "remote_b", "remote_c"]
    plans = []
    for order in itertools.islice(itertools.permutations(names), 40):
        r, _ = build(*names, order=list(order))
        plans.append(plan_route(r, rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS))
    assert len({p.plan_id for p in plans}) == 1 and len({json.dumps(p.to_dict(), sort_keys=True) for p in plans}) == 1
    again = [plan_route(build(*names)[0], rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS).plan_id for _ in range(10)]
    assert set(again) == {plans[0].plan_id}


def test_ties_between_equal_providers_break_deterministically_by_id():
    r = ModelRegistry()
    for mid in ("zeta", "alpha", "mid"):
        r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "m"), model_id=mid, **reg(max_data_class=DataClass.SENSITIVE))
    p = plan_route(r, rq())
    assert [p.selected.model_id] + [c.model_id for c in p.fallbacks] == ["alpha", "mid", "zeta"]
    p2 = plan_route(r, rq(), prefer=["mid", "ghost", 5, "zeta"])                 # naməlum/yanlış id-lər ignor; sıra prefer-ə görə
    assert [p2.selected.model_id] + [c.model_id for c in p2.fallbacks] == ["mid", "zeta", "alpha"]


def test_equal_level_random_inputs_are_stable():
    rnd = random.Random(7)
    names = ["local_free", "local_paid", "remote_a", "remote_b", "remote_c"]
    for _ in range(25):
        reqk = dict(data_class=rnd.choice([DataClass.PUBLIC, DataClass.INTERNAL]), allow_remote=rnd.random() < .5, granted_consents=rnd.choice([CONSENTS, frozenset()]))
        b = BudgetSnapshot(rnd.choice([None, 0, 0.0001, 5, 100]), rnd.choice([None, 1]))
        a1 = plan_route(build(*names)[0], rq(**reqk), budget=b)
        a2 = plan_route(build(*reversed(names))[0], rq(**reqk), budget=b)
        assert a1.plan_id == a2.plan_id


# ------------------------------------------------------------------ nəticə strukturu
def test_selected_plan_has_reasons_rejections_and_flags():
    r, _ = build("local_free", "remote_a", "remote_b")
    p = plan_route(r, rq(), budget=FUNDS)                                          # uzaq: allow_remote=False
    assert p.status == "selected" and p.selected.model_id == "local_free" and p.selected.simulated and p.GRANTS_EXECUTION is False and p.EXECUTES_NOTHING
    assert [x.model_id for x in p.rejected] == ["remote_a", "remote_b"] and "remote_not_allowed" in reasons(p, "remote_a")
    assert "local_free" in p.selection_reason and p.refusal_code == ""
    assert p.selected.payment == "free" and p.selected.est_max_cost == 0.0 and p.selected.locality == "local" and p.selected.reality == "simulated"


def test_refusal_when_no_provider_fits_lists_every_reason():
    r, _ = build("remote_a", "remote_b")
    p = plan_route(r, rq(data_class=DataClass.PUBLIC), budget=FUNDS)               # uzaq icazəsi yoxdur
    assert p.status == "refused" and p.selected is None and p.fallbacks == () and p.refusal_code == "no_eligible_provider"
    assert {x.model_id for x in p.rejected} == {"remote_a", "remote_b"} and dict(p.refusal_summary)["remote_not_allowed"] == 2
    empty = plan_route(ModelRegistry(), rq())
    assert (empty.status, empty.refusal_code) == ("refused", "no_models_registered")


# ------------------------------------------------------------------ capability
def test_required_capabilities_filter():
    r = ModelRegistry()
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "a"), model_id="basic", **reg(max_data_class=DataClass.SENSITIVE))
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "b"), model_id="json", **reg(max_data_class=DataClass.SENSITIVE, capabilities=BASE + (C.JSON_OUTPUT, C.CODE)))
    p = plan_route(r, rq(capabilities=(C.JSON_OUTPUT, C.CODE)))
    assert p.selected.model_id == "json" and reasons(p, "basic") == ("missing_capability:code,json_output",)
    none = plan_route(r, rq(capabilities=(C.JSON_OUTPUT, C.TRANSLATION)))
    assert none.status == "refused" and none.refusal_code == "no_eligible_provider"
    for bad in ((), ("tool_calling",), (5,), None):
        q = plan_route(r, rq(capabilities=bad))                                    # type: ignore[arg-type]
        assert (q.status, q.refusal_code) == ("refused", "invalid_request")


# ------------------------------------------------------------------ məxfilik
def test_secret_and_unclassified_never_reach_remote():
    r, _ = build("local_free", "remote_a", "remote_b", "remote_c", "real_remote")
    full = dict(allow_remote=True, granted_consents=CONSENTS)
    for cls in (DataClass.UNCLASSIFIED, "unclassified", None, "garbage", "PUBLIC", 5, ["x"], DataClass.SECRET, "secret"):
        p = plan_route(r, rq(data_class=cls, **full), budget=FUNDS)
        assert all(c.locality == "local" for c in (p.selected, *p.fallbacks) if c), cls
        for m in ("remote_a", "remote_b", "remote_c", "real_remote"):
            assert any(x.startswith("remote_forbidden_for_class") for x in reasons(p, m)), (cls, m)
    unc = plan_route(r, rq(data_class=None, **full), budget=FUNDS)
    assert unc.status == "selected" and unc.selected.model_id == "local_free" and unc.effective_data_class == "unclassified"          # naməlum → yalnız lokal
    assert plan_route(r, rq(data_class="garbage", **full), budget=FUNDS).data_class_note == "unknown_data_class"


def test_omitted_data_class_defaults_to_unclassified_and_stays_local():
    r, _ = build("local_free", "remote_a", "real_remote")
    p = plan_route(r, RouteRequest(texts=("salam",), max_output_tokens=10, allow_remote=True, allow_simulated=True, granted_consents=CONSENTS), budget=FUNDS)
    assert RouteRequest(texts=("x",), max_output_tokens=1).data_class is DataClass.UNCLASSIFIED and p.effective_data_class == "unclassified"
    assert p.selected.model_id == "local_free" and all(c.locality == "local" for c in (p.selected, *p.fallbacks))


def test_unclassified_requires_a_sensitive_capable_local_model():
    """Təsnif edilməmiş məlumat PUBLIC/INTERNAL maksimumlu lokal modelə də verilmir (naməlum = ən həssas sayılır)."""
    r = ModelRegistry()
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "a"), model_id="pub", **reg(max_data_class=DataClass.PUBLIC))
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "b"), model_id="int", **reg(max_data_class=DataClass.INTERNAL))
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "c"), model_id="sens", **reg(max_data_class=DataClass.SENSITIVE))
    p = plan_route(r, rq(data_class=DataClass.UNCLASSIFIED))
    assert p.selected.model_id == "sens" and not p.fallbacks
    assert reasons(p, "pub") == ("data_class_exceeds_model_max:unclassified>public",) and reasons(p, "int") == ("data_class_exceeds_model_max:unclassified>internal",)
    pub = plan_route(r, rq(data_class=DataClass.PUBLIC))
    assert [pub.selected.model_id] + [c.model_id for c in pub.fallbacks] == ["int", "pub", "sens"]               # PUBLIC hamısına uyğundur; bərabərlikdə id sırası


def test_secret_data_is_refused_for_every_model_even_local():
    r, _ = build("local_free", "real_local", "remote_a")
    p = plan_route(r, rq(data_class=DataClass.SECRET, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert p.status == "refused" and p.refusal_code == "secret_data_not_allowed" and p.selected is None


def test_forged_low_class_is_overridden_when_text_contains_secret():
    r, _ = build("local_free", "remote_a")
    secret_text = "Açar: sk-" + "Ab1C" * 10
    p = plan_route(r, rq(texts=("salam", secret_text), data_class=DataClass.PUBLIC, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert p.effective_data_class == "secret" and p.data_class_note == "secret_detected_overrides_declared_class" and p.status == "refused"
    assert KEY not in json.dumps(p.to_dict()) and secret_text not in json.dumps(p.to_dict())             # plan sirri göstərmir


def test_memory_context_raises_class_floor_and_needs_transfer_consent():
    r, _ = build("remote_a", "local_free")
    p = plan_route(r, rq(data_class=DataClass.PUBLIC, includes_memory_context=True, allow_remote=True, granted_consents=frozenset({CONSENT_PAID})), budget=FUNDS)
    assert p.effective_data_class == "internal" and p.data_class_note == "memory_context_floor_internal"
    assert "consent_missing:cloud_data_transfer" in reasons(p, "remote_a") and p.selected.model_id == "local_free"
    ok = plan_route(r, rq(data_class=DataClass.PUBLIC, includes_memory_context=True, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert "remote_a" in [c.model_id for c in (ok.selected, *ok.fallbacks)]


def test_data_class_above_model_max_rejected():
    r, _ = build("remote_c", "remote_a", "local_free")                       # remote_c max=PUBLIC, remote_a max=INTERNAL
    p = plan_route(r, rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert any(x.startswith("data_class_exceeds_model_max") for x in reasons(p, "remote_c")) and "remote_a" in [c.model_id for c in (p.selected, *p.fallbacks)]
    sens = plan_route(r, rq(data_class=DataClass.SENSITIVE, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert sens.selected.model_id == "local_free" and not sens.fallbacks


def test_remote_requires_explicit_allow_and_consents_with_truthy_values_only():
    r, _ = build("remote_a")
    for allow in (False, None, 1, "true"):                                   # yalnız `True` obyekti
        p = plan_route(r, rq(allow_remote=allow, granted_consents=CONSENTS), budget=FUNDS)                  # type: ignore[arg-type]
        assert p.status == "refused" and "remote_not_allowed" in reasons(p, "remote_a")
    for cons in (frozenset(), frozenset({"cloud_data_transfer"}), "cloud_paid_use", None, 5):
        p = plan_route(r, rq(allow_remote=True, granted_consents=cons), budget=FUNDS)                       # type: ignore[arg-type]
        assert p.status == "refused" and "consent_missing:cloud_paid_use" in reasons(p, "remote_a")
    assert plan_route(r, rq(allow_remote=True, granted_consents=frozenset({CONSENT_PAID})), budget=FUNDS).status == "selected"


# ------------------------------------------------------------------ büdcə
def est(price_in, price_out, texts=("salam",), out=100) -> float:
    n = sum(len(t.encode("utf-8")) for t in texts) + _INPUT_TOKEN_OVERHEAD
    return n * price_in / 1000 + out * price_out / 1000


def test_paid_provider_never_selected_when_budget_unknown_or_zero_or_invalid():
    r, _ = build("local_paid", "remote_a", "local_free")
    full = dict(allow_remote=True, granted_consents=CONSENTS, data_class=DataClass.INTERNAL)
    cases = [None, BudgetSnapshot(), BudgetSnapshot(None, 5), BudgetSnapshot(0), BudgetSnapshot(0.0, 5), BudgetSnapshot(5, 0), BudgetSnapshot(float("nan")),
             BudgetSnapshot(float("inf")), BudgetSnapshot(-1), BudgetSnapshot(True), BudgetSnapshot("5"), BudgetSnapshot([5]), BudgetSnapshot(5, "x")]
    for b in cases:
        p = plan_route(r, rq(**full), budget=b)                              # type: ignore[arg-type]
        chosen = [c for c in (p.selected, *p.fallbacks) if c]
        assert [c.model_id for c in chosen] == ["local_free"], b             # yalnız pulsuz lokal
        for m in ("local_paid", "remote_a"):
            assert any(x in ("budget_unknown", "budget_zero") for x in reasons(p, m)), (b, m)


def test_budget_limit_uses_min_of_headroom_and_request_cap_and_same_formula_as_guardedllm():
    r, _ = build("remote_b")
    cost = est(0.2, 0.4)
    full = dict(allow_remote=True, granted_consents=CONSENTS, data_class=DataClass.INTERNAL)
    p = plan_route(r, rq(**full), budget=BudgetSnapshot(1.0))
    assert p.selected.est_max_cost == pytest.approx(cost) and p.selected.output_cap == 100 and p.selected.est_input_tokens == len("salam".encode()) + _INPUT_TOKEN_OVERHEAD
    assert plan_route(r, rq(**full), budget=BudgetSnapshot(cost * 1.001)).status == "selected"
    assert "budget_exceeded" in reasons(plan_route(r, rq(**full), budget=BudgetSnapshot(cost * 0.999)), "remote_b")
    assert "budget_exceeded" in reasons(plan_route(r, rq(**full), budget=BudgetSnapshot(1.0, cost * 0.5)), "remote_b")        # request_cap daha dardır
    assert plan_route(r, rq(**full), budget=BudgetSnapshot(cost * 2, 1.0)).status == "selected"                                # headroom daha dardır, kifayətdir
    assert "budget_exceeded" in reasons(plan_route(r, rq(**full), budget=BudgetSnapshot(cost * 0.5, 1.0)), "remote_b")


def test_unicode_bytes_drive_the_cost_estimate_not_character_count():
    r, _ = build("remote_b")
    az = ("ğüşəöçı🙂" * 30,)
    full = dict(allow_remote=True, granted_consents=CONSENTS, data_class=DataClass.INTERNAL)
    p = plan_route(r, rq(texts=az, **full), budget=BudgetSnapshot(10))
    assert p.selected.est_max_cost == pytest.approx(est(0.2, 0.4, az)) and p.selected.est_input_tokens == len(az[0].encode()) + _INPUT_TOKEN_OVERHEAD > len(az[0])


def test_free_local_ignores_budget_and_plan_has_no_currency():
    r, _ = build("local_free")
    p = plan_route(r, rq())
    assert p.status == "selected" and p.selected.est_max_cost == 0.0
    text = json.dumps(p.to_dict()).lower()
    assert "azn" not in text and "usd" not in text and "currency" not in text


# ------------------------------------------------------------------ real / simulated
def test_simulated_requires_opt_in_and_is_always_flagged():
    r, _ = build("local_free", "remote_b", "real_remote")
    full = dict(allow_remote=True, granted_consents=CONSENTS, data_class=DataClass.INTERNAL)
    p = plan_route(r, rq(allow_simulated=False, **full), budget=FUNDS)
    assert p.selected.model_id == "real_remote" and not p.selected.simulated and p.selected.reality == "real"
    assert reasons(p, "local_free") == ("simulated_not_allowed",) and reasons(p, "remote_b") == ("simulated_not_allowed",)
    only_sim, _ = build("local_free", "remote_b")
    refused = plan_route(only_sim, rq(allow_simulated=False, **full), budget=FUNDS)
    assert refused.status == "refused"
    allowed = plan_route(only_sim, rq(allow_simulated=True, **full), budget=FUNDS)
    assert allowed.selected.simulated is True and allowed.selected.reality == "simulated"


def test_real_preferred_over_simulated_then_local_over_remote_then_cheaper():
    r, _ = build("local_free", "real_remote", "real_local", "remote_b")
    full = dict(allow_remote=True, granted_consents=CONSENTS_LR, data_class=DataClass.INTERNAL)
    p = plan_route(r, rq(**full), budget=FUNDS, resources=GOOD_RES)
    assert [p.selected.model_id] + [c.model_id for c in p.fallbacks] == ["real_local", "real_remote", "local_free", "remote_b"]
    r2, _ = build("remote_a", "remote_b")                                    # eyni səviyyə: təxmini xərc kiçik olan
    p2 = plan_route(r2, rq(**full), budget=FUNDS)
    assert p2.selected.model_id == "remote_b" and p2.fallbacks[0].model_id == "remote_a"


def test_local_is_preferred_over_remote_even_when_the_local_model_costs_more():
    """Məxfilik-əvvəl sıralama: eyni reality-də lokal model, qiyməti uzaqdan BAHA olsa belə, üstün tutulur (yalnız qapıları keçmiş namizədlər arasında)."""
    r2 = ModelRegistry()
    r2.register(OpenAICompatProvider("http://127.0.0.1:9", "lp", None, spy_transport(), kind="private"), model_id="local_expensive",
                **reg(max_data_class=DataClass.SENSITIVE, price_in_per_1k=50.0, price_out_per_1k=100.0))
    r2.register(GeminiProvider("rb", Secret(KEY), spy_transport()), model_id="remote_cheap", **reg(max_data_class=DataClass.INTERNAL, price_in_per_1k=0.001, price_out_per_1k=0.001))
    p = plan_route(r2, rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=BudgetSnapshot(10_000.0))
    cands = {c.model_id: c for c in (p.selected, *p.fallbacks)}
    assert cands["local_expensive"].est_max_cost > cands["remote_cheap"].est_max_cost                      # lokal BAHADIR
    assert [p.selected.model_id, p.fallbacks[0].model_id] == ["local_expensive", "remote_cheap"]           # lakin birinci seçilir


def test_not_configured_and_disabled_models_are_never_selected():
    r, _ = build("local_free", "real_local")
    r2 = ModelRegistry()
    r2.register(OpenAICompatProvider("http://127.0.0.1:9", "x", None, UrllibTransport(kind="private"), kind="private"), model_id="noevidence",
                **reg(max_data_class=DataClass.SENSITIVE))                    # REAL, lakin sübutsuz → not_configured
    r2.register(LocalRuntimeProvider(FixtureLocalRuntime(), "d"), model_id="off", **reg(max_data_class=DataClass.SENSITIVE, available=False))
    p = plan_route(r2, rq(allow_simulated=True))
    assert p.status == "refused" and reasons(p, "noevidence")[0] == "unavailable:not_configured" and reasons(p, "off") == ("unavailable:disabled",)
    r.disable("real_local")
    assert "unavailable:disabled" in reasons(plan_route(r, rq()), "real_local")


# ------------------------------------------------------------------ sağlamlıq
def test_unhealthy_providers_are_excluded_and_sanitised():
    r, _ = build("local_free", "real_local")
    p = plan_route(r, rq(), unhealthy={"real_local": "circuit_open", "ghost": "x"})
    assert p.selected.model_id == "local_free" and "unhealthy:circuit_open" in reasons(p, "real_local")
    nasty = plan_route(r, rq(), unhealthy={"real_local": "x\n\x00<script>" + "y" * 200})
    assert all(ch.isalnum() or ch in "_-:. " for ch in reasons(nasty, "real_local")[0].replace("unhealthy:", "")) and len(reasons(nasty, "real_local")[0]) < 60
    for junk in (None, "x", 5, []):
        assert plan_route(r, rq(granted_consents=CONSENTS_LR), unhealthy=junk, resources=GOOD_RES).selected.model_id == "real_local"                 # type: ignore[arg-type]
    allbad = plan_route(r, rq(), unhealthy={"local_free": "down", "real_local": "down"})
    assert allbad.status == "refused" and allbad.refusal_code == "no_eligible_provider" and dict(allbad.refusal_summary)["unhealthy"] == 2


def test_health_tracker_threshold_cooldown_and_recovery():
    now = [100.0]
    h = HealthTracker(lambda: now[0], threshold=3, cooldown_s=30)
    for _ in range(2):
        h.record("m", False)
    assert h.snapshot() == {}
    h.record("m", False)
    h.record("z", False)
    assert h.snapshot() == {"m": "circuit_open"}
    now[0] = 129.9
    assert "m" in h.snapshot()
    now[0] = 130.1
    assert h.snapshot() == {}                                                 # cooldown bitdi
    h.record("m", True)
    h.record("m", False)
    assert h.snapshot() == {}                                                 # uğur sayğacı sıfırlayır
    for bad in ((None, 3, 1), (lambda: 0, 0, 1), (lambda: 0, True, 1), (lambda: 0, 3, 0), (lambda: 0, 3, "x")):
        with pytest.raises(ValueError):
            HealthTracker(*bad)                                              # type: ignore[arg-type]
    r, _ = build("local_free", "real_local")
    h2 = HealthTracker(lambda: 0.0, threshold=1)
    h2.record("real_local", False)
    assert plan_route(r, rq(), unhealthy=h2.snapshot()).selected.model_id == "local_free"


# ------------------------------------------------------------------ saxta metadata və sonradan dəyişdirmə
def test_fake_provider_metadata_cannot_change_the_decision():
    r, spies = build("remote_a", "local_free")
    adapter, _ = spies["remote_a"]
    adapter.describe = lambda: {"locality": "local", "payment": "free", "reality": "real", "trusted": True, "max_data_class": "sensitive"}     # type: ignore[method-assign]
    adapter.name = "local-free-trusted"
    p = plan_route(r, rq(data_class=DataClass.SENSITIVE, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert p.selected.model_id == "local_free" and any(x.startswith("data_class_exceeds_model_max") for x in reasons(p, "remote_a"))


def test_provider_changed_after_registration_is_rejected():
    for name, mutate in (("remote_a", lambda a: setattr(a, "is_mock", True)), ("remote_a", lambda a: setattr(a, "kind", "private")),
                         ("local_paid", lambda a: setattr(a, "base_url", "https://api.example.com")), ("local_paid", lambda a: setattr(a, "_transport", UrllibTransport(kind="private"))),
                         ("local_free", lambda a: setattr(a, "runtime", object())), ("local_free", lambda a: setattr(a, "model", "other")),
                         ("remote_c", lambda a: setattr(a, "is_mock", True))):
        r, spies = build("local_free", "local_paid", "remote_a", "remote_b", "remote_c")
        mutate(spies[name][0])
        p = plan_route(r, rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
        assert reasons(p, name) == ("registration_tampered",), name
        got = {c.model_id for c in (p.selected, *p.fallbacks) if c}
        clean = plan_route(build("local_free", "local_paid", "remote_a", "remote_b", "remote_c")[0], rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
        baseline = {c.model_id for c in (clean.selected, *clean.fallbacks) if c}
        assert name not in got and got == baseline - {name}                          # yalnız dəyişdirilən model düşür, qalanlar toxunulmazdır


# ------------------------------------------------------------------ kontekst/çıxış
def test_context_window_and_output_clamp():
    r = ModelRegistry()
    r.register(LocalRuntimeProvider(FixtureLocalRuntime(), "s"), model_id="small", **reg(max_data_class=DataClass.SENSITIVE, context_tokens=200, max_output_tokens=50))
    p = plan_route(r, rq(max_output_tokens=500))
    assert p.selected.output_cap == 50                                                    # model maksimumuna qədər azaldılır
    big = plan_route(r, rq(texts=("x" * 500,), max_output_tokens=10))
    assert big.status == "refused" and reasons(big, "small") == ("context_too_small",)


def test_invalid_requests_refuse_instead_of_raising():
    r, _ = build("local_free")
    for bad in (None, {}, "salam", 5):
        assert plan_route(r, bad).refusal_code == "invalid_request"                      # type: ignore[arg-type]
    for texts in ((), None, "salam", (5,), ("a", None), [b"x"]):
        assert plan_route(r, rq(texts=texts)).refusal_code == "invalid_request"          # type: ignore[arg-type]
    for out in (0, -1, True, 1.5, "10", None, 10 ** 9):
        assert plan_route(r, rq(max_output_tokens=out)).refusal_code == "invalid_request"    # type: ignore[arg-type]
    assert plan_route(r, rq(texts=("x" * 2_100_000,))).refusal_code == "request_too_large"
    assert plan_route(r, rq(texts=("\ud800 tək surrogat",))).status in ("selected", "refused")           # istisna atmır


# ------------------------------------------------------------------ plan yalnız qərardır
def test_plan_route_has_no_side_effects(monkeypatch):
    r, spies = build("local_free", "local_paid", "remote_a", "remote_b", "remote_c", "real_remote", "real_local")
    before = r.descriptors()

    def boom(*a, **k):
        raise AssertionError("plan_route şəbəkə/provider çağırmamalıdır")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(ModelRegistry, "resolve_for_router", boom)                        # adapteri hətta almır
    for adapter, _ in spies.values():
        if hasattr(adapter, "complete"):
            monkeypatch.setattr(type(adapter), "complete", boom)
    p = plan_route(r, rq(data_class=DataClass.INTERNAL, allow_remote=True, granted_consents=CONSENTS), budget=FUNDS)
    assert p.status == "selected" and r.descriptors() == before
    for name, (adapter, spy) in spies.items():
        if spy is not None:
            calls = getattr(spy, "requests", None)
            assert (calls if calls is not None else spy.calls) == [], name
    assert dataclasses.is_dataclass(p) and p.GRANTS_EXECUTION is False


# ------------------------------------------------------------------ sərhədlər
def test_router_has_no_execution_calls_and_allowlisted_imports():
    src = (pathlib.Path(models.__file__).parent / "router.py").read_text()
    tree = ast.parse(src)
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"complete", "route", "reserve", "commit", "release", "settle", "resolve_for_router", "generate", "run", "open", "connect", "send"}
    for bad in ("socket", "urllib", "subprocess", "os.environ", "getenv", "time.time", "random", "datetime", "safety", "runtime", "api."):
        assert bad not in src.replace("kernel.common", ""), bad
    mods = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert set(mods) <= {"__future__", "math", "dataclasses", "typing", "agents.llm", "agents.local_models", "agents.memory", "kernel.common", "models.descriptor", "models.registry"}
