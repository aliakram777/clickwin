"""Android-first interfeyslər: bildiriş DTO, hadisə axını (sinxronizasiya), səs interfeysləri.

Əsas istifadə platforması Android tətbiqidir (`PRIMARY_PLATFORM`); Telegram əsas platforma deyil (yalnız istəyə bağlı
ikinci kanal ola bilər). Burada YALNIZ interfeys, DTO və mock-lar var: real push (FCM), TTS/STT provayderi və ya
şəbəkə kodu yoxdur.

Səs: gələcək təbii qadın səsi (`VoiceProfile`: az-AZ, female, natural) üçün `VoiceSynthesizer`/`VoiceRecognizer`
interfeysləri. Səs HEÇ VAXT təsdiq mənbəyi deyil (`SpeechPolicy.voice_may_approve` həmişə False): pul/hesab/nəşr təsdiqi
yalnız autentifikasiya olunmuş toxunuş/biometrik ilə ola bilər. Transkriptlər etibarsız giriş sayılır.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from agents.memory import detect_secrets

PRIMARY_PLATFORM = "android"
PLATFORMS = ("android", "web", "telegram")  # android əsasdır; telegram yalnız istəyə bağlı ikinci kanaldır
SCHEMA_VERSION = 1
IMPORTANCES = ("low", "medium", "high", "critical")
EVENT_TYPES = ("notification", "approval_request", "job_update", "research_ready")
ACTION_KINDS = ("open", "ack", "snooze", "dismiss")
MAX_EVENT_BYTES = 65536
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_TAGS = re.compile(r"<[^>]*>")
_URL = re.compile(r"https?://\S+")
_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
DEFAULT_EVENTS_DB = Path("data/events.db")


def clean_text(text: Any, cap: int) -> str:
    """Bildiriş/səs mətni üçün: etiket və idarəedici simvolları təmizləyir, boşluqları yığır, ölçünü məhdudlaşdırır."""
    if not isinstance(text, str):
        raise ValueError("mətn sətir olmalıdır")
    t = _TAGS.sub(" ", text)
    t = "".join(" " if unicodedata.category(c).startswith("C") else c for c in t)
    return " ".join(t.split())[:cap]


def speakable(text: str, limit: int = 300) -> str:
    """Mətni ucadan oxumaq üçün təhlükəsiz edir: URL -> "bağlantı", sirr bənzər sözlər və uzun identifikatorlar
    gizlədilir, idarəedici simvollar təmizlənir, ölçü məhdudlaşdırılır."""
    t = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(text))
    t = _URL.sub("bağlantı", t)
    words = [("[gizli]" if detect_secrets(w) else w) for w in t.split()]
    t = _HEX.sub("[identifikator]", " ".join(words))
    t = " ".join(t.split())
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] if " " in t[:limit] else t[:limit]
    return t


# --- səs ---

@dataclass(frozen=True)
class VoiceProfile:
    """Hədəf səs: təbii qadın səsi, Azərbaycan dili. Real TTS provayderi bu profili dəstəkləməlidir."""
    language: str = "az-AZ"
    gender: str = "female"
    style: str = "natural"
    rate: float = 1.0
    pitch: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", self.language):
            raise ValueError("language 'az-AZ' formatında olmalıdır")
        if self.gender not in ("female", "male", "neutral"):
            raise ValueError("gender female|male|neutral olmalıdır")
        if not isinstance(self.style, str) or not self.style.strip() or len(self.style) > 32:
            raise ValueError("style boş ola bilməz")
        for name, v, lo, hi in (("rate", self.rate, 0.5, 2.0), ("pitch", self.pitch, -12.0, 12.0)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name} {lo}..{hi} aralığında olmalıdır")

    def to_dict(self) -> dict[str, Any]:
        return {"language": self.language, "gender": self.gender, "style": self.style, "rate": float(self.rate),
                "pitch": float(self.pitch)}


@dataclass(frozen=True)
class SpeechResult:
    text: str
    profile: VoiceProfile
    audio_ref: str | None  # real sintezdə audio fayl/axın göstəricisi; mock-da None
    is_mock: bool


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float
    is_mock: bool

    def __post_init__(self) -> None:
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) \
                or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence 0..1 olmalıdır")

    @property
    def untrusted(self) -> bool:
        return True  # səs komandası etibarsız girişdir: təklifdir, təsdiq deyil


class VoiceSynthesizer(Protocol):
    def synthesize(self, text: str, profile: VoiceProfile) -> SpeechResult: ...


class VoiceRecognizer(Protocol):
    def transcribe(self, audio: Any, language: str) -> Transcript: ...


class MockVoiceSynthesizer:
    """Real səs YARATMIR: yalnız interfeys müqaviləsini yoxlamaq üçün. Nəticə `is_mock=True`."""

    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, text: str, profile: VoiceProfile) -> SpeechResult:
        self.calls += 1
        return SpeechResult(speakable(text), profile, None, True)


class MockVoiceRecognizer:
    def __init__(self, script: list[str]) -> None:
        self.script = list(script)

    def transcribe(self, audio: Any, language: str) -> Transcript:
        return Transcript(self.script.pop(0) if self.script else "", 0.5, True)


class SpeechPolicy:
    @staticmethod
    def voice_may_approve(permission: str) -> bool:
        """Səslə təsdiq HEÇ VAXT mümkün deyil (həssas və ya digər): təsdiq autentifikasiya olunmuş toxunuşla olmalıdır."""
        return False


# --- bildiriş DTO ---

@dataclass(frozen=True)
class NotificationAction:
    action_id: str
    label: str
    requires_approval: bool = False
    kind: str = "open"

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not _ACTION_ID.match(self.action_id):
            raise ValueError("action_id yanlışdır")
        label = clean_text(self.label, 60) if isinstance(self.label, str) else ""
        if not label:
            raise ValueError("etiket boş ola bilməz")
        object.__setattr__(self, "label", label)
        if self.kind not in ACTION_KINDS:
            raise ValueError("action kind yanlışdır")

    def to_dict(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "label": self.label, "requires_approval": bool(self.requires_approval),
                "kind": self.kind}


@dataclass(frozen=True)
class Notification:
    id: str
    title: str
    body: str
    importance: str
    priority: int
    category: str
    created_at: float
    expires_at: float
    dedupe_key: str
    actions: tuple[NotificationAction, ...] = ()
    speech_text: str = ""
    locale: str = "az"
    platform: str = PRIMARY_PLATFORM
    is_mock: bool = False
    voice: VoiceProfile = field(default_factory=VoiceProfile)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.match(self.id):
            raise ValueError("id yanlışdır")
        title = clean_text(self.title, 200) if isinstance(self.title, str) else ""
        body = clean_text(self.body, 2000) if isinstance(self.body, str) else ""
        if not title or len(title) > 120:
            raise ValueError("başlıq 1..120 simvol olmalıdır")
        if len(body) > 600:
            raise ValueError("mətn ≤600 simvol olmalıdır")
        speech = speakable(self.speech_text, 300) if self.speech_text else ""
        if self.importance not in IMPORTANCES:
            raise ValueError("importance yanlışdır")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not 0 <= self.priority <= 100:
            raise ValueError("priority 0..100 olmalıdır")
        if not isinstance(self.category, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.category):
            raise ValueError("category yanlışdır")
        if self.platform not in PLATFORMS:
            raise ValueError("platform yanlışdır")
        if not isinstance(self.dedupe_key, str) or not 1 <= len(self.dedupe_key) <= 128:
            raise ValueError("dedupe_key yanlışdır")
        if len(self.actions) > 4:
            raise ValueError("ən çox 4 əməliyyat")
        if not isinstance(self.created_at, (int, float)) or not isinstance(self.expires_at, (int, float)) \
                or self.expires_at <= self.created_at:
            raise ValueError("expires_at created_at-dan sonra olmalıdır")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("dəstəklənməyən schema_version")
        if detect_secrets(f"{title} {body} {self.speech_text}"):
            raise ValueError("bildiriş sirr bənzər məzmun ehtiva edir")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "speech_text", speech)
        object.__setattr__(self, "actions", tuple(self.actions))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id, "title": self.title, "body": self.body,
                "speech_text": self.speech_text, "importance": self.importance, "priority": self.priority,
                "category": self.category, "created_at": self.created_at, "expires_at": self.expires_at,
                "dedupe_key": self.dedupe_key, "locale": self.locale, "platform": self.platform,
                "is_mock": self.is_mock, "actions": [a.to_dict() for a in self.actions],
                "voice": self.voice.to_dict()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: Any) -> "Notification":
        """Müştəridən/xarici mənbədən gələn məlumat ETİBARSIZDIR: tam doğrulanır."""
        if not isinstance(d, dict):
            raise ValueError("bildiriş xəritə olmalıdır")
        return cls(
            id=d["id"], title=d["title"], body=d["body"], importance=d["importance"], priority=d["priority"],
            category=d["category"], created_at=d["created_at"], expires_at=d["expires_at"],
            dedupe_key=d["dedupe_key"],
            actions=tuple(NotificationAction(a["action_id"], a["label"], bool(a.get("requires_approval", False)),
                                             a.get("kind", "open")) for a in d.get("actions", [])),
            speech_text=d.get("speech_text", ""), locale=d.get("locale", "az"), platform=d["platform"],
            is_mock=bool(d.get("is_mock", False)), voice=VoiceProfile(**d.get("voice", {})),
            schema_version=d["schema_version"])


# --- kanallar ---

@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str = ""


class NotificationChannel(Protocol):
    """Gələcək Android push (FCM) və ya digər kanallar bunu həyata keçirir."""
    name: str

    def deliver(self, notification: Notification) -> DeliveryResult: ...


class InMemoryChannel:
    name = "memory"

    def __init__(self) -> None:
        self.delivered: list[Notification] = []
        self.fail = False

    def deliver(self, notification: Notification) -> DeliveryResult:
        if self.fail:
            return DeliveryResult(False, "kanal əlçatan deyil")
        self.delivered.append(notification)
        return DeliveryResult(True)


class NullChannel:
    """Push qoşulmayıb: heç nə çatdırılmır (çökmə yoxdur)."""
    name = "null"

    def deliver(self, notification: Notification) -> DeliveryResult:
        return DeliveryResult(False, "kanal qoşulmayıb")


# --- hadisə axını ---

@dataclass(frozen=True)
class MobileEvent:
    seq: int
    type: str
    id: str
    ts: float
    payload: dict
    schema_version: int


class EventStore:
    """Android sinxronizasiyası üçün append-only hadisə jurnalı: `seq` kursoru və cihaz başına `ack`."""

    def __init__(self, db_path: str | Path = DEFAULT_EVENTS_DB, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "id TEXT NOT NULL, type TEXT NOT NULL, ts REAL NOT NULL, payload_json TEXT NOT NULL, "
                           "schema_version INTEGER NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS acks (device_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)")

    def close(self) -> None:
        self._conn.close()

    def append(self, event_type: str, payload: Any) -> int:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"naməlum hadisə növü: {event_type}")
        if not isinstance(payload, dict):
            raise ValueError("payload xəritə olmalıdır")
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("payload JSON-a çevrilə bilmir") from None
        if len(text.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("payload çox böyükdür")
        if detect_secrets(text):
            raise ValueError("payload sirr bənzər məzmun ehtiva edir")
        cur = self._conn.execute(
            "INSERT INTO events (id, type, ts, payload_json, schema_version) VALUES (?, ?, ?, ?, ?)",
            (f"ev_{time.time_ns()}", event_type, float(self.clock()), text, SCHEMA_VERSION))
        return int(cur.lastrowid)

    def since(self, cursor: int = 0, limit: int = 100) -> list[MobileEvent]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit 1..500 olmalıdır")
        rows = self._conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (int(cursor), limit)).fetchall()
        return [MobileEvent(r["seq"], r["type"], r["id"], r["ts"], json.loads(r["payload_json"]),
                            r["schema_version"]) for r in rows]

    def ack(self, device_id: str, seq: int) -> None:
        if not isinstance(device_id, str) or not _ID.match(device_id):
            raise ValueError("device_id yanlışdır")
        top = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= top:
            raise ValueError("mövcud olmayan hadisə")
        self._conn.execute("INSERT INTO acks (device_id, seq) VALUES (?, ?) ON CONFLICT(device_id) DO UPDATE SET "
                           "seq = MAX(seq, excluded.seq)", (device_id, seq))

    def last_ack(self, device_id: str) -> int:
        row = self._conn.execute("SELECT seq FROM acks WHERE device_id = ?", (device_id,)).fetchone()
        return int(row["seq"]) if row else 0

    def prune(self, keep_last: int) -> int:
        """Ən yeni `keep_last` hadisəni saxlayır, qalanını silir (ack-lardan asılı olmayaraq)."""
        if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 0:
            raise ValueError("keep_last mənfi olmayan tam ədəd olmalıdır")
        top = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        return self._conn.execute("DELETE FROM events WHERE seq <= ?", (top - keep_last,)).rowcount
