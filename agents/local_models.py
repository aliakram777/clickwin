"""Lokal (cihazda/şəxsi serverdə) kiçik model variantları, cihaz resurs qoruyucusu və yükləmə planı.

DİQQƏT: kataloq araşdırma nəticəsidir və TƏXMİNİDİR (ölçülər/RAM tələbi kvantizasiyadan və icraçıdan asılıdır, burada ölçülməyib);
`approximate=True`. Heç bir model burada yüklənmir və şəbəkə kodu yoxdur: yükləmə yalnız PLANLANIR və istifadəçi razılığı tələb edir.
Azərbaycan dili üçün heç bir kiçik modelin keyfiyyəti sübut olunmayıb, ona görə `languages`-də `az` YOXDUR (Local AI Azərbaycan
mətnini dəstəklədiyi iddia edilmir; real model qoşulub ölçüləndən sonra yenilənməlidir).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LocalModelSpec:
    id: str
    family: str
    params_b: float            # milyard parametr
    quant: str
    approx_size_mb: int        # təxmini disk ölçüsü
    min_ram_mb: int            # təxmini işləmə RAM tələbi (kontekst daxil)
    runtime: str
    platforms: tuple[str, ...]
    languages: tuple[str, ...]
    note: str
    kinds: tuple[str, ...] = ("chat", "classify", "summarize")
    max_complexity: str = "simple"   # kiçik modellər ciddi JSON plan/uzun təhlil üçün etibarlı deyil
    approximate: bool = True


_RT_ANDROID = "llama.cpp (GGUF) / MediaPipe LLM Inference / MLC LLM / ExecuTorch"
_RT_SERVER = "llama.cpp / Ollama-uyğun yerli HTTP server"

LOCAL_MODEL_CATALOG: tuple[LocalModelSpec, ...] = (
    LocalModelSpec("llama3.2-3b-q4", "Llama 3.2", 3.2, "Q4", 2000, 3800, _RT_ANDROID, ("android-highend", "server"), ("en",),
                   "Güclü cihazlar üçün; rəsmi dil siyahısında az/tr/ru yoxdur (yoxlanmalı)."),
    LocalModelSpec("phi3-mini-q4", "Phi-3 mini", 3.8, "Q4", 2300, 4200, _RT_ANDROID, ("android-highend", "server"), ("en",),
                   "İngilis dilli; lisenziya şərtləri yoxlanmalıdır."),
    LocalModelSpec("gemma2-2b-q4", "Gemma 2", 2.6, "Q4", 1600, 3000, _RT_ANDROID, ("android-highend", "android-mid", "server"), ("en",),
                   "Orta/yüksək cihazlar; Azərbaycan dili sübut olunmayıb; lisenziya şərtləri yoxlanmalıdır."),
    LocalModelSpec("qwen2.5-1.5b-q4", "Qwen2.5", 1.5, "Q4", 1000, 2000, _RT_ANDROID, ("android-mid", "android-highend", "server"), ("en",),
                   "Çoxdilli iddia edilir, lakin az/tr keyfiyyəti yoxlanmayıb."),
    LocalModelSpec("llama3.2-1b-q4", "Llama 3.2", 1.2, "Q4", 800, 1600, _RT_ANDROID, ("android-mid", "android-highend", "server"), ("en",),
                   "Orta cihazlar; qısa cavablar və təsnifat."),
    LocalModelSpec("qwen2.5-0.5b-q4", "Qwen2.5", 0.5, "Q4", 400, 900, _RT_ANDROID, ("android-low", "android-mid", "android-highend", "server"),
                   ("en",), "Zəif cihazlar; yalnız sadə təsnifat/qısa cavab."),
    LocalModelSpec("smollm2-360m-q4", "SmolLM2", 0.36, "Q4", 250, 600, _RT_ANDROID, ("android-low", "android-mid", "android-highend", "server"),
                   ("en",), "Ən kiçik variant; keyfiyyət çox məhduddur."),
)


@dataclass(frozen=True)
class DeviceState:
    total_ram_mb: int
    free_ram_mb: int
    free_storage_mb: int
    battery_pct: int
    charging: bool
    thermal_ok: bool
    metered_network: bool
    cpu_cores: int = 4


@dataclass(frozen=True)
class ResourceLimits:
    min_free_ram_mb: int = 1024
    min_battery_pct: int = 20
    min_free_storage_mb: int = 512          # modeldən əlavə ehtiyat
    max_prompt_chars: int = 2000
    max_output_tokens: int = 256
    allow_metered_download: bool = False


class ResourceGuard:
    """İcradan əvvəl cihaz resurslarını yoxlayır (RAM, batareya, istilik, sorğu ölçüsü). Cihaz vəziyyəti naməlumdursa icazə verilmir."""

    def __init__(self, limits: ResourceLimits | None = None, state: Callable[[], DeviceState] | None = None) -> None:
        self.limits = limits or ResourceLimits()
        self._state = state

    def check_run(self, prompt_chars: int) -> tuple[bool, str]:
        if self._state is None:
            return False, "device_state_unknown"
        s, lim = self._state(), self.limits
        if prompt_chars > lim.max_prompt_chars:
            return False, f"prompt_too_long:{prompt_chars}>{lim.max_prompt_chars}"
        if s.free_ram_mb < lim.min_free_ram_mb:
            return False, f"ram_low:{s.free_ram_mb}<{lim.min_free_ram_mb}"
        if s.battery_pct < lim.min_battery_pct and not s.charging:
            return False, f"battery_low:{s.battery_pct}<{lim.min_battery_pct}"
        if not s.thermal_ok:
            return False, "thermal_throttled"
        return True, ""


def recommend(state: DeviceState, limits: ResourceLimits) -> list[LocalModelSpec]:
    """Cihaza sığan modellər (ən güclüsü birinci). Yalnız tövsiyədir: heç nə yüklənmir."""
    out = [m for m in LOCAL_MODEL_CATALOG
           if m.min_ram_mb <= state.free_ram_mb and m.approx_size_mb + limits.min_free_storage_mb <= state.free_storage_mb]
    return sorted(out, key=lambda m: (-m.params_b, m.id))


@dataclass(frozen=True)
class DownloadPlan:
    spec_id: str
    size_mb: int
    allowed: bool
    needs_consent: bool
    reasons: tuple[str, ...] = ()


def plan_download(spec: LocalModelSpec, state: DeviceState, limits: ResourceLimits, consent_granted: bool) -> DownloadPlan:
    """Yükləməni YALNIZ planlaşdırır (real yükləmə bu mərhələdə yoxdur). İstifadəçi razılığı, yaddaş, şəbəkə növü və batareya yoxlanılır."""
    reasons: list[str] = []
    if not consent_granted:
        reasons.append("consent_required:local_model_download")
    if state.metered_network and not limits.allow_metered_download:
        reasons.append("metered_network")
    if spec.approx_size_mb + limits.min_free_storage_mb > state.free_storage_mb:
        reasons.append(f"storage_low:{state.free_storage_mb}")
    if state.battery_pct < limits.min_battery_pct and not state.charging:
        reasons.append(f"battery_low:{state.battery_pct}")
    allowed = not reasons
    return DownloadPlan(spec.id, spec.approx_size_mb, allowed, not consent_granted, tuple(reasons))
