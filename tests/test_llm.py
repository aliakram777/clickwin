"""LLM provider qatı. Yalnız MockProvider: heç bir real API, şəbəkə və ya ödəniş yoxdur.
Testlərdəki "API açarları" uydurma sətirlərdir."""
import ast
import copy
import json
import pickle
from pathlib import Path

import pytest

from agents.llm import (
    DEFAULT_REGISTRY, ConfigError, GuardedLLM, LLMBadResponse, LLMBlocked, LLMBudgetExceeded, LLMConfig,
    LLMError, LLMProviderError, LLMRequest, LLMResponse, LLMTimeout, LLMTooLarge, LLMUnavailable,
    MockProvider, ProviderRegistry, Secret, create_provider, estimate_tokens, load_config, redact,
)
from safety.execution_context import broker_context
from safety.ledger import CostLedger

ROOT = Path(__file__).resolve().parent.parent
FAKE_KEY = "sk-test-FAKEKEY1234567890abcdef"


def cfg(**kw):
    base = dict(provider="mock", timeout_s=1.0, max_output_tokens=50, max_total_tokens=10_000,
                max_prompt_chars=1000, max_response_chars=500)
    base.update(kw)
    return LLMConfig(**base)


@pytest.fixture
def make(tmp_path):
    log = tmp_path / "llm.jsonl"

    def build(provider=None, config=None, **kw):
        c = config or cfg()
        return GuardedLLM(provider or MockProvider(default="salam"), c, log_path=log,
                          stop_path=kw.pop("stop_path", tmp_path / "STOP"), **kw), log

    return build


def log_rows(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# --- Secret və konfiqurasiya ---

def test_secret_never_reveals_itself():
    s = Secret(FAKE_KEY)
    for text in (repr(s), str(s), f"{s}", f"{s!r}", format(s, ">20"), json.dumps({"k": str(s)})):
        assert FAKE_KEY not in text and "FAKEKEY" not in text
    assert s.reveal() == FAKE_KEY and bool(s) is True
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(TypeError):
        copy.deepcopy(s)
    with pytest.raises(ValueError):
        Secret("")
    with pytest.raises(ValueError):
        Secret(5)


def test_load_config_defaults_are_safe_and_key_optional():
    c = load_config({})
    assert c.provider == "none" and c.api_key is None
    assert c.timeout_s > 0 and c.max_output_tokens > 0 and c.max_total_tokens > 0


def test_load_config_reads_environment_mapping_without_leaking_key():
    env = {"NAYMERA_LLM_PROVIDER": "Mock", "NAYMERA_LLM_API_KEY": f"  {FAKE_KEY}  ",
           "NAYMERA_LLM_MODEL": "m-1", "NAYMERA_LLM_TIMEOUT_S": "2.5", "NAYMERA_LLM_MAX_OUTPUT_TOKENS": "77",
           "NAYMERA_LLM_MAX_TOTAL_TOKENS": "999", "OTHER": "x"}
    c = load_config(env)
    assert (c.provider, c.model, c.timeout_s, c.max_output_tokens, c.max_total_tokens) == \
        ("mock", "m-1", 2.5, 77, 999)
    assert c.api_key.reveal() == FAKE_KEY
    for text in (repr(c), str(c), json.dumps(c.describe())):
        assert FAKE_KEY not in text and "FAKEKEY" not in text
    assert c.describe()["key_configured"] is True


def test_blank_key_counts_as_missing():
    assert load_config({"NAYMERA_LLM_API_KEY": "   "}).api_key is None


@pytest.mark.parametrize("var,val", [
    ("NAYMERA_LLM_TIMEOUT_S", "abc"), ("NAYMERA_LLM_TIMEOUT_S", "-1"), ("NAYMERA_LLM_TIMEOUT_S", "nan"),
    ("NAYMERA_LLM_MAX_OUTPUT_TOKENS", "0"), ("NAYMERA_LLM_MAX_TOTAL_TOKENS", "1.5"),
    ("NAYMERA_LLM_MAX_PROMPT_CHARS", "true"),
])
def test_invalid_numeric_config_rejected_without_echoing_value(var, val):
    with pytest.raises(ConfigError) as exc:
        load_config({var: val})
    assert var in str(exc.value) and val not in str(exc.value)


def test_config_validation():
    for bad in ({"timeout_s": 0}, {"max_output_tokens": -1}, {"max_total_tokens": True},
                {"price_in_per_1k": -1}, {"cost_limit": -1}):
        with pytest.raises(ValueError):
            cfg(**bad)


# --- provayder seçimi ---

def test_no_provider_selected_is_a_clear_unavailable_error():
    with pytest.raises(LLMUnavailable) as exc:
        create_provider(load_config({}))
    assert "NAYMERA_LLM_PROVIDER" in str(exc.value)


def test_mock_provider_needs_no_key_and_is_labeled():
    p = create_provider(cfg())
    assert isinstance(p, MockProvider) and p.is_mock is True and p.name == "mock"


def test_unknown_and_real_providers_are_unavailable_even_with_key():
    for name in ("teleport", "openai", "anthropic"):
        with pytest.raises(LLMUnavailable) as exc:
            create_provider(cfg(provider=name, api_key=Secret(FAKE_KEY)))
        assert FAKE_KEY not in str(exc.value)
    with pytest.raises(LLMUnavailable) as exc:
        create_provider(cfg(provider="openai", api_key=Secret(FAKE_KEY)))
    assert "real" in str(exc.value) and "mərhələ" in str(exc.value)


def test_providers_are_swappable_and_key_requirement_is_enforced():
    reg = ProviderRegistry()
    reg.register("mock", lambda c: MockProvider(default="A"), requires_key=False)
    reg.register("fake-keyed", lambda c: MockProvider(default=f"B:{c.model}"), requires_key=True)
    assert create_provider(cfg(), reg).complete(LLMRequest("", "x", 10, 1.0)).text == "A"
    keyed = cfg(provider="fake-keyed", model="m")
    with pytest.raises(LLMUnavailable) as exc:
        create_provider(keyed, reg)
    assert "NAYMERA_LLM_API_KEY" in str(exc.value)
    keyed = cfg(provider="fake-keyed", model="m", api_key=Secret(FAKE_KEY))
    assert create_provider(keyed, reg).complete(LLMRequest("", "x", 10, 1.0)).text == "B:m"
    with pytest.raises(ValueError):
        reg.register("mock", lambda c: None)
    assert set(reg.names()) == {"mock", "fake-keyed"}
    assert DEFAULT_REGISTRY.names() == ("mock",)


# --- MockProvider ---

def test_mock_script_in_order_with_exceptions_and_callables():
    p = MockProvider(script=["bir", LLMProviderError("xəta"), lambda req: req.prompt.upper()])
    req = LLMRequest("s", "abc", 10, 1.0)
    assert p.complete(req).text == "bir"
    with pytest.raises(LLMProviderError):
        p.complete(req)
    assert p.complete(req).text == "ABC"
    with pytest.raises(LLMProviderError):  # ssenari bitdi, default yoxdur
        p.complete(req)
    assert len(p.calls) == 4


# --- GuardedLLM ---

def test_success_returns_text_usage_and_logs_without_prompt_text(make):
    client, log = make(MockProvider(default="cavab", input_tokens=7, output_tokens=3))
    res = client.complete("məxfi sorğu mətni", system="sistem")
    assert (res.text, res.input_tokens, res.output_tokens, res.provider, res.is_mock) == \
        ("cavab", 7, 3, "mock", True)
    row = log_rows(log)[0]
    assert row["status"] == "ok" and row["provider"] == "mock" and row["input_tokens"] == 7
    assert len(row["prompt_sha256"]) == 64 and "məxfi" not in json.dumps(row) and "cavab" not in json.dumps(row)
    assert client.tokens_used == 10


def test_oversized_prompt_rejected_before_provider_call(make):
    p = MockProvider(default="x")
    client, log = make(p)
    with pytest.raises(LLMTooLarge):
        client.complete("a" * 1001)
    assert p.calls == [] and log_rows(log)[0]["status"] == "rejected"
    for bad in (None, 5, b"x"):
        with pytest.raises(LLMError):
            client.complete(bad)


def test_oversized_response_is_rejected_and_not_returned(make):
    client, log = make(MockProvider(default="x" * 501, output_tokens=10))
    with pytest.raises(LLMTooLarge):
        client.complete("p")
    assert log_rows(log)[0]["status"] == "response_too_large" and client.tokens_used > 0  # token xərclənib


def test_token_budget_blocks_before_calling_provider(make):
    p = MockProvider(default="ok", input_tokens=40, output_tokens=10)
    client, log = make(p, cfg(max_total_tokens=120, max_output_tokens=50))
    client.complete("a" * 30)               # rezerv ~10+50=60 <= 120, faktiki 50
    assert client.tokens_used == 50
    client.complete("b" * 30)               # 50 + 60 <= 120
    assert client.tokens_used == 100
    with pytest.raises(LLMBudgetExceeded):   # 100 + 60 > 120
        client.complete("c" * 30)
    assert len(p.calls) == 2 and log_rows(log)[-1]["status"] == "budget_exceeded"


def test_provider_overshooting_output_tokens_is_bad_response_but_counted(make):
    client, log = make(MockProvider(default="x", input_tokens=5, output_tokens=999), cfg(max_output_tokens=50))
    with pytest.raises(LLMBadResponse):
        client.complete("p")
    assert client.tokens_used == 1004


@pytest.mark.parametrize("bad", [
    "mətn", None, {"text": "x"},
    LLMResponse("x", -1, 1, "m"), LLMResponse("x", 1, True, "m"), LLMResponse(5, 1, 1, "m"),
])
def test_invalid_provider_responses_rejected(make, bad):
    client, log = make(_RawProvider(bad))
    with pytest.raises(LLMBadResponse):
        client.complete("p")
    assert log_rows(log)[-1]["status"] == "bad_response"


class _RawProvider:
    name, is_mock = "raw", True

    def __init__(self, ret):
        self.ret = ret

    def complete(self, request):
        return self.ret


def test_timeout_is_enforced_and_logged(make):
    client, log = make(MockProvider(default="gec", delay=0.4), cfg(timeout_s=0.05))
    with pytest.raises(LLMTimeout):
        client.complete("p")
    assert log_rows(log)[0]["status"] == "timeout"


def test_provider_errors_are_wrapped_and_redacted(make):
    boom = RuntimeError(f"401 Unauthorized for key {FAKE_KEY} Authorization: Bearer abcdef1234567890")
    client, log = make(_RawRaising(boom), cfg(api_key=Secret(FAKE_KEY)))
    with broker_context(), pytest.raises(LLMProviderError) as exc:      # real (mock olmayan) provayder yalnız Broker daxilində çağırılır (A-8)
        client.complete("p")
    assert FAKE_KEY not in str(exc.value) and "abcdef1234567890" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True
    assert FAKE_KEY not in json.dumps(log_rows(log)) and "abcdef1234567890" not in json.dumps(log_rows(log))


class _RawRaising:
    name, is_mock = "raw", False

    def __init__(self, exc):
        self.exc = exc

    def complete(self, request):
        raise self.exc


def test_stop_flag_blocks_llm_calls(make, tmp_path):
    p = MockProvider(default="x")
    client, log = make(p)
    (tmp_path / "STOP").write_text("stop")
    with pytest.raises(LLMBlocked):
        client.complete("p")
    assert p.calls == [] and log_rows(log)[0]["status"] == "blocked"


def test_all_llm_errors_share_a_base():
    for exc in (LLMUnavailable, LLMTimeout, LLMProviderError, LLMBadResponse, LLMTooLarge, LLMBudgetExceeded,
                LLMBlocked):
        assert issubclass(exc, LLMError)


# --- ledger ilə xərc ---

def priced(**kw):
    base = dict(price_in_per_1k=1.0, price_out_per_1k=2.0, max_output_tokens=50)
    base.update(kw)
    return cfg(**base)


def test_cost_is_reserved_then_committed_in_ledger(make, tmp_path):
    ledger = CostLedger(tmp_path / "l.db")
    client, _ = make(MockProvider(default="ok", input_tokens=90, output_tokens=10), priced(), ledger=ledger)
    client.complete("x" * 300)
    e = ledger.entries()[0]
    assert e.agent == "llm" and e.status == "committed" and e.amount == pytest.approx(0.09 + 0.02)
    assert e.reserved == pytest.approx(0.1 + 0.1)


def test_cost_limit_blocks_before_provider_call(make, tmp_path):
    ledger = CostLedger(tmp_path / "l.db")
    p = MockProvider(default="ok")
    client, log = make(p, priced(cost_limit=0.15), ledger=ledger)
    with pytest.raises(LLMBudgetExceeded):
        client.complete("x" * 300)  # təxmini 0.2 > 0.15
    assert p.calls == [] and ledger.entries() == []


def test_timeout_keeps_cost_reserved_and_flagged_unknown(make, tmp_path):
    ledger = CostLedger(tmp_path / "l.db")
    client, _ = make(MockProvider(default="x", delay=0.4), priced(timeout_s=0.05), ledger=ledger)
    with pytest.raises(LLMTimeout):
        client.complete("x" * 30)
    assert [e.status for e in ledger.unknown_entries()] == ["reserved"]  # silinmir, əl ilə uzlaşdırılır


def test_unknown_provider_error_keeps_reservation_but_uncharged_error_releases(make, tmp_path):
    ledger = CostLedger(tmp_path / "l.db")
    client, _ = make(MockProvider(script=[RuntimeError("naməlum"), LLMProviderError("rədd", charged=False)]),
                     priced(), ledger=ledger)
    for _ in range(2):
        with pytest.raises(LLMProviderError):
            client.complete("x" * 30)
    st = sorted(e.status for e in ledger.entries())
    assert st == ["released", "reserved"] and len(ledger.unknown_entries()) == 1


# --- redaksiya ---

@pytest.mark.parametrize("raw", [
    f"key={FAKE_KEY}", "Authorization: Bearer abcdef1234567890xyz", '{"api_key": "abcd1234efgh5678"}',
    "x-api-key: abcd1234efgh5678", "token=abcd1234efgh5678", "sk-ant-api03-ABCDEFGHIJKLMNOP",
])
def test_redact_masks_secret_like_strings(raw):
    out = redact(raw)
    assert "abcd1234efgh5678" not in out and "abcdef1234567890xyz" not in out
    assert "FAKEKEY" not in out and "ABCDEFGHIJKLMNOP" not in out and "***" in out


def test_redact_masks_the_configured_secret_anywhere():
    assert "unikal-sirr-qiymeti" not in redact("xəta: unikal-sirr-qiymeti burada", [Secret("unikal-sirr-qiymeti")])
    assert redact("adi mətn") == "adi mətn"


# --- statik yoxlamalar ---

def test_llm_modules_use_no_network_and_read_env_only_in_load_config():
    banned = {"requests", "urllib", "http", "httpx", "socket", "subprocess", "aiohttp", "openai", "anthropic",
              "ssl", "ctypes"}
    tree = ast.parse((ROOT / "agents" / "llm.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not banned & {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            p = node
            while p in parents and not isinstance(p, ast.FunctionDef):
                p = parents[p]
            assert isinstance(p, ast.FunctionDef) and p.name == "load_config", node.lineno


def test_estimate_tokens_is_monotonic_and_positive():
    assert estimate_tokens("") == 0 and estimate_tokens("a") == 1
    assert estimate_tokens("a" * 300) == 100
