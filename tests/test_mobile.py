"""Android-first mobil interfeys: bildiriş DTO, hadisə axını, səs interfeysləri. Heç bir real push, TTS/STT və ya
xarici çağırış yoxdur: səs yalnız profil/interfeys və mock-dur."""
import ast
import json
import threading
from pathlib import Path

import pytest

from agents.mobile import (
    PLATFORMS, PRIMARY_PLATFORM, SCHEMA_VERSION, DeliveryResult, EventStore, InMemoryChannel, MockVoiceRecognizer,
    MockVoiceSynthesizer, Notification, NotificationAction, NullChannel, SpeechPolicy, Transcript, VoiceProfile,
    speakable,
)

ROOT = Path(__file__).resolve().parent.parent


def notif(**kw):
    base = dict(id="n1", title="Təsdiq gözlənilir", body="Ödəniş sorğusu 30 dəqiqədir gözləyir.",
                importance="high", priority=75, category="approval", created_at=1000.0, expires_at=5000.0,
                dedupe_key="approval:abc", actions=(NotificationAction("open_approval", "Sorğuya bax"),))
    base.update(kw)
    return Notification(**base)


# --- Android-first ---

def test_android_is_the_primary_platform_and_telegram_is_not():
    assert PRIMARY_PLATFORM == "android" and PLATFORMS[0] == "android"
    n = notif()
    assert n.platform == "android" and n.locale == "az" and n.schema_version == SCHEMA_VERSION
    assert notif(platform="web").platform == "web"
    with pytest.raises(ValueError):
        notif(platform="symbian")


def test_core_modules_do_not_depend_on_telegram_or_network():
    banned = {"requests", "socket", "subprocess", "http", "httpx", "ssl", "telegram", "telebot", "aiogram"}
    for name in ("mobile.py", "proactive.py", "research.py"):
        tree = ast.parse((ROOT / "agents" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not banned & {a.name.split(".")[0] for a in node.names}, name
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned, name


# --- bildiriş DTO ---

def test_notification_json_roundtrip_is_versioned_and_stable():
    n = notif(speech_text="Təsdiq gözlənilir.")
    d = json.loads(n.to_json())
    assert d["schema_version"] == 1 and d["platform"] == "android" and d["importance"] == "high"
    assert d["actions"] == [{"action_id": "open_approval", "label": "Sorğuya bax", "requires_approval": False,
                             "kind": "open"}]
    assert d["voice"] == {"language": "az-AZ", "gender": "female", "style": "natural", "rate": 1.0, "pitch": 0.0}
    assert Notification.from_dict(d) == n


@pytest.mark.parametrize("kw", [
    dict(importance="urgent"), dict(title=""), dict(title="x" * 200), dict(body="x" * 700),
    dict(priority=101), dict(priority=-1), dict(priority=True), dict(id=""), dict(category="x" * 50),
    dict(actions=tuple(NotificationAction(f"a{i}", "l") for i in range(6))), dict(expires_at=500.0),
    dict(title="açar sk-test-FAKEKEY1234567890abcdef"), dict(body="password: hunter2hunter2"),
])
def test_invalid_notifications_are_rejected(kw):
    with pytest.raises(ValueError):
        notif(**kw)


def test_notification_text_is_sanitized_and_actions_validated():
    n = notif(title="Salam\x00\x1b[31m dünya", body="Mətn\n\n<script>x</script>")
    assert "\x00" not in n.title and "\x1b" not in n.title and "<" not in n.body and "\n" not in n.body
    for bad in ("", "Bad Id!", "x" * 80):
        with pytest.raises(ValueError):
            NotificationAction(bad, "etiket")
    with pytest.raises(ValueError):
        NotificationAction("ok_id", "")


def test_from_dict_treats_client_data_as_untrusted():
    for bad in (None, [], {}, {"id": "x"}, {**json.loads(notif().to_json()), "importance": "çox"},
                {**json.loads(notif().to_json()), "schema_version": 99}):
        with pytest.raises((ValueError, TypeError, KeyError)):
            Notification.from_dict(bad)


def test_mock_flag_travels_with_the_notification():
    assert notif(is_mock=True).to_dict()["is_mock"] is True and notif().to_dict()["is_mock"] is False


# --- kanallar ---

def test_channels_satisfy_the_delivery_contract():
    ch = InMemoryChannel()
    res = ch.deliver(notif())
    assert isinstance(res, DeliveryResult) and res.ok and [n.id for n in ch.delivered] == ["n1"]
    ch.fail = True
    assert not ch.deliver(notif(id="n2")).ok and [n.id for n in ch.delivered] == ["n1"]
    assert not NullChannel().deliver(notif()).ok  # push qoşulmayıb: çatdırılmır, çökmür


# --- hadisə axını (Android sinxronizasiyası) ---

@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "events.db")


def test_event_store_sequence_cursor_and_ack(store):
    s1 = store.append("notification", {"id": "n1"})
    s2 = store.append("approval_request", {"approval_id": "a1"})
    s3 = store.append("job_update", {"job": "j1"})
    assert s1 < s2 < s3
    got = store.since(0)
    assert [(e.seq, e.type) for e in got] == [(s1, "notification"), (s2, "approval_request"), (s3, "job_update")]
    assert [e.seq for e in store.since(s1)] == [s2, s3] and len(store.since(0, limit=2)) == 2
    assert got[0].payload == {"id": "n1"} and got[0].schema_version == 1
    assert store.last_ack("phone-1") == 0
    store.ack("phone-1", s2)
    store.ack("phone-1", s1)          # geriyə getmir
    assert store.last_ack("phone-1") == s2 and store.last_ack("phone-2") == 0
    with pytest.raises(ValueError):
        store.ack("phone-1", s3 + 100)  # mövcud olmayan hadisə


@pytest.mark.parametrize("etype,payload", [
    ("naməlum", {}), ("notification", "mətn"), ("notification", {"x": object()}),
    ("notification", {"k": "sk-test-FAKEKEY1234567890abcdef"}), ("notification", {"big": "x" * 70000}),
])
def test_event_store_rejects_bad_events(store, etype, payload):
    with pytest.raises(ValueError):
        store.append(etype, payload)
    assert store.since(0) == []


def test_event_store_persists_prunes_and_is_concurrency_safe(tmp_path):
    path = tmp_path / "events.db"
    seqs = []

    def worker():
        s = EventStore(path)
        seqs.extend(s.append("job_update", {"n": i}) for i in range(5))
        s.close()

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(seqs)) == 20
    s = EventStore(path)
    assert len(s.since(0, limit=100)) == 20
    s.ack("p", seqs[0])
    assert s.prune(keep_last=5) == 15 and len(s.since(0, limit=100)) == 5


# --- səs: gələcək təbii qadın səsi üçün interfeys ---

def test_voice_profile_targets_a_natural_female_azerbaijani_voice():
    p = VoiceProfile()
    assert (p.language, p.gender, p.style) == ("az-AZ", "female", "natural")
    for bad in ({"language": ""}, {"gender": "robot"}, {"rate": 0}, {"rate": 5}, {"pitch": 99}, {"style": ""}):
        with pytest.raises(ValueError):
            VoiceProfile(**bad)


def test_mock_synthesizer_is_clearly_mock_and_produces_no_audio():
    syn = MockVoiceSynthesizer()
    res = syn.synthesize("Salam, bu bir sınaqdır.", VoiceProfile())
    assert res.is_mock is True and res.audio_ref is None and res.text == "Salam, bu bir sınaqdır."
    assert res.profile.gender == "female" and syn.calls == 1


def test_speakable_text_is_safe_to_read_aloud():
    out = speakable("Bax https://evil.example/x?token=abc və açar sk-test-FAKEKEY1234567890abcdef.\n"
                    "Sorğu a3f9c2d1e4b5a6978877665544332211 üçün.\x00" + "x" * 500, limit=200)
    assert "http" not in out and "sk-test" not in out and "\x00" not in out and "a3f9c2d1e4b5" not in out
    assert "bağlantı" in out and len(out) <= 200 and "Sorğu" in out


def test_voice_can_never_approve_and_transcripts_are_untrusted():
    for perm in ("publish", "spend_money", "external_account", "read_files"):
        assert SpeechPolicy.voice_may_approve(perm) is False
    rec = MockVoiceRecognizer(["Hesabatı göstər"])
    t = rec.transcribe(b"audio-ref", "az-AZ")
    assert isinstance(t, Transcript) and t.untrusted is True and t.is_mock is True and t.text == "Hesabatı göstər"
    with pytest.raises(ValueError):
        Transcript("x", 1.5, True)
