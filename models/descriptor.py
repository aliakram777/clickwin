"""ModelDescriptor / CapabilityDescriptor (Mərhələ 18.1-A): modelin dəyişməz, doğrulanan təsviri.

Üç AYRI ölçü (heç biri digərindən çıxarılmır və ya digərini nəzərdə tutmur):
- `locality`: LOCAL | REMOTE (məlumat cihazdan/etibarlı şəbəkədən çıxırmı)
- `payment`:  FREE | PAID   (qiymət metadatasından HESABLANIR; `price_*` > 0 ⇒ PAID)
- `reality`:  MOCK | SIMULATED | REAL (real xidmət çağırıla bilirmi)
Bu sahələri provider deyil, `ModelRegistry` müəyyən edir. Deserializasiya olunmuş descriptor yalnız MƏLUMATDIR, etibar sübutu deyil.

Qiymətlər «ledger vahidi»ndədir: `CostLedger`/`LLMConfig.price_*_per_1k` ilə EYNİ ədədi vahid. Ledger valyuta bildirmir (yalnız ədəd), buna görə burada valyuta ADI YOXDUR/fərz edilmir.
Heç bir sahə icra icazəsi vermir (`GRANTS_EXECUTION=False`). Məxfilik sinfi (`DataClass`) və qapıları router tətbiq edir; təsnif edilməmiş məlumat uzaq modelə GETMİR (fail-closed).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from kernel.capability import Availability
from kernel.common import (ContractError, Evidence, UnsupportedVersion, as_tuple, canonical_json, check_enum, check_int, check_keys, check_money, check_slug, clean_text,
                           parse_json_object, sha256_hex)

MODEL_SCHEMA_VERSION = 1


class Locality(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class Payment(str, Enum):
    FREE = "free"
    PAID = "paid"


class Reality(str, Enum):
    MOCK = "mock"                  # simulyasiya sinfi (MockProvider): real model DEYİL
    SIMULATED = "simulated"        # real adapter, lakin endpoint-tətbiq edən nəqliyyat YOXDUR (oflayn fixture): real xidmət kimi göstərilmir
    REAL = "real"                  # real adapter + endpoint siyasətini hər sorğuda tətbiq edən nəqliyyat


class DataClass(str, Enum):
    """Məlumat həssaslıq sinfi. Sıra: UNCLASSIFIED (etibarsız/naməlum) ayrıca qiymətləndirilir, qalanları artan həssaslıqdır."""
    UNCLASSIFIED = "unclassified"
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"

    @property
    def rank(self) -> int:
        return {"public": 0, "internal": 1, "sensitive": 2}.get(self.value, 99)       # unclassified/secret: heç bir modelin maksimumu ola bilməz


class ModelCapability(str, Enum):
    """Yalnız mətn generasiyası imkanları. Alət çağırışı, görmə, embedding, axın bu mərhələdə DƏSTƏKLƏNMİR (elan edilə bilməz)."""
    TEXT_GENERATION = "text_generation"
    CHAT = "chat"
    PLANNING = "planning"
    CODE = "code"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    JSON_OUTPUT = "json_output"
    LONG_CONTEXT = "long_context"


LONG_CONTEXT_MIN = 32_000
MAX_CONTEXT = 10_000_000
MAX_REMOTE_CLASS = DataClass.INTERNAL        # uzaq model heç vaxt SENSITIVE/SECRET qəbul etmir
MAX_LOCAL_CLASS = DataClass.SENSITIVE        # SECRET heç bir modelə verilmir


@dataclass(frozen=True)
class ModelDescriptor:
    model_id: str
    provider: str
    model_name: str
    capabilities: tuple[ModelCapability, ...]
    context_tokens: int
    max_output_tokens: int
    locality: Locality
    reality: Reality
    price_in_per_1k: float
    price_out_per_1k: float
    max_data_class: DataClass
    availability: Availability
    evidence: tuple[Evidence, ...] = ()
    min_ram_mb: int = 0                       # lokal REAL model üçün təxmini RAM tələbi (MB); resurs qoruyucusu istifadə edir (0 = qeyd yoxdur)
    schema_version: int = MODEL_SCHEMA_VERSION

    GRANTS_EXECUTION = False

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_SCHEMA_VERSION:
            raise UnsupportedVersion("model_descriptor: schema_version dəstəklənmir")
        check_slug("model_id", self.model_id)
        check_slug("provider", self.provider)
        object.__setattr__(self, "model_name", clean_text("model_name", self.model_name, max_len=80, min_len=1))
        caps = tuple(check_enum("capability", ModelCapability, c) for c in as_tuple("capabilities", self.capabilities, max_items=len(ModelCapability)))
        if not caps or len(set(caps)) != len(caps):
            raise ContractError("capabilities: boş olmayan, təkrarsız siyahı tələb olunur", "bad_capabilities")
        object.__setattr__(self, "capabilities", tuple(sorted(caps, key=lambda c: c.value)))
        object.__setattr__(self, "context_tokens", check_int("context_tokens", self.context_tokens, lo=1, hi=MAX_CONTEXT))
        object.__setattr__(self, "max_output_tokens", check_int("max_output_tokens", self.max_output_tokens, lo=1, hi=MAX_CONTEXT))
        if self.max_output_tokens > self.context_tokens:
            raise ContractError("max_output_tokens kontekst pəncərəsindən böyük ola bilməz", "bad_limits")
        if ModelCapability.LONG_CONTEXT in caps and self.context_tokens < LONG_CONTEXT_MIN:
            raise ContractError(f"long_context üçün kontekst ən azı {LONG_CONTEXT_MIN} olmalıdır", "bad_capabilities")
        object.__setattr__(self, "locality", check_enum("locality", Locality, self.locality))
        object.__setattr__(self, "reality", check_enum("reality", Reality, self.reality))
        object.__setattr__(self, "price_in_per_1k", check_money("price_in_per_1k", self.price_in_per_1k))
        object.__setattr__(self, "price_out_per_1k", check_money("price_out_per_1k", self.price_out_per_1k))
        object.__setattr__(self, "max_data_class", check_enum("max_data_class", DataClass, self.max_data_class))
        object.__setattr__(self, "availability", check_enum("availability", Availability, self.availability))
        ev = as_tuple("evidence", self.evidence, max_items=20)
        if not all(isinstance(e, Evidence) for e in ev):
            raise ContractError("evidence: Evidence elementləri tələb olunur", "bad_type")
        object.__setattr__(self, "evidence", ev)
        object.__setattr__(self, "min_ram_mb", check_int("min_ram_mb", self.min_ram_mb, lo=0, hi=1_000_000))
        # --- invariantlar (hər yaradılışda və deserializasiyada tətbiq olunur)
        if self.locality is Locality.REMOTE:                      # unclassified/secret (rank 99) hər iki halda yuxarı hədləri aşır: heç bir model üçün maksimum ola bilməz
            if self.max_data_class.rank > MAX_REMOTE_CLASS.rank:
                raise ContractError("uzaq model yalnız public/internal məlumat qəbul edə bilər", "bad_data_class")
            if not (self.price_in_per_1k > 0 and self.price_out_per_1k > 0):
                raise ContractError("uzaq model üçün giriş/çıxış qiyməti (ledger vahidi) > 0 tələb olunur", "price_required")
        elif self.max_data_class.rank > MAX_LOCAL_CLASS.rank:
            raise ContractError("lokal model üçün maksimum sinif həddi aşılıb", "bad_data_class")
        if self.reality is Reality.MOCK and (self.locality is not Locality.LOCAL or self.price_in_per_1k or self.price_out_per_1k):
            raise ContractError("mock model lokal və qiymətsiz olmalıdır", "bad_mock")
        if self.availability is Availability.AVAILABLE:
            if self.reality is not Reality.REAL or not ev:
                raise ContractError("available yalnız sübutlu REAL model üçündür (mock/simulyasiya real kimi göstərilmir)", "unverified_available")
        elif self.availability is Availability.NOT_CONFIGURED and self.reality is Reality.MOCK:
            raise ContractError("mock model not_configured ola bilməz", "bad_availability")

    @property
    def payment(self) -> Payment:
        """Qiymət metadatasından HESABLANIR (saxlanmır, elan edilə bilməz)."""
        return Payment.PAID if (self.price_in_per_1k > 0 or self.price_out_per_1k > 0) else Payment.FREE

    @property
    def usable(self) -> bool:
        """İstifadə üçün namizəd ola bilər (real və ya açıq simulyasiya); bu, İCAZƏ deyil: razılıq/məxfilik/büdcə qapıları router və GuardedLLM-dədir."""
        return self.availability in (Availability.AVAILABLE, Availability.SIMULATION)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "model_id": self.model_id, "provider": self.provider, "model_name": self.model_name,
                "capabilities": [c.value for c in self.capabilities], "context_tokens": self.context_tokens, "max_output_tokens": self.max_output_tokens,
                "locality": self.locality.value, "payment": self.payment.value, "reality": self.reality.value, "price_in_per_1k": self.price_in_per_1k,
                "price_out_per_1k": self.price_out_per_1k, "max_data_class": self.max_data_class.value, "availability": self.availability.value,
                "evidence": [e.to_dict() for e in self.evidence], "min_ram_mb": self.min_ram_mb}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.to_json())

    @classmethod
    def from_dict(cls, data: Any) -> "ModelDescriptor":
        check_keys("model_descriptor", data, ("schema_version", "model_id", "provider", "model_name", "capabilities", "context_tokens", "max_output_tokens", "locality",
                                              "payment", "reality", "price_in_per_1k", "price_out_per_1k", "max_data_class", "availability"), ("evidence", "min_ram_mb"))
        obj = cls(data["model_id"], data["provider"], data["model_name"], tuple(as_tuple("capabilities", data["capabilities"], max_items=len(ModelCapability))),
                  data["context_tokens"], data["max_output_tokens"], check_enum("locality", Locality, data["locality"]), check_enum("reality", Reality, data["reality"]),
                  data["price_in_per_1k"], data["price_out_per_1k"], check_enum("max_data_class", DataClass, data["max_data_class"]),
                  check_enum("availability", Availability, data["availability"]), tuple(Evidence.from_dict(e) for e in as_tuple("evidence", data.get("evidence", []), max_items=20)),
                  data.get("min_ram_mb", 0), data["schema_version"])
        if data["payment"] != obj.payment.value:                      # payment hesablanan sahədir: uyğunsuz elan rədd edilir
            raise ContractError("payment qiymət metadatasına uyğun deyil (elan edilə bilməz)", "payment_mismatch")
        return obj

    @classmethod
    def from_json(cls, text: str) -> "ModelDescriptor":
        return cls.from_dict(parse_json_object("model_descriptor", text))


CapabilityDescriptor = ModelDescriptor           # plan tələbindəki ad: model təsviri = imkan təsviri
