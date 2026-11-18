"""Fabrik və Company sərhədində asimmetrik (Ed25519) imzalı etibarnamələr (audit F-1, A-1..A-6).

Model:
- ŞƏXSİ AÇAR yalnız `TrustedSigner`-dədir (etibarlı bootstrap/autentifikasiya kodu). Agentlərə baxan heç bir komponent (fabrik nüvəsi, `AgentHandle`,
  `Company`, `Keyring`) imzalama açarına və ya imza verən obyektə sahib deyil: onlara YALNIZ yoxlama imkanı (`Keyring`, açıq açarlar) verilir.
- İmza verən obyektlər ROL-a bağlıdır (`SystemIssuer`, `DispatcherIssuer`, `OwnerIssuer`, `MockOwnerIssuer`): hər birinin imza funksiyası
  yalnız öz rolunu imzalaya bilir. Sahib etibarnaməsi üçün ixtiyari `owner_id` qəbul edən metod YOXDUR: `OwnerIssuer.from_step_up(receipt)`
  yalnız `api/auth.py` step-up yoxlamasından keçmiş, bir dəfəlik və vaxtlı qəbzi qəbul edir.
- Etibarnamələr (şəxsiyyət və tapşırıq səlahiyyəti) qısa ömürlüdür (`expires_at`), açar identifikatoru (`key_id`) daşıyır; `Keyring` açarı və ya
  tək etibarnaməni (nonce) ləğv edə bilər. Naməlum/saxta/vaxtı keçmiş/ləğv olunmuş/yanlış formatlı etibarnamə QAPALI qalır.
- `DispatchAuthorization` bir hərəkətə (`submit_result`, `fail`, `await_approval`, `note_success`), bir tapşırığa, agentə, icra op-una, əməliyyat id-sinə
  və cəhd nömrəsinə bağlıdır; faktiki bir-dəfəlik istifadə (nonce) fabrikin bazasında qeyd olunur.

Məhdudiyyət: bu proses-daxili sərhəddir. Eyni prosesdə ixtiyari kod icra edə bilən tərəf qarşısında (məs. yaddaşı oxumaq, `__closure__`) qoruma vəd
edilmir; bu OS sandbox deyil. Açıq açarlar və yoxlama mexanizmi gizli deyil, şəxsi açar isə yalnız etibarlı bootstrap tərəfindədir.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from safety.approvals import Principal

ROLES = ("owner", "system", "dispatcher", "mock_owner")
ACTIONS = ("submit_result", "fail", "await_approval", "note_success")
MAX_PRINCIPAL_TTL = 900.0
MAX_AUTH_TTL = 300.0
CLOCK_SKEW = 5.0
DEFAULT_OWNER_TTL = 120.0
DEFAULT_SYSTEM_TTL = 300.0
DEFAULT_DISPATCHER_TTL = 120.0
DEFAULT_AUTH_TTL = 60.0
_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,40}$")
_B64_32 = re.compile(r"^[A-Za-z0-9_\-]{43}=?$")


class CredentialError(ValueError):
    """`code`: forged_principal | credential_expired | credential_revoked | role_not_allowed | weak_key | bad_key | proof_required."""

    def __init__(self, message: str, code: str = "forged_principal") -> None:
        super().__init__(message)
        self.code = code


# --- etibarnamə tipləri ---

@dataclass(frozen=True)
class SignedPrincipal(Principal):
    role: str = ""
    key_id: str = ""
    issued_at: float = 0.0
    expires_at: float = 0.0
    nonce: str = ""
    # Sahib etibarnaməsi (B-3) YALNIZ BİR dəqiq əməliyyata bağlıdır: əməliyyat adı, hədəf (subject) və parametrlərin SHA-256-sı. Digər rollarda boşdur.
    scope_op: str = ""
    scope_subject: str = ""
    scope_hash: str = ""
    signature: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class DispatchAuthorization:
    """Tapşırığa, agentə, icra op-una, əməliyyat id-sinə, cəhdə və HƏRƏKƏTƏ bağlı qısa ömürlü, bir-dəfəlik səlahiyyət."""
    task_id: str
    agent_id: str
    run_op: str
    op_id: str
    attempt: int
    action: str
    key_id: str = ""
    issued_at: float = 0.0
    expires_at: float = 0.0
    nonce: str = ""
    signature: str = field(default="", repr=False, compare=False)


def _num(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _principal_bytes(p: Any) -> bytes:
    fields = [p.id, p.kind, p.channel, p.role, p.key_id, p.issued_at, p.expires_at, p.nonce, p.scope_op, p.scope_subject, p.scope_hash]
    return b"naymera-principal-v3\x1f" + json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _authorization_bytes(a: Any) -> bytes:
    fields = [a.task_id, a.agent_id, a.run_op, a.op_id, a.attempt, a.action, a.key_id, a.issued_at, a.expires_at, a.nonce]
    return b"naymera-dispatch-v2\x1f" + json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,200}", text):
        raise ValueError("base64")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- açar keyfiyyəti ---

_COMMON_PHRASES = ("password", "passw0rd", "secret", "changeme", "admin", "root", "test", "testing", "naymera", "naymera-signing-key", "signing-key", "signing_key",
                   "key", "qwerty", "123456", "12345678", "letmein", "welcome", "default", "private", "privatekey", "private-key", "ed25519", "00000000", "a" * 32)


def _weak_digests() -> frozenset[bytes]:
    import hashlib
    out = set()
    for ph in _COMMON_PHRASES:
        b = ph.encode("utf-8")
        d1 = hashlib.sha256(b).digest()
        out.update({d1, hashlib.sha256(d1).digest(), hashlib.sha256(b + b"\n").digest(), hashlib.sha256(b.upper()).digest()})
    return frozenset(out)


_WEAK = _weak_digests()


def _longest_bit_run(b: bytes) -> int:
    bits = "".join(f"{x:08b}" for x in b)
    best = cur = 1
    for i in range(1, len(bits)):
        cur = cur + 1 if bits[i] == bits[i - 1] else 1
        best = max(best, cur)
    return best


def validate_signing_seed(seed: Any) -> None:
    """Şəxsi açar materialı 32 bayt kriptoqrafik təsadüfi olmalıdır (`secrets.token_bytes(32)`). Bu yoxlama təsadüfiliyin SÜBUTU DEYİL; bariz zəif/statik/törəmə açarları kəsir
    (təsadüfi açarın səhvən rədd olunma ehtimalı təxminən 5e-5-dir, generator rədd olunanı yenidən yaradır):
    az fərqli bayt, bayt təkrarı, sabit-fərqli (arifmetik) ardıcıllıq (istənilən addım, mod 256), qısa dövr, çap olunan/7-bit mətn, bit sayı (monobit) və ən uzun eyni-bit seriyası
    paylanmadan kənarda, tanınmış parol/ifadə hash-ləri (sha256 və iki-qat sha256)."""
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise CredentialError("imza açarı tam 32 bayt olmalıdır", "weak_key")
    b = bytes(seed)
    if len(set(b)) < 20:
        raise CredentialError("imza açarı kifayət qədər təsadüfi deyil (az fərqli bayt)", "weak_key")
    if max(b.count(x) for x in set(b)) > 4:
        raise CredentialError("imza açarında bayt çox təkrarlanır", "weak_key")
    for period in (1, 2, 3, 4, 5, 6, 7, 8, 16):
        if all(b[i] == b[i % period] for i in range(32)):
            raise CredentialError("imza açarı təkrarlanan nümunədir", "weak_key")
    diffs = [(b[i + 1] - b[i]) % 256 for i in range(31)]
    if max(diffs.count(d) for d in set(diffs)) >= 20:
        raise CredentialError("imza açarı arifmetik/sabit-addımlı ardıcıllıqdır", "weak_key")
    if all(x < 0x80 for x in b):
        raise CredentialError("imza açarı oxunaqlı/7-bit mətndir (parol deyil, təsadüfi bayt olmalıdır)", "weak_key")
    ones = sum(bin(x).count("1") for x in b)
    if not 88 <= ones <= 168 or _longest_bit_run(b) > 28:
        raise CredentialError("imza açarının bit paylanması təsadüfi deyil", "weak_key")
    if b in _WEAK:
        raise CredentialError("imza açarı tanınmış zəif ifadənin hash-idir", "weak_key")


def generate_signing_seed() -> str:
    """Təhlükəsiz açar yaratma: 32 təsadüfi bayt (`secrets`), URL-təhlükəsiz base64 (43 simvol). Heç bir yerdə saxlanmır/loqlanmır."""
    while True:
        raw = secrets.token_bytes(32)
        try:
            validate_signing_seed(raw)
        except CredentialError:
            continue
        return _b64e(raw)


def decode_signing_seed(text: Any) -> bytes:
    if not isinstance(text, str) or not _B64_32.match(text.strip()):
        raise CredentialError("imza açarı 32 baytın URL-təhlükəsiz base64 (43 simvol) formatında olmalıdır", "bad_key")
    raw = _b64d(text.strip().rstrip("="))
    if len(raw) != 32:
        raise CredentialError("imza açarı 32 bayt deyil", "bad_key")
    if _b64e(raw) != text.strip().rstrip("="):
        raise CredentialError("imza açarı kanonik base64 deyil", "bad_key")
    validate_signing_seed(raw)
    return raw


def valid_key_id(key_id: Any) -> bool:
    return isinstance(key_id, str) and bool(_KEY_ID_RE.match(key_id))


def decode_public_key(text: Any) -> bytes:
    if not isinstance(text, str) or not _B64_32.match(text.strip()):
        raise CredentialError("açıq açar 32 baytın URL-təhlükəsiz base64 formatında olmalıdır", "bad_key")
    raw = _b64d(text.strip().rstrip("="))
    if len(raw) != 32:
        raise CredentialError("açıq açar 32 bayt deyil", "bad_key")
    return raw


# --- yalnız yoxlama ---

class Keyring:
    """Yalnız YOXLAMA (ictimai səth): `check_principal`, `check_authorization`, `key_ids`, `frozen`. Açıq açar əlavə etmək, açar/etibarnamə ləğvi və dondurma bu
    obyektdə YOXDUR: onlar yalnız `take_control()` ilə BİR dəfə alınan `KeyringControl`-dadır (etibarlı bootstrap/control-plane). Nüvədə/Company-də/yoxlayıcıda
    yalnız bu yoxlama üzü saxlanır. Qeyd: bu proses-daxili ayrılıqdır; agent kodu AYRI prosesdə işləməlidir (`runtime/agent_gateway.py`)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._revoked_keys: set[str] = set()
        self._revoked_nonces: set[str] = set()
        self._revoked_channels: set[str] = set()
        self._frozen = False
        self._control_taken = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Keyring(keys={sorted(self._keys)}, revoked={sorted(self._revoked_keys)}, frozen={self._frozen})"

    @property
    def frozen(self) -> bool:
        return self._frozen

    def take_control(self) -> "KeyringControl":
        """Control-plane tutacağı YALNIZ BİR dəfə verilir (bootstrap qurulan kimi alır); sonrakı çağırışlar rədd edilir."""
        with self._lock:
            if self._control_taken:
                raise CredentialError("keyring idarəsi artıq götürülüb", "keyring_control_taken")
            self._control_taken = True
        return KeyringControl(self)

    def _add(self, key_id: str, public: bytes | str) -> None:
        if not isinstance(key_id, str) or not _KEY_ID_RE.match(key_id):
            raise CredentialError("açar id yanlışdır", "bad_key")
        raw = decode_public_key(public) if isinstance(public, str) else bytes(public)
        if len(raw) != 32:
            raise CredentialError("açıq açar 32 bayt olmalıdır", "bad_key")
        with self._lock:
            if self._frozen:
                raise CredentialError("etibarlı açar konfiqurasiyası dondurulub: yeni açar əlavə/dəyişdirmək olmaz", "keyring_frozen")
            if key_id in self._keys:
                raise CredentialError("açar id artıq mövcuddur", "bad_key")
            self._keys[key_id] = Ed25519PublicKey.from_public_bytes(raw)

    def _revoke_key(self, key_id: str) -> None:
        with self._lock:
            self._revoked_keys.add(str(key_id))

    def _revoke_credential(self, nonce: str) -> None:
        with self._lock:
            self._revoked_nonces.add(str(nonce))

    def _revoke_channel(self, channel: str) -> None:
        with self._lock:
            self._revoked_channels.add(str(channel))

    def _freeze(self) -> None:
        with self._lock:
            self._frozen = True

    def key_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._keys))

    def _check(self, payload: bytes, sig_text: str, key_id: Any, nonce: Any, issued: Any, expires: Any, max_ttl: float) -> None:
        if not isinstance(key_id, str) or not isinstance(nonce, str) or not nonce or not _num(issued) or not _num(expires) \
                or not isinstance(sig_text, str) or not sig_text:
            raise CredentialError("etibarnamə yanlış formatdadır", "forged_principal")
        with self._lock:
            key = self._keys.get(key_id)
            key_revoked = key_id in self._revoked_keys
            nonce_revoked = nonce in self._revoked_nonces
        if key is None:
            raise CredentialError("naməlum imza açarı", "forged_principal")
        try:
            key.verify(_b64d(sig_text), payload)
        except (InvalidSignature, ValueError, binascii.Error):
            raise CredentialError("imza yanlışdır", "forged_principal") from None
        if key_revoked or nonce_revoked:
            raise CredentialError("etibarnamə ləğv edilib", "credential_revoked")
        now = float(self._clock())
        if expires <= issued or expires - issued > max_ttl + 1e-9:
            raise CredentialError("etibarnamənin ömrü yanlışdır", "forged_principal")
        if issued > now + CLOCK_SKEW:
            raise CredentialError("etibarnamə hələ etibarlı deyil", "credential_expired")
        if now >= expires:
            raise CredentialError("etibarnamənin vaxtı keçib", "credential_expired")

    def check_principal(self, principal: Any, *roles: str) -> None:
        if not isinstance(principal, SignedPrincipal):
            raise CredentialError("şəxsiyyət imzalanmayıb", "forged_principal")
        if principal.role not in ROLES or (roles and principal.role not in roles):
            raise CredentialError("rol bu əməliyyat üçün icazəli deyil", "role_not_allowed")
        try:
            payload = _principal_bytes(principal)
        except (TypeError, ValueError):
            raise CredentialError("etibarnamə yanlış formatdadır", "forged_principal") from None
        self._check(payload, principal.signature, principal.key_id, principal.nonce, principal.issued_at, principal.expires_at, MAX_PRINCIPAL_TTL)
        scoped = (principal.scope_op, principal.scope_subject, principal.scope_hash)
        if not all(isinstance(x, str) for x in scoped):
            raise CredentialError("əhatə yanlış formatdadır", "forged_principal")
        if principal.role in ("owner", "mock_owner"):
            if not principal.scope_op or not principal.scope_hash:
                raise CredentialError("sahib etibarnaməsi konkret əməliyyata bağlı olmalıdır (scope)", "scope_missing")
        elif any(scoped):
            raise CredentialError("bu rol əhatə daşımamalıdır", "forged_principal")
        with self._lock:
            if principal.channel in self._revoked_channels:
                raise CredentialError("etibarnamənin kanalı ləğv edilib (cihaz)", "credential_revoked")

    def check_authorization(self, auth: Any, action: str | None = None) -> None:
        if not isinstance(auth, DispatchAuthorization) or auth.action not in ACTIONS or (action is not None and auth.action != action):
            raise CredentialError("səlahiyyət yanlışdır və ya bu hərəkət üçün deyil", "forged_principal")
        if not all(isinstance(getattr(auth, f), str) and getattr(auth, f) for f in ("task_id", "agent_id", "run_op", "op_id")) \
                or isinstance(auth.attempt, bool) or not isinstance(auth.attempt, int):
            raise CredentialError("səlahiyyət yanlış formatdadır", "forged_principal")
        self._check(_authorization_bytes(auth), auth.signature, auth.key_id, auth.nonce, auth.issued_at, auth.expires_at, MAX_AUTH_TTL)


class KeyringControl:
    """Etibarlı control-plane: açıq açar əlavə etmək (dondurulmamışdan əvvəl), dondurmaq və açar/etibarnamə ləğv etmək. Yalnız `Keyring.take_control()` verir."""
    __slots__ = ("_ring",)

    def __init__(self, ring: Keyring) -> None:
        object.__setattr__(self, "_ring", ring)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")

    def add_public_key(self, key_id: str, public: bytes | str) -> None:
        self._ring._add(key_id, public)

    def freeze(self) -> None:
        """Bootstrap-dan sonra etibarlı açar çoxluğu geri qaytarılmaz şəkildə dondurulur. Ləğv (daraltma) dondurulmadan sonra da mümkündür."""
        self._ring._freeze()

    def revoke_key(self, key_id: str) -> None:
        self._ring._revoke_key(key_id)

    def revoke_credential(self, nonce: str) -> None:
        self._ring._revoke_credential(nonce)

    def revoke_channel(self, channel: str) -> None:
        """Kanalın (məs. `stepup:<cihaz>`) BÜTÜN etibarnamələrini dərhal ləğv edir (cihaz ləğvi, B-4)."""
        self._ring._revoke_channel(channel)


# --- imza verən tərəf (YALNIZ etibarlı bootstrap) ---

def _ttl(value: float, cap: float) -> float:
    if not _num(value) or value <= 0 or value > cap:
        raise CredentialError(f"ömür 0 < ttl <= {cap:g} olmalıdır", "bad_key")
    return float(value)


class SystemIssuer:
    """Yalnız `system` rolunu imzalayır (NAYMERA/CEO)."""
    __slots__ = ("principal",)

    def __init__(self, mint: Callable[[str, str, str, float], SignedPrincipal], system_id: str = "naymera", channel: str = "core",
                 ttl: float = DEFAULT_SYSTEM_TTL) -> None:
        ttl = _ttl(ttl, MAX_PRINCIPAL_TTL)
        object.__setattr__(self, "principal", lambda: mint(system_id, "system", channel, ttl))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")


class DispatcherIssuer:
    """Yalnız `dispatcher` rolunu və `DispatchAuthorization` imzalayır; yalnız etibarlı dispetçerə verilir."""
    __slots__ = ("principal", "authorize")

    def __init__(self, mint: Callable[[str, str, str, float], SignedPrincipal], sign_auth: Callable[..., DispatchAuthorization],
                 dispatcher_id: str = "dispatcher", channel: str = "runtime", ttl: float = DEFAULT_DISPATCHER_TTL,
                 auth_ttl: float = DEFAULT_AUTH_TTL) -> None:
        ttl, auth_ttl = _ttl(ttl, MAX_PRINCIPAL_TTL), _ttl(auth_ttl, MAX_AUTH_TTL)
        object.__setattr__(self, "principal", lambda: mint(dispatcher_id, "system", channel, ttl))
        object.__setattr__(self, "authorize", lambda task_id, agent_id, run_op, op_id, attempt, action: sign_auth(task_id, agent_id, run_op, op_id, attempt, action, auth_ttl))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")


class OwnerIssuer:
    """Sahib etibarnaməsi YALNIZ yoxlanmış step-up qəbzi ilə verilir: ixtiyari `owner_id` qəbul edən metod yoxdur.
    `consume(receipt)` -> `(owner_id, device_id)` (bir dəfəlik istifadə, vaxt/cihaz/bağlılıq yoxlaması etibarlı autentifikasiya xidmətindədir) və ya None."""
    __slots__ = ("from_step_up",)

    def __init__(self, mint: Callable[..., SignedPrincipal], consume: Callable[[Any], tuple[str, str, str, str, str] | None],
                 ttl: float = DEFAULT_OWNER_TTL, action_prefix: str = "factory:") -> None:
        ttl = _ttl(ttl, MAX_PRINCIPAL_TTL)

        def from_step_up(receipt: Any) -> SignedPrincipal:
            verified = consume(receipt) if receipt is not None else None
            if not verified or len(verified) != 5 or not all(isinstance(x, str) for x in verified) or not (verified[0] and verified[1] and verified[2] and verified[4]):
                raise CredentialError("yoxlanmış step-up sübutu tələb olunur", "proof_required")
            owner_id, device_id, action, subject, payload_hash = verified
            if not action.startswith(action_prefix) or len(action) == len(action_prefix):
                raise CredentialError("step-up əməliyyatı bu etibarnamə üçün uyğun deyil", "proof_required")
            # etibarnamə YALNIZ bu step-up-ın bağlandığı əməliyyat/hədəf/parametr hash-ına verilir (B-3)
            return mint(owner_id, "human", f"stepup:{device_id}", ttl, (action[len(action_prefix):], subject, payload_hash))

        object.__setattr__(self, "from_step_up", from_step_up)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")


class MockOwnerIssuer:
    """TEST üçün (`kind="mock"`): real sahib təsdiqi deyil; fabrik/Company yalnız `allow_mock=True` ilə qəbul edir. İstehsal bootstrap-ı yaratmır."""
    __slots__ = ("principal",)

    def __init__(self, mint: Callable[[str, str, str, float], SignedPrincipal], ttl: float = DEFAULT_OWNER_TTL) -> None:
        ttl = _ttl(ttl, MAX_PRINCIPAL_TTL)
        object.__setattr__(self, "principal", lambda owner_id="owner", channel="test", scope=("mock", "", "0" * 64): mint(owner_id, "mock", channel, ttl, scope))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")


class TrustedSigner:
    """Şəxsi açar YALNIZ bu obyektin bağlanışındadır (atribut kimi saxlanmır). Yalnız etibarlı bootstrap yaradır; heç vaxt agentə/fabrik nüvəsinə/Company-yə verilmir."""

    def __init__(self, key_id: str, seed: bytes, clock: Callable[[], float] = time.time) -> None:
        if not isinstance(key_id, str) or not _KEY_ID_RE.match(key_id):
            raise CredentialError("açar id yanlışdır", "bad_key")
        validate_signing_seed(seed)
        key = Ed25519PrivateKey.from_private_bytes(bytes(seed))
        self.key_id = key_id
        self.public_key: bytes = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._clock = clock

        def sign(payload: bytes) -> str:
            return _b64e(key.sign(payload))

        def principal_minter(role: str) -> Callable[[str, str, str, float], SignedPrincipal]:
            if role not in ROLES:
                raise CredentialError("naməlum rol", "bad_key")

            def mint(pid: str, kind: str, channel: str, ttl: float, scope: tuple[str, str, str] = ("", "", "")) -> SignedPrincipal:
                now = float(clock())
                op, subject, digest = scope
                draft = SignedPrincipal(pid, kind, channel, role, key_id, now, now + _ttl(ttl, MAX_PRINCIPAL_TTL), secrets.token_hex(12), op, subject, digest, "")
                return SignedPrincipal(pid, kind, channel, role, key_id, draft.issued_at, draft.expires_at, draft.nonce, op, subject, digest, sign(_principal_bytes(draft)))
            return mint

        def sign_auth(task_id: str, agent_id: str, run_op: str, op_id: str, attempt: int, action: str, ttl: float) -> DispatchAuthorization:
            if action not in ACTIONS:
                raise CredentialError("naməlum hərəkət", "bad_key")
            now = float(clock())
            draft = DispatchAuthorization(task_id, agent_id, run_op, op_id, int(attempt), action, key_id, now, now + _ttl(ttl, MAX_AUTH_TTL),
                                          secrets.token_hex(12), "")
            return DispatchAuthorization(task_id, agent_id, run_op, op_id, int(attempt), action, key_id, draft.issued_at, draft.expires_at, draft.nonce,
                                         sign(_authorization_bytes(draft)))

        self._principal_minter, self._sign_auth = principal_minter, sign_auth

    def __repr__(self) -> str:
        return f"TrustedSigner(key_id={self.key_id!r}, <private key hidden>)"

    def __reduce__(self):
        raise TypeError("TrustedSigner serializasiya olunmur")

    def verifier(self) -> Keyring:
        """Yalnız bu açarın açıq hissəsini tanıyan YOXLAMA obyekti (şəxsi açar yoxdur)."""
        ring = Keyring(self._clock)
        ring._add(self.key_id, self.public_key)
        return ring

    def system_issuer(self, system_id: str = "naymera", channel: str = "core", ttl: float = DEFAULT_SYSTEM_TTL) -> SystemIssuer:
        return SystemIssuer(self._principal_minter("system"), system_id, channel, ttl)

    def dispatcher_issuer(self, dispatcher_id: str = "dispatcher", channel: str = "runtime", ttl: float = DEFAULT_DISPATCHER_TTL,
                          auth_ttl: float = DEFAULT_AUTH_TTL) -> DispatcherIssuer:
        return DispatcherIssuer(self._principal_minter("dispatcher"), self._sign_auth, dispatcher_id, channel, ttl, auth_ttl)

    def owner_issuer(self, consume: Callable[[Any], tuple[str, str] | None], ttl: float = DEFAULT_OWNER_TTL) -> OwnerIssuer:
        return OwnerIssuer(self._principal_minter("owner"), consume, ttl)

    def mock_owner_issuer(self, ttl: float = DEFAULT_OWNER_TTL) -> MockOwnerIssuer:
        return MockOwnerIssuer(self._principal_minter("mock_owner"), ttl)
