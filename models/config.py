"""Model seçimi konfiqurasiyası (Mərhələ 18.1-E): kod dəyişmədən model ÜSTÜNLÜYÜNÜ dəyişmək. Konfiqurasiya YALNIZ SIRA və əlavə capability tələbi verir.

Konfiqurasiya təhlükəsizlik qaydalarını zəiflədə BİLMƏZ (sxem səviyyəsində): `allow_remote`, `allow_simulated`, razılıq, məlumat sinfi, büdcə/qiymət, `max_data_class`, endpoint, açar, adapter tipi,
əlçatanlıq və s. sahələr TANINMIR və fayl rədd edilir (fail-closed; bilinən həssas adlar üçün ayrıca `security_key_forbidden` kodu). Naməlum sahə, təkrar açar, yanlış tip/ölçü, naməlum capability,
qeydsiz `model_id` (reyestrə qarşı `validate_against`) ⇒ yükləmə uğursuz, əvvəlki konfiqurasiya qalır. Üstünlük yalnız qapıları keçmiş namizədlər arasında SIRALAMAQdır: uyğunsuz modeli uyğun edə bilməz.
`capabilities` yalnız ƏLAVƏ tələbdir (sorğunun tələbini azalda bilməz). Fayl oxumur, mühit dəyişəni/şəbəkə istifadə etmir; mətn çağıran tərəfdən verilir. Real API/ödəniş aktivləşdirmir.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kernel.common import (ContractError, UnsupportedVersion, as_tuple, canonical_json, check_enum, check_keys, check_slug, parse_json_object, sha256_hex)
from models.descriptor import ModelCapability
from models.registry import ModelRegistry

CONFIG_SCHEMA_VERSION = 1
MAX_PROFILES = 16
MAX_PREFER = 16

FORBIDDEN_KEYS = frozenset({
    "allow_remote", "allow_simulated", "granted_consents", "consents", "consent", "data_class", "max_data_class", "budget", "budgets", "cost_limit", "global_limit", "agent_limit",
    "price", "price_in_per_1k", "price_out_per_1k", "api_key", "key", "secret", "token", "password", "endpoint", "base_url", "url", "adapter", "provider", "available", "availability",
    "reality", "locality", "payment", "is_mock", "trusted", "safe", "verified", "evidence", "unhealthy", "health", "skip_checks", "disable_checks", "bypass", "broker", "ledger"})


class ConfigError(ContractError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    prefer: tuple[str, ...]
    capabilities: tuple[ModelCapability, ...] = ()


@dataclass(frozen=True)
class ModelRoutingConfig:
    profiles: tuple[Profile, ...]
    schema_version: int = CONFIG_SCHEMA_VERSION

    GRANTS_EXECUTION = False

    def profile(self, name: Any) -> Profile | None:
        return next((p for p in self.profiles if p.name == name), None) if isinstance(name, str) else None

    def validate_against(self, registry: ModelRegistry) -> None:
        """Hər `prefer` id-si reyestrdə qeydli olmalıdır (naməlum model ⇒ rədd). Konfiqurasiya modeli qeydiyyata ALA bilməz."""
        known = {d.model_id for d in registry.descriptors()}
        for p in self.profiles:
            unknown = [m for m in p.prefer if m not in known]
            if unknown:
                raise ConfigError(f"profil '{p.name}': qeydsiz model_id: {', '.join(unknown[:3])}", "unknown_model")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "profiles": {p.name: {"prefer": list(p.prefer), "capabilities": [c.value for c in p.capabilities]} for p in self.profiles}}

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, data: Any) -> "ModelRoutingConfig":
        _reject_forbidden("config", data)
        check_keys("config", data, ("schema_version", "profiles"))
        if isinstance(data["schema_version"], bool) or data["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise UnsupportedVersion("config: schema_version dəstəklənmir")
        raw = data["profiles"]
        if not isinstance(raw, dict) or not raw or len(raw) > MAX_PROFILES:
            raise ConfigError(f"profiles: 1..{MAX_PROFILES} profilli obyekt tələb olunur", "bad_profiles")
        out = []
        for name in sorted(raw):
            check_slug("profile.name", name)
            body = raw[name]
            _reject_forbidden(f"profile.{name}", body)
            check_keys(f"profile.{name}", body, ("prefer",), ("capabilities",))
            prefer = tuple(check_slug("prefer", m) for m in as_tuple("prefer", body["prefer"], max_items=MAX_PREFER))
            if not prefer or len(set(prefer)) != len(prefer):
                raise ConfigError(f"profil '{name}': boş olmayan, təkrarsız prefer siyahısı tələb olunur", "bad_prefer")
            caps = tuple(check_enum("capability", ModelCapability, c) for c in as_tuple("capabilities", body.get("capabilities", []), max_items=len(ModelCapability)))
            if len(set(caps)) != len(caps):
                raise ConfigError(f"profil '{name}': təkrar capability", "bad_capabilities")
            out.append(Profile(name, prefer, tuple(sorted(caps, key=lambda c: c.value))))
        return cls(tuple(out))

    @classmethod
    def from_json(cls, text: Any) -> "ModelRoutingConfig":
        return cls.from_dict(parse_json_object("config", text))


def _reject_forbidden(where: str, data: Any) -> None:
    if isinstance(data, dict):
        bad = sorted(str(k) for k in data if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS)
        if bad:
            raise ConfigError(f"{where}: təhlükəsizliyə təsir edən sahə konfiqurasiya ilə dəyişdirilə bilməz: {', '.join(bad[:3])}", "security_key_forbidden")
