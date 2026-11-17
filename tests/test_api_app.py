"""Android API: söhbət, hadisələr, bildirişlər, yaddaş, şirkət (yalnız oxu), CEO işləri, təsdiq (step-up), səs, sistem.
Simulyasiya olunan "cihaz" və "sahib" test obyektləridir; real Android cihazı, real LLM, ödəniş və xarici çağırış yoxdur."""
import ast
import base64
import json
from pathlib import Path

import pytest

from agents.ceo import CEO, RulePlanner
from agents.company import Authority, Company
from agents.durable import DurableRunner, WorkflowStore
from agents.llm import GuardedLLM, LLMConfig, LLMProviderError, MockProvider
from agents.llm_planner import LLMPlanner
from agents.memory import MemoryService
from agents.mobile import EventStore, MockVoiceSynthesizer
from agents.notify_store import NotificationStore
from agents.orchestrator import Orchestrator
from agents.proactive import NotificationPolicy, ProactiveAnalyzer, ProactiveEngine, Thresholds
from agents.registry import Registry
from agents.spec import AgentSpec
from api.app import ApiApp
from api.auth import AuthService
from api.chat import ChatService, ConversationStore, LLMReplyEngine, RuleAssistant
from api.events import ApprovalEventChannel
from api.http import Request
from api.jobs import JobService
from safety.approvals import ApprovalService, Principal
from safety.ledger import BudgetPolicy, CostLedger, reconcile
from tests.p256_tools import Keypair

ROOT = Path(__file__).resolve().parent.parent
OWNER_P = Principal("owner", "human", "web")  # sahib konsolu (test obyekti)
PLAN_OPS = '{"steps":[{"id":"a","department":"operations","permission":"read_files"}]}'
PLAN_FIN = ('{"steps":[{"id":"a","department":"operations","permission":"read_files"},'
            '{"id":"b","department":"finance","permission":"spend_money","depends_on":["a"]}]}')


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def b64(b):
    return base64.b64encode(b).decode()


class Dev:
    """Android cihazının simulyasiyası: yalnız HTTP API ilə danışır."""

    def __init__(self, app, auth, name="Pixel 8", remote="10.0.0.5"):
        self.app, self.remote = app, remote
        self.kp = Keypair()  # Keystore simulyasiyası: şəxsi açar cihazda qalır
        code = auth.create_pairing_code()
        body = {"code": code, "device_name": name, "public_key": b64(self.kp.spki()),
                "signature": b64(self.kp.sign(AuthService.pair_message(code)))}
        r = app.handle(Request("POST", "/v1/pair", {}, {"content-type": "application/json"},
                               json.dumps(body).encode(), remote))
        assert r.status == 200, r.body
        self.paired = r.body
        self.access, self.refresh = r.body["access_token"], r.body["refresh_token"]
        self.device_id = r.body["device_id"]

    def call(self, method, path, body=None, query=None, token="own", headers=None):
        h = {"content-type": "application/json"}
        if token == "own":
            token = self.access
        if token:
            h["authorization"] = f"Bearer {token}"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else b""
        return self.app.handle(Request(method, path, query or {}, h, data, self.remote))

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, {} if body is None else body, **kw)

    def sign(self, ch, action, subject, payload_hash, decision):
        """Biometrik açılışdan sonra Keystore açarı ilə imza (simulyasiya)."""
        return b64(self.kp.sign(AuthService.step_up_message(ch["challenge_id"], ch["nonce"], action, subject,
                                                            payload_hash, decision)))

    def decide(self, approval_id, decision="approve", method="biometric", proof_override=None):
        ch = self.post(f"/v1/approvals/{approval_id}/challenge", {"decision": decision})
        if ch.status != 200:
            return ch
        b = ch.body
        proof = proof_override or self.sign(b, "approval", approval_id, b["approval"]["payload_hash"], decision)
        return self.post(f"/v1/approvals/{approval_id}/decision",
                         {"decision": decision, "challenge_id": b["challenge_id"], "proof": proof, "method": method})


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    audit = tmp_path / "audit.jsonl"
    stop = tmp_path / "STOP"
    reg = Registry(":memory:")
    company = Company(reg, tmp_path / "company.db", audit, owners={"owner"})

    def spec(name, perms):
        return AgentSpec(name=name, purpose="p", tools=[], permissions=list(perms), inputs=["sorgu"],
                         outputs=["netice"], cost_limit=5.0)

    company.admin.register_agent(spec("ops_agent", ["read_files"]), "operations", "Əməliyyat",
                                 Authority(frozenset({"read_files"}), max_task_cost=1.0), 5.0, OWNER_P)
    company.admin.register_agent(spec("fin_agent", ["read_files", "spend_money"]), "finance", "Maliyyə",
                                 Authority(frozenset({"read_files", "spend_money"}), True, max_task_cost=1.0), 5.0,
                                 OWNER_P)
    events = EventStore(tmp_path / "events.db", clock=clock)
    ledger = CostLedger(tmp_path / "ledger.db")
    svc = ApprovalService(tmp_path / "approvals.db", audit, owners={"owner"}, ttl_seconds=3600, clock=clock,
                          channels=[ApprovalEventChannel(events)])
    calls = []
    orch = Orchestrator(reg, lambda s, d: calls.append(s.name) or {"netice": f"{s.name}-ok"}, audit_path=audit,
                        stop_path=stop, errors_path=tmp_path / "e.jsonl", ledger=ledger, approvals=svc.client,
                        policy=company.view.budget_policy(global_limit=20.0))
    wf = WorkflowStore(tmp_path / "wf.db", clock=clock, lease_s=60)
    durable = DurableRunner(CEO(orch, RulePlanner([]), budget=50.0), wf)
    memory = MemoryService(tmp_path / "mem.db", tmp_path / "mem_audit.jsonl", clock=clock)
    pro = ProactiveEngine(
        ProactiveAnalyzer(clock=clock, ledger=ledger, budget=company.view.budget_policy(global_limit=20.0),
                          approvals=svc.reader, workflows=wf, memory=memory.reader, stop_path=stop,
                          thresholds=Thresholds(approval_wait_s=60)),
        NotificationStore(tmp_path / "notif.db", clock=clock), NotificationPolicy(), clock=clock)
    auth = AuthService(tmp_path / "auth.db", audit, clock=clock, owner_id="owner", access_ttl=7200)

    def build(plan=PLAN_OPS, allow_sensitive=False, engine=None, voice=True, ai=None, voice_registry=None):
        provider = MockProvider(default=plan)
        client = GuardedLLM(provider, LLMConfig(provider="mock", max_response_chars=5000),
                            log_path=tmp_path / "llm.jsonl", stop_path=stop)
        planner = LLMPlanner(client, company.view.department_costs(), allow_sensitive=allow_sensitive)
        jobs = JobService(planner, durable, tmp_path / "jobs.db", clock=clock)
        chat = ChatService(ConversationStore(tmp_path / "chat.db", clock=clock),
                           engine or RuleAssistant(lambda: {"pending_approvals": len(svc.reader.pending())}))
        app = ApiApp(auth=auth, chat=chat, events=events, stop_path=stop, gateway=svc.gateway, reader=svc.reader,
                     memory=memory, company=company.view, proactive=pro, jobs=jobs,
                     voice=MockVoiceSynthesizer() if voice else None, owner_id="owner", clock=clock,
                     audit_path=audit, llm_is_mock=True, ai=ai, voice_registry=voice_registry)
        return app

    class W:
        pass

    w = W()
    w.__dict__.update(clock=clock, audit=audit, stop=stop, company=company, events=events, ledger=ledger, svc=svc,
                      calls=calls, memory=memory, auth=auth, build=build, tmp=tmp_path, wf=wf, pro=pro,
                      reg=reg)
    return w


@pytest.fixture
def api(world):
    app = world.build()
    return app, Dev(app, world.auth), world


def audit_rows(world):
    p = Path(world.audit)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# --- giriş qapısı ---

def test_health_is_public_and_minimal(world):
    app = world.build()
    r = app.handle(Request("GET", "/v1/health", {}, {}, b"", "1.2.3.4"))
    assert r.status == 200 and r.body == {"status": "ok", "api_version": 1}


def test_every_protected_route_requires_a_valid_session(api):
    app, dev, world = api
    protected = [(m, p) for m, p, auth in app.router.routes() if auth == "session"]
    assert len(protected) >= 25
    for method, pattern in protected:
        path = pattern.replace("{id}", "x").replace("{key}", "x")
        assert dev.call(method, path, {} if method == "POST" else None, token=None).status == 401, pattern
        assert dev.call(method, path, {} if method == "POST" else None, token="nay_a_" + "x" * 43).status == 401
    world.auth.revoke_device(dev.device_id)
    assert dev.get("/v1/me").status == 401


def test_pairing_over_the_api(world):
    app = world.build()
    bad = app.handle(Request("POST", "/v1/pair", {}, {"content-type": "application/json"},
                             json.dumps({"code": "WRONGCOD", "device_name": "x"}).encode(), "9.9.9.9"))
    assert bad.status == 401
    dev = Dev(app, world.auth)
    assert set(dev.paired) == {"device_id", "access_token", "refresh_token", "access_expires_in"}
    me = dev.get("/v1/me")
    assert me.status == 200 and me.body == {"device_id": dev.device_id, "device_name": "Pixel 8",
                                            "owner_id": "owner", "api_version": 1}
    assert b64(dev.kp.spki()) not in json.dumps(me.body)


def test_refresh_rotation_logout_and_reuse(api):
    app, dev, world = api
    r = dev.post("/v1/token/refresh", {"refresh_token": dev.refresh}, token=None)
    assert r.status == 200 and r.body["refresh_token"] != dev.refresh
    assert dev.get("/v1/me", token=r.body["access_token"]).status == 200
    assert dev.post("/v1/token/refresh", {"refresh_token": dev.refresh}, token=None).status == 401  # təkrar
    assert dev.get("/v1/me", token=r.body["access_token"]).status == 401  # cihaz ləğv olundu
    app2 = world.build()
    d2 = Dev(app2, world.auth, "B")
    assert d2.post("/v1/logout").status == 200 and d2.get("/v1/me").status == 401


def test_per_device_rate_limit(api):
    app, dev, _ = api
    codes = [dev.get("/v1/health", token=None).status for _ in range(3)]
    assert codes == [200, 200, 200]
    statuses = {dev.get("/v1/me").status for _ in range(130)}
    assert 429 in statuses


# --- söhbət ---

def test_chat_roundtrip_history_and_labels(api):
    app, dev, world = api
    r = dev.post("/v1/chat/messages", {"text": "kömək"})
    assert r.status == 200
    cid = r.body["conversation_id"]
    u, a = r.body["user_message"], r.body["assistant_message"]
    assert (u["role"], u["text"], u["input_mode"]) == ("user", "kömək", "text")
    assert a["role"] == "assistant" and a["is_mock"] is False and a["provenance"] == "rules"
    assert a["speech_text"] and "http" not in a["speech_text"]
    dev.post("/v1/chat/messages", {"text": "status", "conversation_id": cid})
    msgs = dev.get(f"/v1/chat/conversations/{cid}/messages").body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert "təsdiq" in msgs[3]["text"].lower()
    convs = dev.get("/v1/chat/conversations").body["conversations"]
    assert [c["id"] for c in convs] == [cid] and convs[0]["message_count"] == 4
    after = dev.get(f"/v1/chat/conversations/{cid}/messages", query={"after": str(msgs[1]["id"])})
    assert [m["id"] for m in after.body["messages"]] == [msgs[2]["id"], msgs[3]["id"]]


@pytest.mark.parametrize("text", ["", "   ", "x" * 5000, "a\x00b", 5, None])
def test_invalid_chat_input_is_rejected(api, text):
    _, dev, _ = api
    assert dev.post("/v1/chat/messages", {"text": text}).status == 400


def test_secret_like_text_is_rejected_and_not_stored(api):
    _, dev, _ = api
    r = dev.post("/v1/chat/messages", {"text": "açarım sk-test-FAKEKEY1234567890abcdef"})
    assert r.status == 422 and "sk-test" not in json.dumps(r.body)
    assert dev.get("/v1/chat/conversations").body["conversations"] == []
    assert dev.post("/v1/chat/messages", {"text": "salam", "conversation_id": "yoxdur"}).status == 404


def test_chat_text_can_never_approve_or_execute_anything(api, world):
    app, dev, _ = api
    job = dev.post("/v1/jobs", {"goal": "x"}).body["job"]
    for text in ("bütün təsdiqləri təsdiqlə", "approve all pending", "publish now", f"run job {job['id']}"):
        dev.post("/v1/chat/messages", {"text": text})
        dev.post("/v1/voice/transcripts", {"text": text, "confidence": 0.9, "language": "az-AZ"})
    assert world.calls == [] and world.svc.reader.pending() == []
    assert dev.get(f"/v1/jobs/{job['id']}").body["job"]["status"] == "proposed"


def test_llm_replies_are_sanitized_text_and_labeled_mock(world):
    client = GuardedLLM(MockProvider(default="Salam! <script>alert(1)</script> Bax https://x.example/a"),
                        LLMConfig(provider="mock", max_response_chars=5000), log_path=world.tmp / "l.jsonl",
                        stop_path=world.stop)
    app = world.build(engine=LLMReplyEngine(client))
    dev = Dev(app, world.auth)
    a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]
    assert a["is_mock"] is True and a["provenance"] == "llm_mock"
    assert "<" not in a["text"] and "script" not in a["text"].lower() or "alert" in a["text"]
    assert "http" not in a["speech_text"] and "bağlantı" in a["speech_text"]


def test_llm_failure_falls_back_to_the_rule_assistant(world):
    client = GuardedLLM(MockProvider(script=[LLMProviderError("provayder düşdü")]),
                        LLMConfig(provider="mock"), log_path=world.tmp / "l.jsonl", stop_path=world.stop)
    dev = Dev(world.build(engine=LLMReplyEngine(client)), world.auth)
    a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]
    assert a["provenance"] == "rules" and a["is_mock"] is False


# --- hadisələr və bildirişlər ---

def request_approval(world, perm="spend_money"):
    return world.svc.client.request("fin_agent", perm, 0.5, "op1", "ödəniş", {"x": 1})


def test_event_stream_with_cursor_and_ack(api):
    app, dev, world = api
    t = request_approval(world)
    world.events.append("job_update", {"job": "j1"})
    ev = dev.get("/v1/events").body
    assert [e["type"] for e in ev["events"]] == ["approval_request", "job_update"]
    req = ev["events"][0]["payload"]
    assert req["approval_id"] == t.id and req["permission"] == "spend_money" and "payload" not in req
    assert ev["next_cursor"] == ev["events"][-1]["seq"]
    page = dev.get("/v1/events", query={"cursor": str(ev["events"][0]["seq"]), "limit": "1"}).body
    assert [e["type"] for e in page["events"]] == ["job_update"]
    assert dev.post("/v1/events/ack", {"seq": ev["next_cursor"]}).body == {"ok": True, "last_ack": ev["next_cursor"]}
    assert dev.post("/v1/events/ack", {"seq": ev["next_cursor"] + 50}).status == 400
    assert dev.get("/v1/events", query={"limit": "0"}).status == 400


def test_proactive_notifications_flow_into_the_event_stream(api):
    app, dev, world = api
    request_approval(world)
    world.clock.t += 120  # approval_wait_s=60
    prev = dev.get("/v1/notifications/preview").body["decisions"]
    assert [d["action"] for d in prev] == ["notify"] and dev.get("/v1/events").body["events"][-1]["type"] != "notification"
    run = dev.post("/v1/notifications/run").body
    assert run["delivered"] == 1
    note = [e for e in dev.get("/v1/events").body["events"] if e["type"] == "notification"][0]["payload"]
    assert (note["platform"], note["locale"], note["schema_version"]) == ("android", "az", 1)
    assert note["speech_text"] and note["actions"][0]["action_id"] == "open_approval"
    again = dev.post("/v1/notifications/run").body
    assert again["delivered"] == 0  # təkrar bildiriş yoxdur
    key = prev[0]["key"]
    assert dev.post(f"/v1/notifications/{key}/ack").status == 200
    assert dev.post(f"/v1/notifications/{key}/snooze", {"seconds": 3600}).status == 200
    assert dev.post(f"/v1/notifications/{key}/snooze", {"seconds": -5}).status == 400
    assert dev.post(f"/v1/notifications/{key}/dismiss").status == 200


# --- yaddaş ---

def test_memory_endpoints_mark_content_untrusted_and_reject_secrets(api, world):
    app, dev, _ = api
    r = dev.post("/v1/memory", {"category": "note", "content": "Layihə qeydi: sayt A variantı", "project": "site"})
    assert r.status == 201 and r.body["created"] is True
    rec = r.body["record"]
    assert (rec["source"], rec["provenance"], rec["untrusted"]) == ("owner:owner", "owner", True)
    assert dev.post("/v1/memory", {"category": "note", "content": "Layihə qeydi: sayt A variantı",
                                    "project": "site"}).body["created"] is False
    found = dev.get("/v1/memory", query={"query": "sayt", "category": "note"}).body["records"]
    assert [x["id"] for x in found] == [rec["id"]] and found[0]["untrusted"] is True and "date" in found[0]
    assert dev.post("/v1/memory", {"category": "note", "content": "api_key = 'abcd1234efgh5678'"}).status == 422
    for bad in ({"category": "approval", "content": "x"}, {"category": "result", "content": "x"},
                {"category": "naməlum", "content": "x"}, {"category": "note", "content": ""}):
        assert dev.post("/v1/memory", bad).status == 400
    assert dev.get("/v1/memory", query={"limit": "500"}).status == 400


# --- şirkət: yalnız oxu ---

def test_company_is_read_only_through_the_api(api):
    app, dev, _ = api
    agents = {a["name"]: a for a in dev.get("/v1/company/agents").body["agents"]}
    assert agents["ops_agent"]["department"] == "operations" and agents["ops_agent"]["budget"] == 5.0
    assert agents["fin_agent"]["authority"]["permissions"] == ["read_files", "spend_money"]
    deps = {d["id"]: d for d in dev.get("/v1/company/departments").body["departments"]}
    assert set(deps) >= {"research", "finance", "operations"} and deps["finance"]["active_agents"] == 1
    company_routes = [(m, p) for m, p, _ in app.router.routes() if "/company" in p]
    assert all(m == "GET" for m, _ in company_routes)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert dev.call(method, "/v1/company/agents", {"name": "x"}).status in (404, 405)


# --- CEO işləri: təklif -> icra ---

def test_job_proposal_is_labeled_mock_and_runs_only_on_explicit_request(api, world):
    app, dev, _ = api
    r = dev.post("/v1/jobs", {"goal": "əməliyyat hesabatı hazırla"})
    job = r.body["job"]
    assert r.status == 201 and job["ok"] is True and job["is_mock"] is True and job["provider"] == "mock"
    assert [(s["id"], s["department"], s["permission"]) for s in job["steps"]] == [("a", "operations", "read_files")]
    assert world.calls == [] and job["status"] == "proposed"
    run = dev.post(f"/v1/jobs/{job['id']}/run")
    assert run.status == 200 and run.body["job"]["report"]["status"] == "ok" and world.calls == ["ops_agent"]
    step = run.body["job"]["report"]["steps"][0]
    assert (step["agent"], step["status"]) == ("ops_agent", "ok") and step["output_preview"]["untrusted"] is True
    gate = [r for r in audit_rows(world) if "event" not in r and "source" not in r]
    assert gate and all(r["allowed"] for r in gate)               # Gate/Broker yolundan keçib
    again = dev.post(f"/v1/jobs/{job['id']}/run")                 # təkrar: yenidən icra yoxdur
    assert again.status == 200 and world.calls == ["ops_agent"]
    assert dev.get(f"/v1/jobs/{job['id']}").body["job"]["status"] == "ok"
    assert reconcile(world.ledger, world.audit).ok


def test_invalid_plans_unknown_jobs_and_goal_validation(world):
    app = world.build(plan="bu json deyil")
    dev = Dev(app, world.auth)
    job = dev.post("/v1/jobs", {"goal": "x"}).body["job"]
    assert job["ok"] is False and job["reason"] and job["steps"] == []
    assert dev.post(f"/v1/jobs/{job['id']}/run").status == 409
    assert dev.post("/v1/jobs/yoxdur/run").status == 404 and dev.get("/v1/jobs/yoxdur").status == 404
    for bad in ({"goal": ""}, {"goal": "x" * 2000}, {"goal": 5}, {}, {"goal": "sk-test-FAKEKEY1234567890abcdef"}):
        assert dev.post("/v1/jobs", bad).status in (400, 422)


def test_sensitive_step_needs_step_up_approval_before_it_runs(world):
    app = world.build(plan=PLAN_FIN, allow_sensitive=True)
    dev = Dev(app, world.auth)
    job = dev.post("/v1/jobs", {"goal": "ödəniş hazırla"}).body["job"]
    run = dev.post(f"/v1/jobs/{job['id']}/run").body["job"]["report"]
    assert [s["status"] for s in run["steps"]] == ["ok", "pending_approval"] and world.calls == ["ops_agent"]
    pending = dev.get("/v1/approvals").body["approvals"]
    assert len(pending) == 1 and pending[0]["permission"] == "spend_money" and pending[0]["agent"] == "fin_agent"
    aid = pending[0]["id"]
    assert run["pending_approvals"] == [aid]

    # sübutsuz/yanlış/səs/başqa qərar -> rədd
    assert dev.post(f"/v1/approvals/{aid}/decision", {"decision": "approve"}).status == 400
    assert dev.decide(aid, proof_override="0" * 64).status == 403
    assert dev.decide(aid, method="voice").status == 403
    other = Dev(app, world.auth, "Başqa cihaz")
    ch = dev.post(f"/v1/approvals/{aid}/challenge", {"decision": "approve"}).body
    stolen = other.sign(ch, "approval", aid, ch["approval"]["payload_hash"], "approve")
    assert dev.post(f"/v1/approvals/{aid}/decision", {"decision": "approve", "challenge_id": ch["challenge_id"],
                                                      "proof": stolen, "method": "biometric"}).status == 403
    assert world.svc.gateway.get(aid).status == "pending" and world.calls == ["ops_agent"]
    assert dev.post(f"/v1/jobs/{job['id']}/run").body["job"]["report"]["steps"][1]["status"] == "pending_approval"

    # düzgün biometrik sübut -> təsdiq, sonra iş davam edir və addım BİR dəfə icra olunur
    ok = dev.decide(aid, "approve")
    assert ok.status == 200 and ok.body == {"ok": True, "status": "approved"}
    t = world.svc.gateway.get(aid)
    assert (t.approver_kind, t.channel, t.human_approved) == ("human", "android", True)
    assert dev.decide(aid, "approve").status == 409  # təkrar qərar
    final = dev.post(f"/v1/jobs/{job['id']}/run").body["job"]["report"]
    assert final["status"] == "ok" and world.calls == ["ops_agent", "fin_agent"]
    assert world.svc.gateway.get(aid).status == "consumed"
    assert dev.post(f"/v1/jobs/{job['id']}/run").status == 200 and world.calls == ["ops_agent", "fin_agent"]
    assert any(r.get("event") == "api_approval_decision" and r["device_id"] == dev.device_id for r in audit_rows(world))
    assert reconcile(world.ledger, world.audit).ok


def test_deny_decision_blocks_the_step(world):
    app = world.build(plan=PLAN_FIN, allow_sensitive=True)
    dev = Dev(app, world.auth)
    job = dev.post("/v1/jobs", {"goal": "ödəniş"}).body["job"]
    dev.post(f"/v1/jobs/{job['id']}/run")
    aid = dev.get("/v1/approvals").body["approvals"][0]["id"]
    assert dev.decide(aid, "deny").body["status"] == "denied"
    report = dev.post(f"/v1/jobs/{job['id']}/run").body["job"]["report"]
    assert report["steps"][1]["status"] == "blocked" and world.calls == ["ops_agent"]


def test_expired_approvals_cannot_be_decided(world):
    app = world.build()
    dev = Dev(app, world.auth)
    t = request_approval(world)
    world.clock.t += 3700
    assert dev.post(f"/v1/approvals/{t.id}/challenge", {"decision": "approve"}).status == 404
    assert dev.get("/v1/approvals").body["approvals"] == []


def test_approval_routes_validate_input(api, world):
    _, dev, _ = api
    t = request_approval(world)
    assert dev.post(f"/v1/approvals/{t.id}/challenge", {"decision": "belki"}).status == 400
    assert dev.post("/v1/approvals/yoxdur/challenge", {"decision": "approve"}).status == 404
    ch = dev.post(f"/v1/approvals/{t.id}/challenge", {"decision": "approve"}).body
    bad = dev.post(f"/v1/approvals/{t.id}/decision", {"decision": "deny", "challenge_id": ch["challenge_id"],
                                                      "proof": "0" * 64, "method": "biometric"})
    assert bad.status == 403
    assert dev.post(f"/v1/approvals/{t.id}/decision", {"decision": "approve", "challenge_id": 5, "proof": 5,
                                                       "method": "biometric"}).status == 400


# --- sistem: stop / davam ---

def test_stop_is_easy_but_resume_needs_step_up(api, world):
    app, dev, _ = api
    assert dev.get("/v1/system/status").body["stop_active"] is False
    job = dev.post("/v1/jobs", {"goal": "x"}).body["job"]  # stop-dan əvvəl təklif (stop zamanı LLM də dayanır)
    assert dev.post("/v1/system/stop").body == {"ok": True, "stop_active": True} and world.stop.exists()
    run = dev.post(f"/v1/jobs/{job['id']}/run").body["job"]["report"]
    assert run["steps"][0]["status"] == "skipped" and world.calls == []
    assert dev.post("/v1/system/resume", {"challenge_id": "x", "proof": "0" * 64, "method": "biometric"}).status in (400, 403)
    ch = dev.post("/v1/system/resume/challenge").body
    proof = dev.sign(ch, "resume", "stop", "", "approve")
    assert dev.post("/v1/system/resume", {"challenge_id": ch["challenge_id"], "proof": proof,
                                          "method": "voice"}).status == 403
    ch2 = dev.post("/v1/system/resume/challenge").body
    proof2 = dev.sign(ch2, "resume", "stop", "", "approve")
    ok = dev.post("/v1/system/resume", {"challenge_id": ch2["challenge_id"], "proof": proof2, "method": "biometric"})
    assert ok.body == {"ok": True, "stop_active": False} and not world.stop.exists()
    assert dev.post(f"/v1/jobs/{job['id']}/run").body["job"]["report"]["status"] == "ok"


def test_system_status_summarizes_without_secrets(api, world):
    _, dev, _ = api
    request_approval(world)
    s = dev.get("/v1/system/status").body
    assert s == {"stop_active": False, "pending_approvals": 1, "api_version": 1, "llm_is_mock": True}


# --- səs ---

def test_voice_profile_is_azerbaijani_female_and_never_approves(api):
    _, dev, _ = api
    v = dev.get("/v1/voice/profile").body
    assert v["profile"] == {"language": "az-AZ", "gender": "female", "style": "natural", "rate": 1.0, "pitch": 0.0}
    assert v["tts"]["is_mock"] is True and v["tts"]["provider"] == "mock" and v["stt"]["available"] is False
    assert v["voice_may_approve"] is False


def test_voice_transcripts_are_untrusted_chat_input_and_synthesis_is_mock(api):
    _, dev, _ = api
    r = dev.post("/v1/voice/transcripts", {"text": "hesabatı göstər", "confidence": 0.8, "language": "az-AZ"})
    assert r.status == 200 and r.body["user_message"]["input_mode"] == "voice"
    assert r.body["user_message"]["untrusted"] is True
    for bad in ({"text": "x", "confidence": 2, "language": "az-AZ"}, {"text": "", "confidence": 0.5, "language": "az-AZ"},
                {"text": "x", "confidence": 0.5, "language": "../x"}):
        assert dev.post("/v1/voice/transcripts", bad).status == 400
    s = dev.post("/v1/voice/synthesize", {"text": "Bax https://x.example/a sk-test-FAKEKEY1234567890abcdef"})
    assert s.status == 200 and s.body["is_mock"] is True and s.body["audio_ref"] is None
    assert "http" not in s.body["text"] and "sk-test" not in s.body["text"] and "bağlantı" in s.body["text"]
    assert s.body["profile"]["gender"] == "female"


def test_synthesis_is_unavailable_without_an_engine(world):
    dev = Dev(world.build(voice=False), world.auth)
    assert dev.post("/v1/voice/synthesize", {"text": "salam"}).status == 503
    assert dev.get("/v1/voice/profile").body["tts"]["available"] is False


# --- sirr və səlahiyyət gigiyenası ---

def test_secrets_appear_only_where_they_must(api, world):
    app, dev, _ = api
    request_approval(world)
    seen = []
    for path in ("/v1/me", "/v1/events", "/v1/approvals", "/v1/system/status", "/v1/company/agents",
                 "/v1/chat/conversations", "/v1/voice/profile", "/v1/notifications/preview"):
        seen.append(json.dumps(dev.get(path).body))
    blob = "\n".join(seen) + Path(world.audit).read_text()
    for secret in (dev.access, dev.refresh, b64(dev.kp.spki())):
        assert secret not in blob


def test_api_modules_cannot_bypass_the_security_core_or_admin_facets():
    forbidden = {"safety.broker", "safety.gate", "safety.tools", "safety.workspace", "safety.ledger"}
    for name in ("app.py", "jobs.py", "chat.py", "events.py", "http.py", "auth.py"):
        text = (ROOT / "api" / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden, (name, node.module)
        assert "CompanyAdmin" not in text and "set_authority" not in text and "register_agent" not in text, name
    for name in ("app.py", "auth.py", "chat.py", "jobs.py", "events.py", "http.py"):
        tree = ast.parse((ROOT / "api" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"environ", "getenv", "system", "popen"}, (name, node.lineno)


# --- imkanlar: mock/real fərqi ---

def test_capabilities_state_what_is_mock_and_what_is_missing(api):
    _, dev, _ = api
    c = dev.get("/v1/capabilities").body
    assert c == {
        "api_version": 1, "backend_mode": "mock",
        "chat": {"available": True, "engine": "rules"},
        "llm": {"available": True, "is_mock": True},
        "agent_runner": {"connected": False},
        "voice_tts": {"available": True, "is_mock": True},
        "voice_stt": {"available": False, "is_mock": True},
        "push": {"available": False},
        "events": {"available": True, "transport": "polling"},
        "step_up": {"scheme": "ecdsa-p256-sha256", "voice_allowed": False},
    }


def test_capabilities_without_voice_engine_report_it_unavailable(world):
    dev = Dev(world.build(voice=False), world.auth)
    assert dev.get("/v1/capabilities").body["voice_tts"] == {"available": False, "is_mock": True}


def test_job_run_reports_when_no_agent_runner_is_connected(api):
    _, dev, _ = api
    assert dev.get("/v1/capabilities").body["agent_runner"] == {"connected": False}
    assert dev.get("/v1/system/status").body["llm_is_mock"] is True
