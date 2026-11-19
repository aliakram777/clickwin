"""ECDSA P-256 / SHA-256 imza yoxlaması (yalnız stdlib). Android Keystore `SHA256withECDSA` (DER) imzalarını və X.509
SubjectPublicKeyInfo açıq açarlarını yoxlayır. Yalnız açıq məlumatla işləyir (şəxsi açar serverə heç vaxt gəlmir); imzalama burada YOXDUR.
Yoxlama sabit-vaxtlı olmaq məcburiyyətində deyil (gizli skalyar yoxdur)."""
from __future__ import annotations

import hashlib

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)
_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")

Point = tuple[int, int] | None


class InvalidKey(ValueError):
    pass


def on_curve(pt: tuple[int, int]) -> bool:
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x - 3 * x + B)) % P == 0


def _add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0]:
        if (a[1] + b[1]) % P == 0:
            return None
        lam = (3 * a[0] * a[0] - 3) * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def scalar_mult(k: int, pt: Point) -> Point:
    out: Point = None
    add = pt
    while k:
        if k & 1:
            out = _add(out, add)
        add = _add(add, add)
        k >>= 1
    return out


def parse_spki(data: bytes) -> tuple[int, int]:
    if not isinstance(data, (bytes, bytearray)) or len(data) != 91 or bytes(data[:26]) != _SPKI_PREFIX \
            or data[26] != 4:
        raise InvalidKey("P-256 SubjectPublicKeyInfo gözlənilir")
    pt = (int.from_bytes(data[27:59], "big"), int.from_bytes(data[59:91], "big"))
    if not on_curve(pt):
        raise InvalidKey("nöqtə əyri üzərində deyil")
    return pt


def _der_len(d: bytes, i: int) -> tuple[int, int]:
    if i >= len(d):
        raise ValueError
    n = d[i]
    if n < 0x80:
        return n, i + 1
    if n != 0x81 or i + 1 >= len(d) or d[i + 1] < 0x80:
        raise ValueError
    return d[i + 1], i + 2


def _der_int(d: bytes, i: int) -> tuple[int, int]:
    if i >= len(d) or d[i] != 2:
        raise ValueError
    n, i = _der_len(d, i + 1)
    if n == 0 or i + n > len(d):
        raise ValueError
    raw = d[i:i + n]
    if raw[0] & 0x80 or (n > 1 and raw[0] == 0 and not raw[1] & 0x80):
        raise ValueError  # mənfi və ya qeyri-minimal kodlaşdırma
    return int.from_bytes(raw, "big"), i + n


def _parse_sig(sig: bytes) -> tuple[int, int]:
    if not sig or sig[0] != 0x30:
        raise ValueError
    n, i = _der_len(sig, 1)
    if i + n != len(sig):
        raise ValueError
    r, i = _der_int(sig, i)
    s, i = _der_int(sig, i)
    if i != len(sig):
        raise ValueError
    return r, s


def verify(public_spki: bytes, message: bytes, signature_der: bytes) -> bool:
    """ECDSA-SHA256 imzasını yoxlayır. Hər hansı pozuq giriş üçün istisna atmır, False qaytarır."""
    try:
        if not isinstance(message, (bytes, bytearray)) or not isinstance(signature_der, (bytes, bytearray)):
            return False
        q = parse_spki(public_spki)
        r, s = _parse_sig(bytes(signature_der))
        if not (1 <= r < N and 1 <= s < N):
            return False
        z = int.from_bytes(hashlib.sha256(bytes(message)).digest(), "big")
        w = pow(s, -1, N)
        pt = _add(scalar_mult(z * w % N, G), scalar_mult(r * w % N, q))
        return pt is not None and pt[0] % N == r
    except (ValueError, TypeError, ZeroDivisionError):
        return False
