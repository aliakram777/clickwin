"""PR #25 sonrası üç təsdiqlənmiş audit tapıntısı: (1) Unicode giriş rezervi (UTF-8 bayt), (2) fail-closed mühərrik yoxlaması, (3) çıxış tavanı pozuntusu (AYRI provayder kəsicisi).

DÜRÜST QEYD: artıq göndərilmiş provayder haqqı geri qaytarıla bilməz; faktiki xərc həmişə dəqiq yazılır (kəsilmir/azaldılmır)."""
from __future__ import annotations

import ast
import threading
import types
from pathlib import Path

import pytest

from agents.hybrid import CloudEngine, LocalEngine, MockLocalRuntime, PersonalServerRuntime
from agents.llm import GuardedLLM, LLMBudgetExceeded, LLMBudgetOverrun, LLMConfig, LLMResponse, LLMTooLarge, MockProvider
from api.bootstrap import build_app
from runtime.env import build_engines
from runtime.production import build_production
from safety.execution_context import broker_context
from safety.ledger import CostLedger, LedgerPort, ResolutionDenied, clear_provider_breaker_scope
from tests.factory_keys import owner_cred
from tests.engine_support import trust_test_types  # noqa: F401  (fixture)
from tests.ledger_helpers import owned_ledger
from tests.test_api_auth import pair
from tests.test_hybrid_router import Fake
from tests.test_overrun_breaker import Paid, guarded, prod
from tests.test_providers import FakeTransport, env as cloud_env, ok_body
from tests.test_security_boundary import authenticate

ROOT = Path(__file__).resolve().parent.parent
PIN, POUT = 0.01, 0.02


class ByteTokenizerProvider:
    """DÜRÜST provayder, byte-fallback tokenizer: giriş tokenləri = UTF-8 baytların sayı (BPE byte-fallback üçün yuxarı hədd)."""
    name, is_mock = "bytetok", False

    def __init__(self, factor=1, pout=10, barrier=None):
        self.factor, self.pout, self.barrier, self.calls = factor, pout, barrier, 0
        self._lock = threading.Lock()

    def complete(self, r):
        with self._lock:
            self.calls += 1
        if self.barrier:
            self.barrier.wait(5)
        n = len((r.system + r.prompt).encode("utf-8")) * self.factor
        return LLMResponse(text="ok", input_tokens=n, output_tokens=self.pout, model="m")


TEXTS = {
    "ascii": "hello world " * 100,
    "azerbaijani": "Salam, necəsən? Əlaqə məlumatı üçün şəhər, ölkə. " * 40,
    "russian": "Привет, как дела? Это длинный текст на русском языке. " * 40,
    "chinese": "你好，这是一个很长的中文测试文本。" * 120,
    "emoji": "😀🚀🔥" * 400,
    "zwj_emoji": "👨‍👩‍👧‍👦" * 200,
}


# ============================================================================ (1) Unicode giriş rezervi

@pytest.mark.parametrize("name", list(TEXTS))
def test_reservation_covers_one_token_per_utf8_byte_for_every_script(tmp_path, name):
    text = TEXTS[name]
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    prov = ByteTokenizerProvider()
    g = guarded(tmp_path, prov, led, max_output_tokens=10, max_prompt_chars=100000)
    with broker_context():
        res = g.complete(text)                                                           # kəsici/xəta YOXDUR
    e = led.entries()[0]
    nbytes = len(text.encode("utf-8"))
    assert e.reserved >= nbytes * PIN / 1000 + 10 * POUT / 1000 - 1e-12                  # bayt başına 1 token + çıxış tavanı
    assert e.amount == pytest.approx(nbytes * PIN / 1000 + 10 * POUT / 1000)             # dəqiq faktiki xərc
    assert res.overrun == 0.0 and led.breaker() is None and led.events() == []


def test_honest_tokenizer_difference_within_the_cap_does_not_open_the_global_breaker(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    prov = ByteTokenizerProvider(factor=3)                                               # provayder baytdan 3x çox token sayır (rezervi aşır)
    g = guarded(tmp_path, prov, led, max_output_tokens=10, max_prompt_chars=100000)
    text = TEXTS["emoji"]
    with broker_context():
        r1 = g.complete(text)
        r2 = g.complete(text + "!")                                                      # sonrakı çağırış yenə işləyir
    nbytes = len(text.encode("utf-8"))
    assert r1.overrun > 0 and r1.cost == pytest.approx(nbytes * 3 * PIN / 1000 + 10 * POUT / 1000)     # dəqiq xərc, aşma bildirilir
    assert led.breaker() is None and prov.calls == 2
    ev = [e for e in led.events() if e["event"] == "overrun"]
    assert len(ev) == 2 and all(e["data"]["over_cap"] is False for e in ev)             # auditə yazılıb, kəsici yoxdur
    assert led.total() == pytest.approx(r1.cost + r2.cost)


def test_exceeding_the_cap_still_opens_the_financial_breaker(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.05)
    prov = ByteTokenizerProvider(factor=3)
    g = guarded(tmp_path, prov, led, max_output_tokens=10, max_prompt_chars=100000)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun) as e:
            g.complete(TEXTS["emoji"])
        assert e.value.over_cap and e.value.ceiling_exceeded is False and led.breaker() is not None
        with pytest.raises(LLMBudgetExceeded):
            g.complete("sonra")
    assert prov.calls == 1


def test_max_prompt_chars_still_counts_characters_not_bytes(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    g = guarded(tmp_path, ByteTokenizerProvider(), led, max_output_tokens=10, max_prompt_chars=400)
    with broker_context():
        g.complete("😀" * 400)                                                           # 400 simvol (1600 bayt): limit simvolladır, icazəlidir
        with pytest.raises(LLMTooLarge):
            g.complete("😀" * 401)


def test_concurrent_unicode_calls_do_not_trip_anything_and_lose_no_cost(tmp_path):
    n = 6
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    prov = ByteTokenizerProvider(barrier=threading.Barrier(n))
    g = guarded(tmp_path, prov, led, max_output_tokens=10, max_prompt_chars=100000)
    out = []

    def work(i):
        with broker_context():
            out.append(g.complete(TEXTS["chinese"] + str(i)))
    th = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join(10) for t in th]
    assert len(out) == n and led.breaker() is None and all(r.overrun == 0.0 for r in out)
    assert led.total() == pytest.approx(sum(r.cost for r in out))


def test_production_a_unicode_chat_message_no_longer_halts_later_messages(tmp_path):
    text = "😀🚀🔥" * 400
    svc, engines, transport = prod(tmp_path, limit=10.0, responses=[ok_body(pin=len(text.encode("utf-8")), pout=10)])
    svc.app.chat.send(text)
    svc.app.chat.send("salam, necəsən? " * 60)
    assert len(transport.calls) == 2 and svc.ledger.breaker() is None
    assert svc.ledger.reserve("probe", "x", 0.0).status == "reserved"
    svc.factory.agents.close()


# ============================================================================ (2) fail-closed mühərrik yoxlaması

class RogueCloud:
    """Ördək tipli ÖDƏNİŞLİ mühərrik: CloudEngine/GuardedLLM deyil, öz hesablaması, kitabsız."""
    kind, name, is_mock = "cloud", "rogue-paid", False

    def status(self):
        from agents.hybrid import EngineStatus
        return EngineStatus("cloud", self.name, "ready", False, reason="", model="m", supported_kinds=("chat",))

    def supports(self, task):
        return True, ""

    def consent_scopes(self):
        return ()

    def generate(self, prompt, system, request_id):
        from agents.hybrid import EngineOutput
        return EngineOutput("x", "m", self.name, False, 0.5)


class LyingProvider:
    name, is_mock = "liar", True                                                          # ödənişli, lakin özünü mock elan edir

    def complete(self, request):
        return LLMResponse("x", 1, 1, "m")


def make_app(tmp_path, **kw):
    return build_app(tmp_path / f"d{abs(hash(str(sorted(kw))))}", **kw)


def test_duck_typed_paid_engine_is_rejected_by_default_and_never_reaches_the_router(tmp_path):
    with pytest.raises(ValueError, match="naməlum mühərrik tipi"):
        make_app(tmp_path, global_limit=0.0, cloud_engine=RogueCloud())
    with pytest.raises(ValueError, match="naməlum mühərrik tipi"):
        make_app(tmp_path, local_engine=RogueCloud())


def test_cloud_engine_with_an_unsupported_client_is_rejected_even_when_test_types_are_registered(tmp_path, trust_test_types):
    class PaidClient:
        is_mock = False

        def complete(self, prompt, system=""):
            raise AssertionError("çağırılmamalıdır")
    for registered in (False, True):
        if registered:
            trust_test_types(CloudEngine, PaidClient)                                     # məlum tiplərin yoxlaması test reyestri ilə də tətbiq olunur
        with pytest.raises(ValueError, match="yalnız GuardedLLM"):
            make_app(tmp_path, cloud_engine=CloudEngine(PaidClient()))


def test_provider_self_declared_is_mock_is_not_trusted(tmp_path, trust_test_types):
    led = CostLedger(tmp_path / "l.db")
    cfg = LLMConfig(provider="mock")
    for registered in (False, True):
        if registered:
            trust_test_types(CloudEngine, GuardedLLM, LyingProvider)
        with pytest.raises(ValueError, match="naməlum provayder tipi"):
            make_app(tmp_path, cloud_engine=CloudEngine(GuardedLLM(LyingProvider(), cfg, ledger=led)), ledger=led)

    class SubGuarded(GuardedLLM):                                                         # alt sinif də etibarlı deyil (tam tip yoxlanılır)
        pass
    with pytest.raises(ValueError, match="yalnız GuardedLLM"):
        make_app(tmp_path, cloud_engine=CloudEngine(SubGuarded(MockProvider(), cfg)))


def test_local_engine_runtime_types_are_validated(tmp_path, trust_test_types):
    class Rogue:
        is_mock = False
        languages = ("en",)

        def loaded(self):
            return True

        def generate(self, prompt, system, max_tokens):
            return "x"
    with pytest.raises(ValueError, match="lokal icra mühiti"):
        make_app(tmp_path, local_engine=LocalEngine(Rogue()))
    make_app(tmp_path, local_engine=LocalEngine(None))
    make_app(tmp_path, local_engine=LocalEngine(MockLocalRuntime()))
    make_app(tmp_path, local_engine=LocalEngine(PersonalServerRuntime()))                # nəqliyyatsız (qurulmamış): qəbul olunur
    with pytest.raises(ValueError, match="nəqliyyatlı PersonalServerRuntime"):
        make_app(tmp_path, local_engine=LocalEngine(PersonalServerRuntime(lambda op, p: {"text": "x"}, is_mock=False)))
    trust_test_types(FakeTransport)                                                       # FakeTransport yalnız test reyestri ilə (UrllibTransport(kind=private) istehsal nəqliyyatıdır)
    real_local = build_engines({"NAYMERA_LOCAL_AI_URL": "http://192.168.1.20:8000", "NAYMERA_LOCAL_AI_MODEL": "small"}, log_dir=tmp_path, stop_path=tmp_path / "STOP",
                               transport=FakeTransport([ok_body()]))
    assert type(real_local.local.runtime).__name__ == "OpenAICompatLocalRuntime"
    make_app(tmp_path, local_engine=real_local.local)


def test_legitimate_none_mock_and_not_configured_configurations_are_preserved(tmp_path):
    make_app(tmp_path)
    make_app(tmp_path, cloud_engine=CloudEngine(None), local_engine=LocalEngine(None))
    mock = build_engines({"NAYMERA_CLOUD_PROVIDER": "mock"}, log_dir=tmp_path, stop_path=tmp_path / "STOP", ledger=None)
    make_app(tmp_path, cloud_engine=mock.cloud, local_engine=mock.local)


def test_real_engine_with_an_explicit_identical_ledger_is_still_accepted_and_others_are_not(tmp_path):
    shared = CostLedger(tmp_path / "s.db", global_limit=0.5)
    e = cloud_env(NAYMERA_LLM_COST_LIMIT="5.0", NAYMERA_LLM_PRICE_IN_PER_1K="0.01", NAYMERA_LLM_PRICE_OUT_PER_1K="0.02")
    eng = build_engines(e, log_dir=tmp_path, stop_path=tmp_path / "STOP", transport=FakeTransport([ok_body()]), ledger=LedgerPort(shared))
    assert make_app(tmp_path, global_limit=0.5, cloud_engine=eng.cloud, ledger=shared).ledger is shared
    with pytest.raises(ValueError):
        make_app(tmp_path, global_limit=0.5, cloud_engine=eng.cloud)
    with pytest.raises(ValueError):
        make_app(tmp_path, global_limit=0.5, cloud_engine=eng.cloud, ledger=CostLedger(tmp_path / "o.db", global_limit=0.5))


# ============================================================================ (3) çıxış tavanı pozuntusu

class Defiant:
    """Provayder `max_tokens`-i nəzərə almır: giriş kiçik, çıxış tavandan çox; faktiki xərc REZERVDƏN AZ qalır (uzun giriş, ucuz çıxış)."""
    name, is_mock = "defiant", False

    def __init__(self, pin=300, pout=2000, barrier=None):
        self.pin, self.pout, self.barrier, self.calls = pin, pout, barrier, 0
        self._lock = threading.Lock()

    def complete(self, request):
        with self._lock:
            self.calls += 1
        if self.barrier:
            self.barrier.wait(5)
        return LLMResponse(text="ok", input_tokens=self.pin, output_tokens=self.pout, model="m")


LONG = "word " * 4000


def cheap_output(tmp_path, prov, led):
    return guarded(tmp_path, prov, led, max_output_tokens=50, max_prompt_chars=100000, price_in_per_1k=0.01, price_out_per_1k=0.0005)


def test_ceiling_violation_with_cost_below_the_reservation_is_reported_recorded_and_blocks_only_that_provider(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    prov = Defiant()
    g = cheap_output(tmp_path, prov, led)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun) as e:
            g.complete(LONG)
    ex = e.value
    entry = led.entries()[0]
    assert entry.amount <= entry.reserved                                                  # xərc rezervdən AZDIR: yalnız çıxış tavanı pozulub
    assert ex.ceiling_exceeded is True and ex.over_cap is False and ex.result.text == "ok" and ex.cost == pytest.approx(300 * 0.01 / 1000 + 2000 * 0.0005 / 1000)
    assert entry.amount == pytest.approx(ex.cost)                                          # dəqiq xərc saxlanıb
    assert led.breaker() is None                                                           # MALİYYƏ kəsicisi açılmayıb (tavan qorunur)
    pb = led.provider_breaker("defiant")
    assert pb is not None and pb["output_tokens"] == 2000 and pb["max_output_tokens"] == 50
    names = [x["event"] for x in led.events()]
    assert "output_ceiling_violation" in names and "provider_breaker_tripped" in names and "breaker_tripped" not in names
    with broker_context():
        with pytest.raises(LLMBudgetExceeded, match="provayder təhlükəsizlik kəsicisi"):
            g.complete(LONG + "2")
    assert prov.calls == 1                                                                 # pozan provayderə sonrakı çağırış getməyib


def test_other_providers_and_normal_calls_are_unaffected(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    bad = cheap_output(tmp_path, Defiant(), led)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun):
            bad.complete(LONG)
    good_prov = Paid(pin=10, pout=10)
    good_prov.name = "well_behaved"
    good = guarded(tmp_path, good_prov, led)
    with broker_context():
        r = good.complete("salam")                                                         # başqa provayder işləyir
    assert r.text == "cavab" and led.provider_breaker("well_behaved") is None and led.breaker() is None
    normal = guarded(tmp_path, Paid(pin=10, pout=50), CostLedger(tmp_path / "n.db", global_limit=5.0), max_output_tokens=50)
    with broker_context():
        for i in range(3):                                                                 # tavan DAXİLİNDƏ çıxış: heç bir kəsici/hadisə yoxdur
            assert normal.complete(f"q{i}").overrun == 0.0
    assert normal.ledger.events() == [] and normal.ledger.provider_breakers() == []


def test_ceiling_violation_that_also_exceeds_the_cap_opens_both_breakers(tmp_path):
    led = CostLedger(tmp_path / "l.db", global_limit=0.002)
    g = guarded(tmp_path, Defiant(pin=10, pout=5000), led, max_output_tokens=50, price_out_per_1k=0.02, price_in_per_1k=0.0001, max_prompt_chars=100000)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun) as e:
            g.complete("x")
    assert e.value.ceiling_exceeded and e.value.over_cap and led.breaker() is not None and led.provider_breaker("defiant") is not None


def test_concurrent_ceiling_violations_trip_the_provider_breaker_once_and_lose_no_cost(tmp_path):
    n = 5
    led = CostLedger(tmp_path / "l.db", global_limit=100.0)
    prov = Defiant(barrier=threading.Barrier(n))
    g = cheap_output(tmp_path, prov, led)
    out = []

    def work(i):
        with broker_context():
            try:
                out.append(g.complete(LONG + str(i)))
            except Exception as exc:  # noqa: BLE001
                out.append(exc)
    th = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join(10) for t in th]
    assert prov.calls == n and all(isinstance(x, LLMBudgetOverrun) and x.ceiling_exceeded for x in out)
    assert [e["event"] for e in led.events()].count("provider_breaker_tripped") == 1 and [e["event"] for e in led.events()].count("output_ceiling_violation") == n
    assert led.total() == pytest.approx(sum(x.cost for x in out))
    with broker_context():
        with pytest.raises(LLMBudgetExceeded):
            g.complete("sonra")
    assert prov.calls == n


def test_only_an_exact_one_time_owner_approval_clears_the_provider_breaker(tmp_path):
    led = owned_ledger(tmp_path / "l.db", global_limit=100.0)
    g = cheap_output(tmp_path, Defiant(), led)
    with broker_context():
        with pytest.raises(LLMBudgetOverrun):
            g.complete(LONG)
    b = led.provider_breaker("defiant")
    subject, digest = clear_provider_breaker_scope("defiant", b["id"])
    wrong = [owner_cred("clear_provider_breaker", *clear_provider_breaker_scope("other", b["id"])),            # başqa provayder
             owner_cred("clear_provider_breaker", *clear_provider_breaker_scope("defiant", b["id"] + 1)),      # başqa kəsici
             owner_cred("clear_cap_breaker", subject, digest),                                                  # başqa əməliyyat
             owner_cred("clear_provider_breaker", subject, digest, "stranger")]                                 # sahib deyil
    for ap in wrong:
        with pytest.raises(ResolutionDenied):
            led.clear_provider_breaker("defiant", ap)
    for junk in (None, "owner"):
        with pytest.raises(ResolutionDenied):
            led.clear_provider_breaker("defiant", junk)
    assert led.provider_breaker("defiant") is not None
    ok = owner_cred("clear_provider_breaker", subject, digest)
    assert led.clear_provider_breaker("defiant", ok) is True and led.provider_breaker("defiant") is None
    assert led.clear_provider_breaker("defiant", ok) is False                                                   # təkrar: kəsici yoxdur
    ev = [e for e in led.events() if e["event"] == "provider_breaker_cleared"]
    assert len(ev) == 1 and ev[0]["data"]["by"] == "owner"
    assert led.reserve("again", "x", 0.0, provider="defiant").ok                                                # təmizləmədən sonra yenə rezerv olunur


def test_the_execution_port_cannot_clear_the_provider_breaker(tmp_path):
    port = LedgerPort(owned_ledger(tmp_path / "l.db", global_limit=100.0))
    assert port.provider_breaker("x") is None and port.provider_breakers() == []
    assert not hasattr(port, "clear_provider_breaker") and not hasattr(port, "clear_breaker")


def test_production_provider_breaker_blocks_chat_until_the_owner_clears_it(tmp_path):
    svc, engines, transport = prod(tmp_path, limit=10.0, responses=[ok_body(pin=10, pout=5000), ok_body(pin=10, pout=10)])     # 1-ci cavab tavanı (1024) pozur
    svc.app.chat.send("x" * 900)
    assert len(transport.calls) == 1 and svc.ledger.breaker() is None
    (pb,) = svc.reconciler.provider_breakers()
    svc.app.chat.send("y" * 901)                                                           # (router-in öz dövrə kəsicisi 3 ardıcıl uğursuzluqda açılır: burada 1 əlavə cəhd)
    assert len(transport.calls) == 1                                                       # pozan provayderə yeni çağırış YOXDUR
    dev = pair(svc.auth)
    p = types.SimpleNamespace(stack=svc.factory, dev=dev)
    subject, digest = svc.reconciler.clear_provider_scope(pb["provider"])
    approval = authenticate(p, "clear_provider_breaker", subject, digest)
    assert svc.reconciler.clear_provider_breaker(pb["provider"], approval) is True and svc.reconciler.provider_breakers() == []
    svc.app.chat.send("z" * 905)
    assert len(transport.calls) == 2                                                       # təmizləmədən sonra işləyir
    assert svc.ledger.breaker() is None
    svc.factory.agents.close()
