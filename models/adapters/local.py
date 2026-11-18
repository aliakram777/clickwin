"""Proses-daxili lokal model adapteri (Mərhələ 18.1-B). Şəbəkə YOXDUR. Lokal SERVER (llama.cpp/Ollama) üçün `OpenAICompatibleProvider(kind="private")` istifadə olunur.

`runtime` yalnız etibarlı DƏQİQ tiplərdən biri ola bilər (`TRUSTED_LOCAL_RUNTIMES`; bu mərhələdə yalnız oflayn `FixtureLocalRuntime`): ixtiyari çağırıla bilən kod «lokal model» kimi qəbul olunmur.
Real lokal model işləmə mühiti hələ mövcud deyil, ona görə reyestr bu adapteri HƏMİŞƏ `SIMULATED` sayır (real kimi göstərilmir)."""
from __future__ import annotations

from typing import Any

from agents.llm import LLMBadResponse, LLMProviderError, LLMRequest, LLMResponse, estimate_tokens
from models.adapters.base import check_model_name, safe_text


class FixtureLocalRuntime:
    """Oflayn lokal «model» (yalnız test/fixture): deterministik cavab, real model DEYİL."""

    def __init__(self, reply: Any = "lokal cavab", loaded: bool = True) -> None:
        self.reply, self._loaded, self.calls = reply, loaded, []

    def loaded(self) -> bool:
        return self._loaded

    def generate(self, prompt: str, system: str, max_tokens: int) -> Any:
        self.calls.append((prompt, system, max_tokens))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply(prompt) if callable(self.reply) else self.reply


TRUSTED_LOCAL_RUNTIMES: frozenset[type] = frozenset({FixtureLocalRuntime})


class LocalRuntimeProvider:
    name = "local_runtime"
    is_mock = False
    kind = "inprocess"

    def __init__(self, runtime: Any, model: str) -> None:
        if type(runtime) not in TRUSTED_LOCAL_RUNTIMES:
            raise ValueError("lokal runtime tipi etibarlı siyahıda deyil")
        self.model = check_model_name(model)
        self.runtime = runtime

    def describe(self) -> dict[str, Any]:                      # diaqnostika: etibar mənbəyi DEYİL
        return {"name": self.name, "kind": self.kind, "model": self.model}

    def __repr__(self) -> str:
        return f"LocalRuntimeProvider({self.describe()})"

    __str__ = __repr__

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            if not self.runtime.loaded():
                raise LLMProviderError("lokal model yüklənməyib", charged=False, retryable=True)
            text = self.runtime.generate(request.prompt, request.system, request.max_output_tokens)
        except LLMProviderError:
            raise
        except Exception:                                        # noqa: BLE001  lokal icra xətası: haqq tutulmur
            raise LLMProviderError("lokal model icra xətası", charged=False, retryable=True) from None
        text = safe_text(text)
        if not text:
            raise LLMBadResponse("lokal model boş cavab qaytardı")
        return LLMResponse(text, estimate_tokens(request.system + request.prompt), estimate_tokens(text), self.model)
