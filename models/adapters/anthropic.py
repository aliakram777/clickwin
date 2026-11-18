"""Anthropic Messages API adapteri (Mərhələ 18.1-B). Nəqliyyat inyeksiya olunur; real çağırış yoxdur. Format ictimai API sənədinə əsaslanır və real xidmətə qarşı YOXLANMAYIB."""
from __future__ import annotations

from typing import Any

from agents.llm import LLMBadResponse, LLMRequest, LLMResponse
from models.adapters.base import HttpJsonProvider, clean_response_model, safe_text, tokens

_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


class AnthropicProvider(HttpJsonProvider):
    name = "anthropic"
    DEFAULT_BASE = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def _url(self) -> str:
        return self.base_url + ("/messages" if self.base_url.endswith("/v1") else "/v1/messages")

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-api-key": key, "anthropic-version": self.API_VERSION}

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "max_tokens": request.max_output_tokens, "temperature": 0, "messages": [{"role": "user", "content": request.prompt}]}
        if request.system:
            body["system"] = request.system
        return body

    def _parse(self, data: dict, request: LLMRequest) -> LLMResponse:
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise LLMBadResponse("provayder cavabında content yoxdur")
        parts = []
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "text":
                raise LLMBadResponse("provayder cavabında gözlənilməyən blok növü (alət/başqa) rədd edildi")
            parts.append(safe_text(b.get("text")))
        text = "".join(parts)
        usage = data.get("usage")
        in_t, out_t = self._estimate(request, text)
        stop = data.get("stop_reason")
        return LLMResponse(text, tokens(usage, "input_tokens", in_t), tokens(usage, "output_tokens", out_t), clean_response_model(data.get("model"), self.model),
                           _FINISH.get(stop, "other") if isinstance(stop, str) else "stop")
