"""Vahid provider protokolu (Mərhələ 18.1-A). `ModelProvider` mövcud `agents.llm.LLMProvider` ilə UYĞUNDUR (GuardedLLM dəyişməz işləyir).

ETİBAR QAYDASI: provider obyektinin ÖZ atributları (`is_mock`, `name`, `describe()` və s.) etibar daşımır. Yerli/uzaq, pulsuz/ödənişli, mock/real statusunu provider deyil,
`models.registry.ModelRegistry` müstəqil müəyyən edir (dəqiq adapter tipi + yoxlanmış endpoint + nəqliyyat). `describe()` yalnız məlumat/diaqnostikadır."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from agents.llm import LLMRequest, LLMResponse


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    is_mock: bool
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
