"""Mərhələ 18.1-E: 18.1-A–D sisteminin təhlükəsizlik auditi (hücum ssenariləri). Oflayn fixture; real API/açar/şəbəkə YOXDUR.

Bəzi testlər AÇIQ MƏHDUDİYYƏTİ «kanarya» kimi sənədləşdirir (Broker konteksti proses-daxilidir; Stage 17 köhnə yolundakı boşluqlar): onlar boşluğun HƏLƏ MÖVCUD olduğunu təsdiqləyir ki, sənədlər
yalan danışmasın; boşluq bağlandıqda test uğursuz olacaq və sənəd yenilənməlidir."""
from __future__ import annotations

import ast
import dataclasses
import pathlib
import threading

import pytest

import runtime.brokered_models as bm
from agents.llm import GuardedLLM, LLMConfig, LLMBadResponse, LLMRequest, Secret
from agents.providers import HttpResponse, OpenAICompatibleProvider
from agents.spec import AgentSpec
from models import DataClass, ModelCapability
from models.adapters.fixtures import FixtureTransport, anthropic_ok, gemini_ok
from models.adapters.openai_compat import OpenAICompatProvider
from runtime.brokered_models import BrokeredModelRunner, ContextBlock, _TrustedProviderView
from runtime.transport import UrllibTransport
from safety.execution_context import broker_context
from safety.ledger import LedgerPort
from tests.test_models_runner import KEY, W, req

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 1. ContextVar sərhədinin məhdudiyyəti (açıq)
def test_documented_limit_in_process_code_can_forge_the_broker_context(tmp_path):
    """KANARYA: eyni prosesdə ixtiyari kod Broker kontekstini və view-nun silahlanmasını ÖZÜ qura bilər. Bu OS sərhədi DEYİL; yalnız səhv/yan keçid qorunmasıdır. Xərc yenə ledger-dən keçir."""
    w = W(tmp_path)
    view = _TrustedProviderView(w.registry, w.registry.descriptor("remote_a"), w.adapters["remote_a"])
    g = GuardedLLM(view, LLMConfig(provider="p", model="m", price_in_per_1k=0.5, price_out_per_1k=1.0), log_path=tmp_path / "x.jsonl", ledger=LedgerPort(w.ledger))
    view._arm(True)
    with broker_context():
        g.complete("salam")
    assert w.calls("remote_a") == 1 and any(e.op_id.startswith("llm:") for e in w.ledger.entries())            # məhdudiyyət MÖVCUDDUR (kitab uçotu isə qorunur)
    for doc in ((ROOT / "safety" / "execution_context.py").read_text(), bm.__doc__ or ""):
        assert "OS" in doc and ("DEYİL" in doc)                                                                  # sənədlər bunu açıq deyir


# ------------------------------------------------------------------ 2. provider metadata saxtalaşdırılması (tam runner ilə)
@pytest.mark.parametrize("attr,value", [("is_mock", True), ("kind", "private"), ("base_url", "https://127.0.0.1"), ("_transport", UrllibTransport(kind="cloud")),
                                       ("model", "other-model"), ("base_url", "http://insecure.example.com")])
def test_post_registration_forgery_of_a_remote_adapter_is_never_called(tmp_path, attr, value):
    w = W(tmp_path)
    setattr(w.adapters["remote_a"], attr, value)
    r = w.runner.run(req(prefer=("remote_a", "remote_b", "local_free")))
    assert w.calls("remote_a") == 0 and r.status == "ok" and r.model_id != "remote_a"
    assert r.attempts == () or r.attempts[0].model_id != "remote_a"                                             # plan onu artıq seçmir (reverify)


@pytest.mark.parametrize("attr,value", [("runtime", object()), ("model", "x-y"), ("is_mock", True)])
def test_post_registration_forgery_of_the_local_adapter_is_never_called(tmp_path, attr, value):
    w = W(tmp_path)
    setattr(w.adapters["local_free"], attr, value)
    r = w.runner.run(req(prefer=("local_free",), data_class=DataClass.SENSITIVE))
    assert w.rt.calls == [] and r.status == "refused"                                                           # SENSITIVE: uzaq yoxdur, lokal etibarsızdır


def test_lying_describe_name_and_key_swaps_do_not_change_the_reported_status(tmp_path):
    w = W(tmp_path)
    a = w.adapters["remote_a"]
    a.describe = lambda: {"locality": "local", "payment": "free", "reality": "real", "trusted": True}               # type: ignore[method-assign]
    a.name = "local-free-trusted"
    r = w.runner.run(req(prefer=("remote_a",)))
    assert (r.model_id, r.locality, r.payment, r.simulated) == ("remote_a", "remote", "paid", True)                  # reyestrin statusu: uzaq, ödənişli, simulyasiya


# ------------------------------------------------------------------ 3. büdcə və payment sürüşməsi
def test_price_drift_is_accounted_with_the_current_descriptor_prices(tmp_path):
    w = W(tmp_path)
    desc, adapter = w.registry._items["remote_b"]
    w.registry._items["remote_b"] = (dataclasses.replace(desc, price_in_per_1k=2.0, price_out_per_1k=4.0), adapter)
    r = w.runner.run(req(prefer=("remote_b",)))
    assert r.model_id == "remote_b" and r.cost == pytest.approx(5 * 2.0 / 1000 + 7 * 4.0 / 1000) and w.ledger.total() == pytest.approx(r.cost)       # ledger = yeni qiymət


def test_shrinking_global_cap_by_other_agents_stops_paid_calls_and_never_exceeds_the_cap(tmp_path):
    w = W(tmp_path, global_limit=0.5, agent_limit=5.0)
    assert w.ledger.reserve("other-agent-spend", "agent_other", 0.45).status == "reserved"                          # başqa agent büdcənin çoxunu götürdü
    r = w.runner.run(req(prefer=("remote_a", "remote_b")))
    assert r.model_id == "local_free" and w.calls("remote_a") == 0 and w.calls("remote_b") == 0 and w.ledger.total() <= 0.5 + 1e-9


# ------------------------------------------------------------------ 4. məxfilik sinfinin yüksəldilməsi / zəiflədilməsi
def test_declared_low_class_with_secret_context_is_raised_and_refused(tmp_path):
    w = W(tmp_path)
    secret = "sk-" + "Pq5T" * 10
    r = w.runner.run(req(data_class=DataClass.PUBLIC, context_blocks=(ContextBlock("agent_a", "qeyd: " + secret),), prefer=("remote_a",)))
    assert r.status == "refused" and "secret_data_not_allowed" in r.reason and all(w.calls(n) == 0 for n in ("local_free", "remote_a", "remote_b")) and secret not in repr(r)


@pytest.mark.parametrize("spoof", ["Public", "PUBLIC", " public", "public ", "pub1ic", "internal\n", None, 0, "", ["public"]])
def test_class_spoofing_never_weakens_to_a_remote_eligible_class(tmp_path, spoof):
    w = W(tmp_path)
    r = w.runner.run(req(data_class=spoof, prefer=("remote_a", "remote_b")))
    assert r.model_id == "local_free" and w.calls("remote_a") == 0 and w.calls("remote_b") == 0                         # naməlum ⇒ yalnız lokal


def test_model_max_class_and_memory_floor_exclude_remote_models(tmp_path):
    w = W(tmp_path, models={"local_free": None, "remote_a": [anthropic_ok("x")]})
    w.add("remote_b", [gemini_ok("y")], max_data_class=DataClass.PUBLIC)                                               # yalnız PUBLIC qəbul edən uzaq model
    pub = w.runner.run(req("c1", data_class=DataClass.PUBLIC, prefer=("remote_b",)))
    assert pub.model_id == "remote_b"
    internal = w.runner.run(req("c2", data_class=DataClass.INTERNAL, prefer=("remote_b",)))
    assert internal.model_id != "remote_b" and w.calls("remote_b") == 1
    mem = w.runner.run(req("c3", data_class=DataClass.PUBLIC, prefer=("remote_b",), context_blocks=(ContextBlock("agent_a", "yaddaş"),)))
    assert mem.model_id != "remote_b" and w.calls("remote_b") == 1                                                    # yaddaş konteksti INTERNAL minimumu yaradır


# ------------------------------------------------------------------ 5. fallback zamanı qapıların saxlanması
def test_every_fallback_attempt_replans_with_identical_request_and_accumulating_exclusions(tmp_path, monkeypatch):
    w = W(tmp_path, models={"local_free": None, "remote_a": [HttpResponse(401, b"x")], "remote_b": [HttpResponse(500, b"x")]})
    seen, budget_calls = [], []
    real, real_budget = bm.plan_route, w.runner._budget

    def counting_budget():
        budget_calls.append(1)
        return real_budget()
    w.runner._budget = counting_budget                                                                                    # type: ignore[method-assign]

    def spy(registry, request, **kw):
        seen.append((request, kw))
        return real(registry, request, **kw)
    monkeypatch.setattr(bm, "plan_route", spy)
    r = w.runner.run(req(prefer=("remote_a", "remote_b", "local_free"), data_class=DataClass.INTERNAL))
    assert [a.outcome for a in r.attempts] == ["failed_not_charged", "unknown_charge", "ok"] and r.model_id == "local_free"
    assert len(seen) == 3                                                                                               # hər cəhd üçün YENİDƏN plan
    first = seen[0][0]
    for request, kw in seen:
        assert (request.texts, request.data_class, request.allow_remote, request.allow_simulated, request.max_output_tokens) == (first.texts, first.data_class, first.allow_remote,
                                                                                                                                    first.allow_simulated, first.max_output_tokens)
    unhealthy = [set(kw["unhealthy"]) for _, kw in seen]
    assert unhealthy[0] < unhealthy[1] < unhealthy[2] and {"remote_a", "remote_b"} <= unhealthy[2]                          # istisnalar yığılır
    assert (kw["unhealthy"]["remote_a"], kw["unhealthy"]["remote_b"]) == ("failed_not_charged", "unknown_charge")
    assert budget_calls == [1, 1, 1]                                                                                       # hər cəhddə büdcə yenidən oxunur (təzə snapshot)


def test_fallback_after_unknown_paid_outcome_cannot_reach_any_paid_provider_even_with_headroom(tmp_path):
    w = W(tmp_path, models={"remote_a": [HttpResponse(500, b"x")], "remote_b": [gemini_ok("b")]})
    r = w.runner.run(req(prefer=("remote_a", "remote_b")))
    assert r.status == "refused" and w.calls("remote_b") == 0 and len([e for e in w.ledger.entries() if e.op_id.startswith("llm:") and e.amount > 0]) == 1


def test_sensitive_data_has_no_remote_fallback_when_the_local_model_fails(tmp_path):
    w = W(tmp_path)
    w.rt.reply = RuntimeError("çökdü")
    r = w.runner.run(req(data_class=DataClass.SENSITIVE, prefer=("local_free", "remote_a", "remote_b")))
    assert r.status == "refused" and w.calls("remote_a") == 0 and w.calls("remote_b") == 0


# ------------------------------------------------------------------ 6. paralel agentlər, təkrar əməliyyatlar
def test_parallel_agents_keep_limits_isolation_and_single_execution(tmp_path):
    w = W(tmp_path, global_limit=0.40, agent_limit=0.30, models={"local_free": None, "remote_a": [anthropic_ok("a")] * 60, "remote_b": [gemini_ok("b")] * 60})
    spec_b = AgentSpec(name="agent_b", purpose="p", tools=[], permissions=["read_files"], inputs=["x"], outputs=["y"], cost_limit=0.30)
    rb = BrokeredModelRunner(w.registry, w.broker, agent=spec_b, consents=w.consents, log_dir=tmp_path)
    results, errors, peak = [], [], [0.0]
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            peak[0] = max(peak[0], w.ledger.total())

    def work(runner, tag, i):
        try:
            results.append((tag, runner.run(req(f"{tag}-{i % 6}", prefer=("remote_a", "remote_b", "local_free")))))          # op_id-lər ƏKS olaraq təkrarlanır
        except Exception as exc:                                                                                              # noqa: BLE001
            errors.append(exc)
    mon = threading.Thread(target=monitor)
    mon.start()
    ts = [threading.Thread(target=work, args=(rn, tag, i)) for i in range(12) for rn, tag in ((w.runner, "a"), (rb, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stop.set()
    mon.join()
    assert not errors
    statuses = {r.status for _, r in results}
    assert statuses <= {"ok", "duplicate", "refused", "blocked"} and "error" not in statuses
    assert sum(1 for _, r in results if r.status == "ok") <= 12                                                                    # hər unikal op ən çox bir dəfə
    assert w.ledger.total() <= 0.40 + 1e-9 and peak[0] <= 0.40 + 1e-9                                                              # ümumi tavan heç vaxt aşılmayıb
    assert w.ledger.agent_total("agent_a") <= 0.30 + 1e-9 and w.ledger.agent_total("agent_b") <= 0.30 + 1e-9
    broker_ops = [e.op_id for e in w.ledger.entries() if e.op_id.startswith("model:")]
    assert len(broker_ops) == len(set(broker_ops)) <= 12
    for e in w.ledger.entries():
        if e.op_id.startswith("llm:a-"):
            assert e.agent == "agent_a"
        if e.op_id.startswith("llm:b-"):
            assert e.agent == "agent_b"                                                                                           # xərc yalnız öz agentinə yazılır
    assert not [e for e in w.ledger.entries() if e.unknown]


def test_repeated_operations_do_not_create_repeated_charges(tmp_path):
    w = W(tmp_path)
    first = w.runner.run(req("same", prefer=("remote_a",)))
    totals = [w.ledger.total()]
    for _ in range(5):
        assert w.runner.run(req("same", prefer=("remote_a",))).status == "duplicate"
        totals.append(w.ledger.total())
    assert first.status == "ok" and len(set(totals)) == 1 and w.calls("remote_a") == 1


# ------------------------------------------------------------------ 7. Stage 17 köhnə yolundakı məlum boşluqlar (KANARYA)
def _raw(body: bytes):
    return OpenAICompatibleProvider("https://api.example.com/v1", "m", Secret(KEY), FixtureTransport(HttpResponse(200, body)), kind="cloud")


def test_stage17_old_path_gaps_still_exist_and_the_new_path_closes_them():
    """KANARYA: xam `OpenAICompatibleProvider` (Stage 17, DƏYİŞDİRİLMƏYİB) — (a) dərin JSON RecursionError, (b) tək surrogat, (c) cavab ölçüsü limiti yoxdur. Yeni yol hamısını rədd edir."""
    req_ = LLMRequest("", "x", 10, 5.0)
    with pytest.raises(RecursionError):
        _raw(b"[" * 100_000).complete(req_)
    surrogate = b'{"choices":[{"message":{"content":"\\ud800"}}]}'
    assert _raw(surrogate).complete(req_).text == "\ud800"
    big = b'{"choices":[{"message":{"content":"' + b"x" * 5_000_000 + b'"}}]}'
    assert len(_raw(big).complete(req_).text) == 5_000_000
    hardened = lambda body: OpenAICompatProvider("https://api.example.com/v1", "m", Secret(KEY), FixtureTransport(HttpResponse(200, body)), kind="cloud")      # noqa: E731
    for body in (b"[" * 100_000, surrogate, big):
        with pytest.raises(LLMBadResponse):
            hardened(body).complete(req_)


# ------------------------------------------------------------------ 8. sərhədlər: istehsala qoşulma yoxdur, run_session bağlıdır
def test_nothing_in_the_production_path_imports_the_new_layer_and_run_session_stays_closed():
    for rel in ("runtime/env.py", "api/bootstrap.py", "runtime/production.py", "tools/serve.py", "api/app.py"):
        tree = ast.parse((ROOT / rel).read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not {m for m in mods if m.split(".")[0] == "models" or m == "runtime.brokered_models"}, rel
    for rel in ("runtime/brokered_models.py", "models/router.py", "models/registry.py", "models/config.py", "models/adapters/base.py"):
        assert "run_session" not in (ROOT / rel).read_text()
    from runtime.agent_gateway import AgentGateway
    assert hasattr(AgentGateway, "run_session")                                                                                 # mövcuddur və toxunulmayıb (bu mərhələ onu aktivləşdirmir)
