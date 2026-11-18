"""Real AI runtime: OpenAI-uyğun provayder adapteri (nəqliyyat inyeksiya olunur), endpoint siyasəti, env-dən qoşulma, budjet qapısı.
Testlər yalnız fake nəqliyyat və loopback (127.0.0.1) üzərində yerli test serveri istifadə edir; real provayder/açar/ödəniş YOXDUR."""
from safety.execution_context import broker_context
import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agents.hybrid import CloudEngine, LocalEngine, Mode
from agents.llm import (GuardedLLM, LLMBadResponse, LLMConfig, LLMProviderError, LLMRequest, Secret)
from agents.providers import (EndpointError, HttpRequest, HttpResponse, OpenAICompatLocalRuntime, OpenAICompatibleProvider,
                              TransportError, validate_endpoint)
from runtime.env import build_engines
from safety.ledger import CostLedger
from runtime.transport import UrllibTransport

ROOT = Path(__file__).resolve().parent.parent
KEY = "test-" + "credential-value-0123456789abcdef"   # saxta dəyər (heç bir real açar deyil)


def req(prompt="salam", system="sən köməkçisən"):
    return LLMRequest(system, prompt, 64, 5.0)


class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, r: HttpRequest) -> HttpResponse:
        self.calls.append(r)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def ok_body(text="cavab", pin=7, pout=3):
    return HttpResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": pin, "completion_tokens": pout}, "model": "m-1"}).encode())


# --- endpoint siyasəti ---

@pytest.mark.parametrize("url", ["https://api.example.com", "https://api.example.com/v1", "https://api.example.com/v1/"])
def test_cloud_endpoints_must_be_https_dns_names(url):
    assert validate_endpoint(url, "cloud").startswith("https://api.example.com")


@pytest.mark.parametrize("url", ["http://api.example.com", "https://user:pw@api.example.com", "https://127.0.0.1", "https://localhost",
                                 "https://10.0.0.5", "https://api.example.com:8443/../x", "ftp://x.y", "https://api.example.com?x=1",
                                 "https://api.example.com#f", "", "https://", "javascript:alert(1)"])
def test_bad_cloud_endpoints_are_rejected(url):
    with pytest.raises(EndpointError):
        validate_endpoint(url, "cloud")


def test_private_endpoints_allow_loopback_and_private_networks_but_never_public_hosts():
    for url in ("http://127.0.0.1:11434", "http://localhost:8080", "http://192.168.1.20:8000", "http://10.1.2.3:9000",
                "http://172.16.5.4:8000", "http://myserver.local:8000", "http://ollama:11434", "https://nas.lan:8443"):
        validate_endpoint(url, "private")
    for url in ("http://8.8.8.8", "http://api.example.com", "http://169.254.169.254", "https://u:p@192.168.1.1",
                "https://api.example.com", "https://api.openai.com/v1", "https://8.8.8.8"):          # ictimai DNS/IP (HTTPS ilə də) LOKAL deyil: ödənişli uzaq servis ola bilər
        with pytest.raises(EndpointError):
            validate_endpoint(url, "private")


# --- provayder adapteri ---

def test_provider_sends_an_openai_compatible_request_and_parses_the_response():
    t = FakeTransport([ok_body("salam dünya")])
    p = OpenAICompatibleProvider("https://api.example.com", "m-1", Secret(KEY), t, kind="cloud")
    res = p.complete(req())
    assert (res.text, res.input_tokens, res.output_tokens, res.model) == ("salam dünya", 7, 3, "m-1")
    assert p.is_mock is False and p.name == "openai_compat"
    r = t.calls[0]
    assert r.method == "POST" and r.url == "https://api.example.com/v1/chat/completions" and r.timeout == 5.0
    assert r.headers["Authorization"] == f"Bearer {KEY}" and r.headers["Content-Type"] == "application/json"
    body = json.loads(r.body)
    assert body["model"] == "m-1" and body["max_tokens"] == 64 and body["stream"] is False
    assert body["messages"] == [{"role": "system", "content": "sən köməkçisən"}, {"role": "user", "content": "salam"}]
    OpenAICompatibleProvider("https://api.example.com/v1", "m", Secret(KEY), FakeTransport([ok_body()]), kind="cloud").complete(req())


def test_private_provider_needs_no_key_and_sends_no_authorization_header():
    t = FakeTransport([ok_body()])
    p = OpenAICompatibleProvider("http://127.0.0.1:11434", "small", None, t, kind="private", name="local_openai_compat")
    p.complete(req())
    assert "Authorization" not in t.calls[0].headers
    with pytest.raises(ValueError):
        OpenAICompatibleProvider("https://api.example.com", "m", None, t, kind="cloud")  # cloud üçün açar məcburidir


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (500, True), (503, True), (400, False), (404, False)])
def test_http_errors_map_to_provider_errors_without_leaking_credentials_or_bodies(status, retryable):
    leak = f"echo {KEY} secret-body"
    p = OpenAICompatibleProvider("https://api.example.com", "m", Secret(KEY), FakeTransport([HttpResponse(status, leak.encode())]), kind="cloud")
    with pytest.raises(LLMProviderError) as e:
        p.complete(req())
    assert e.value.retryable is retryable and KEY not in str(e.value) and "secret-body" not in str(e.value) and str(status) in str(e.value)


def test_malformed_responses_and_transport_failures_are_handled():
    for body in (b"not json", b"{}", json.dumps({"choices": []}).encode(), json.dumps({"choices": [{"message": {"content": 5}}]}).encode(),
                 json.dumps({"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": -1, "completion_tokens": "a"}}).encode()):
        p = OpenAICompatibleProvider("https://api.example.com", "m", Secret(KEY), FakeTransport([HttpResponse(200, body)]), kind="cloud")
        try:
            r = p.complete(req())
            assert r.input_tokens >= 0 and r.output_tokens >= 0       # yanlış usage: təxmin ilə əvəz olunur
        except (LLMBadResponse, LLMProviderError):
            pass
    for exc, retry in ((TransportError("timeout", timeout=True), True), (TransportError("refused"), True)):
        p = OpenAICompatibleProvider("https://api.example.com", "m", Secret(KEY), FakeTransport([exc]), kind="cloud")
        with pytest.raises(LLMProviderError) as e:
            p.complete(req())
        assert e.value.retryable is retry and KEY not in str(e.value)


def test_provider_reprs_never_contain_the_key():
    p = OpenAICompatibleProvider("https://api.example.com", "m", Secret(KEY), FakeTransport([ok_body()]), kind="cloud")
    assert KEY not in repr(p) and KEY not in str(p) and KEY not in json.dumps(p.describe())


def test_guarded_llm_and_cloud_engine_treat_the_adapter_as_a_real_provider(tmp_path):
    t = FakeTransport([ok_body("real cavab")])
    prov = OpenAICompatibleProvider("https://api.example.com", "m-1", Secret(KEY), t, kind="cloud")
    client = GuardedLLM(prov, LLMConfig(provider="openai_compat", api_key=Secret(KEY)), log_path=tmp_path / "llm.jsonl", stop_path=tmp_path / "STOP")
    eng = CloudEngine(client)
    st = eng.status()
    assert st.state == "ready" and st.is_mock is False and eng.consent_scopes() == ("cloud_paid_use",)    # razılıq tələb olunur
    with broker_context():                                      # real provayder yalnız Broker daxilində (A-8)
        assert eng.generate("salam", "", "r1").is_mock is False
    assert KEY not in (tmp_path / "llm.jsonl").read_text()


def test_local_runtime_adapter_exposes_the_local_runtime_interface():
    t = FakeTransport([ok_body("lokal real")])
    rt = OpenAICompatLocalRuntime(OpenAICompatibleProvider("http://127.0.0.1:11434", "small", None, t, kind="private",
                                                           name="local_openai_compat"), languages=("en", "az"))
    assert rt.loaded() and rt.is_mock is False and rt.languages == ("en", "az")
    assert rt.generate("hello", "", 32) == "lokal real"
    eng = LocalEngine(rt, consent_scope=None)
    assert eng.status().state == "ready" and eng.supports(__import__("agents.hybrid", fromlist=["classify_task"]).classify_task("chat", "hello how are you"))[0]


# --- həqiqi HTTP (loopback): nəqliyyat + adapter uçdan-uca ---

class _Handler(BaseHTTPRequestHandler):
    seen = []
    mode = "ok"

    def log_message(self, *a):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append((self.path, self.headers.get("Authorization"), body))
        if _Handler.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://evil.example/")
            self.end_headers()
            return
        if _Handler.mode == "big":
            payload = b"x" * 3_000_000
        elif _Handler.mode == "429":
            self.send_response(429)
            self.end_headers()
            return
        elif _Handler.mode == "slow":
            threading.Event().wait(1.5)
            payload = b"{}"
        else:
            payload = json.dumps({"choices": [{"message": {"content": "loopback cavabı"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def loopback():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.seen, _Handler.mode = [], "ok"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_urllib_transport_talks_to_a_real_local_server_end_to_end(loopback):
    prov = OpenAICompatibleProvider(loopback, "small", None, UrllibTransport(), kind="private", name="local_openai_compat")
    res = prov.complete(req("salam"))
    assert res.text == "loopback cavabı" and (res.input_tokens, res.output_tokens) == (2, 3)
    path, auth, body = _Handler.seen[0]
    assert path == "/v1/chat/completions" and auth is None and body["messages"][-1]["content"] == "salam"


def test_urllib_transport_refuses_redirects_oversized_bodies_and_reports_timeouts(loopback):
    prov = OpenAICompatibleProvider(loopback, "small", None, UrllibTransport(max_bytes=1_000_000), kind="private", name="local_openai_compat")
    _Handler.mode = "redirect"
    with pytest.raises(LLMProviderError):
        prov.complete(req())
    _Handler.mode = "big"
    with pytest.raises(LLMProviderError):
        prov.complete(req())
    _Handler.mode = "429"
    with pytest.raises(LLMProviderError) as e:
        prov.complete(req())
    assert e.value.retryable is True
    _Handler.mode = "slow"
    with pytest.raises(LLMProviderError) as e:
        prov.complete(LLMRequest("", "x", 8, 0.3))
    assert e.value.retryable is True


def test_transport_connection_refused_is_a_transport_error():
    with pytest.raises(TransportError):
        UrllibTransport()(HttpRequest("POST", "http://127.0.0.1:9/x", {}, b"{}", 1.0))


# --- env-dən qoşulma: 0 AZN prioritet ---

def env(**kw):
    base = {"NAYMERA_CLOUD_PROVIDER": "openai_compat", "NAYMERA_CLOUD_BASE_URL": "https://api.example.com", "NAYMERA_LLM_MODEL": "m-1",
            "NAYMERA_LLM_API_KEY": KEY, "NAYMERA_LLM_COST_LIMIT": "1.0", "NAYMERA_LLM_PRICE_IN_PER_1K": "0.01",
            "NAYMERA_LLM_PRICE_OUT_PER_1K": "0.02"}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def build(e, tmp, transport=None, ledger=True):
    return build_engines(e, log_dir=tmp, stop_path=tmp / "STOP", transport=transport or FakeTransport([ok_body()]),
                         ledger=CostLedger(tmp / "ledger.db") if ledger else None)


def test_nothing_configured_means_not_configured_everywhere(tmp_path):
    rt = build({}, tmp_path)
    assert rt.cloud.status().state == "not_configured" and rt.local.status().state == "not_configured"
    assert any("cloud" in n for n in rt.notes) and any("local" in n for n in rt.notes)


def test_fully_configured_cloud_is_ready_but_needs_consent(tmp_path):
    rt = build(env(), tmp_path)
    st = rt.cloud.status()
    assert st.state == "ready" and st.is_mock is False and st.consent_needed == ("cloud_paid_use",)


@pytest.mark.parametrize("missing,reason", [("NAYMERA_LLM_API_KEY", "açar"), ("NAYMERA_CLOUD_BASE_URL", "url"), ("NAYMERA_LLM_COST_LIMIT", "büdcə"),
                                            ("NAYMERA_LLM_PRICE_IN_PER_1K", "qiymət"), ("NAYMERA_LLM_PRICE_OUT_PER_1K", "qiymət"), ("NAYMERA_LLM_MODEL", "model")])
def test_cloud_stays_not_configured_without_each_required_setting(tmp_path, missing, reason):
    rt = build(env(**{missing: None}), tmp_path)
    st = rt.cloud.status()
    assert st.state == "not_configured" and reason in " ".join(rt.notes).lower()


def test_real_cloud_needs_a_cost_ledger_so_spending_is_actually_limited(tmp_path):
    rt = build(env(), tmp_path, ledger=False)
    assert rt.cloud.status().state == "not_configured" and "ledger" in " ".join(rt.notes).lower()


def test_zero_or_invalid_budget_keeps_real_cloud_off(tmp_path):
    for value in ("0", "-1", "abc", "nan"):
        assert build(env(NAYMERA_LLM_COST_LIMIT=value), tmp_path).cloud.status().state == "not_configured"


def test_bad_cloud_url_or_unknown_provider_is_not_configured_and_never_raises(tmp_path):
    assert build(env(NAYMERA_CLOUD_BASE_URL="http://api.example.com"), tmp_path).cloud.status().state == "not_configured"
    assert build(env(NAYMERA_CLOUD_PROVIDER="mystery"), tmp_path).cloud.status().state == "not_configured"


def test_mock_cloud_provider_can_be_selected_explicitly_and_is_a_simulation(tmp_path):
    st = build({"NAYMERA_CLOUD_PROVIDER": "mock"}, tmp_path).cloud.status()
    assert st.state == "simulation" and st.is_mock is True


def test_private_local_server_is_configured_from_env_and_is_real(tmp_path):
    rt = build({"NAYMERA_LOCAL_AI_URL": "http://192.168.1.20:8000", "NAYMERA_LOCAL_AI_MODEL": "small", "NAYMERA_LOCAL_AI_LANGUAGES": "en,tr"}, tmp_path)
    st = rt.local.status()
    assert st.state == "ready" and st.is_mock is False and st.model == "small" and st.consent_needed == ()
    assert rt.local.supports(__import__("agents.hybrid", fromlist=["classify_task"]).classify_task("chat", "merhaba nasılsın"))[0]
    assert build({"NAYMERA_LOCAL_AI_URL": "http://8.8.8.8"}, tmp_path).local.status().state == "not_configured"      # açıq host + düz HTTP


def test_keys_never_appear_in_engine_status_notes_or_reprs(tmp_path):
    rt = build(env(NAYMERA_LOCAL_AI_URL="http://127.0.0.1:11434"), tmp_path)
    blob = json.dumps([rt.cloud.status().__dict__, rt.local.status().__dict__, rt.notes], default=str) + repr(rt)
    assert KEY not in blob


# --- təhlükəsizlik: statik ---

def test_agents_providers_has_no_network_env_or_process_access_and_runtime_is_the_only_network_layer():
    tree = ast.parse((ROOT / "agents" / "providers.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m.split(".")[0] for m in mods if m} & {"socket", "http", "urllib", "requests", "ssl", "subprocess", "os"}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"environ", "getenv"}
    for path in (ROOT / "api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "runtime.transport" not in text and "UrllibTransport" not in text, path.name   # API qatı şəbəkə kodu ilə işləmir
