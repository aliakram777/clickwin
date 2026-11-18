"""Oflayn fixture nəqliyyatlar və cavab qurucuları (Mərhələ 18.1-B). Şəbəkə YOXDUR. `FixtureTransport` endpoint-tətbiq edən nəqliyyat DEYİL (qeydli tip deyil):
ona görə fixture ilə qurulan hər adapter reyestrdə avtomatik `SIMULATED` olur və real kimi göstərilmir."""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from agents.providers import HttpRequest, HttpResponse, TransportError


def _body(obj: Any) -> bytes:
    return json.dumps(obj).encode("utf-8")


def openai_ok(text: str, *, prompt_tokens: int | None = 5, completion_tokens: int | None = 7, model: str = "fixture-model", finish: str = "stop") -> HttpResponse:
    usage = {k: v for k, v in (("prompt_tokens", prompt_tokens), ("completion_tokens", completion_tokens)) if v is not None}
    return HttpResponse(200, _body({"model": model, "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}], "usage": usage}))


def anthropic_ok(text: str, *, input_tokens: int | None = 5, output_tokens: int | None = 7, model: str = "fixture-model", stop: str = "end_turn") -> HttpResponse:
    usage = {k: v for k, v in (("input_tokens", input_tokens), ("output_tokens", output_tokens)) if v is not None}
    return HttpResponse(200, _body({"id": "msg_fixture", "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": text}],
                                    "stop_reason": stop, "usage": usage}))


def gemini_ok(text: str, *, prompt_tokens: int | None = 5, candidate_tokens: int | None = 7, model: str = "fixture-model", finish: str = "STOP") -> HttpResponse:
    usage = {k: v for k, v in (("promptTokenCount", prompt_tokens), ("candidatesTokenCount", candidate_tokens)) if v is not None}
    return HttpResponse(200, _body({"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}], "usageMetadata": usage, "modelVersion": model}))


Step = HttpResponse | BaseException | Callable[[HttpRequest], HttpResponse]


class FixtureTransport:
    """Ssenarili oflayn nəqliyyat: növbə ilə cavab/istisna/funksiya verir; sorğuları qeyd edir. Ssenari bitərsə TransportError."""

    def __init__(self, *steps: Step) -> None:
        self._steps, self.requests, self._lock = list(steps), [], threading.Lock()

    def __call__(self, req: HttpRequest) -> HttpResponse:
        with self._lock:                                      # paralel agent testləri üçün iplik-təhlükəsiz
            self.requests.append(req)
            if not self._steps:
                raise TransportError("fixture ssenarisi bitdi")
            step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step(req) if callable(step) else step
