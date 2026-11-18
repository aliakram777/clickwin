"""M1: CORE step-up imza axınının doğrulanması — server challenge-in action/subject/payload_hash/decision-ını müstəqil yoxlayır:
dəyişdirilmiş, köhnə (stale), təkrar (replay), uyğunsuz (başqa məqsəd/əməliyyat/cihaz/qərar), vaxtı keçmiş sübutlar RƏDD olunur və challenge yanır."""
from __future__ import annotations

import json
import pathlib

import pytest

from core import StepState
from tests.test_api_app import Dev
from tests.test_core_api import CoreWorld, cw, h, world  # noqa: F401  (fixtures)

VECTORS = pathlib.Path(__file__).resolve().parent.parent / "android" / "jvm-contract" / "src" / "test" / "resources" / "core_vectors.json"


def _goal_ch(cw, gid, to):
    return cw.dev.post(f"/v1/core/goals/{gid}/challenge", {"to_status": to}).body


def _go(cw, gid, ch, to, *, action="core_goal", subject=None, ph=None, decision=None, method="biometric", dev=None):
    d = dev or cw.dev
    proof = d.sign(ch, action, subject if subject is not None else gid, ph if ph is not None else ch["payload_hash"], decision if decision is not None else ch["decision"])
    return d.post(f"/v1/core/goals/{gid}/transition", {"to_status": to, "challenge_id": ch["challenge_id"], "proof": proof, "method": method})


@pytest.mark.parametrize("field", ["action", "subject", "payload_hash", "decision"])
def test_a_signature_over_any_altered_binding_field_is_rejected_and_burns_the_challenge(cw, field):
    gid = cw.create(op=f"client-op-{field}0").body["goal"]["id"]
    ch = _goal_ch(cw, gid, "active")
    alt = {"action": "approval", "subject": gid + "x", "ph": h("active:99"), "decision": "cancelled"}
    assert _go(cw, gid, ch, "active", **{("ph" if field == "payload_hash" else field): alt["ph" if field == "payload_hash" else field]}).status == 403
    assert _go(cw, gid, ch, "active").status in (403, 409)                       # challenge yandı: düzgün imza ilə də təkrar olmur
    assert cw.dev.get(f"/v1/core/goals/{gid}").body["goal"]["status"] == "draft"  # heç bir keçid olmayıb


def test_the_server_binds_the_decision_to_the_challenge_not_to_the_request_body(cw):
    gid = cw.create().body["goal"]["id"]
    ch = _goal_ch(cw, gid, "active")
    assert ch["decision"] == "active"
    r = _go(cw, gid, ch, "cancelled")                                            # "active" üçün alınan challenge ilə "cancelled" tələbi
    assert r.status == 403
    assert cw.dev.get(f"/v1/core/goals/{gid}").body["goal"]["status"] == "draft"


def test_challenge_for_one_goal_cannot_transition_another_goal(cw):
    a = cw.create(op="client-op-aaaa1").body["goal"]["id"]
    b = cw.create(op="client-op-bbbb1").body["goal"]["id"]
    ch = _goal_ch(cw, a, "active")
    assert _go(cw, b, ch, "active", subject=a).status == 403                     # imza a-ya, sorğu b-yə
    assert _go(cw, b, ch, "active").status == 403                                # imza b-yə (subject dəyişib) — challenge a-ya bağlıdır
    assert cw.dev.get(f"/v1/core/goals/{b}").body["goal"]["status"] == "draft"


def test_stale_version_and_stale_ui_are_rejected(cw):
    gid = cw.create().body["goal"]["id"]
    ch = _goal_ch(cw, gid, "active")
    assert ch["payload_hash"] == h("active:1")
    assert _go(cw, gid, ch, "active").status == 200                              # v1 -> v2
    ch2 = _goal_ch(cw, gid, "paused")
    assert ch2["payload_hash"] == h("paused:2")
    # challenge verildikdən sonra məqsəd başqa yolla dəyişir (sahib başqa cihazdan / döngü): köhnə hash artıq cari deyil
    other = Dev(cw.app, cw.world.auth, name="İkinci", remote="10.0.0.7")
    assert _go(cw, gid, _goal_ch_dev(other, gid, "paused"), "paused", dev=other).status == 200      # v2 -> v3
    assert _go(cw, gid, ch2, "paused").status in (403, 409)                      # ch2 versiya 2-yə bağlı idi
    assert cw.dev.get(f"/v1/core/goals/{gid}").body["goal"]["version"] == 3


def _goal_ch_dev(dev, gid, to):
    return dev.post(f"/v1/core/goals/{gid}/challenge", {"to_status": to}).body


def test_replay_of_a_successful_transition_is_rejected(cw):
    gid = cw.create().body["goal"]["id"]
    ch = _goal_ch(cw, gid, "active")
    body = {"to_status": "active", "challenge_id": ch["challenge_id"], "proof": cw.dev.sign(ch, "core_goal", gid, ch["payload_hash"], "active"), "method": "biometric"}
    assert cw.dev.post(f"/v1/core/goals/{gid}/transition", body).status == 200
    assert cw.dev.post(f"/v1/core/goals/{gid}/transition", body).status in (403, 409)
    assert cw.dev.get(f"/v1/core/goals/{gid}").body["goal"]["version"] == 2     # ikinci dəfə tətbiq olunmayıb


def test_expired_challenge_is_rejected(cw):
    gid = cw.create().body["goal"]["id"]
    ch = _goal_ch(cw, gid, "active")
    cw.world.clock.t += ch["expires_in"] + 5
    assert _go(cw, gid, ch, "active").status == 403
    assert cw.dev.get(f"/v1/core/goals/{gid}").body["goal"]["status"] == "draft"


def test_goal_challenge_cannot_authorize_an_approval_or_a_step(cw):
    gid = cw.create().body["goal"]["id"]
    ch = _goal_ch(cw, gid, "active")
    proof = cw.dev.sign(ch, "core_goal", gid, ch["payload_hash"], "active")
    r = cw.dev.post("/v1/approvals/apr_nonexistent00/decision", {"decision": "approve", "challenge_id": ch["challenge_id"], "proof": proof, "method": "biometric"})
    assert r.status in (403, 404, 400)
    r2 = cw.dev.post("/v1/core/steps/plan_x/a/resolve", {"resolution": "retry", "challenge_id": ch["challenge_id"], "proof": proof, "method": "biometric"})
    assert r2.status in (403, 404, 409)
    assert _go(cw, gid, ch, "active").status in (200, 403, 409)                  # challenge hələ də yalnız öz əməliyyatına bağlıdır (200 yalnız ona görə ki, yuxarıdakılar onu yandırmayıb)


def test_foreign_device_and_voice_are_rejected(cw):
    gid = cw.create().body["goal"]["id"]
    other = Dev(cw.app, cw.world.auth, name="Başqa", remote="10.0.0.9")
    ch = _goal_ch(cw, gid, "active")
    assert _go(cw, gid, ch, "active", dev=other).status == 403                  # başqa cihaz öz açarı ilə imzalayır (challenge təyin olunmayıb)
    ch2 = _goal_ch(cw, gid, "active")
    assert _go(cw, gid, ch2, "active", method="voice").status == 403
    assert cw.dev.get(f"/v1/core/goals/{gid}").body["goal"]["status"] == "draft"


def test_step_resolution_binding_is_enforced_server_side(cw):
    gid = cw.create().body["goal"]["id"]
    cw.transition(gid, "active", 1)
    [cw.loop.tick() for _ in range(3)]
    plan = cw.faces.engine.current_plan(gid)
    cw.faces.engine.begin_step(plan.plan_id, 1, "a", "core.sfop0001", 1, 60)
    cw.faces.engine.finish_step(plan.plan_id, 1, "a", "core.sfop0001", StepState.UNKNOWN, "", "", False, "executor_exception")
    path, sub = f"/v1/core/steps/{plan.plan_id}/a", f"{plan.plan_id}/a"
    for alt in ({"decision": "succeeded"}, {"ph": h("retry:2:unknown")}, {"ph": h("retry:1:failed")}, {"subject": sub + "z"}, {"action": "core_goal"}):
        ch = cw.dev.post(path + "/challenge", {"resolution": "retry"}).body
        proof = cw.dev.sign(ch, alt.get("action", "core_step"), alt.get("subject", sub), alt.get("ph", ch["payload_hash"]), alt.get("decision", "retry"))
        assert cw.dev.post(path + "/resolve", {"resolution": "retry", "challenge_id": ch["challenge_id"], "proof": proof, "method": "biometric"}).status == 403, alt
        cw.world.clock.t += 601                                                  # uğursuz cəhd kilidi (429) hər halı müstəqil yoxlamaq üçün pəncərədən çıxarılır
    ch = cw.dev.post(path + "/challenge", {"resolution": "retry"}).body
    proof = cw.dev.sign(ch, "core_step", sub, ch["payload_hash"], "retry")
    assert cw.dev.post(path + "/resolve", {"resolution": "failed", "challenge_id": ch["challenge_id"], "proof": proof, "method": "biometric"}).status == 403   # qərar uyğunsuzluğu
    assert cw.faces.owner.steps(plan.plan_id)[0].state is StepState.UNKNOWN       # heç biri tətbiq olunmayıb


def test_cross_language_vectors_are_current():
    """Kotlin və Python eyni baytları/hash-ləri hesablamalıdır: vektor faylı canlı Python hesablamasına bərabər olmalıdır."""
    from api.auth import AuthService
    data = json.loads(VECTORS.read_text())
    assert data["vectors"], "vektor yoxdur"
    for v in data["vectors"]:
        msg = AuthService.step_up_message(v["challenge_id"], v["nonce"], v["action"], v["subject"], v["payload_hash"], v["decision"])
        assert msg.decode() == v["message"]
        if v["action"] == "core_goal":
            assert v["payload_hash"] == h(f"{v['decision']}:{v['version']}")
        else:
            assert v["payload_hash"] == h(f"{v['decision']}:{v['attempt']}:{v['state']}")
