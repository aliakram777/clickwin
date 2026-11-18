"""Test üçün Ed25519 etibar çıpası (A-1..A-6). Açar HƏR test prosesində `secrets` ilə təsadüfi yaradılır (statik/commit olunmuş açar YOXDUR)."""
import secrets
from types import SimpleNamespace

from safety.approvals import Principal

from agents.factory_auth import Keyring, TrustedSigner, validate_signing_seed

T0 = 1_000_000.0          # testlərdəki `Clock` başlanğıcı


def random_seed() -> bytes:
    while True:
        raw = secrets.token_bytes(32)
        try:
            validate_signing_seed(raw)
            return raw
        except Exception:  # noqa: BLE001
            continue


SEED = random_seed()
KEY_ID = "test-key-1"
SIGNER = TrustedSigner(KEY_ID, SEED, clock=lambda: T0)           # sabit T0 vaxtında imzalayır: modul-səviyyəli sabit şəxsiyyətlər üçün


def ring_with_control(clock):
    """(yalnız yoxlama Keyring, KeyringControl): control-plane yalnız testin özündədir."""
    r = Keyring(clock)
    control = r.take_control()
    control.add_public_key(KEY_ID, SIGNER.public_key)
    return r, control


def ring(clock) -> Keyring:
    """Yalnız yoxlama (açıq açar); control götürülüb və atılıb (açar əlavə/ləğv/dondurma mümkün deyil)."""
    return ring_with_control(clock)[0]


def trust(clock) -> SimpleNamespace:
    """Bir test üçün etibar çıpası: `ring` (yoxlama) + test saatına bağlı rola xas imza obyektləri."""
    signer = TrustedSigner(KEY_ID, SEED, clock=clock)
    r, control = ring_with_control(clock)
    return SimpleNamespace(signer=signer, ring=r, control=control, dispatcher=signer.dispatcher_issuer(), system=signer.system_issuer(),
                           owner=signer.owner_issuer(lambda r: ("owner", "dev1")))


HASH0 = "0" * 64


def owner_cred(op: str, subject: str = "", digest: str = HASH0, owner_id: str = "owner", *, signer=None, ttl: float = 900):
    """DƏQİQ əhatəli (əməliyyat, hədəf, parametr-hash), YENİ nonce-lu bir-dəfəlik sahib etibarnaməsi (test etibar çıpası ilə). Real yolda bunu yalnız step-up qəbzi verir."""
    issuer = (signer or SIGNER).owner_issuer(lambda r: (owner_id, "dev-" + owner_id, "factory:" + r[0], r[1], r[2]), ttl=ttl)
    return issuer.from_step_up((op, subject, digest))


class OwnerSentinel:
    """Testlərdə `OWNER` yerinə keçən işarə: `ScopedAgentFactory/ScopedCompany` bunu hər əməliyyat üçün DƏQİQ əhatəli yeni etibarnamə ilə əvəz edir (sahibin hər əməliyyata ayrıca step-up
    etdiyini modelləşdirir). Özü SignedPrincipal DEYİL: birbaşa istehsal obyektlərinə verilsə rədd edilir."""

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id

    def mint(self, op: str, subject: str, digest: str):
        return owner_cred(op, subject, digest, self.owner_id)


class MockSentinel(OwnerSentinel):
    """`kind="mock"` sahib işarəsi (real sahib təsdiqi DEYİL; fabrik/Company yalnız `allow_mock=True` ilə qəbul edir)."""

    def mint(self, op: str, subject: str, digest: str):
        return SIGNER.mock_owner_issuer(ttl=900).principal(self.owner_id, "test", (op, subject, digest))


OWNER_APPROVER = Principal("owner", "human", "console")        # ApprovalService/Gateway üçün (ayrı sistem; fabrik etibarnaməsi deyil)
OWNER = OwnerSentinel("owner")
STRANGER = OwnerSentinel("mallory")
SYSTEM = SIGNER.system_issuer(ttl=900).principal()
DISPATCHER = SIGNER.dispatcher_issuer(ttl=900).principal()
MOCK = MockSentinel("owner")
_DISPATCH = SIGNER.dispatcher_issuer(ttl=900, auth_ttl=300)


def auth_for(task, action: str):
    """T0 anında imzalanmış hərəkətə bağlı səlahiyyət (yalnız saat T0 ətrafında olan testlər üçün)."""
    return _DISPATCH.authorize(task.task_id, task.agent_id, task.run_op, task.op_id, task.attempts, action)
