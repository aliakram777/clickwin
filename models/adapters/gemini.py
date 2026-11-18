"""Google Gemini generateContent adapteri (Mərhələ 18.1-B). Açar URL-də deyil, başlıqdadır (URL loqlara düşməsin). Nəqliyyat inyeksiya olunur; real çağırış yoxdur.
Format ictimai API sənədinə əsaslanır və real xidmətə qarşı YOXLANMAYIB."""
from __future__ import annotations

from typing import Any

from agents.llm import LLMBadResponse, LLMRequest, LLMResponse
from models.adapters.base import HttpJsonProvider, clean_response_model, safe_text, tokens

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"}


class GeminiProvider(HttpJsonProvider):
    name = "gemini"
    DEFAULT_BASE = "https://generativelanguage.googleapis.com"

    def _url(self) -> str:
        return f"{self.base_url}/v1beta/models/{self.model}:generateContent"        # model adı `check_model_name` ilə yol-təhlükəsizdir

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-goog-api-key": key}

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
                                "generationConfig": {"maxOutputTokens": request.max_output_tokens, "temperature": 0}}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        return body

    def _parse(self, data: dict, request: LLMRequest) -> LLMResponse:
        cands = data.get("candidates")
        if not isinstance(cands, list) or not cands or not isinstance(cands[0], dict):
            raise LLMBadResponse("provayder cavabında candidates yoxdur (bloklanmış ola bilər)")
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") if isinstance(cand.get("content"), dict) else None
        if not isinstance(parts, list) or not parts:
            raise LLMBadResponse("provayder cavabında parts yoxdur")
        texts = []
        for p in parts:
            if not isinstance(p, dict) or set(p) - {"text"} or "text" not in p:
                raise LLMBadResponse("provayder cavabında gözlənilməyən hissə növü (alət/başqa) rədd edildi")
            texts.append(safe_text(p["text"]))
        text = "".join(texts)
        usage = data.get("usageMetadata")
        in_t, out_t = self._estimate(request, text)
        fin = cand.get("finishReason")
        return LLMResponse(text, tokens(usage, "promptTokenCount", in_t), tokens(usage, "candidatesTokenCount", out_t),
                           clean_response_model(data.get("modelVersion"), self.model), _FINISH.get(fin, "other") if isinstance(fin, str) else "stop")
