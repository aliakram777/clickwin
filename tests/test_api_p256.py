"""P-256 ECDSA yoxlaması (yalnız stdlib). Vektorlar `openssl` ilə yaradılıb (Android Keystore SHA256withECDSA/DER formatı)."""
import base64

import pytest

from api import p256
from tests.p256_tools import Keypair, der_sig

PUB = base64.b64decode("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEUhDNTjUemFJUTePYOYuPGNf872EMwaNw6XJQn4mOpc81gitxS/GccViAp3eKzAPr/i32gzTqr8tTe+DOvap2Jw==")
SIG = base64.b64decode("MEQCIGQ+Gc6q9UPDbqJV/oRFUKnMeOJj94uR5OiIYBzv0mnLAiBiRvD3Ek2eN1JaYQBc59cRBT2L7D+32Yt09EUJTAXhwQ==")
MSG = b"naymera-step-up|test message"
P384 = base64.b64decode("MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAEuKm3uqB7muoYg/GcE1hJpUrdhmHgKu2e4OEA6soImLYDajZRHA7jcjK4qRg03SDEG9kzVu8fBY4B3NEcYmznVq8R6AGPlu0tTBBBpBgrR6Wse84aFIWv19B0bj2imRY8")


def test_openssl_signature_verifies():
    assert p256.verify(PUB, MSG, SIG) is True


def test_tampered_message_signature_or_key_is_rejected():
    assert p256.verify(PUB, MSG + b"x", SIG) is False
    bad = bytearray(SIG)
    bad[10] ^= 1
    assert p256.verify(PUB, MSG, bytes(bad)) is False
    assert p256.verify(Keypair().spki(), MSG, SIG) is False


def test_own_signer_and_verifier_agree_and_reject_other_messages():
    kp = Keypair()
    sig = kp.sign(b"a|b|c")
    assert p256.verify(kp.spki(), b"a|b|c", sig) is True
    assert p256.verify(kp.spki(), b"a|b|d", sig) is False


@pytest.mark.parametrize("sig", [b"", b"\x30", b"\x30\x00", b"\x00" * 70, SIG[:-1], SIG + b"\x00", b"\x31" + SIG[1:],
                                 der_sig(0, 5), der_sig(5, 0), der_sig(p256.N, 5), der_sig(5, p256.N),
                                 der_sig(p256.N + 7, 5)])
def test_malformed_or_out_of_range_signatures_never_verify_and_never_raise(sig):
    assert p256.verify(PUB, MSG, sig) is False


@pytest.mark.parametrize("pub", [b"", PUB[:-1], PUB + b"\x00", P384, b"\x00" * 91, PUB[:26] + b"\x04" + b"\x01" * 64])
def test_invalid_public_keys_are_rejected(pub):
    assert p256.verify(pub, MSG, SIG) is False
    with pytest.raises(p256.InvalidKey):
        p256.parse_spki(pub)


def test_parse_spki_returns_a_point_on_the_curve():
    x, y = p256.parse_spki(PUB)
    assert (y * y - (x ** 3 - 3 * x + p256.B)) % p256.P == 0


def test_non_bytes_inputs_return_false():
    assert p256.verify("x", MSG, SIG) is False and p256.verify(PUB, "x", SIG) is False and p256.verify(PUB, MSG, None) is False
