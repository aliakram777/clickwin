"""Agent işçi prosesi ilə etibarlı server arasındakı RPC protokolu (yalnız stdlib: işçi proses bu faylı öz şəxsi qovluğundan idxal edir).

Kanal: `socket.socketpair()` (AF_UNIX); uc yalnız doğulan işçi prosesə irsi verilir. Əlavə autentifikasiya: hər sessiyaya xas 32 baytlıq təsadüfi açar
(ayrıca borudan, bir dəfə oxunur) ilə hər çərçivənin HMAC-SHA256-sı; sıra nömrəsi (hər istiqamətdə ciddi artan) təkrar/yenidən-sıralamanı pozur.
Çərçivə: 4 bayt uzunluq + 32 bayt MAC + JSON gövdəsi (UTF-8, ASCII-escape, NaN/Infinity qadağan, maks. 64 KiB). Obyekt/istinad ötürülmür, yalnız JSON.
Qeyd: kanal məxfilik şifrələməsi vermir (yerli socketpair); məqsəd autentifikasiya, bütövlük və təkrarın qarşısıdır.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import struct

PROTO = 1
MAX_BODY = 64 * 1024
MAX_DEPTH = 6          # JSON iç-içəlik həddi (protokolun özü ən çox 4 səviyyə işlədir); ayrışdırmadan ƏVVƏL yoxlanır
MAC_LEN = 32
C2S = b"c2s"
S2C = b"s2c"


class WireError(Exception):
    """Çərçivə/autentifikasiya xətası: tərəf sessiyanı bağlamalıdır."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mac(key: bytes, direction: bytes, seq: int, body: bytes) -> bytes:
    return hmac.new(key, direction + seq.to_bytes(8, "big") + body, hashlib.sha256).digest()


def _no_constant(name: str):
    raise ValueError(f"constant {name}")


def check_depth(body: bytes, limit: int = MAX_DEPTH) -> None:
    """JSON gövdəsinin `[`/`{` iç-içəliyini (sətirlərdən kənarda) xətti keçidlə sayır; hədd aşılarsa `json.loads` ÇAĞIRILMIR (RecursionError/yaddaş hücumu yoxdur)."""
    depth, in_str, esc = 0, False, False
    for b in body:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:      # \
                esc = True
            elif b == 0x22:      # "
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b == 0x5B or b == 0x7B:
            depth += 1
            if depth > limit:
                raise WireError("too_deep")
        elif b == 0x5D or b == 0x7D:
            depth -= 1
            if depth < 0:
                raise WireError("bad_json")


class Writer:
    def __init__(self, key: bytes, direction: bytes) -> None:
        self._key, self._dir, self._seq = key, direction, 0

    def pack(self, obj: dict) -> bytes:
        body = json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(body) > MAX_BODY:
            raise WireError("too_large")
        self._seq += 1
        payload = _mac(self._key, self._dir, self._seq, body) + body
        return struct.pack(">I", len(payload)) + payload


class Reader:
    def __init__(self, key: bytes, direction: bytes) -> None:
        self._key, self._dir, self._seq = key, direction, 0
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        out: list[dict] = []
        while True:
            if len(self._buf) < 4:
                return out
            (n,) = struct.unpack(">I", bytes(self._buf[:4]))
            if n < MAC_LEN + 2 or n > MAC_LEN + MAX_BODY:
                raise WireError("bad_length")
            if len(self._buf) < 4 + n:
                return out
            payload = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            mac, body = payload[:MAC_LEN], payload[MAC_LEN:]
            self._seq += 1
            if not hmac.compare_digest(mac, _mac(self._key, self._dir, self._seq, body)):
                raise WireError("bad_mac")
            check_depth(body)
            try:
                obj = json.loads(body.decode("utf-8"), parse_constant=_no_constant)
            except (ValueError, UnicodeDecodeError, RecursionError, MemoryError, OverflowError):
                raise WireError("bad_json") from None
            if not isinstance(obj, dict):
                raise WireError("bad_json")
            out.append(obj)
