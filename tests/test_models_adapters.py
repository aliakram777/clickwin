"""Mərhələ 18.1-B: dörd provider adapteri (OpenAI-uyğun, Anthropic, Gemini, lokal) — oflayn fixture nəqliyyatlar. Real API/açar/şəbəkə YOXDUR."""
from __future__ import annotations

import json

import pytest

from agents.llm import (GuardedLLM, LLMBadResponse, LLMBlocked, LLMConfig, LLMProviderError, LLMRequest, LLMResponse, Secret)
from agents.providers import HttpResponse, OpenAICompatibleProvider, ProviderTimeout, TransportError  # noqa: F401
from models.adapters.openai_compat import OpenAICompatProvider
from kernel.capability import Availability
from models import DataClass, Locality, ModelCapability, ModelProvider, ModelRegistry, Payment, Reality, RegistryError
from models.adapters.anthropic import AnthropicProvider
from models.adapters.base import MAX_RESPONSE_BYTES
from models.adapters.fixtures import FixtureTransport, anthropic_ok, gemini_ok, openai_ok
from models.adapters.gemini import GeminiProvider
from models.adapters.local import FixtureLocalRuntime, LocalRuntimeProvider
from safety.execution_context import broker_context

KEY = "sk-" + "Qw7E" * 10                                 # çalışma vaxtında qurulur
AZ = "Salam, dünya! ğüşöçıə İŞ 🙂 é"
REQ = LLMRequest("sistem: sən köməkçisən", AZ, 100, 5.0)
CAPS = (ModelCapability.TEXT_GENERATION, ModelCapability.CHAT)


def make(kind: str, *steps):
    """(provider, transport|runtime) — hər növ üçün eyni quruluş."""
    if kind == "local":
        rt = FixtureLocalRuntime(*steps) if steps else FixtureLocalRuntime()
        return LocalRuntimeProvider(rt, "local-fixture"), rt
    t = FixtureTransport(*steps)
    if kind == "openai":
        return OpenAICompatProvider("https://api.example.com/v1", "oa-model", Secret(KEY), t, kind="cloud"), t
    if kind == "openai_private":
        return OpenAICompatProvider("http://127.0.0.1:8080", "oa-local", None, t, kind="private"), t
    if kind == "anthropic":
        return AnthropicProvider("claude-fixture", Secret(KEY), t), t
    if kind == "gemini":
        return GeminiProvider("gemini-fixture", Secret(KEY), t), t
    raise AssertionError(kind)


OK = {"openai": openai_ok, "openai_private": openai_ok, "anthropic": anthropic_ok, "gemini": gemini_ok}
HTTP_KINDS = ["openai", "openai_private", "anthropic", "gemini"]
ALL_KINDS = HTTP_KINDS + ["local"]


def ok_provider(kind: str, text: str = "cavab", **kw):
    if kind == "local":
        return make("local", text)
    return make(kind, OK[kind](text, **kw))


# ------------------------------------------------------------------ eyni interfeys
@pytest.mark.parametrize("kind", ALL_KINDS)
def test_all_adapters_share_the_standard_interface(kind):
    p, _ = ok_provider(kind, AZ)
    assert isinstance(p, ModelProvider) and p.is_mock is False
    r = p.complete(REQ)
    assert isinstance(r, LLMResponse) and r.text == AZ and r.finish_reason == "stop"
    assert all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (r.input_tokens, r.output_tokens)) and isinstance(r.model, str) and r.model


def test_swapping_provider_needs_no_caller_change():
    def ask(provider) -> str:
        return provider.complete(REQ).text
    assert {ask(ok_provider(k, f"cavab-{k}")[0]) for k in ALL_KINDS} == {f"cavab-{k}" for k in ALL_KINDS}


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_adapters_work_inside_guardedllm_and_are_blocked_outside_broker(kind, tmp_path):
    """GuardedLLM dəyişməz: mock olmayan adapter Broker konteksti olmadan provayderə 0 çağırışla bloklanır."""
    p, backend = ok_provider(kind, "salam")
    g = GuardedLLM(p, LLMConfig(provider=kind, model="x"), log_path=tmp_path / "llm.jsonl")
    with pytest.raises(LLMBlocked):
        g.complete("sorğu")
    assert (backend.calls if kind == "local" else backend.requests) == []
    with broker_context():
        assert g.complete("sorğu").text == "salam"


# ------------------------------------------------------------------ sorğu formatı
def test_wire_formats_and_key_placement():
    for kind, url_part, hdr in (("openai", "/v1/chat/completions", "Authorization"), ("anthropic", "/v1/messages", "x-api-key"),
                                ("gemini", "/v1beta/models/gemini-fixture:generateContent", "x-goog-api-key")):
        p, t = ok_provider(kind, "ok")
        p.complete(REQ)
        r = t.requests[0]
        body = json.loads(r.body.decode("utf-8"))
        assert r.method == "POST" and r.url.endswith(url_part) and r.url.startswith("https://")
        assert KEY in r.headers[hdr] and KEY not in r.url and KEY.encode() not in r.body       # açar yalnız başlıqda
        assert AZ in json.dumps(body, ensure_ascii=False) and REQ.system in json.dumps(body, ensure_ascii=False)
        assert r.timeout == 5.0
    ap, at = ok_provider("anthropic", "ok")
    ap.complete(LLMRequest("", "p", 7, 5.0))
    b = json.loads(at.requests[0].body)
    assert "system" not in b and b["max_tokens"] == 7 and b["temperature"] == 0 and at.requests[0].headers["anthropic-version"]
    gp, gt = ok_provider("gemini", "ok")
    gp.complete(LLMRequest("", "p", 7, 5.0))
    gb = json.loads(gt.requests[0].body)
    assert "systemInstruction" not in gb and gb["generationConfig"]["maxOutputTokens"] == 7


def test_local_runtime_receives_prompt_system_and_cap():
    p, rt = ok_provider("local", "x")
    p.complete(REQ)
    assert rt.calls == [(REQ.prompt, REQ.system, 100)]


# ------------------------------------------------------------------ normallaşdırma
def test_finish_reason_and_response_model_normalisation():
    p, _ = make("anthropic", anthropic_ok("x", stop="max_tokens", model="claude-x"))
    r = p.complete(REQ)
    assert (r.finish_reason, r.model) == ("length", "claude-x")
    assert make("anthropic", anthropic_ok("x", stop="weird"))[0].complete(REQ).finish_reason == "other"
    assert make("gemini", gemini_ok("x", finish="MAX_TOKENS"))[0].complete(REQ).finish_reason == "length"
    assert make("gemini", gemini_ok("x", finish="SAFETY"))[0].complete(REQ).finish_reason == "other"
    for evil in ("a" * 500, "<script>", "x y", "", None, 5, "../../x"):          # cavabdakı model adı etibarsızdır: sorğu modelinə qayıdır
        assert make("anthropic", anthropic_ok("x", model=evil))[0].complete(REQ).model == "claude-fixture"


@pytest.mark.parametrize("kind,kw", [("openai", dict(prompt_tokens=None, completion_tokens=None)), ("anthropic", dict(input_tokens=None, output_tokens=None)),
                                     ("gemini", dict(prompt_tokens=None, candidate_tokens=None))])
def test_missing_usage_falls_back_to_estimates(kind, kw):
    r = make(kind, OK[kind]("cavab mətni", **kw))[0].complete(REQ)
    assert r.input_tokens > 0 and r.output_tokens > 0


def test_invalid_usage_values_fall_back_and_large_values_are_reported_honestly():
    for bad in (-1, True, "5", 1.5, None, [1]):
        r = make("anthropic", anthropic_ok("x", input_tokens=bad, output_tokens=bad))[0].complete(REQ)       # type: ignore[arg-type]
        assert r.input_tokens > 0 and not isinstance(r.input_tokens, bool)
    big = make("anthropic", anthropic_ok("x", input_tokens=10 ** 9, output_tokens=10 ** 9))[0].complete(REQ)
    assert (big.input_tokens, big.output_tokens) == (10 ** 9, 10 ** 9)           # GuardedLLM/Ledger bunu aşma kimi qeyd edəcək; adapter gizlətmir


def test_multi_block_text_is_concatenated_and_unicode_preserved():
    body = {"content": [{"type": "text", "text": "Əhməd "}, {"type": "text", "text": "🙂 ğ"}], "usage": {"input_tokens": 1, "output_tokens": 2}}
    p, _ = make("anthropic", HttpResponse(200, json.dumps(body).encode()))
    assert p.complete(REQ).text == "Əhməd 🙂 ğ"
    g = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "ə"}]}, "finishReason": "STOP"}]}
    assert make("gemini", HttpResponse(200, json.dumps(g).encode()))[0].complete(REQ).text == "aə"


# ------------------------------------------------------------------ pozulmuş cavablar
def j(obj) -> HttpResponse:
    return HttpResponse(200, json.dumps(obj).encode())


MALFORMED = {
    "openai": [HttpResponse(200, b"not json"), HttpResponse(200, b"[]"), HttpResponse(200, b"null"), HttpResponse(200, b"\xff\xfe"), HttpResponse(200, b""),
               j({}), j({"choices": []}), j({"choices": [{}]}), j({"choices": [{"message": {}}]}), j({"choices": [{"message": {"content": 5}}]}),
               j({"choices": [{"message": {"content": "\ud800"}}]}), HttpResponse(200, b"[" * 100000), HttpResponse(200, b'{"a":' * 50000)],
    "anthropic": [HttpResponse(200, b"x"), j([]), j({}), j({"content": []}), j({"content": "x"}), j({"content": [{"type": "tool_use", "name": "rm"}]}),
                  j({"content": [{"type": "text"}]}), j({"content": [{"type": "text", "text": 7}]}), j({"content": [{"type": "text", "text": "ok"}, "x"]}),
                  j({"content": [{"type": "text", "text": "\ud800"}]}), j({"content": [{"type": "text", "text": "a"}, {"type": "tool_use"}]}), j({"content": [{"type": "tool_use", "text": "x"}]}),
                  j({"content": [{"type": "thinking", "text": "x"}]})],
    "gemini": [HttpResponse(200, b"x"), j({}), j({"candidates": []}), j({"candidates": ["x"]}), j({"candidates": [{}]}), j({"candidates": [{"content": {}}]}),
               j({"candidates": [{"content": {"parts": []}}]}), j({"candidates": [{"content": {"parts": [{"functionCall": {"name": "rm"}}]}}]}),
               j({"candidates": [{"content": {"parts": [{"text": "a", "inlineData": {}}]}}]}), j({"candidates": [{"content": {"parts": [{"text": 3}]}}]}),
               j({"promptFeedback": {"blockReason": "SAFETY"}})],
}


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_malformed_responses_raise_bad_response_with_unknown_charge(kind):
    for resp in MALFORMED[kind]:
        p, _ = make(kind, resp)
        with pytest.raises(LLMBadResponse) as ei:
            p.complete(REQ)
        assert getattr(ei.value, "charged", None) is None and KEY not in str(ei.value)      # naməlum haqq: rezerv qorunur


@pytest.mark.parametrize("kind", ["anthropic", "gemini"])
def test_bad_status_types_and_non_response_from_transport(kind):
    for step in (HttpResponse("200", b"{}"), HttpResponse(True, b"{}"), HttpResponse(200, "text"), lambda r: "not-a-response", lambda r: None):    # type: ignore[arg-type]
        p, _ = make(kind, step)
        with pytest.raises(LLMBadResponse):
            p.complete(REQ)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_oversized_response_rejected_before_parsing(kind):
    huge = HttpResponse(200, b"{" + b" " * MAX_RESPONSE_BYTES + b"}")
    with pytest.raises(LLMBadResponse, match="böyük"):
        make(kind, huge)[0].complete(REQ)
    big_text = "x" * 400_000                                                                     # limit daxilində böyük cavab qəbul olunur
    p, _ = make(kind, OK[kind](big_text))
    assert len(p.complete(REQ).text) == 400_000


# ------------------------------------------------------------------ provayder/nəqliyyat xətaları
@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("status,charged,retry", [(401, False, False), (403, False, False), (429, False, True), (500, None, True), (503, None, True),
                                                  (400, False, False), (404, False, False), (302, False, False)])
def test_status_mapping(kind, status, charged, retry):
    p, _ = make(kind, HttpResponse(status, b'{"error":"x"}'))
    with pytest.raises(LLMProviderError) as ei:
        p.complete(REQ)
    assert (ei.value.charged, ei.value.retryable) == (charged, retry) and KEY not in str(ei.value)
    if status in (401, 403):
        assert "etibarnamə" in str(ei.value)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_timeout_and_connection_errors(kind):
    with pytest.raises(ProviderTimeout) as ei:
        make(kind, TransportError("slow", timeout=True))[0].complete(REQ)
    assert ei.value.charged is None and ei.value.retryable
    with pytest.raises(LLMProviderError) as ei2:
        make(kind, TransportError("refused"))[0].complete(REQ)
    assert ei2.value.charged is False and not isinstance(ei2.value, ProviderTimeout) and "refused" not in str(ei2.value)
    with pytest.raises(LLMProviderError):                                                            # ssenari bitdi
        make(kind)[0].complete(REQ)


@pytest.mark.parametrize("kind", ["anthropic", "gemini"])
def test_endpoint_policy_rechecked_on_every_request(kind):
    p, t = make(kind, OK[kind]("x"))
    p.base_url = "http://insecure.example.com"                                                        # HTTP
    with pytest.raises(LLMProviderError) as ei:
        p.complete(REQ)
    assert ei.value.charged is False and t.requests == []
    for url in ("https://127.0.0.1", "https://localhost", "https://box.internal", "https://a.b/../c"):
        p.base_url = url
        with pytest.raises(LLMProviderError):
            p.complete(REQ)
    assert t.requests == []


def test_keys_never_appear_in_repr_describe_or_errors():
    for kind in ("openai", "anthropic", "gemini"):
        p, _ = make(kind, HttpResponse(401, b"bad"))
        assert KEY not in repr(p) and KEY not in str(p) and KEY not in json.dumps(p.describe())
        with pytest.raises(LLMProviderError) as ei:
            p.complete(REQ)
        assert KEY not in repr(ei.value)


# ------------------------------------------------------------------ konstruktor yoxlamaları
@pytest.mark.parametrize("cls", [AnthropicProvider, GeminiProvider])
def test_constructor_validation(cls):
    t = FixtureTransport()
    for model in ("", "a b", "../x", "a/b", "m:generate", "x" * 81, None, 5, "ə", "a\nb"):
        with pytest.raises(ValueError):
            cls(model, Secret(KEY), t)                                                              # type: ignore[arg-type]
    for key in (KEY, None, ""):
        with pytest.raises(ValueError):
            cls("m", key, t)                                                                         # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cls("m", Secret(KEY), "not callable")                                                       # type: ignore[arg-type]
    for url in ("http://api.example.com", "https://127.0.0.1", "https://user:pw@api.example.com", "https://api.example.com?x=1", "ftp://x.example.com"):
        with pytest.raises(ValueError):
            cls("m", Secret(KEY), t, base_url=url)
    assert cls("m", Secret(KEY), t).kind == "cloud"


# ------------------------------------------------------------------ lokal runtime
def test_local_runtime_errors_and_trust():
    with pytest.raises(ValueError):
        LocalRuntimeProvider(object(), "m")                                                         # etibarsız runtime tipi
    with pytest.raises(ValueError):
        LocalRuntimeProvider(lambda p: "x", "m")                                                    # ixtiyari callable lokal model deyil

    class SubRt(FixtureLocalRuntime):
        pass
    with pytest.raises(ValueError):
        LocalRuntimeProvider(SubRt(), "m")
    unloaded = LocalRuntimeProvider(FixtureLocalRuntime(loaded=False), "m")
    with pytest.raises(LLMProviderError) as ei:
        unloaded.complete(REQ)
    assert ei.value.charged is False and ei.value.retryable
    boom = LocalRuntimeProvider(FixtureLocalRuntime(RuntimeError("daxili detal: gizli")), "m")
    with pytest.raises(LLMProviderError) as ei2:
        boom.complete(REQ)
    assert ei2.value.charged is False and "gizli" not in str(ei2.value)
    for bad in (5, None, "", "\ud800", b"x"):
        with pytest.raises(LLMBadResponse):
            LocalRuntimeProvider(FixtureLocalRuntime(bad), "m").complete(REQ)
    assert LocalRuntimeProvider(FixtureLocalRuntime(lambda p: p.upper()), "m").complete(LLMRequest("", "salam", 5, 1.0)).text == "SALAM"


# ------------------------------------------------------------------ reyestr: dəqiq tip, üç ayrı ölçü
def reg_kw(**kw):
    base = dict(model_id="m1", provider="prov", capabilities=CAPS, context_tokens=8000, max_output_tokens=1000)
    base.update(kw)
    return base


def test_registry_derives_independent_properties_per_adapter():
    r = ModelRegistry()
    expect = {"openai": (Locality.REMOTE, Payment.PAID), "anthropic": (Locality.REMOTE, Payment.PAID), "gemini": (Locality.REMOTE, Payment.PAID),
              "openai_private": (Locality.LOCAL, Payment.FREE), "local": (Locality.LOCAL, Payment.FREE)}
    for i, (kind, (loc, pay)) in enumerate(expect.items()):
        prices = dict(price_in_per_1k=1.0, price_out_per_1k=2.0) if loc is Locality.REMOTE else {}
        d = r.register(make(kind)[0], **reg_kw(model_id=f"m{i}"), **prices)
        assert (d.locality, d.payment) == (loc, pay) and d.reality is Reality.SIMULATED and d.availability is Availability.SIMULATION, kind
    paid_local = r.register(make("local")[0], **reg_kw(model_id="pl", price_in_per_1k=0.1, price_out_per_1k=0.1))
    assert (paid_local.locality, paid_local.payment) == (Locality.LOCAL, Payment.PAID)               # lokal ≠ pulsuz
    for kind in ("anthropic", "gemini", "openai"):
        with pytest.raises(Exception):
            ModelRegistry().register(make(kind)[0], **reg_kw())                                      # uzaq: qiymətsiz rədd


def test_local_runtime_is_never_real_even_with_evidence():
    from kernel.common import Evidence
    from datetime import datetime, timezone
    ev = (Evidence("test", "x", datetime(2026, 1, 1, tzinfo=timezone.utc)),)
    d = ModelRegistry().register(make("local")[0], **reg_kw(evidence=ev))
    assert d.reality is Reality.SIMULATED and d.availability is Availability.SIMULATION


def test_registry_rejects_subclasses_and_drift_for_new_adapters():
    class SubA(AnthropicProvider):
        pass

    class SubG(GeminiProvider):
        pass

    class SubL(LocalRuntimeProvider):
        pass
    t = FixtureTransport()
    for adapter in (SubA("m", Secret(KEY), t), SubG("m", Secret(KEY), t), SubL(FixtureLocalRuntime(), "m")):
        with pytest.raises(RegistryError) as ei:
            ModelRegistry().register(adapter, **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
        assert ei.value.code == "untrusted_adapter_type"
    a = AnthropicProvider("m", Secret(KEY), t)
    r = ModelRegistry()
    r.register(a, **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
    a.kind = "private"                                                                                # uzaq adapter «lokal» elan edilir
    assert r.resolve_for_router("m1") is None
    a2 = AnthropicProvider("m", Secret(KEY), t)
    r2 = ModelRegistry()
    r2.register(a2, **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
    a2.is_mock = True
    assert not r2.reverify("m1")
    lp = LocalRuntimeProvider(FixtureLocalRuntime(), "m")
    r3 = ModelRegistry()
    r3.register(lp, **reg_kw())
    lp.runtime = object()                                                                             # qeydiyyatdan sonra runtime dəyişdirilir
    assert not r3.reverify("m1")


def test_adapter_lies_via_describe_do_not_change_status():
    a = AnthropicProvider("m", Secret(KEY), FixtureTransport())
    a.describe = lambda: {"locality": "local", "payment": "free", "reality": "real", "trusted": True}     # type: ignore[method-assign]
    d = ModelRegistry().register(a, **reg_kw(price_in_per_1k=1, price_out_per_1k=1, max_data_class=DataClass.INTERNAL))
    assert (d.locality, d.payment, d.reality) == (Locality.REMOTE, Payment.PAID, Reality.SIMULATED)


def test_raw_stage17_openai_class_is_not_trusted_by_the_models_registry():
    raw = OpenAICompatibleProvider("https://api.example.com/v1", "m", Secret(KEY), FixtureTransport(), kind="cloud")
    with pytest.raises(RegistryError) as ei:
        ModelRegistry().register(raw, **reg_kw(price_in_per_1k=1, price_out_per_1k=1))
    assert ei.value.code == "untrusted_adapter_type"


def test_fixtures_never_touch_the_network():
    import ast
    import pathlib
    import models.adapters as pkg
    for f in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        mods = {a.name for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, ast.ImportFrom)}
        assert not {m for m in mods if m.split(".")[0] in {"socket", "ssl", "urllib", "http", "requests", "subprocess", "os"}}, f.name


def test_openai_hardening_layers_work_independently(monkeypatch):
    """İki qat: (1) `parse_json_body` ön yoxlaması, (2) əsas sinfin ayrışdırması zamanı RecursionError tutulması (ön yoxlama keçilsə belə: yığın dərinliyi fərqi)."""
    import models.adapters.openai_compat as oc
    deep = HttpResponse(200, b"[" * 100_000)
    monkeypatch.setattr(oc, "parse_json_body", lambda resp: {})                                       # ön yoxlama «keçdi»
    p = OpenAICompatProvider("https://api.example.com/v1", "m", Secret(KEY), FixtureTransport(deep), kind="cloud")
    with pytest.raises(LLMBadResponse):
        p.complete(REQ)                                                                                # 2-ci qat tutur (xam sinif RecursionError verərdi)
    monkeypatch.undo()
    monkeypatch.setattr(oc, "safe_text", lambda v: v)                                                  # 3-cü qat söndürülür: tək surrogat yalnız ön qat tərəfindən deyil, safe_text ilə də tutulmalıdır
    p2 = OpenAICompatProvider("https://api.example.com/v1", "m", Secret(KEY), FixtureTransport(HttpResponse(200, b'{"choices":[{"message":{"content":"\\ud800"}}]}')), kind="cloud")
    assert p2.complete(REQ).text == "\ud800"                                                           # kanarya: safe_text söndürüləndə boşluq açılır => qat real işləyir
    monkeypatch.undo()
    p3 = OpenAICompatProvider("https://api.example.com/v1", "m", Secret(KEY), FixtureTransport(HttpResponse(200, b'{"choices":[{"message":{"content":"\\ud800"}}]}')), kind="cloud")
    with pytest.raises(LLMBadResponse):
        p3.complete(REQ)
