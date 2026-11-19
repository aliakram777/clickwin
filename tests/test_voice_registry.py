"""Səs imkanları: az/en/ru/tr, TTS/STT adapterləri, Local və Cloud ayrı göstərilir; işləməyən səs aktiv kimi göstərilmir."""
import pytest

from agents.mobile import MockVoiceRecognizer, MockVoiceSynthesizer
from agents.voice import (SUPPORTED_LANGUAGES, VoiceAdapter, VoiceRegistry, VoiceUnavailable)


class RealTTS:
    name, is_mock = "acme-tts", False
    languages = ("az-AZ", "en-US", "ru-RU", "tr-TR")

    def synthesize(self, text, profile):
        from agents.mobile import SpeechResult
        return SpeechResult(text, profile, "audio://1", False)


def test_supported_languages_have_az_first_and_a_female_natural_profile_each():
    assert list(SUPPORTED_LANGUAGES) == ["az", "en", "ru", "tr"]
    reg = VoiceRegistry()
    for short, tag in SUPPORTED_LANGUAGES.items():
        p = reg.profile_for(short)
        assert (p.language, p.gender, p.style) == (tag, "female", "natural")
    with pytest.raises(ValueError):
        reg.profile_for("de")


def test_nothing_configured_means_nothing_is_active():
    reg = VoiceRegistry()
    st = {(e.kind, e.location): e for e in reg.status()}
    assert set(st) == {("tts", "local"), ("tts", "cloud"), ("stt", "local"), ("stt", "cloud")}
    assert all(e.state == "not_configured" and e.available is False for e in st.values())
    with pytest.raises(VoiceUnavailable):
        reg.synthesize("salam", "az")
    with pytest.raises(VoiceUnavailable):
        reg.transcribe(b"audio", "az")


def test_mock_engines_are_simulation_and_results_are_labeled():
    reg = VoiceRegistry(tts_local=VoiceAdapter("tts", "local", MockVoiceSynthesizer()),
                        stt_local=VoiceAdapter("stt", "local", MockVoiceRecognizer(["salam"])))
    st = {(e.kind, e.location): e for e in reg.status()}
    assert st[("tts", "local")].state == "simulation" and st[("tts", "local")].is_mock is True
    assert st[("tts", "cloud")].state == "not_configured"
    res = reg.synthesize("Bax https://x.example", "az")
    assert res.is_mock is True and res.audio_ref is None and "http" not in res.text
    assert reg.transcribe(b"x", "az").is_mock is True and reg.transcribe(b"x", "az").untrusted is True


def test_real_cloud_voice_needs_consent_and_local_and_cloud_are_reported_separately():
    granted = set()
    reg = VoiceRegistry(tts_cloud=VoiceAdapter("tts", "cloud", RealTTS()),
                        tts_local=VoiceAdapter("tts", "local", MockVoiceSynthesizer()),
                        consent=lambda scope: scope in granted)
    st = {(e.kind, e.location): e for e in reg.status()}
    assert st[("tts", "cloud")].state == "ready" and st[("tts", "cloud")].is_mock is False
    assert st[("tts", "cloud")].consent_needed == ("cloud_paid_use", "cloud_data_transfer")
    # razılıq yoxdur: real cloud səsi istifadə olunmur, simulyasiya (local) açıq etiketlə işləyir
    res = reg.synthesize("salam", "az", prefer="cloud")
    assert res.is_mock is True
    granted.update({"cloud_paid_use", "cloud_data_transfer"})
    res = reg.synthesize("salam", "az", prefer="cloud")
    assert res.is_mock is False and res.audio_ref == "audio://1"


def test_unsupported_language_and_unavailable_prefer_are_rejected():
    reg = VoiceRegistry(tts_local=VoiceAdapter("tts", "local", MockVoiceSynthesizer()))
    with pytest.raises(ValueError):
        reg.synthesize("hallo", "de")
    with pytest.raises(VoiceUnavailable):
        reg.synthesize("salam", "az", prefer="cloud", strict=True)


def test_voice_never_approves():
    assert VoiceRegistry().may_approve is False
