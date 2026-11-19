"""Test köməkçisi: cihaz (Android Keystore) simulyasiyası — P-256 açar cütü və ECDSA-SHA256 DER imzası.
Yalnız testlər üçündür (deterministik k; istehsalda istifadə olunmur). Şəxsi açar yalnız bu obyektdə qalır."""
import hashlib
import secrets

from api import p256

_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


def _der_int(v: int) -> bytes:
    b = v.to_bytes((v.bit_length() + 7) // 8 or 1, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return b"\x02" + bytes([len(b)]) + b


def der_sig(r: int, s: int) -> bytes:
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


class Keypair:
    def __init__(self) -> None:
        self._d = secrets.randbelow(p256.N - 1) + 1
        self.point = p256.scalar_mult(self._d, p256.G)

    def spki(self) -> bytes:
        x, y = self.point
        return _SPKI_PREFIX + b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")

    def sign(self, message: bytes) -> bytes:
        z = int.from_bytes(hashlib.sha256(message).digest(), "big")
        ctr = 0
        while True:
            k = int.from_bytes(hashlib.sha256(self._d.to_bytes(32, "big") + message + bytes([ctr])).digest(),
                               "big") % (p256.N - 1) + 1
            x, _ = p256.scalar_mult(k, p256.G)
            r = x % p256.N
            s = pow(k, -1, p256.N) * (z + r * self._d) % p256.N
            if r and s:
                return der_sig(r, s)
            ctr += 1
