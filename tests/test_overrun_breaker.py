"""Faktiki xərc > rezerv / ümumi tavan: dəqiq uçot, davamlı kəsici, sonrakı ödənişli çağırışların bağlanması.

DÜRÜST QEYD: artıq göndərilmiş (uçuşdakı) provayder çağırışının haqqını heç bir tavan geri qaytara bilməz. Bu mexanizm (1) mühafizəkar rezerv ilə normal aşmanı qarşısını alır, (2) faktiki xərci HEÇ VAXT
kəsmir/azaltmır, (3) aşmanı çağırana açıq bildirir, (4) aşmadan sonra HƏR yeni rezervi (sıfır xərclilər daxil) sahib təmizləyənə qədər rədd edir."""
from __future__ import annotations

import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents.agent_runtime import SpendScope  # noqa: F401
from agents.factory_auth import generate_signing_seed
from agents.llm import GuardedLLM, LLMBudgetExceeded, LLMBudgetOverrun, LLMConfig, LLMResponse, spend_scope
from agents.orchestrator import Orchestrator  # noqa: F401
from agents.spec import AgentSpec
from runtime.production import build_production
from safety.broker import Effect, EffectBroker
from safety.execution_context import broker_context
from safety.ledger import CostLedger, LedgerPort, ResolutionDenied, Settlement, clear_breaker_scope, resolution_scope
from tests.factory_keys import owner_cred
from tests.ledger_helpers import owned_ledger
from tests.test_api_auth import pair
from tests.test_providers import FakeTransport, env as cloud_env, ok_body
from tests.test_security_boundary import authenticate


def clear_approval(led, *, scope_op="clear_cap_breaker", breaker_id=None, owner_id="owner"):
    b = led.breaker()
    subject, digest = clear_breaker_scope(b["id"] if breaker_id is None else breaker_id)
    return owner_cred(scope_op, subject, digest, owner_id)


# ============================================================================ ledger: settle / breaker

def test_overrun_is_recorded_exactly_and_trips_a_durable_breaker(tmp_path):
    path = tmp_path / "l.db"
    led = owned_ledger(path, global_limit=0.5)
    assert led.reserve("a", "x", 0.3).ok
    st = led.settle("a", 0.75)                                                      # faktiki rezervdən çox
    assert isinstance(st, Settlement) and st.ok and st.overran and st.breaker_tripped
    assert st.reserved == pytest.approx(0.3) and st.actual == 0.75 and st.overrun == pytest.approx(0.45) and st.over_cap       # 0.75 > tavan 0.5
    assert led.entries()[0].amount == 0.75 and led.total() == 0.75                  # dəqiq: kəsilməyib, azaldılmayıb
    b = led.breaker()
    assert b["op_id"] == "a" and b["actual"] == 0.75 and b["reserved"] == pytest.approx(0.3) and b["total"] == 0.75
    led.close()
    again = owned_ledger(path, global_limit=0.5)                                   # yenidən açılış: kəsici DAVAMLIDIR
    assert again.breaker()["id"] == b["id"]
    assert again.reserve("b", "x", 0.0).status == "denied"


def test_commit_keeps_its_boolean_contract_and_also_trips_the_breaker(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.4)
    led.reserve("a", "x", 0.1)
    assert led.commit("a", 0.5) is True
    assert led.commit("a", 0.5) is False and led.commit("ghost", 1.0) is False
    assert led.breaker() is not None and led.entries()[0].amount == 0.5


def test_after_the_breaker_every_new_reservation_is_denied_including_zero_cost(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.25)
    led.reserve("a", "x", 0.1)
    led.reserve("inflight", "x", 0.1)
    led.settle("a", 0.2)
    for amount in (0.0, 0.0001, 5.0):
        r = led.reserve(f"n{amount}", "x", amount)
        assert r.status == "denied" and "kəsici" in r.reason
    assert led.settle("inflight", 0.1).ok                                           # artıq uçuşda olan əməliyyatın hesablaşması bloklanmır


def test_overruns_without_exceeding_reservation_or_cap_do_not_trip(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    led.reserve("a", "x", 0.5)
    st = led.settle("a", 0.4)                                                        # aşma yoxdur
    assert st.ok and not st.overran and led.breaker() is None
    assert led.reserve("b", "x", 0.5).ok and led.events() == []


def test_exceeding_the_global_cap_trips_even_when_the_reservation_was_respected(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    assert led.record("direct", "x", 1.5) is True                                    # birbaşa yazılmış faktiki xərc tavanı aşır
    assert led.total() == 1.5 and led.breaker() is not None
    assert led.reserve("n", "x", 0.0).status == "denied"


def test_overrun_events_are_audited_with_exact_numbers(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    led.reserve("a", "agent", 0.4)
    led.settle("a", 1.25)
    ev = led.events()
    assert [e["event"] for e in ev] == ["overrun", "breaker_tripped"]
    d = ev[0]["data"]
    assert ev[0]["op_id"] == "a" and d["actual"] == 1.25 and d["reserved"] == pytest.approx(0.4) and d["overrun"] == pytest.approx(0.85) and d["over_cap"] is True and d["cap"] == 1.0


def test_concurrent_settlement_trips_exactly_once_and_loses_no_cost(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    n = 12
    for i in range(n):
        assert led.reserve(f"op{i}", "x", 0.05).ok
    bar = threading.Barrier(n)

    def settle(i):
        bar.wait(5)
        return led.settle(f"op{i}", 0.25)
    with ThreadPoolExecutor(n) as ex:
        results = list(ex.map(settle, range(n)))
    assert all(r.ok and r.overrun > 0 for r in results) and sum(r.breaker_tripped for r in results) == 1      # kəsici ATOMİK: tam bir dəfə (tavan 1.0 aşılanda)
    assert led.total() == pytest.approx(0.25 * n)                                                          # heç bir xərc itirilməyib
    assert [e["event"] for e in led.events()].count("breaker_tripped") == 1 and [e["event"] for e in led.events()].count("overrun") == n
    assert led.reserve("late", "x", 0.0).status == "denied"


def test_concurrent_reservations_are_atomic_against_the_cap_and_a_tripped_breaker(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.15)
    led.reserve("seed", "x", 0.1)
    led.settle("seed", 0.2)                                                          # tavan aşıldı: breaker açıq
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(lambda i: led.reserve(f"c{i}", "x", 0.0), range(32)))
    assert all(r.status == "denied" for r in res) and len(led.entries()) == 1


# ---- təmizləmə: yalnız dəqiq, bir-dəfəlik sahib təsdiqi ilə, auditli

def test_only_an_exact_one_time_owner_approval_clears_the_breaker(tmp_path):
    led = owned_ledger(tmp_path / "l.db", global_limit=0.15)
    led.reserve("a", "x", 0.1)
    led.settle("a", 0.2)
    bid = led.breaker()["id"]
    bad = [clear_approval(led, breaker_id=bid + 1),                                  # başqa kəsici
           clear_approval(led, scope_op="resolve_unknown"),                          # başqa əməliyyat
           owner_cred("clear_cap_breaker", *resolution_scope("a", None)),            # başqa hash
           clear_approval(led, owner_id="stranger")]
    for ap in bad:
        with pytest.raises(ResolutionDenied):
            led.clear_breaker(ap)
    for junk in (None, "owner"):
        with pytest.raises(ResolutionDenied):
            led.clear_breaker(junk)
    assert led.breaker() is not None and led.reserve("n", "x", 0.0).status == "denied"
    ok = clear_approval(led)
    assert led.clear_breaker(ok) is True and led.breaker() is None
    assert led.clear_breaker(ok) is False                                            # kəsici yoxdur: heç nə dəyişmir
    led.record("direct", "x", 0.5)                                                   # yeni tavan aşması: yeni kəsici, köhnə təsdiq yararsız
    with pytest.raises(ResolutionDenied) as e:
        led.clear_breaker(ok)
    assert e.value.code in ("scope_mismatch", "credential_replayed")
    assert led.breaker() is not None


def test_clearing_is_audited_and_does_not_raise_the_cap(tmp_path):
    led = owned_ledger(tmp_path / "l.db", global_limit=1.0)
    led.reserve("a", "x", 0.4)
    led.settle("a", 1.4)                                                             # tavan aşıldı
    assert led.clear_breaker(clear_approval(led)) is True
    ev = [e for e in led.events() if e["event"] == "breaker_cleared"]
    assert len(ev) == 1 and ev[0]["data"]["by"] == "owner"
    assert led.reserve("n", "x", 0.0).status == "denied" and led.global_limit == 1.0       # tavan arifmetikası hələ rədd edir (tavan yüksəlmədi)


def test_without_a_verifier_the_breaker_cannot_be_cleared(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.2)
    led.reserve("a", "x", 0.1)
    led.settle("a", 0.3)
    with pytest.raises(ResolutionDenied) as e:
        led.clear_breaker(owner_cred("clear_cap_breaker", *clear_breaker_scope(led.breaker()["id"])))
    assert e.value.code == "owner_auth_unavailable" and led.breaker() is not None


def test_the_execution_port_can_settle_and_read_but_not_clear(tmp_path):
    port = LedgerPort(owned_ledger(tmp_path / "l.db", global_limit=0.2))
    port.reserve("a", "x", 0.1)
    assert port.settle("a", 0.3).breaker_tripped and port.breaker() is not None and port.events()
    assert not hasattr(port, "clear_breaker") and not hasattr(port, "record")


# ============================================================================ GuardedLLM: mühafizəkar rezerv, aşma bildirişi, kəsici sonrası provayderə çağırış YOXDUR

class Paid:
    name, is_mock = "paid", False

    def __init__(self, pin=10, pout=10, barrier=None):
        self.pin, self.pout, self.barrier, self.calls = pin, pout, barrier, 0
        self._lock = threading.Lock()

    def complete(self, request):
        with self._lock:
            self.calls += 1
        if self.barrier:
            self.barrier.wait(5)
        return LLMResponse(text="cavab", input_tokens=self.pin, output_tokens=self.pout, model="m")


def guarded(tmp_path, prov, led, **cfg):
    c = dict(provider="openai", timeout_s=5.0, max_output_tokens=1024, max_total_tokens=10**7, max_prompt_chars=10000, max_response_chars=500,
             price_in_per_1k=0.01, price_out_per_1k=0.02, cost_limit=100.0)
    c.update(cfg)
    return GuardedLLM(prov, LLMConfig(**c), log_path=tmp_path / "llm.jsonl", stop_path=tmp_path / "STOP", ledger=led)


def test_reservation_is_conservative_one_token_per_character_plus_output_ceiling(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    g = guarded(tmp_path, Paid(), led)
    prompt, system = "x" * 600, "s" * 100
    with broker_context():
        g.complete(prompt, system=system)
    e = led.entries()[0]
    assert e.reserved >= (len(prompt) + len(system)) * 0.01 / 1000 + 1024 * 0.02 / 1000 - 1e-12                 # giriş (simvol başına 1 token) + çıxış tavanı
    assert e.amount == pytest.approx(10 * 0.01 / 1000 + 10 * 0.02 / 1000) and led.breaker() is None            # normal çağırış aşmır


def test_sequential_overrun_is_reported_exact_and_blocks_the_next_provider_call(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.03)
    prov = Paid(pin=2000, pout=1000)                                                  # faktiki 0.04 > rezerv ~0.0215 və > tavan 0.03
    g = guarded(tmp_path, prov, led)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun) as e:                                    # "uğur" kimi qaytarılmır
            g.complete("salam")
        assert e.value.cost == pytest.approx(0.04) and e.value.over_cap and e.value.overrun > 0 and e.value.result.text == "cavab" and e.value.charged is True
        assert led.total() == pytest.approx(0.04) and led.entries()[0].amount == pytest.approx(0.04)       # dəqiq uçot
        assert led.breaker() is not None
        for i in range(3):
            with pytest.raises(LLMBudgetExceeded):
                g.complete(f"salam {i}")
    assert prov.calls == 1                                                              # kəsicidən sonra provayderə HEÇ BİR çağırış getməyib


def test_output_over_the_ceiling_is_charged_exactly_reported_and_blocks_only_the_offending_provider(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    prov = Paid(pin=10, pout=5000)                                                    # çıxış tavanı (1024) aşılıb
    g = guarded(tmp_path, prov, led)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun) as e:
            g.complete("x")
        assert led.total() == pytest.approx(10 * 0.01 / 1000 + 5000 * 0.02 / 1000)
        assert e.value.ceiling_exceeded is True and led.breaker() is None            # MALİYYƏ kəsicisi açılmır (tavan qorunur)
        assert led.provider_breaker("paid") is not None                              # AYRI provayder kəsicisi açıldı
        with pytest.raises(LLMBudgetExceeded):
            g.complete("y")
    assert prov.calls == 1


def test_concurrent_inflight_overruns_are_all_reported_none_under_reported_and_later_calls_never_reach_the_provider(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.10)
    n = 4
    prov = Paid(pin=2000, pout=1000, barrier=threading.Barrier(n))                    # hamısı rezerv alır (4 * ~0.0215 <= 0.10), sonra hamısı 0.04 xərcləyir
    g = guarded(tmp_path, prov, led)
    out: list = []

    def work(i):
        with broker_context():
            try:
                out.append(g.complete(f"q{i}"))
            except Exception as exc:  # noqa: BLE001
                out.append(exc)
    th = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join(10) for t in th]
    assert prov.calls == n
    assert all(isinstance(x, LLMBudgetOverrun) for x in out)                          # hər aşma açıq bildirilib: "büdcə daxilində uğur" yoxdur
    assert led.total() == pytest.approx(n * 0.04)                                     # uçuşdakı haqq GERİ QAYTARILA BİLMƏZ, lakin dəqiq uçota alınıb (azaldılmayıb)
    assert [e["event"] for e in led.events()].count("breaker_tripped") == 1
    with broker_context():
        with pytest.raises(LLMBudgetExceeded):
            g.complete("sonra")
    assert prov.calls == n


def test_agent_scoped_calls_use_the_same_conservative_reservation_and_breaker(tmp_path):
    from agents.llm import SpendScope as Scope
    led = CostLedger(tmp_path / "l.db", global_limit=0.03)                           # ledger tavanı agent çağırışının verdiyi 100.0 AZN limitindən üstündür
    prov = Paid(pin=3000, pout=10)
    g = guarded(tmp_path, prov, led)
    with broker_context(), spend_scope(Scope("agent_a", "agt:t1", 1.0, 100.0)):
        with pytest.raises(LLMBudgetOverrun):
            g.complete("iş")
        with pytest.raises(LLMBudgetExceeded):
            g.complete("iş 2")
    assert prov.calls == 1 and led.agent_total("agent_a") == pytest.approx(3000 * 0.01 / 1000 + 10 * 0.02 / 1000)


# ============================================================================ Broker: icra nəticəsi aşmanı bildirir, kəsicidən sonra icraçı çağırılmır

def spec(limit=5.0):
    return AgentSpec(name="tasnif_agenti", purpose="p", tools=[], permissions=["read_files"], inputs=["sorgu"], outputs=["netice"], cost_limit=limit)


def test_broker_reports_an_overrun_and_refuses_everything_afterwards(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=1.0)
    broker = EffectBroker(tmp_path / "a.jsonl", tmp_path / "STOP", ledger=led, actual_cost=lambda out: out["cost"])
    ran = []

    def work(cost):
        def run():
            ran.append(cost)
            return {"cost": cost}
        return run
    res = broker.run(Effect(spec(), "read_files", 0.5, "op1", "iş"), work(2.0))        # faktiki 2.0 > rezerv 0.5
    assert res.status == "error" and res.cost_status == "overrun" and res.cost == 2.0 and "budget_overrun" in res.reason and res.stage == "settlement"
    assert led.total() == 2.0 and led.breaker() is not None
    for i, cost in enumerate((0.0, 0.1)):                                              # sıfır xərcli effekt də bloklanır
        r = broker.run(Effect(spec(), "read_files", cost, f"op{i + 2}", "iş"), work(cost))
        assert r.status == "blocked" and "kəsici" in r.reason
    assert ran == [2.0]                                                                  # kəsicidən sonra icraçı HEÇ VAXT çağırılmayıb


# ============================================================================ istehsal yığılması: söhbət → Broker → Cloud → kitab

def prod(tmp_path, *, limit, responses):
    e = cloud_env(NAYMERA_LLM_COST_LIMIT="5.0", NAYMERA_LLM_PRICE_IN_PER_1K="0.01", NAYMERA_LLM_PRICE_OUT_PER_1K="0.02")
    e.update({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-ovr-001"})
    transport = FakeTransport(responses)
    svc, engines, _ = build_production(tmp_path / "data", e, global_limit=limit, transport=transport)
    svc.router.store.set_consent("cloud_paid_use", True, "owner")
    return svc, engines, transport


def test_production_overrun_over_the_cap_trips_the_breaker_and_no_later_chat_reaches_the_provider(tmp_path):
    svc, engines, transport = prod(tmp_path, limit=0.04, responses=[ok_body(pin=3000, pout=1000)])      # faktiki 0.05 > tavan 0.04
    svc.app.chat.send("x" * 900)
    assert len(transport.calls) == 1
    assert svc.ledger.breaker() is not None and svc.ledger.total() == pytest.approx(3000 * 0.01 / 1000 + 1000 * 0.02 / 1000)
    assert [e for e in svc.reconciler.events() if e["event"] == "overrun"]
    for i in range(3):
        svc.app.chat.send("y" * (901 + i))                                                              # Broker sıfır xərcli rezervi də rədd edir
    assert len(transport.calls) == 1                                                                    # provayderə yeni çağırış YOXDUR
    dev = pair(svc.auth)
    p = types.SimpleNamespace(stack=svc.factory, dev=dev)
    subject, digest = svc.reconciler.clear_scope()
    approval = authenticate(p, "clear_cap_breaker", subject, digest)                                    # sahib step-up ilə təmizləyir
    assert svc.reconciler.clear_breaker(approval) is True and svc.reconciler.breaker() is None
    svc.app.chat.send("z" * 905)
    assert len(transport.calls) == 1                                                                    # təmizləmə tavanı yüksəltmir: ümumi xərc hələ tavandan çoxdur
    assert svc.reconciler.clear_breaker(approval) is False                                              # eyni təsdiq təkrar işləmir (kəsici yoxdur)
    svc.factory.agents.close()
