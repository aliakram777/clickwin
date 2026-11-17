"""Autentifikasiya, cihaz cütləşməsi, sessiyalar və step-up (həssas əməliyyatlar üçün sübut). Simulyasiya olunan
"cihaz" test obyektidir: real Android cihazı və ya real istifadəçi deyil."""
import ast
import base64
import json
import re
import sqlite3
from pathlib import Path

import pytest

from api.auth import AuthError, AuthService
from tests.p256_tools import Keypair

ROOT = Path(__file__).resolve().parent.parent
H = "a" * 64


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    audit = tmp_path / "audit.jsonl"
    auth = AuthService(tmp_path / "auth.db", audit, clock=clock, owner_id="owner")
    return auth, clock, audit, tmp_path


def b64(b):
    return base64.b64encode(b).decode()


class Dev:
    """Android cihazının simulyasiyası: şəxsi açar yalnız burada qalır (Keystore kimi); server yalnız açıq açarı görür."""

    def __init__(self, res, kp):
        self.device_id, self.access_token, self.refresh_token, self.kp = res.device_id, res.access_token, res.refresh_token, kp
        self.result = res

    def sign(self, ch, action, subject, phash, decision):
        return b64(self.kp.sign(AuthService.step_up_message(ch.id, ch.nonce, action, subject, phash, decision)))


def join(auth, code, name, remote, kp=None):
    kp = kp or Keypair()
    return Dev(auth.pair(code, name, remote, b64(kp.spki()), b64(kp.sign(AuthService.pair_message(code)))), kp)


def pair(auth, name="Pixel 8", remote="10.0.0.5"):
    return join(auth, auth.create_pairing_code(), name, remote)


def audit_events(audit):
    p = Path(audit)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# --- cütləşmə kodu ---

def test_pairing_code_is_short_lived_single_use_and_unambiguous(env):
    auth, clock, _, _ = env
    code = auth.create_pairing_code()
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", code)
    res = join(auth, code, "Pixel 8", "10.0.0.5")
    assert res.access_token.startswith("nay_a_") and res.refresh_token.startswith("nay_r_")
    assert len(res.access_token) >= 40 and not hasattr(res.result, "device_secret")
    with pytest.raises(AuthError) as e:
        join(auth, code, "Başqa", "10.0.0.5")  # təkrar istifadə
    assert e.value.status == 401
    code2 = auth.create_pairing_code()
    clock.t += 601
    with pytest.raises(AuthError):
        join(auth, code2, "Gec", "10.0.0.5")


def test_wrong_codes_trigger_a_lockout_even_for_the_right_code(env):
    auth, clock, audit, _ = env
    good = auth.create_pairing_code()
    for _ in range(5):
        with pytest.raises(AuthError) as e:
            join(auth, "WRONGCOD", "x", "6.6.6.6")
        assert e.value.status == 401
    with pytest.raises(AuthError) as e:
        join(auth, good, "x", "6.6.6.6")
    assert e.value.status == 429
    assert join(auth, good, "x", "10.0.0.9").device_id  # başqa ünvan bloklanmayıb
    clock.t += 700
    assert "auth_pair_denied" in [ev["event"] for ev in audit_events(audit)]


def test_tokens_and_codes_are_never_stored_in_plaintext(env):
    auth, _, audit, tmp = env
    code = auth.create_pairing_code()
    res = join(auth, code, "Pixel", "10.0.0.5")
    blob = ""
    conn = sqlite3.connect(tmp / "auth.db")
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        blob += json.dumps(conn.execute(f"SELECT * FROM {name}").fetchall(), default=str)
    for secret in (code, res.access_token, res.refresh_token):
        assert secret not in blob and secret not in Path(audit).read_text()


def test_device_name_is_sanitized(env):
    auth, *_ = env
    res = join(auth, auth.create_pairing_code(), "Telefon\x00<b>x</b>\n" + "y" * 100, "10.0.0.5")
    [d] = auth.list_devices()
    assert d["device_id"] == res.device_id and len(d["name"]) <= 40 and "<" not in d["name"]
    with pytest.raises(AuthError):
        join(auth, auth.create_pairing_code(), "   ", "10.0.0.5")


# --- sessiya ---

def test_access_token_authenticates_until_expiry_or_revocation(env):
    auth, clock, _, _ = env
    res = pair(auth)
    s = auth.authenticate(res.access_token)
    assert s.device_id == res.device_id and s.owner_id == "owner"
    for bad in ("", "nay_a_" + "x" * 43, "Bearer x", res.refresh_token, None, 5, "nay_a_" + "!" * 43):
        with pytest.raises(AuthError) as e:
            auth.authenticate(bad)
        assert e.value.status == 401
    clock.t += 901
    with pytest.raises(AuthError):
        auth.authenticate(res.access_token)


def test_refresh_rotates_and_reuse_revokes_the_whole_device(env):
    auth, clock, audit, _ = env
    res = pair(auth)
    new = auth.refresh(res.refresh_token, "10.0.0.5")
    assert new.access_token != res.access_token and new.refresh_token != res.refresh_token
    assert auth.authenticate(new.access_token).device_id == res.device_id
    with pytest.raises(AuthError) as e:
        auth.refresh(res.refresh_token, "10.0.0.5")  # köhnə token təkrar: oğurlanma əlaməti
    assert e.value.status == 401
    for tok in (new.access_token, res.access_token):
        with pytest.raises(AuthError):
            auth.authenticate(tok)
    with pytest.raises(AuthError):
        auth.refresh(new.refresh_token, "10.0.0.5")
    assert "auth_refresh_reuse" in [ev["event"] for ev in audit_events(audit)]
    assert auth.list_devices()[0]["status"] == "revoked"


def test_refresh_token_expires_and_must_be_a_refresh_token(env):
    auth, clock, _, _ = env
    res = pair(auth)
    with pytest.raises(AuthError):
        auth.refresh(res.access_token, "10.0.0.5")
    clock.t += 31 * 86400
    with pytest.raises(AuthError):
        auth.refresh(res.refresh_token, "10.0.0.5")


def test_logout_and_admin_revocation(env):
    auth, *_ = env
    a, b = pair(auth, "A"), pair(auth, "B")
    auth.logout(a.device_id)
    with pytest.raises(AuthError):
        auth.authenticate(a.access_token)
    with pytest.raises(AuthError):
        auth.refresh(a.refresh_token, "10.0.0.5")
    auth.revoke_device(b.device_id)
    with pytest.raises(AuthError):
        auth.authenticate(b.access_token)
    assert {d["status"] for d in auth.list_devices()} == {"revoked"}
    assert all(set(d) == {"device_id", "name", "status", "created_at", "last_seen"} for d in auth.list_devices())


def test_rate_limit_helper(env):
    auth, clock, *_ = env
    for _ in range(3):
        auth.hit("k", 3, 60)
    with pytest.raises(AuthError) as e:
        auth.hit("k", 3, 60)
    assert e.value.status == 429
    clock.t += 61
    auth.hit("k", 3, 60)


# --- step-up: həssas əməliyyatlar üçün cihaz sübutu ---

def challenge(auth, dev, action="approval", subject="abc", phash=H, decision="approve"):
    ch = auth.create_challenge(dev.device_id, action, subject, phash, decision)
    return ch, dev.sign(ch, action, subject, phash, decision)


def verify(auth, dev, ch, proof, action="approval", subject="abc", phash=H, decision="approve"):
    return auth.verify_proof(dev.device_id, ch.id, action, subject, phash, decision, proof)


def test_valid_proof_is_accepted_once(env):
    auth, clock, audit, _ = env
    dev = pair(auth)
    ch, proof = challenge(auth, dev)
    assert ch.expires_in == 60 and len(ch.nonce) >= 32
    verify(auth, dev, ch, proof)
    with pytest.raises(AuthError):
        verify(auth, dev, ch, proof)  # tək istifadəlik
    assert "auth_stepup_ok" in [e["event"] for e in audit_events(audit)]


@pytest.mark.parametrize("change", [
    dict(subject="başqa"), dict(phash="b" * 64), dict(decision="deny"), dict(action="resume"),
])
def test_proof_is_bound_to_action_subject_payload_and_decision(env, change):
    auth, _, _, _ = env
    dev = pair(auth)
    ch, proof = challenge(auth, dev)
    with pytest.raises(AuthError) as e:
        verify(auth, dev, ch, proof, **change)
    assert e.value.status in (401, 403)


def test_wrong_other_device_expired_and_unknown_proofs_are_rejected(env):
    auth, clock, _, _ = env
    a, b = pair(auth, "A"), pair(auth, "B")
    ch, proof = challenge(auth, a)
    with pytest.raises(AuthError):
        verify(auth, a, ch, b64(b"\x00" * 70))
    ch2, proof2 = challenge(auth, a)
    with pytest.raises(AuthError):
        auth.verify_proof(b.device_id, ch2.id, "approval", "abc", H, "approve", proof2)  # başqa cihaz
    with pytest.raises(AuthError):
        verify(auth, a, ch2, b.sign(ch2, "approval", "abc", H, "approve"))  # başqa cihazın açarı ilə imza
    ch3, proof3 = challenge(auth, a)
    clock.t += 61
    with pytest.raises(AuthError):
        verify(auth, a, ch3, proof3)
    with pytest.raises(AuthError):
        auth.verify_proof(a.device_id, "yoxdur", "approval", "abc", H, "approve", proof)
    for junk in (None, 5, "", "zz", "!!!", b64(b"\x30\x00")):
        with pytest.raises(AuthError):
            verify(auth, a, ch2, junk)


def test_repeated_failed_proofs_lock_the_device_for_stepup(env):
    auth, clock, audit, _ = env
    dev = pair(auth)
    for _ in range(5):
        ch, _ = challenge(auth, dev)
        with pytest.raises(AuthError):
            verify(auth, dev, ch, b64(b"\x00" * 70))
    ch, proof = challenge(auth, dev)
    with pytest.raises(AuthError) as e:
        verify(auth, dev, ch, proof)  # düzgün sübut belə bloklanır
    assert e.value.status == 429
    assert "auth_stepup_denied" in [ev["event"] for ev in audit_events(audit)]


def test_revoked_device_cannot_get_challenges(env):
    auth, *_ = env
    dev = pair(auth)
    auth.revoke_device(dev.device_id)
    with pytest.raises(AuthError):
        auth.create_challenge(dev.device_id, "approval", "abc", H, "approve")


def test_audit_contains_no_tokens_secrets_or_codes(env):
    auth, _, audit, _ = env
    code = auth.create_pairing_code()
    dev = join(auth, code, "P", "1.2.3.4")
    ch, proof = challenge(auth, dev)
    verify(auth, dev, ch, proof)
    auth.refresh(dev.refresh_token, "1.2.3.4")
    text = Path(audit).read_text()
    for secret in (code, dev.access_token, dev.refresh_token, proof, ch.nonce):
        assert secret not in text


def test_auth_module_verifies_signatures_with_the_public_key_only_and_uses_no_network():
    tree = ast.parse((ROOT / "api" / "auth.py").read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "verify" in attrs and "hmac" not in {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    banned = {"requests", "socket", "subprocess", "http", "urllib", "ssl"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            assert not banned & {a.name.split(".")[0] for a in n.names}
        if isinstance(n, ast.ImportFrom):
            assert (n.module or "").split(".")[0] not in banned


# --- açıq açar ilə cütləşmə (Android Keystore) ---

def test_pairing_requires_a_valid_public_key_and_proof_of_possession(env):
    auth, _, audit, _ = env
    kp, other = Keypair(), Keypair()
    code = auth.create_pairing_code()
    msg = AuthService.pair_message(code)
    cases = [
        (b64(kp.spki()), b64(other.sign(msg))),      # başqa açarla imza: sahiblik sübutu yoxdur
        (b64(kp.spki()), b64(kp.sign(b"x" + msg))),  # başqa mesaj
        (b64(kp.spki()[:-1]), b64(kp.sign(msg))),    # pozuq açıq açar
        ("!!!", b64(kp.sign(msg))), (None, None), (5, "x"), ("", ""),
    ]
    for i, (pub, sig) in enumerate(cases):
        with pytest.raises(AuthError) as e:
            auth.pair(code, "Pixel", f"10.0.1.{i}", pub, sig)
        assert e.value.status in (400, 401)
    assert auth.list_devices() == []
    # kod bu uğursuz cəhdlərdə yandırılmayıb: düzgün açarla hələ işləyir
    assert join(auth, code, "Pixel", "10.0.0.5", kp).device_id


def test_server_stores_only_the_public_key(env):
    auth, _, audit, tmp = env
    dev = pair(auth)
    conn = sqlite3.connect(tmp / "auth.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(devices)").fetchall()]
    assert "public_key" in cols and "secret" not in cols
    blob = json.dumps([list(map(str, r)) for r in conn.execute("SELECT * FROM devices").fetchall()])
    assert b64(dev.kp.spki()) in blob and str(dev.kp._d) not in blob
