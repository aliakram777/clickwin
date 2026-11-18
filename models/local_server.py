"""Lokal model serveri qeydiyyatı (Mərhələ 18.6): `OpenAICompatProvider(kind="private")` + endpoint-tətbiq edən nəqliyyat ilə REAL lokal modelin reyestrə təhlükəsiz qeydi. Bu modul ŞƏBƏKƏYƏ ÇIXMIR,
heç nə yükləmir/başlatmır: yalnız qeydiyyat. Model faylının yüklənməsi `agents.local_models.plan_download` (sahib razılığı) ilə ayrıca planlanır.

Qaydalar: `base_url` yalnız həqiqətən lokal/özəl hədəf ola bilər (`validate_endpoint(…, "private")`); `require_real=True` (defolt) olduqda nəqliyyat endpoint-tətbiq edən qeydli tip olmalıdır, əks halda qeydiyyat rədd
edilir (səssiz SIMULATED yox); `min_ram_mb` kataloqdan (təxmini, `LOCAL_MODEL_CATALOG`) və ya açıq verilir; AVAILABLE üçün sübut (`evidence`) lazımdır. İşləmə zamanı `local_resource_use` razılığı və cihaz resursları tələb olunur (router)."""
from __future__ import annotations

from typing import Any, Sequence

from agents.local_models import LOCAL_MODEL_CATALOG
from agents.providers import Transport
from kernel.common import ContractError, Evidence
from models.adapters.openai_compat import OpenAICompatProvider
from models.descriptor import DataClass, ModelCapability, ModelDescriptor, Reality
from models.registry import ModelRegistry, RegistryError


def catalog_min_ram_mb(catalog_id: str) -> int:
    for spec in LOCAL_MODEL_CATALOG:
        if spec.id == catalog_id:
            return spec.min_ram_mb
    raise ContractError("catalog_id kataloqda yoxdur", "unknown_catalog_id")


def register_local_server(registry: ModelRegistry, *, model_id: str, base_url: str, model_name: str, transport: Transport, capabilities: Sequence[ModelCapability],
                          context_tokens: int, max_output_tokens: int, catalog_id: str | None = None, min_ram_mb: int | None = None, max_data_class: DataClass = DataClass.SENSITIVE,
                          evidence: Sequence[Evidence] = (), require_real: bool = True, provider: str = "local_server") -> ModelDescriptor:
    ram = catalog_min_ram_mb(catalog_id) if catalog_id else (min_ram_mb or 0)
    adapter: Any = OpenAICompatProvider(base_url, model_name, None, transport, kind="private")           # ictimai host ⇒ ValueError (provider özü)
    desc = registry.register(adapter, model_id=model_id, provider=provider, capabilities=tuple(capabilities), context_tokens=context_tokens, max_output_tokens=max_output_tokens,
                             max_data_class=max_data_class, evidence=tuple(evidence), min_ram_mb=ram)
    if require_real and desc.reality is not Reality.REAL:
        registry.disable(model_id)                                                                      # real kimi görünməsin
        raise RegistryError("nəqliyyat endpoint-tətbiq edən qeydli tip deyil: REAL lokal model kimi qeydə alına bilməz", "not_real")
    return registry.descriptor(model_id) or desc
