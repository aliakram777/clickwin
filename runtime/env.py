"""Mühit dəyişənlərindən Cloud/Local mühərriklərinin qurulması. Açarlar YALNIZ burada, `Secret` ilə oxunur.

0 AZN prioritet: real Cloud yalnız (açar + baza URL + model + açıq büdcə limiti > 0 + giriş/çıxış qiyməti > 0 + xərc kitabı) olduqda qurulur; əks halda
`not_configured` və səbəb `notes`-da. Qurulsa belə, istifadə `cloud_paid_use` razılığı tələb edir (CloudEngine). Heç nə avtomatik aktivləşdirilmir.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from agents.hybrid import CloudEngine, LocalEngine
from agents.llm import ConfigError, GuardedLLM, LLMConfig, MockProvider, load_config
from agents.providers import (EndpointError, OpenAICompatLocalRuntime, OpenAICompatibleProvider, Transport, validate_endpoint)


@dataclass
class RuntimeEngines:
    cloud: CloudEngine
    local: LocalEngine
    notes: tuple[str, ...]

    def __repr__(self) -> str:
        return f"RuntimeEngines(cloud={self.cloud.status().state}, local={self.local.status().state})"


def _positive(env: Mapping[str, str], name: str) -> float | None:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def build_engines(env: Mapping[str, str], *, log_dir: str | Path, stop_path: str | Path, transport: Transport | None = None,
                  ledger: Any = None) -> RuntimeEngines:
    notes: list[str] = []
    log_dir = Path(log_dir)
    cloud = _build_cloud(env, log_dir, Path(stop_path), transport, ledger, notes)
    local = _build_local(env, transport, notes)
    return RuntimeEngines(cloud, local, tuple(notes))


def _transport(transport: Transport | None) -> Transport:
    if transport is not None:
        return transport
    from runtime.transport import UrllibTransport  # gec idxal: real şəbəkə yalnız lazım olduqda
    return UrllibTransport()


def _build_cloud(env: Mapping[str, str], log_dir: Path, stop_path: Path, transport: Transport | None, ledger: Any,
                 notes: list[str]) -> CloudEngine:
    name = (env.get("NAYMERA_CLOUD_PROVIDER") or "").strip().lower()
    if not name:
        notes.append("cloud: provayder seçilməyib (NAYMERA_CLOUD_PROVIDER); real Cloud AI qoşulmayıb")
        return CloudEngine(None)
    if name == "mock":
        notes.append("cloud: MOCK provayder seçilib (simulyasiya, real model deyil)")
        return CloudEngine(GuardedLLM(MockProvider(default="Mock cavab: real provayder qoşulmayıb."), LLMConfig(provider="mock"),
                                      log_path=log_dir / "llm.jsonl", stop_path=stop_path))
    if name != "openai_compat":
        notes.append(f"cloud: naməlum provayder: {name[:30]}")
        return CloudEngine(None)
    problems: list[str] = []
    base = (env.get("NAYMERA_CLOUD_BASE_URL") or "").strip()
    try:
        cfg = load_config(env)
    except ConfigError:
        cfg, problems = LLMConfig(provider="openai_compat"), ["konfiqurasiya dəyəri yanlışdır"]
    if not base:
        problems.append("baza URL təyin edilməyib")
    else:
        try:
            base = validate_endpoint(base, "cloud")
        except EndpointError as exc:
            problems.append(f"baza URL yararsızdır ({exc})")
    if not cfg.model:
        problems.append("model təyin edilməyib (NAYMERA_LLM_MODEL)")
    if cfg.api_key is None:
        problems.append("API açarı təyin edilməyib (yalnız server mühit dəyişənində)")
    limit = _positive(env, "NAYMERA_LLM_COST_LIMIT")
    if limit is None:
        problems.append("büdcə limiti təyin edilməyib və ya yanlışdır (NAYMERA_LLM_COST_LIMIT > 0; 0 AZN prioritet)")
    price_in, price_out = _positive(env, "NAYMERA_LLM_PRICE_IN_PER_1K"), _positive(env, "NAYMERA_LLM_PRICE_OUT_PER_1K")
    if price_in is None or price_out is None:
        problems.append("qiymət (giriş/çıxış, 1K token) təyin edilməyib")
    if ledger is None:
        problems.append("xərc kitabı (ledger) qoşulmayıb: xərc məhdudlaşdırıla bilməz")
    if problems:
        notes.append("cloud: qurulmayıb — " + "; ".join(problems))
        return CloudEngine(None)
    provider = OpenAICompatibleProvider(base, cfg.model, cfg.api_key, _transport(transport), kind="cloud")
    cfg = replace(cfg, provider="openai_compat", price_in_per_1k=price_in, price_out_per_1k=price_out, cost_limit=limit)
    notes.append("cloud: qurulub (real provayder); istifadə üçün cloud_paid_use razılığı tələb olunur")
    return CloudEngine(GuardedLLM(provider, cfg, log_path=log_dir / "llm.jsonl", stop_path=stop_path, ledger=ledger))


def _build_local(env: Mapping[str, str], transport: Transport | None, notes: list[str]) -> LocalEngine:
    url = (env.get("NAYMERA_LOCAL_AI_URL") or "").strip()
    if not url:
        notes.append("local: şəxsi server URL-i təyin edilməyib (NAYMERA_LOCAL_AI_URL); lokal AI qoşulmayıb")
        return LocalEngine(None)
    model = (env.get("NAYMERA_LOCAL_AI_MODEL") or "local-model").strip()
    langs = tuple(x.strip() for x in (env.get("NAYMERA_LOCAL_AI_LANGUAGES") or "en").split(",") if x.strip()) or ("en",)
    try:
        provider = OpenAICompatibleProvider(url, model, None, _transport(transport), kind="private", name="local_openai_compat")
    except (EndpointError, ValueError) as exc:
        notes.append(f"local: qurulmayıb — {str(exc)[:80]}")
        return LocalEngine(None)
    notes.append("local: şəxsi server qoşulub (real server; modelin yüklənməsi serverdən asılıdır)")
    return LocalEngine(OpenAICompatLocalRuntime(provider, langs), consent_scope=None)
