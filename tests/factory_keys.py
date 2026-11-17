"""Test üçün Ed25519 etibar çıpası (A-1..A-6). Açar HƏR test prosesində `secrets` ilə təsadüfi yaradılır (statik/commit olunmuş açar YOXDUR)."""
import secrets
from types import SimpleNamespace

from agents.factory_auth import Keyring, TrustedSigner, validate_signing_seed

T0 = 1_000_000.0          # testlərdəki `Clock` başlanğıcı


def _seed() -> bytes:
    while True:
        raw = secrets.token_bytes(32)
        try:
            validate_signing_seed(raw)
            return raw
        except Exception:  # noqa: BLE001
            continue


SEED = _seed()
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


def _owner(owner_id: str):
    return SIGNER.owner_issuer(lambda r: (owner_id, "dev-" + owner_id), ttl=900).from_step_up(object())


OWNER = _owner("owner")
STRANGER = _owner("mallory")
SYSTEM = SIGNER.system_issuer(ttl=900).principal()
DISPATCHER = SIGNER.dispatcher_issuer(ttl=900).principal()
MOCK = SIGNER.mock_owner_issuer(ttl=900).principal("owner", "test")
_DISPATCH = SIGNER.dispatcher_issuer(ttl=900, auth_ttl=300)


def auth_for(task, action: str):
    """T0 anında imzalanmış hərəkətə bağlı səlahiyyət (yalnız saat T0 ətrafında olan testlər üçün)."""
    return _DISPATCH.authorize(task.task_id, task.agent_id, task.run_op, task.op_id, task.attempts, action)
