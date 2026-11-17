"""Səs imkanları: TTS/STT adapterləri (Local və Cloud AYRI), az/en/ru/tr, hədəf təbii qadın səsi.

Heç bir real səs mühərriki burada yoxdur: qoşulmayan imkan `not_configured`, mock isə `simulation` kimi göstərilir və nəticələr `is_mock`
ilə işarələnir. Real cloud səsi (ödənişli, mətn/audio xaricə gedir) `cloud_paid_use` və `cloud_data_transfer` razılığı olmadan istifadə olunmur.
Səs heç vaxt təsdiq mənbəyi deyil (`may_approve` həmişə False); transkript etibarsız mətndir.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from agents.mobile import SpeechResult, Transcript, VoiceProfile

SUPPORTED_LANGUAGES: dict[str, str] = {"az": "az-AZ", "en": "en-US", "ru": "ru-RU", "tr": "tr-TR"}
CLOUD_VOICE_CONSENTS = ("cloud_paid_use", "cloud_data_transfer")


class VoiceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceEngineInfo:
    kind: str            # tts | stt
    location: str        # local | cloud
    state: str           # ready | simulation | not_configured
    is_mock: bool
    languages: tuple[str, ...]
    gender: str
    natural: bool
    reason: str = ""
    consent_needed: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.state in ("ready", "simulation")


class VoiceAdapter:
    """Mövcud `VoiceSynthesizer`/`VoiceRecognizer` (və ya real həyata keçirmə) üçün nazik adapter."""

    def __init__(self, kind: str, location: str, backend: Any = None) -> None:
        if kind not in ("tts", "stt") or location not in ("local", "cloud"):
            raise ValueError("kind tts|stt, location local|cloud olmalıdır")
        self.kind, self.location, self.backend = kind, location, backend

    @property
    def is_mock(self) -> bool:
        return True if self.backend is None else bool(getattr(self.backend, "is_mock", True))  # bilinməyən = mock

    @property
    def languages(self) -> tuple[str, ...]:
        langs = getattr(self.backend, "languages", None)
        return tuple(langs) if langs else tuple(SUPPORTED_LANGUAGES.values())

    def info(self) -> VoiceEngineInfo:
        if self.backend is None:
            return VoiceEngineInfo(self.kind, self.location, "not_configured", True, (), "female", False, "backend qoşulmayıb")
        need = CLOUD_VOICE_CONSENTS if (self.location == "cloud" and not self.is_mock) else ()
        return VoiceEngineInfo(self.kind, self.location, "simulation" if self.is_mock else "ready", self.is_mock, self.languages,
                               "female", not self.is_mock, "simulyasiya: real səs deyil" if self.is_mock else "", need)


class VoiceRegistry:
    may_approve = False

    def __init__(self, tts_local: VoiceAdapter | None = None, tts_cloud: VoiceAdapter | None = None,
                 stt_local: VoiceAdapter | None = None, stt_cloud: VoiceAdapter | None = None,
                 consent: Callable[[str], bool] | None = None) -> None:
        self._a = {("tts", "local"): tts_local or VoiceAdapter("tts", "local"),
                   ("tts", "cloud"): tts_cloud or VoiceAdapter("tts", "cloud"),
                   ("stt", "local"): stt_local or VoiceAdapter("stt", "local"),
                   ("stt", "cloud"): stt_cloud or VoiceAdapter("stt", "cloud")}
        self._consent = consent or (lambda scope: False)

    def status(self) -> list[VoiceEngineInfo]:
        return [self._a[k].info() for k in (("tts", "local"), ("tts", "cloud"), ("stt", "local"), ("stt", "cloud"))]

    @staticmethod
    def _lang(short: str) -> str:
        if short not in SUPPORTED_LANGUAGES:
            raise ValueError(f"dəstəklənməyən dil: {short}; mövcud: {list(SUPPORTED_LANGUAGES)}")
        return SUPPORTED_LANGUAGES[short]

    def profile_for(self, short: str) -> VoiceProfile:
        return VoiceProfile(language=self._lang(short), gender="female", style="natural")

    def _usable(self, kind: str, location: str, tag: str) -> VoiceAdapter | None:
        ad = self._a[(kind, location)]
        info = ad.info()
        if not info.available or tag not in info.languages:
            return None
        if info.consent_needed and not all(self._consent(s) for s in info.consent_needed):
            return None
        return ad

    def _pick(self, kind: str, tag: str, prefer: str | None, strict: bool) -> VoiceAdapter:
        order = ["local", "cloud"] if prefer != "cloud" else ["cloud", "local"]
        if strict and prefer:
            order = [prefer]
        cands = [ad for loc in order if (ad := self._usable(kind, loc, tag)) is not None]
        if not prefer:  # real mühərrik simulyasiyadan üstündür
            cands.sort(key=lambda a: a.is_mock)
        if not cands:
            raise VoiceUnavailable(f"{kind}: uyğun səs mühərriki yoxdur ({tag})")
        return cands[0]

    def synthesize(self, text: str, lang: str, prefer: str | None = None, strict: bool = False) -> SpeechResult:
        tag = self._lang(lang)
        ad = self._pick("tts", tag, prefer, strict)
        res = ad.backend.synthesize(text, self.profile_for(lang))
        return SpeechResult(res.text, res.profile, res.audio_ref, bool(res.is_mock) or ad.is_mock)

    def transcribe(self, audio: Any, lang: str, prefer: str | None = None, strict: bool = False) -> Transcript:
        tag = self._lang(lang)
        ad = self._pick("stt", tag, prefer, strict)
        t = ad.backend.transcribe(audio, tag)
        return Transcript(t.text, t.confidence, bool(t.is_mock) or ad.is_mock)


def summary(reg: VoiceRegistry) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"tts": {}, "stt": {}}
    for e in reg.status():
        out[e.kind][e.location] = e.state
    return out


__all__: Sequence[str] = ["SUPPORTED_LANGUAGES", "VoiceAdapter", "VoiceEngineInfo", "VoiceRegistry", "VoiceUnavailable", "summary"]
