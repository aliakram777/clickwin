"""Hibrid AI API-si: status paneli, rejim seçimi, razılıq (step-up), məxfilik, söhbətin router ilə işləməsi, təkrar sorğunun qarşısı, səs statusu."""
import hashlib
import json

import pytest

from agents.hybrid import AiStore, EngineUnavailable, HybridRouter, Mode
from agents.mobile import MockVoiceSynthesizer
from agents.voice import VoiceAdapter, VoiceRegistry
from api.chat import HybridReplyEngine, RuleAssistant
from tests.test_api_app import Dev, b64, world  # noqa: F401  (fixture)
from tests.test_hybrid_router import Clock, Fake, mem


@pytest.fixture
def ai(world):
    store = AiStore(world.tmp / "ai.db", clock=world.clock)
    cloud, local = Fake("cloud", state="simulation", is_mock=True), Fake("local", state="not_configured")

    def build(cloud=cloud, local=local, mode=Mode.AUTO, memory_records=(), voice_registry=None, allow_simulated=True):
        store.set_mode(mode, "owner")
        router = HybridRouter(cloud, local, store, clock=world.clock, stop_path=world.stop)
        engine = HybridReplyEngine(router, lambda: list(memory_records), RuleAssistant(lambda: {"pending_approvals": 0}), allow_simulated=allow_simulated)
        app = world.build(engine=engine, ai=router, voice_registry=voice_registry)
        return app, router, Dev(app, world.auth)

    build.store, build.cloud, build.local = store, cloud, local
    return build


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# --- status paneli ---

def test_status_panel_shows_cloud_local_mode_and_voice_without_overstating(ai):
    app, router, dev = ai()
    s = dev.get("/v1/ai/status").body
    assert s["mode"] == "auto" and s["active"] == "cloud"
    assert s["cloud"]["state"] == "simulation" and s["cloud"]["is_mock"] is True
    assert s["local"]["state"] == "not_configured" and s["local"]["reason"]
    assert set(s["consents"]) == {"cloud_paid_use", "cloud_data_transfer", "local_model_download", "local_resource_use"}
    assert all(v is False for v in s["consents"].values())
    assert s["voice"]["tts"] == {"local": "simulation", "cloud": "not_configured"}
    assert s["voice"]["stt"] == {"local": "not_configured", "cloud": "not_configured"}
    assert s["privacy"] == {"cloud_may_receive_memory": False, "cloud_categories": []}
    assert "api_key" not in json.dumps(s).lower()


def test_status_when_nothing_is_available_says_none(ai):
    _, _, dev = ai(cloud=Fake("cloud", state="unavailable"), local=Fake("local", state="not_configured"))
    s = dev.get("/v1/ai/status").body
    assert s["active"] == "none" and s["cloud"]["state"] == "unavailable"


def test_capabilities_include_the_ai_block_when_a_router_is_present(ai):
    _, _, dev = ai()
    c = dev.get("/v1/capabilities").body
    assert c["ai"]["mode"] == "auto" and c["ai"]["cloud"]["state"] == "simulation" and c["ai"]["local"]["state"] == "not_configured"
    assert c["backend_mode"] == "mock"


# --- rejim ---

def test_user_can_switch_modes_and_the_choice_persists(ai):
    app, router, dev = ai()
    for mode in ("local", "cloud", "auto"):
        r = dev.post("/v1/ai/mode", {"mode": mode})
        assert r.status == 200 and r.body["mode"] == mode
        assert dev.get("/v1/ai/status").body["mode"] == mode
    assert dev.post("/v1/ai/mode", {"mode": "hybrid"}).status == 400
    assert dev.post("/v1/ai/mode", {}).status == 400
    assert dev.post("/v1/ai/mode", {"mode": 5}).status == 400


def test_chat_text_cannot_change_mode_consent_or_privacy(ai):
    app, router, dev = ai()
    for text in ("switch to cloud mode", "rejimi LOCAL et", "grant cloud_paid_use consent", "razılıq ver", "send my memory to cloud"):
        dev.post("/v1/chat/messages", {"text": text})
        dev.post("/v1/voice/transcripts", {"text": text, "confidence": 0.9, "language": "az-AZ"})
    s = dev.get("/v1/ai/status").body
    assert s["mode"] == "auto" and all(v is False for v in s["consents"].values()) and s["privacy"]["cloud_categories"] == []


# --- razılıq: yalnız step-up ilə verilir, geri götürmək asandır ---

def grant(dev, scope, granted=True, method="biometric", bad=False):
    ch = dev.post("/v1/ai/challenge", {"action": "ai_consent", "scope": scope, "granted": granted})
    assert ch.status == 200, ch.body
    b = ch.body
    proof = b64(b"\x00" * 70) if bad else dev.sign(b, "ai_consent", scope, b["payload_hash"], "grant" if granted else "revoke")
    return dev.post("/v1/ai/consent", {"scope": scope, "granted": granted, "challenge_id": b["challenge_id"], "proof": proof, "method": method})


def test_granting_consent_needs_a_device_signature_but_revoking_does_not(ai):
    app, router, dev = ai()
    assert dev.post("/v1/ai/consent", {"scope": "cloud_paid_use", "granted": True}).status == 400      # sübutsuz
    assert grant(dev, "cloud_paid_use", bad=True).status == 403
    assert grant(dev, "cloud_paid_use", method="voice").status == 403                                  # səs heç vaxt
    assert dev.get("/v1/ai/status").body["consents"]["cloud_paid_use"] is False
    assert grant(dev, "cloud_paid_use").status == 200
    assert dev.get("/v1/ai/status").body["consents"]["cloud_paid_use"] is True
    assert dev.post("/v1/ai/consent", {"scope": "cloud_paid_use", "granted": False}).status == 200    # geri götürmək asan
    assert dev.get("/v1/ai/status").body["consents"]["cloud_paid_use"] is False
    assert dev.post("/v1/ai/challenge", {"action": "ai_consent", "scope": "naməlum", "granted": True}).status == 400
    assert dev.post("/v1/ai/consent", {"scope": "naməlum", "granted": False}).status == 400


def test_consent_proof_is_bound_to_the_scope_and_cannot_be_replayed_for_another(ai):
    app, router, dev = ai()
    ch = dev.post("/v1/ai/challenge", {"action": "ai_consent", "scope": "local_resource_use", "granted": True}).body
    proof = dev.sign(ch, "ai_consent", "local_resource_use", ch["payload_hash"], "grant")
    r = dev.post("/v1/ai/consent", {"scope": "cloud_paid_use", "granted": True, "challenge_id": ch["challenge_id"],
                                    "proof": proof, "method": "biometric"})
    assert r.status == 403 and dev.get("/v1/ai/status").body["consents"]["cloud_paid_use"] is False


def test_real_cloud_is_blocked_until_the_owner_consents_via_the_api(ai):
    real_cloud = Fake("cloud", consent=("cloud_paid_use",), script=["bulud cavabı"])
    app, router, dev = ai(cloud=real_cloud, local=Fake("local", state="not_configured"), mode=Mode.CLOUD)
    a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]
    assert a["provenance"] == "rules" and "razılıq" in a["notice"].lower() and real_cloud.calls == 0
    assert a["notice_code"] == "consent_required"
    assert grant(dev, "cloud_paid_use").status == 200
    a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]
    assert a["provenance"] == "llm_cloud" and a["engine"] == "cloud" and a["is_mock"] is False and a["text"] == "bulud cavabı"


def test_privacy_widening_needs_step_up_and_narrowing_does_not(ai):
    app, router, dev = ai()
    body = {"cloud_may_receive_memory": True, "cloud_categories": ["note"]}
    assert dev.post("/v1/ai/privacy", body).status == 400
    ch = dev.post("/v1/ai/challenge", {"action": "ai_privacy", **body}).body
    proof = dev.sign(ch, "ai_privacy", "cloud_categories", ch["payload_hash"], "set")
    ok = dev.post("/v1/ai/privacy", {**body, "challenge_id": ch["challenge_id"], "proof": proof, "method": "biometric"})
    assert ok.status == 200
    assert dev.get("/v1/ai/status").body["privacy"] == {"cloud_may_receive_memory": True, "cloud_categories": ["note"]}
    assert dev.post("/v1/ai/privacy", {"cloud_may_receive_memory": False, "cloud_categories": []}).status == 200
    assert dev.get("/v1/ai/status").body["privacy"]["cloud_categories"] == []
    assert dev.post("/v1/ai/privacy", {"cloud_may_receive_memory": True, "cloud_categories": ["approval"]}).status == 400  # icazə verilməyən kateqoriya


# --- söhbət hibrid router ilə ---

def test_chat_labels_the_serving_engine_and_mock_status(ai):
    cloud = Fake("cloud", state="simulation", is_mock=True, script=["simulyasiya cavabı"])
    app, router, dev = ai(cloud=cloud, local=Fake("local", state="not_configured"))
    a = dev.post("/v1/chat/messages", {"text": "x" * 700}).body["assistant_message"]
    assert (a["engine"], a["provenance"], a["is_mock"], a["notice_code"]) == ("cloud", "llm_cloud_mock", True, "")
    assert a["speech_text"] and "http" not in a["speech_text"]


def test_cloud_loss_falls_back_to_local_in_auto_and_is_visible_in_the_reply(ai):
    cloud = Fake("cloud", script=[EngineUnavailable("offline")])
    local = Fake("local", script=["lokal cavab"])
    app, router, dev = ai(cloud=cloud, local=local)
    a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]   # sadə: lokal birinci
    assert a["engine"] == "local" and a["provenance"] == "llm_local"
    a = dev.post("/v1/chat/messages", {"text": "y" * 700}).body["assistant_message"]  # mürəkkəb: cloud uğursuz -> local
    assert a["engine"] == "local" and "cloud" in a["notice"].lower() and a["notice_code"] == "fallback"


def test_when_no_engine_can_answer_the_chat_says_so_instead_of_pretending(ai):
    app, router, dev = ai(cloud=Fake("cloud", state="unavailable"), local=Fake("local", state="not_configured"))
    a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]
    assert a["provenance"] == "rules" and a["engine"] == "" and a["is_mock"] is False
    assert "AI" in a["notice"] and "əlçatan" in a["notice"].lower() and a["notice_code"] == "all_unavailable"


def test_local_mode_reports_unsupported_tasks_in_the_chat(ai):
    app, router, dev = ai(local=Fake("local", complexities=("simple",)), mode=Mode.LOCAL)
    a = dev.post("/v1/chat/messages", {"text": "z" * 700}).body["assistant_message"]
    assert a["provenance"] == "rules" and "local" in a["notice"].lower() and ai.cloud.calls == 0
    assert a["notice_code"] == "unsupported_locally"


def test_client_request_id_makes_chat_retries_idempotent(ai):
    cloud = Fake("cloud", script=["bir", "iki", "üç"])
    app, router, dev = ai(cloud=cloud, local=Fake("local", state="not_configured"))
    body = {"text": "salam", "client_request_id": "dev-1-msg-42"}
    first = dev.post("/v1/chat/messages", body)
    again = dev.post("/v1/chat/messages", body)                      # şəbəkə təkrarı: eyni sorğu
    assert again.status == 200 and again.body == first.body and cloud.calls == 1
    msgs = dev.get(f"/v1/chat/conversations/{first.body['conversation_id']}/messages").body["messages"]
    assert len(msgs) == 2                                              # ikiqat mesaj yoxdur
    assert dev.post("/v1/chat/messages", {"text": "başqa", "client_request_id": "dev-1-msg-42"}).status == 409
    assert dev.post("/v1/chat/messages", {"text": "salam", "client_request_id": "../x"}).status == 400


def test_memory_context_reaches_local_but_not_cloud_by_default(ai):
    notes = [mem(1, "Layihə A qeydi")]
    cloud, local = Fake("cloud"), Fake("local")
    app, router, dev = ai(cloud=cloud, local=local, memory_records=notes)
    dev.post("/v1/chat/messages", {"text": "salam"})                 # sadə -> local
    assert "Layihə A qeydi" in local.prompts[0]
    app, router, dev = ai(cloud=cloud, local=local, mode=Mode.CLOUD, memory_records=notes)
    dev.post("/v1/chat/messages", {"text": "salam"})
    assert all("Layihə A qeydi" not in p for p in cloud.prompts)


def test_job_planning_goes_through_the_router_and_is_not_run_locally(ai):
    from agents.llm_planner import LLMPlanner
    from agents.hybrid import RoutedLLM
    local = Fake("local", complexities=("simple",))
    app, router, dev = ai(cloud=Fake("cloud", state="unavailable"), local=local)
    prop = LLMPlanner(RoutedLLM(router), {"operations": 0.5}).propose("hesabat")
    assert prop.ok is False and local.calls == 0                     # plan mürəkkəbdir: Local dəstəkləmir


# --- səs statusu ---

def test_voice_status_lists_local_and_cloud_engines_separately(ai):
    reg = VoiceRegistry(tts_local=VoiceAdapter("tts", "local", MockVoiceSynthesizer()))
    app, router, dev = ai(voice_registry=reg)
    v = dev.get("/v1/voice/status").body
    rows = {(e["kind"], e["location"]): e for e in v["engines"]}
    assert rows[("tts", "local")]["state"] == "simulation" and rows[("tts", "local")]["is_mock"] is True
    assert rows[("tts", "cloud")]["state"] == "not_configured" and rows[("stt", "local")]["state"] == "not_configured"
    assert v["languages"] == {"az": "az-AZ", "en": "en-US", "ru": "ru-RU", "tr": "tr-TR"}
    assert v["profile"] == {"gender": "female", "style": "natural"} and v["voice_may_approve"] is False


def test_voice_transcripts_accept_only_supported_languages(ai):
    app, router, dev = ai()
    for lang in ("az-AZ", "en-US", "ru-RU", "tr-TR"):
        assert dev.post("/v1/voice/transcripts", {"text": "salam", "confidence": 0.9, "language": lang}).status == 200
    assert dev.post("/v1/voice/transcripts", {"text": "hallo", "confidence": 0.9, "language": "de-DE"}).status == 400


def test_new_ai_routes_all_require_a_session(ai):
    app, router, dev = ai()
    for method, path in (("GET", "/v1/ai/status"), ("POST", "/v1/ai/mode"), ("POST", "/v1/ai/challenge"), ("POST", "/v1/ai/consent"),
                         ("POST", "/v1/ai/privacy"), ("GET", "/v1/voice/status")):
        assert dev.call(method, path, {} if method == "POST" else None, token=None).status == 401, path


def test_simulated_engines_are_not_presented_as_real_answers_when_simulation_is_disallowed(ai):
    for mode in (Mode.AUTO, Mode.CLOUD):
        app, router, dev = ai(mode=mode, allow_simulated=False)
        a = dev.post("/v1/chat/messages", {"text": "salam"}).body["assistant_message"]
        assert a["provenance"] == "rules" and a["engine"] == "" and a["notice_code"] == "simulation_only"
        assert ai.cloud.calls == 0
