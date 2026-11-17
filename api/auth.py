"""Autentifikasiya: cihaz cütləşməsi, qısa ömürlü sessiya tokenləri, fırlanan yeniləmə tokenləri və step-up sübutu.

Model: tək sahib (`owner_id`). Sahib yerli konsolda birdəfəlik cütləşmə kodu yaradır (HTTP ilə yaradıla bilməz); Android
tətbiqi Keystore-da yaradılmış P-256 açar cütünün YALNIZ açıq açarını göndərir və kodu həmin açarla imzalayaraq sahibliyini
sübut edir. Server heç vaxt şəxsi açar/sirr almır və saxlamır. Tokenlər və kodlar DB-də yalnız SHA-256 hash kimi saxlanır.
Həssas əməliyyatlar (təsdiq, davam) üçün step-up: server bir dəfəlik `nonce` verir, cihaz (biometrik açılışdan sonra, Keystore
açarı ilə) ECDSA-SHA256 imzası qaytarır; imza əməliyyatın nəyə aid olduğuna (action, subject, payload_hash, decision) bağlıdır.
Yoxlama `api.p256` ilə (yalnız stdlib).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agents.mobile import clean_text
from api import p256
from api.http import ApiError

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TOKEN = re.compile(r"^nay_([ar])_[A-Za-z0-9_\-]{43}$")
_CODE = re.compile(r"^[A-HJ-NP-Z2-9]{8}$")
_B64 = re.compile(r"^[A-Za-z0-9+/]{1,400}={0,2}$")


class AuthError(ApiError):
    pass


@dataclass(frozen=True)
class PairResult:
    device_id: str
    access_token: str
    refresh_token: str
    access_expires_in: int


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_in: int


@dataclass(frozen=True)
class Session:
    device_id: str
    device_name: str
    owner_id: str


@dataclass(frozen=True)
class Challenge:
    id: str
    nonce: str
    expires_in: int


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, db_path: str | Path, audit_path: str | Path, clock: Callable[[], float] = time.time,
                 owner_id: str = "owner", access_ttl: int = 900, refresh_ttl: int = 30 * 86400,
                 pairing_ttl: int = 600, challenge_ttl: int = 60) -> None:
        self.clock, self.owner_id = clock, owner_id
        self.access_ttl, self.refresh_ttl = access_ttl, refresh_ttl
        self.pairing_ttl, self.challenge_ttl = pairing_ttl, challenge_ttl
        self.audit_path = Path(audit_path)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        for stmt in (
            "CREATE TABLE IF NOT EXISTS pairing_codes (code_hash TEXT PRIMARY KEY, expires REAL NOT NULL, "
            "used INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, name TEXT NOT NULL, public_key TEXT NOT NULL, "
            "status TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tokens (token_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, device_id TEXT NOT NULL, nonce TEXT NOT NULL, "
            "action TEXT NOT NULL, subject TEXT NOT NULL, payload_hash TEXT NOT NULL, decision TEXT NOT NULL, "
            "expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS rate (key TEXT NOT NULL, ts REAL NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_rate ON rate(key, ts)",
        ):
            self._conn.execute(stmt)

    def close(self) -> None:
        self._conn.close()

    # --- audit və limit ---
    def _audit(self, event: str, **fields: Any) -> None:
        row = {"ts": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "event": event, **fields}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def hit(self, key: str, limit: int, window: float) -> None:
        """Sürət limiti: pəncərədə `limit`-dən çox çağırışa icazə vermir (AuthError 429)."""
        now = self.clock()
        n = self._conn.execute("SELECT COUNT(*) FROM rate WHERE key = ? AND ts > ?", (key, now - window)).fetchone()[0]
        if n >= limit:
            raise AuthError(429, "rate_limited", "Çox sayda sorğu. Bir az sonra yenidən cəhd edin.")
        self._conn.execute("INSERT INTO rate (key, ts) VALUES (?, ?)", (key, now))
        if n == 0:
            self._conn.execute("DELETE FROM rate WHERE ts < ?", (now - 86400,))

    def _failures(self, key: str, window: float) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM rate WHERE key = ? AND ts > ?",
                                  (key, self.clock() - window)).fetchone()[0]

    def _record_failure(self, key: str) -> None:
        self._conn.execute("INSERT INTO rate (key, ts) VALUES (?, ?)", (key, self.clock()))

    # --- cütləşmə ---
    def create_pairing_code(self) -> str:
        """Yalnız yerli/sahib konsolundan çağırılır (HTTP marşrutu yoxdur)."""
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        self._conn.execute("INSERT INTO pairing_codes (code_hash, expires, used) VALUES (?, ?, 0)",
                           (_h(code), self.clock() + self.pairing_ttl))
        self._audit("auth_pairing_code_created")
        return code

    @staticmethod
    def pair_message(code: str) -> bytes:
        """Cütləşmədə cihazın imzaladığı mesaj (açar sahibliyi sübutu)."""
        return b"naymera-pair|" + code.encode("utf-8")

    @staticmethod
    def _b64(value: Any, cap: int = 400) -> bytes | None:
        if not isinstance(value, str) or not _B64.match(value) or len(value) > cap:
            return None
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None

    def pair(self, code: Any, device_name: Any, remote: str, public_key: Any, signature: Any) -> PairResult:
        fail_key = f"pairfail:{remote}"
        if self._failures(fail_key, 600) >= 5:
            self._audit("auth_pair_denied", reason="lockout", remote=remote)
            raise AuthError(429, "locked", "Çox sayda uğursuz cəhd. Bir az sonra yenidən cəhd edin.")

        def deny(reason: str) -> AuthError:
            self._record_failure(fail_key)
            self._audit("auth_pair_denied", reason=reason, remote=remote)
            return AuthError(401, "invalid_pairing_code", "Cütləşmə kodu yanlışdır və ya vaxtı bitib.")

        if not isinstance(code, str) or not _CODE.match(code):
            raise deny("format")
        name = clean_text(device_name, 40) if isinstance(device_name, str) else ""
        if not name:
            raise AuthError(400, "bad_device_name", "Cihaz adı boş ola bilməz.")
        pub, sig = self._b64(public_key), self._b64(signature)
        if pub is None or sig is None:
            raise AuthError(400, "bad_public_key", "Açıq açar və ya imza yanlışdır.")
        if not p256.verify(pub, self.pair_message(code), sig):  # sahiblik sübutu (kod yandırılmazdan əvvəl)
            self._record_failure(fail_key)
            self._audit("auth_pair_denied", reason="bad_key_proof", remote=remote)
            raise AuthError(400, "bad_public_key", "Açıq açar və ya imza yanlışdır.")
        now = self.clock()
        cur = self._conn.execute("UPDATE pairing_codes SET used = 1 WHERE code_hash = ? AND used = 0 AND expires > ?",
                                 (_h(code), now))
        if cur.rowcount != 1:
            raise deny("unknown_or_used_or_expired")
        device_id = "dev_" + secrets.token_hex(8)
        self._conn.execute("INSERT INTO devices (device_id, name, public_key, status, created_at, last_seen) "
                           "VALUES (?, ?, ?, 'active', ?, ?)",
                           (device_id, name, base64.b64encode(pub).decode(), now, now))
        pair = self._issue(device_id)
        self._audit("auth_pair", device_id=device_id, remote=remote)
        return PairResult(device_id, pair.access_token, pair.refresh_token, pair.access_expires_in)

    def _issue(self, device_id: str) -> TokenPair:
        now = self.clock()
        access, refresh = "nay_a_" + secrets.token_urlsafe(32), "nay_r_" + secrets.token_urlsafe(32)
        self._conn.execute("INSERT INTO tokens (token_hash, device_id, kind, expires) VALUES (?, ?, 'a', ?)",
                           (_h(access), device_id, now + self.access_ttl))
        self._conn.execute("INSERT INTO tokens (token_hash, device_id, kind, expires) VALUES (?, ?, 'r', ?)",
                           (_h(refresh), device_id, now + self.refresh_ttl))
        return TokenPair(access, refresh, self.access_ttl)

    # --- sessiya ---
    def _device(self, device_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()

    def authenticate(self, token: Any) -> Session:
        m = _TOKEN.match(token) if isinstance(token, str) else None
        if m is None or m.group(1) != "a":
            raise AuthError(401, "unauthorized", "Sessiya etibarsızdır.")
        row = self._conn.execute("SELECT * FROM tokens WHERE token_hash = ? AND kind = 'a'", (_h(token),)).fetchone()
        if row is None or row["expires"] <= self.clock():
            raise AuthError(401, "unauthorized", "Sessiya etibarsızdır.")
        dev = self._device(row["device_id"])
        if dev is None or dev["status"] != "active":
            raise AuthError(401, "unauthorized", "Sessiya etibarsızdır.")
        self._conn.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (self.clock(), dev["device_id"]))
        return Session(dev["device_id"], dev["name"], self.owner_id)

    def refresh(self, refresh_token: Any, remote: str) -> TokenPair:
        self.hit(f"refresh:{remote}", 30, 60)
        m = _TOKEN.match(refresh_token) if isinstance(refresh_token, str) else None
        if m is None or m.group(1) != "r":
            raise AuthError(401, "unauthorized", "Yeniləmə tokeni etibarsızdır.")
        row = self._conn.execute("SELECT * FROM tokens WHERE token_hash = ? AND kind = 'r'",
                                 (_h(refresh_token),)).fetchone()
        if row is None:
            raise AuthError(401, "unauthorized", "Yeniləmə tokeni etibarsızdır.")
        if row["used"]:  # köhnə token təkrar: oğurlanma əlaməti -> bütün cihaz ləğv olunur
            self.revoke_device(row["device_id"])
            self._audit("auth_refresh_reuse", device_id=row["device_id"], remote=remote)
            raise AuthError(401, "unauthorized", "Yeniləmə tokeni etibarsızdır.")
        dev = self._device(row["device_id"])
        if row["expires"] <= self.clock() or dev is None or dev["status"] != "active":
            raise AuthError(401, "unauthorized", "Yeniləmə tokeni etibarsızdır.")
        cur = self._conn.execute("UPDATE tokens SET used = 1 WHERE token_hash = ? AND used = 0", (_h(refresh_token),))
        if cur.rowcount != 1:
            raise AuthError(401, "unauthorized", "Yeniləmə tokeni etibarsızdır.")
        self._audit("auth_refresh", device_id=row["device_id"])
        return self._issue(row["device_id"])

    def revoke_device(self, device_id: str) -> None:
        self._conn.execute("UPDATE devices SET status = 'revoked' WHERE device_id = ?", (device_id,))
        self._conn.execute("UPDATE tokens SET expires = 0 WHERE device_id = ?", (device_id,))
        self._audit("auth_device_revoked", device_id=device_id)

    def logout(self, device_id: str) -> None:
        self.revoke_device(device_id)
        self._audit("auth_logout", device_id=device_id)

    def list_devices(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT device_id, name, status, created_at, last_seen FROM devices "
                                  "ORDER BY created_at, device_id").fetchall()
        return [dict(r) for r in rows]

    # --- step-up ---
    @staticmethod
    def step_up_message(challenge_id: str, nonce: str, action: str, subject: str, payload_hash: str,
                        decision: str) -> bytes:
        """Cihazın imzaladığı mesaj: challenge + əməliyyatın bütün tərəfləri."""
        return "|".join(["naymera-step-up", challenge_id, nonce, action, subject, payload_hash, decision]).encode("utf-8")

    def create_challenge(self, device_id: str, action: str, subject: str, payload_hash: str,
                         decision: str) -> Challenge:
        dev = self._device(device_id)
        if dev is None or dev["status"] != "active":
            raise AuthError(401, "unauthorized", "Cihaz etibarsızdır.")
        for v in (action, subject, payload_hash, decision):
            if not isinstance(v, str) or len(v) > 128:
                raise AuthError(400, "bad_request", "Yanlış step-up parametri.")
        cid, nonce = secrets.token_hex(16), secrets.token_hex(32)
        self._conn.execute("INSERT INTO challenges (id, device_id, nonce, action, subject, payload_hash, decision, "
                           "expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (cid, device_id, nonce, action, subject, payload_hash, decision,
                            self.clock() + self.challenge_ttl))
        self._audit("auth_stepup_challenge", device_id=device_id, action=action, subject=subject)
        return Challenge(cid, nonce, self.challenge_ttl)

    def verify_proof(self, device_id: str, challenge_id: Any, action: str, subject: str, payload_hash: str,
                     decision: str, proof: Any) -> None:
        """Sübutu yoxlayır. Uğursuzluqda AuthError(403); təkrar uğursuzluqda cihaz müvəqqəti bloklanır (429)."""
        fail_key = f"stepfail:{device_id}"
        if self._failures(fail_key, 600) >= 5:
            self._audit("auth_stepup_denied", device_id=device_id, reason="lockout")
            raise AuthError(429, "locked", "Çox sayda uğursuz təsdiq cəhdi. Bir az sonra yenidən cəhd edin.")

        def deny(reason: str) -> AuthError:
            self._record_failure(fail_key)
            self._audit("auth_stepup_denied", device_id=device_id, reason=reason)
            return AuthError(403, "stepup_failed", "Təsdiq sübutu qəbul olunmadı.")

        sig = self._b64(proof)
        if not isinstance(challenge_id, str) or sig is None:
            raise deny("format")
        row = self._conn.execute("SELECT * FROM challenges WHERE id = ?", (challenge_id,)).fetchone()
        if row is None or row["device_id"] != device_id:
            raise deny("unknown_or_foreign_challenge")
        burned = self._conn.execute("UPDATE challenges SET used = 1 WHERE id = ? AND used = 0", (challenge_id,))
        if burned.rowcount != 1 or row["expires"] <= self.clock():
            raise deny("used_or_expired")
        if (row["action"], row["subject"], row["payload_hash"], row["decision"]) != (action, subject, payload_hash, decision):
            raise deny("binding_mismatch")
        dev = self._device(device_id)
        if dev is None or dev["status"] != "active":
            raise deny("device_inactive")
        msg = self.step_up_message(challenge_id, row["nonce"], action, subject, payload_hash, decision)
        if not p256.verify(base64.b64decode(dev["public_key"]), msg, sig):
            raise deny("bad_proof")
        self._audit("auth_stepup_ok", device_id=device_id, action=action, subject=subject)
