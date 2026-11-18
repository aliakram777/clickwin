"""ModelRegistry (Mərhələ 18.1-A): modelin etibar daşıyan statusunu (locality, reality, availability) provider-dən ASILI OLMADAN müəyyən edir.

Qaydalar (fail-closed):
- Yalnız DƏQİQ tipi `_TRUSTED_ADAPTERS`-də olan adapterlər qəbul olunur (alt sinif/ördək tipi/naməlum tip rədd). Siyahı koddadır; iş vaxtında genişlənmir
  (18.1-B yeni adapter tiplərini bu cədvələ koddakı dəyişiklik kimi əlavə edir).
- Provider-in `is_mock`/`name`/`describe()` dəyərləri status MƏNBƏYİ deyil. `is_mock` yalnız tipdən gözlənən dəyərlə UYĞUNLUQ yoxlaması üçün oxunur; uyğunsuzluq rədd edilir
  (özünü `is_mock=True` elan edərək Broker yoxlamasından yan keçmək mümkün deyil). `describe()` oxunmur.
- locality: endpoint-əsaslı adapterdə `kind` + `validate_endpoint(base_url, kind)` ilə; REAL yalnız `enforces_endpoint_kind(nəqliyyat, kind)` ilə (yoxsa SIMULATED).
- availability: MOCK/SIMULATED ⇒ SIMULATION; REAL ⇒ AVAILABLE yalnız sübutla; qeydiyyatdan sonra yalnız `disable` (geri açma yoxdur).
- Qiymətlər «ledger vahidi»ndədir (valyuta fərz edilmir); uzaq model üçün qiymət > 0 tələb olunur.
`resolve_for_router` yalnız `runtime/brokered_models.py` (Broker daxilində icra) üçündür (AST testi); `plan_route` adapterə toxunmur. İstehsal kodunun başqa yerindən adapteri almaq olmaz.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Sequence

from agents.llm import MockProvider
from agents.providers import EndpointError, enforces_endpoint_kind, validate_endpoint
from models.adapters.anthropic import AnthropicProvider
from models.adapters.gemini import GeminiProvider
from models.adapters.openai_compat import OpenAICompatProvider
from models.adapters.local import TRUSTED_LOCAL_RUNTIMES, LocalRuntimeProvider
from kernel.capability import Availability
from kernel.common import ContractError, Evidence
from models.descriptor import DataClass, Locality, ModelCapability, ModelDescriptor, Reality


class RegistryError(ContractError):
    pass


@dataclass(frozen=True)
class _Traits:
    mode: str                      # "mock" (şəbəkə yox) | "http" (endpoint + nəqliyyat) | "inprocess" (proses-daxili lokal runtime)
    expected_is_mock: bool


_TRUSTED_ADAPTERS: dict[type, _Traits] = {
    MockProvider: _Traits("mock", expected_is_mock=True),
    OpenAICompatProvider: _Traits("http", expected_is_mock=False),      # sərtləşdirilmiş alt sinif; kind=cloud (uzaq) və ya private (lokal server). Xam OpenAICompatibleProvider etibarlı DEYİL
    AnthropicProvider: _Traits("http", expected_is_mock=False),
    GeminiProvider: _Traits("http", expected_is_mock=False),
    LocalRuntimeProvider: _Traits("inprocess", expected_is_mock=False),
}


@dataclass(frozen=True)
class ResolvedModel:
    descriptor: ModelDescriptor
    provider: Any


def _derive(adapter: Any) -> tuple[Locality, Reality, str]:
    """(locality, reality, model_name) — YALNIZ dəqiq tipdən, yoxlanmış endpoint-dən və nəqliyyatdan. Provider-in öz elanına baxmır."""
    traits = _TRUSTED_ADAPTERS.get(type(adapter))
    if traits is None:
        raise RegistryError("adapter tipi etibarlı siyahıda deyil (dəqiq tip tələb olunur)", "untrusted_adapter_type")
    if getattr(adapter, "is_mock", None) is not traits.expected_is_mock:
        raise RegistryError("provider özünü tipinə uyğun olmayan is_mock ilə elan edib: rədd edildi", "is_mock_mismatch")
    model_name = getattr(adapter, "model", None)
    if not isinstance(model_name, str) or not model_name:
        raise RegistryError("provider model adı yoxdur", "bad_adapter")
    if traits.mode == "mock":
        return Locality.LOCAL, Reality.MOCK, model_name
    if traits.mode == "inprocess":
        if type(getattr(adapter, "runtime", None)) not in TRUSTED_LOCAL_RUNTIMES:
            raise RegistryError("lokal runtime tipi etibarlı siyahıda deyil", "untrusted_runtime")
        return Locality.LOCAL, Reality.SIMULATED, model_name              # real lokal runtime hələ yoxdur: HƏMİŞƏ simulyasiya
    kind, base = getattr(adapter, "kind", None), getattr(adapter, "base_url", None)
    if kind not in ("cloud", "private"):
        raise RegistryError("provider endpoint növü yanlışdır", "bad_endpoint")
    try:
        validate_endpoint(base, kind)                  # həmişə yenidən: provider-in daxili yoxlamasına güvənilmir
    except EndpointError:
        raise RegistryError("endpoint siyasəti pozulub", "bad_endpoint") from None
    locality = Locality.REMOTE if kind == "cloud" else Locality.LOCAL
    transport = getattr(adapter, "_transport", None)   # oxunur (təyin edilmir): yalnız dəqiq qeydli endpoint-tətbiq edən tip real sayılır
    reality = Reality.REAL if enforces_endpoint_kind(transport, kind) else Reality.SIMULATED
    return locality, reality, model_name


class ModelRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[ModelDescriptor, Any]] = {}

    def register(self, adapter: Any, *, model_id: str, provider: str, capabilities: Sequence[ModelCapability], context_tokens: int, max_output_tokens: int,
                 price_in_per_1k: float = 0.0, price_out_per_1k: float = 0.0, max_data_class: DataClass = DataClass.PUBLIC, available: bool = True,
                 evidence: Sequence[Evidence] = (), min_ram_mb: int = 0) -> ModelDescriptor:
        locality, reality, model_name = _derive(adapter)
        if available and reality is Reality.REAL and evidence:
            availability = Availability.AVAILABLE
        elif not available:
            availability = Availability.DISABLED
        elif reality is Reality.REAL:
            availability = Availability.NOT_CONFIGURED         # real, lakin sübutsuz: istifadə üçün namizəd deyil
        else:
            availability = Availability.SIMULATION
        desc = ModelDescriptor(model_id, provider, model_name, tuple(capabilities), context_tokens, max_output_tokens, locality, reality, price_in_per_1k, price_out_per_1k,
                               max_data_class, availability, tuple(evidence), min_ram_mb)
        with self._lock:
            if model_id in self._items:
                raise RegistryError("model_id artıq qeydiyyatdadır", "duplicate")
            if any(a is adapter for _, a in self._items.values()):
                raise RegistryError("eyni adapter obyekti iki model_id ilə qeydiyyata alına bilməz", "duplicate")
            self._items[model_id] = (desc, adapter)
        return desc

    def descriptor(self, model_id: str) -> ModelDescriptor | None:
        item = self._items.get(model_id)
        return item[0] if item else None

    def descriptors(self) -> tuple[ModelDescriptor, ...]:
        with self._lock:
            return tuple(d for d, _ in self._items.values())

    def disable(self, model_id: str) -> ModelDescriptor:
        """Təhlükəsiz istiqamət: modeli söndürür. Geri açma YOXDUR (yeni reyestr tələb olunur)."""
        with self._lock:
            item = self._items.get(model_id)
            if item is None:
                raise RegistryError("model tapılmadı", "unknown_model")
            desc = replace(item[0], availability=Availability.DISABLED)
            self._items[model_id] = (desc, item[1])
            return desc

    def reverify(self, model_id: str) -> bool:
        """Qeydiyyatdan sonra provider dəyişdirilibmi? Status yenidən tipdən/endpoint-dən/nəqliyyatdan çıxarılıb saxlanmış descriptor ilə müqayisə olunur."""
        item = self._items.get(model_id)
        if item is None:
            return False
        desc, adapter = item
        try:
            locality, reality, model_name = _derive(adapter)
        except RegistryError:
            return False
        return (locality, reality, model_name) == (desc.locality, desc.reality, desc.model_name)

    def resolve_for_router(self, model_id: str) -> ResolvedModel | None:
        """YALNIZ `runtime/brokered_models.py` (Broker daxilində): adapteri və descriptor-u verir. Yenidən yoxlanılır; uyğunsuzluqda None (fail-closed)."""
        if not self.reverify(model_id):
            return None
        desc, adapter = self._items[model_id]
        return ResolvedModel(desc, adapter)
