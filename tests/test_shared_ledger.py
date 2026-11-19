"""Audit (PR #23 sonrası): TƏK paylaşılan kitab, naməlum rezerv qorunması, ümumi tavan, ApiApp obyekt qrafı.

İstehsal yığılması `runtime/production.build_production` ilə sınanır (`tools/serve.py` eyni funksiyanı çağırır). Şəbəkə/açar/ödəniş YOXDUR: saxta nəqliyyat və təsadüfi açar."""
from __future__ import annotations

import types

import pytest

from agents.factory_auth import generate_signing_seed
from agents.llm import SpendScope, spend_scope
from api.bootstrap import build_app
from runtime.production import build_production
from safety import ledger as ledger_mod
from safety.execution_context import broker_context
from safety.ledger import CostLedger, LedgerPort, LedgerReconciler, ResolutionDenied, resolution_scope
from tests.factory_keys import owner_cred
from tests.ledger_helpers import owned_ledger, owner_approval, owner_resolve
from tests.test_api_auth import pair
from tests.test_providers import FakeTransport, env as cloud_env, ok_body
from tests.test_security_boundary import authenticate


# ============================================================================ (2) naməlum rezerv: commit/release həll ETMİR

def _unknown(led, op="u1", amount=0.5):
    assert led.reserve(op, "agent", amount).ok and led.mark_unknown(op)


def test_commit_cannot_settle_an_unknown_reservation(tmp_path):
    led = owned_ledger(tmp_path / "l.db")
    _unknown(led)
    assert led.commit("u1", 0.0) is False                                 # 0.0 ilə xərci "silmək" cəhdi
    e = led.entries()[0]
    assert (e.status, e.unknown, e.amount) == ("reserved", True, 0.5) and led.total() == pytest.approx(0.5)


def test_release_cannot_release_an_unknown_reservation(tmp_path):
    led = owned_ledger(tmp_path / "l.db")
    _unknown(led)
    assert led.release("u1") is False
    e = led.entries()[0]
    assert (e.status, e.unknown, e.amount) == ("reserved", True, 0.5) and led.total() == pytest.approx(0.5)


def test_unknown_amount_stays_in_budget_totals_and_blocks_new_reservations(tmp_path):
    led = owned_ledger(tmp_path / "l.db", global_limit=1.0)
    _unknown(led, "u1", 0.8)
    led.commit("u1", 0.0), led.release("u1")                              # həll cəhdləri
    assert led.total() == pytest.approx(0.8) and led.reserved_total() == pytest.approx(0.8)
    assert led.reserve("n1", "agent", 0.3).status == "denied"             # 0.8 + 0.3 > 1.0: naməlum məbləğ hələ də sayılır
    assert led.reserve("n2", "agent", 0.2).ok


def test_same_operation_id_stays_blocked_while_unknown(tmp_path):
    led = owned_ledger(tmp_path / "l.db")
    _unknown(led)
    led.release("u1")
    assert led.reserve("u1", "agent", 0.5).status == "duplicate"          # release uğursuz olduğu üçün op_id azad olmayıb
    assert led.total() == pytest.approx(0.5)


def test_valid_exact_owner_resolution_still_works_for_commit_and_release(tmp_path):
    led = owned_ledger(tmp_path / "l.db")
    _unknown(led, "a"), _unknown(led, "b")
    assert owner_resolve(led, "a", 0.3) is True
    assert owner_resolve(led, "b", None) is True
    assert led.total() == pytest.approx(0.3) and led.unknown_entries() == []
    assert led.reserve("b", "agent", 0.1).ok                              # azad edilmiş op_id yenidən rezerv oluna bilər


def test_resolution_rejects_replayed_and_mismatched_approvals_for_unknown_rows(tmp_path):
    led = owned_ledger(tmp_path / "l.db")
    _unknown(led, "a")
    with pytest.raises(ResolutionDenied) as e:
        led.resolve_unknown("a", 0.3, owner_approval("a", 0.4))
    assert e.value.code == "scope_mismatch"
    assert led.unknown_entries()[0].op_id == "a"


# ============================================================================ (3) ümumi tavan kitabın özündədir

def test_ledger_global_cap_binds_every_caller_even_without_a_limit_argument(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    assert led.reserve("a", "llm", 0.6).ok                                # sistem söhbəti kimi: heç bir limit arqumenti yoxdur
    assert led.reserve("b", "agent", 0.6, global_limit=100.0).status == "denied"     # çağıran daha böyük limit versə də tavan qalır
    assert led.reserve("c", "agent", 0.4).ok
    assert led.total() == pytest.approx(1.0)
    with pytest.raises(ValueError):
        CostLedger(":memory:", global_limit=-1)


def test_a_looser_caller_limit_cannot_raise_the_cap_but_a_tighter_one_applies(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    assert led.reserve("a", "x", 0.5, global_limit=0.4).status == "denied"
    assert led.reserve("b", "x", 0.5, global_limit=5.0).ok


def test_the_port_exposes_the_cap_and_a_narrow_surface_only(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=2.0)
    port = LedgerPort(led)
    assert port.global_limit == 2.0 and port.reserve("o", "a", 0.1).ok and port.commit("o", 0.1) and port.total() == pytest.approx(0.1)
    for forbidden in ("record", "expire_stale", "resolve_unknown", "close", "_conn", "_ledger", "_global_cap", "_verifier"):
        assert not hasattr(port, forbidden), forbidden
    with pytest.raises(AttributeError):
        port.reserve = lambda *a: True
    assert not any(isinstance(v, CostLedger) for v in vars(port).values()) if hasattr(port, "__dict__") else True


# ============================================================================ (1) istehsal yığılması: TƏK kitab

PAID_ENV = dict(NAYMERA_LLM_COST_LIMIT="5.0", NAYMERA_LLM_PRICE_IN_PER_1K="0.01", NAYMERA_LLM_PRICE_OUT_PER_1K="0.02")


def prod(tmp_path, *, limit, responses, trust=True):
    e = cloud_env(**PAID_ENV)
    if trust:
        e.update({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-shr-001"})
    transport = FakeTransport(responses)
    svc, engines, anchor = build_production(tmp_path / "data", e, global_limit=limit, transport=transport)
    svc.router.store.set_consent("cloud_paid_use", True, "owner")        # test: sahib razılığı (real Keystore imzası yox)
    return svc, engines, transport


def backend(port):
    return ledger_mod._PORT_BACKENDS[port._token]


def test_production_wiring_uses_one_ledger_instance_and_one_database(tmp_path):
    svc, engines, _ = prod(tmp_path, limit=1.0, responses=[ok_body()])
    cloud_port = engines.cloud.client.ledger
    broker_port = svc.app.chat.engine.router._broker.ledger
    assert isinstance(cloud_port, LedgerPort) and isinstance(broker_port, LedgerPort)
    assert backend(cloud_port) is backend(broker_port) is svc.ledger                                   # eyni nüsxə
    assert ledger_mod._RECONCILER_BACKENDS[svc.reconciler._token] is svc.ledger                         # uzlaşdırma da eyni
    assert backend(cloud_port).global_limit == 1.0 == svc.ledger.global_limit                           # eyni ümumi tavan
    assert sorted(p.name for p in (tmp_path / "data").glob("*ledger*")) == ["ledger.db"]                 # köhnə `llm_ledger.db` bölgüsü YOXDUR
    assert not (tmp_path / "data" / "llm_ledger.db").exists()


def test_build_app_refuses_a_second_or_mismatched_ledger(tmp_path):
    other = CostLedger(tmp_path / "x.db", global_limit=1.0)
    with pytest.raises(ValueError):
        build_app(tmp_path / "d1", global_limit=5.0, ledger=other)                                       # tavan uyğunsuz
    from runtime.factory_bootstrap import TrustAnchor, load_signing_config
    anchor = TrustAnchor(load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-shr-002"}))
    with pytest.raises(ValueError):
        build_app(tmp_path / "d2", global_limit=1.0, trust=anchor, ledger=other)                         # başqa doğrulayıcı


def test_cloud_spend_unknown_costs_and_owner_reconciliation_share_the_ledger(tmp_path):
    svc, engines, transport = prod(tmp_path, limit=1.0, responses=[ok_body(pin=100, pout=100)])
    svc.app.chat.send("x" * 900)                                                                       # sistem söhbəti -> Broker -> Cloud mühərriki
    assert len(transport.calls) == 1
    paid = [e for e in svc.ledger.entries() if e.op_id.startswith("llm:")]
    assert len(paid) == 1 and paid[0].status == "committed" and paid[0].amount == pytest.approx(100 * 0.01 / 1000 + 100 * 0.02 / 1000)
    # naməlum xərc: eyni kitabda görünür və YALNIZ sahib step-up təsdiqi ilə həll olunur
    svc.ledger.reserve("llm:stuck", "llm", 0.2)
    svc.ledger.mark_unknown("llm:stuck")
    assert [e.op_id for e in svc.reconciler.unknown()] == ["llm:stuck"]
    assert svc.ledger.commit("llm:stuck", 0.0) is False and svc.ledger.release("llm:stuck") is False
    dev = pair(svc.auth)
    p = types.SimpleNamespace(stack=svc.factory, dev=dev)
    subject, digest = svc.reconciler.scope("llm:stuck", 0.15)
    ok = authenticate(p, "resolve_unknown", subject, digest)
    with pytest.raises(ResolutionDenied):
        svc.reconciler.resolve("llm:stuck", 0.05, ok)                                                  # başqa məbləğ
    assert svc.reconciler.resolve("llm:stuck", 0.15, ok) is True
    assert svc.reconciler.unknown() == []


def test_system_chat_and_agent_llm_spending_cannot_collectively_exceed_the_global_limit(tmp_path):
    # hər çağırış faktiki 0.015 AZN (500+500 token), mühafizəkar rezerv ~0.031; tavan 0.05: yalnız ikisi sığır
    svc, engines, transport = prod(tmp_path, limit=0.05, responses=[ok_body(pin=500, pout=500)])
    led = svc.ledger
    svc.app.chat.send("x" * 900)                                                                       # 1) sistem söhbəti
    assert len(transport.calls) == 1 and led.total() == pytest.approx(0.015)
    scope = SpendScope("agent_x", "agt:t1", 1.0, 100.0)                                                # 2) agent çağırışı: çağıran tavanı 100 AZN verir (!)
    with broker_context(), spend_scope(scope):
        engines.cloud.client.complete("agent işi " + "y" * 800)
    assert len(transport.calls) == 2 and led.total() == pytest.approx(0.03)
    svc.app.chat.send("z" * 901)                                                                       # 3) tavan: 0.03 + rezerv > 0.04 -> Cloud-a getmir
    with broker_context(), spend_scope(SpendScope("agent_y", "agt:t2", 1.0, 100.0)), pytest.raises(Exception):
        engines.cloud.client.complete("agent işi " + "w" * 800)
    assert len(transport.calls) == 2                                                                   # heç bir əlavə provayder çağırışı olmayıb
    assert led.total() <= 0.05 + 1e-9


def test_system_chat_alone_is_bound_by_the_global_cap_not_only_the_generic_llm_limit(tmp_path):
    svc, engines, transport = prod(tmp_path, limit=0.04, responses=[ok_body(pin=500, pout=500)])      # cost_limit=5.0 (agent tavanı) >> 0.04
    for _ in range(4):
        svc.app.chat.send("x" * 900)
    assert len(transport.calls) == 1 and svc.ledger.total() <= 0.04 + 1e-9


def test_a_zero_global_limit_means_no_paid_spend_at_all(tmp_path):
    svc, engines, transport = prod(tmp_path, limit=0.0, responses=[ok_body()])
    svc.app.chat.send("x" * 900)
    assert transport.calls == [] and svc.ledger.total() == 0.0
