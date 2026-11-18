"""OpenAI-uyğun adapter (Mərhələ 18.1-B): Stage 17-də yoxlanmış `agents.providers.OpenAICompatibleProvider` DƏYİŞDİRİLMİR; üzərinə cavab sərtləşdirməsi əlavə edən alt sinif.

Niyə: fixture sınağı əsas sinifdə iki boşluğu göstərdi — (a) dərin iç-içə JSON `RecursionError` ilə çökür, (b) tək surrogat ehtiva edən mətn aşağı axına buraxılır, (c) ölçü limiti yoxdur.
`OpenAICompatProvider` bunları `LLMBadResponse`-a (haqq naməlum) çevirir; qalan davranış (endpoint siyasəti, status xəritələməsi, başlıqlar) əsas sinifdəndir.
`ModelRegistry` yalnız BU dəqiq tipi qəbul edir; xam `OpenAICompatibleProvider` modellər qatında etibarlı deyil (Stage 17 yolunda `build_app` onu eyni qaydada istifadə etməkdə davam edir).
`kind="cloud"` uzaq (HTTPS + ictimai DNS), `kind="private"` lokal server (llama.cpp/Ollama uyğun)."""
from __future__ import annotations

from agents.llm import LLMBadResponse, LLMRequest, LLMResponse
from agents.providers import HttpResponse, OpenAICompatibleProvider
from models.adapters.base import parse_json_body, safe_text


class OpenAICompatProvider(OpenAICompatibleProvider):
    @staticmethod
    def _parse(resp: HttpResponse, request: LLMRequest) -> LLMResponse:
        if not isinstance(resp, HttpResponse) or isinstance(resp.status, bool) or not isinstance(resp.status, int):
            raise LLMBadResponse("nəqliyyat HttpResponse qaytarmadı və ya status yanlışdır")
        if resp.status == 200:
            parse_json_body(resp)                      # ölçü, UTF-8, iç-içəlik (RecursionError), obyekt yoxlaması
        try:
            out = OpenAICompatibleProvider._parse(resp, request)
        except RecursionError:
            raise LLMBadResponse("provayder cavabı yanlış formatdadır") from None
        safe_text(out.text)
        return out


__all__ = ["OpenAICompatProvider"]
