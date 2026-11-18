"""A-7: naməlum rezervin həlli YALNIZ həmin rezerv id-sinə + nəticəyə/məbləğə bağlı, bir-dəfəlik sahib step-up təsdiqi ilə mümkündür."""
from __future__ import annotations

import dataclasses
import types

import pytest

from agents.factory_auth import generate_signing_seed
from api.bootstrap import build_app
from runtime.factory_bootstrap import TrustAnchor, load_signing_config
from safety.approvals import Principal
from safety.ledger import CostLedger, ResolutionDenied, resolution_scope
from tests.factory_keys import DISPATCHER, SIGNER, SYSTEM, T0, owner_cred, ring
from tests.test_api_auth import pair
from tests.test_security_boundary import Clock, authenticate


def approval(op_id, actual, *, scope_op="resolve_unknown", owner_id="owner", ttl=900, signer=None, company=""):
    subject, digest = resolution_scope(op_id, actual)
    return owner_cred(scope_op, subject, digest, owner_id, ttl=ttl, signer=signer, company=company)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    ring_ = ring(clock)
    led = CostLedger(tmp_path / "l.db", verifier=ring_, owners={"owner"}, clock=clock)
    for op in ("opA", "opB"):
        assert led.reserve(op, "agent", 0.5).ok and led.mark_unknown(op)
    return led, clock, tmp_path


def untouched(led, *ops):
    return all(e.unknown and e.status == "reserved" and abs(e.amount - 0.5) < 1e-9 for e in led.entries() if e.op_id in ops)


def test_exact_approval_resolves_once_for_release_and_for_commit(env):
    led, _, _ = env
    assert led.resolve_unknown("opA", None, approval("opA", None)) is True                 # xərc yoxdur: azad
    assert led.resolve_unknown("opB", 0.3, approval("opB", 0.3)) is True                   # faktiki xərc
    assert abs(led.total() - 0.3) < 1e-9 and led.unknown_entries() == []
    assert led.resolve_unknown("opA", None, approval("opA", None)) is False                # artıq naməlum deyil


def test_without_a_verifier_resolution_is_closed(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    assert led.reserve("opA", "a", 0.5).ok and led.mark_unknown("opA")
    for ap in (approval("opA", None), None, "owner", Principal("owner", "human", "x")):
        with pytest.raises(ResolutionDenied) as e:
            led.resolve_unknown("opA", None, ap)
        assert e.value.code == "owner_auth_unavailable"
    assert untouched(led, "opA")


@pytest.mark.parametrize("make", [
    lambda: None, lambda: "owner", lambda: Principal("owner", "human", "stepup:x"),                                   # özü yaradılmış/imzasız
    lambda: dataclasses.replace(approval("opA", None), id="mallory"),                                                  # dəyişdirilmiş
    lambda: dataclasses.replace(approval("opA", None), scope_subject="opB"),                                           # imza əhatəyə baxır
    lambda: dataclasses.replace(approval("opA", None), signature="A" * 86),
])
def test_forged_or_missing_approvals_are_rejected(env, make):
    led, _, _ = env
    with pytest.raises(ResolutionDenied) as e:
        led.resolve_unknown("opA", None, make())
    assert e.value.code in ("forged_principal", "unauthorized")
    assert untouched(led, "opA", "opB")


def test_approval_bound_to_another_reservation_amount_or_outcome_is_rejected(env):
    led, _, _ = env
    cases = [
        ("opA", None, approval("opB", None)),             # başqa rezerv id-si
        ("opA", 0.3, approval("opA", 0.4)),               # başqa məbləğ
        ("opA", 0.3, approval("opA", None)),              # azad təsdiqi ilə xərc təsdiqi
        ("opA", None, approval("opA", 0.0)),              # 0 məbləğli təsdiq ilə azad etmə (nəticə fərqli)
        ("opA", None, approval("opA", None, scope_op="set_budget")),     # başqa əməliyyat üçün etibarnamə
        ("opA", None, approval("opA", None, company="a" * 64)),          # şirkət təsiri olan etibarnamə
        ("opA", None, owner_cred("resolve_unknown", "opA")),             # sabit/boş hash
    ]
    for op, actual, ap in cases:
        with pytest.raises(ResolutionDenied) as e:
            led.resolve_unknown(op, actual, ap)
        assert e.value.code == "scope_mismatch", (op, actual)
    assert untouched(led, "opA", "opB")
    assert led.resolve_unknown("opA", None, approval("opA", None)) is True               # uyğunsuz cəhdlər etibarnaməni yandırmadı və heç nə dəyişmədi


def test_wrong_roles_non_owners_mock_and_expired_or_revoked_approvals_are_rejected(env):
    led, clock, tmp = env
    for who in (SYSTEM, DISPATCHER):
        with pytest.raises(ResolutionDenied):
            led.resolve_unknown("opA", None, who)
    with pytest.raises(ResolutionDenied) as e:                                           # imzalıdır, amma sahib siyahısında deyil
        led.resolve_unknown("opA", None, approval("opA", None, owner_id="stranger"))
    assert e.value.code == "unauthorized"
    mock = SIGNER.mock_owner_issuer(ttl=900).principal("owner", "test", ("resolve_unknown", *resolution_scope("opA", None), ""))
    with pytest.raises(ResolutionDenied):                                                # mock təsdiq real sahib deyil
        led.resolve_unknown("opA", None, mock)
    short = approval("opA", None, ttl=30)
    clock.t = T0 + 31
    with pytest.raises(ResolutionDenied) as e2:
        led.resolve_unknown("opA", None, short)
    assert e2.value.code == "credential_expired"
    clock.t = T0
    # ləğv olunmuş etibarnamə
    from tests.factory_keys import ring_with_control
    r, control = ring_with_control(clock)
    led2 = CostLedger(tmp / "l2.db", verifier=r, owners={"owner"}, clock=clock)
    assert led2.reserve("opA", "a", 0.5).ok and led2.mark_unknown("opA")
    ap = approval("opA", None)
    control.revoke_credential(ap.nonce)
    with pytest.raises(ResolutionDenied) as e3:
        led2.resolve_unknown("opA", None, ap)
    assert e3.value.code == "credential_revoked"
    assert untouched(led, "opA", "opB") and untouched(led2, "opA")


def test_an_approval_cannot_be_replayed_even_if_the_reservation_becomes_unknown_again(env):
    led, _, _ = env
    ap = approval("opA", None)
    assert led.resolve_unknown("opA", None, ap) is True
    assert led.reserve("opA", "agent", 0.5).ok and led.mark_unknown("opA")                # azad edilmiş op_id yenidən naməlum
    with pytest.raises(ResolutionDenied) as e:
        led.resolve_unknown("opA", None, ap)
    assert e.value.code == "credential_replayed"
    assert untouched(led, "opA")
    assert led.resolve_unknown("opA", None, approval("opA", None)) is True               # yeni təsdiq lazımdır


def test_a_missing_or_known_reservation_returns_false_and_does_not_burn_the_approval(env):
    led, _, _ = env
    ap = approval("ghost", None)
    assert led.resolve_unknown("ghost", None, ap) is False
    assert led.reserve("ghost", "a", 0.1).ok                                              # naməlum deyil, yalnız rezerv
    assert led.resolve_unknown("ghost", None, ap) is False
    assert led.mark_unknown("ghost")
    assert led.resolve_unknown("ghost", None, ap) is True                                 # təsdiq heç vaxt yandırılmayıb


def test_production_owner_step_up_resolves_and_a_revoked_device_cannot(tmp_path):
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-led-001"})
    anchor = TrustAnchor(cfg)
    svc = build_app(tmp_path / "data", owner_id="owner", global_limit=5.0, trust=anchor)
    dev = pair(svc.auth)
    p = types.SimpleNamespace(stack=svc.factory, dev=dev)
    led = svc.reconciler._ledger
    for op in ("u1", "u2"):
        assert led.reserve(op, "agent", 0.5).ok and led.mark_unknown(op)
    assert [e.op_id for e in svc.reconciler.unknown()] == ["u1", "u2"]
    subject, digest = svc.reconciler.scope("u1", 0.2)
    good = authenticate(p, "resolve_unknown", subject, digest)                            # cihazın Keystore imzası ilə step-up
    with pytest.raises(ResolutionDenied) as e:
        svc.reconciler.resolve("u1", 0.9, good)                                           # başqa məbləğ
    assert e.value.code == "scope_mismatch"
    assert svc.reconciler.resolve("u1", 0.2, good) is True
    s2, d2 = svc.reconciler.scope("u2", None)
    pending = authenticate(p, "resolve_unknown", s2, d2)
    svc.auth.revoke_device(dev.device_id)                                                 # cihaz ləğvi: verilmiş təsdiq dərhal yararsız
    with pytest.raises(ResolutionDenied) as e2:
        svc.reconciler.resolve("u2", None, pending)
    assert e2.value.code == "credential_revoked"
    assert [e.op_id for e in svc.reconciler.unknown()] == ["u2"]
    svc.factory.agents.close()


def test_reconciler_exposes_no_write_surface_beyond_resolution(tmp_path):
    cfg = load_signing_config({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-led-002"})
    svc = build_app(tmp_path / "d", owner_id="owner", global_limit=1.0, trust=TrustAnchor(cfg))
    rec = svc.reconciler
    assert not any(hasattr(rec, n) for n in ("reserve", "commit", "release", "mark_unknown", "record", "expire_stale", "_conn"))
    with pytest.raises(AttributeError):
        rec.resolve = lambda *a: True
    svc.factory.agents.close()
