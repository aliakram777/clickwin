"""Lokal model kataloqu (araşdırma nəticəsi, TƏXMİNİ), cihaz resurs qoruyucusu və yükləmə planı. Heç bir model yüklənmir, şəbəkə yoxdur."""
import ast
from pathlib import Path

import pytest

from agents.local_models import (LOCAL_MODEL_CATALOG, DeviceState, ResourceGuard, ResourceLimits, plan_download,
                                 recommend)

ROOT = Path(__file__).resolve().parent.parent


def device(**kw):
    base = dict(total_ram_mb=8000, free_ram_mb=4000, free_storage_mb=20000, battery_pct=80, charging=False,
                thermal_ok=True, metered_network=False, cpu_cores=8)
    base.update(kw)
    return DeviceState(**base)


def test_catalog_is_honest_about_being_approximate_and_about_azerbaijani():
    assert len(LOCAL_MODEL_CATALOG) >= 6
    ids = [m.id for m in LOCAL_MODEL_CATALOG]
    assert len(ids) == len(set(ids))
    for m in LOCAL_MODEL_CATALOG:
        assert m.approximate is True and m.approx_size_mb > 0 and m.min_ram_mb > m.approx_size_mb * 0.5
        assert "az" not in m.languages        # Azərbaycan dili dəstəyi sübut olunmayıb: iddia edilmir
        assert m.max_complexity == "simple" and "plan" not in m.kinds
        assert m.runtime and m.platforms and m.note


def test_recommend_scales_with_the_device_and_returns_nothing_for_tiny_devices():
    weak = recommend(device(total_ram_mb=2000, free_ram_mb=900, free_storage_mb=1500), ResourceLimits())
    strong = recommend(device(), ResourceLimits())
    assert len(strong) > len(weak)
    assert all(m.min_ram_mb <= 900 for m in weak)
    assert strong[0].params_b >= strong[-1].params_b                     # ən güclüsü birinci
    assert recommend(device(total_ram_mb=800, free_ram_mb=300, free_storage_mb=300), ResourceLimits()) == []


def test_resource_guard_refuses_runs_that_would_drain_or_overheat_the_device():
    limits = ResourceLimits(min_free_ram_mb=1024, min_battery_pct=20, max_prompt_chars=2000)
    g = lambda **kw: ResourceGuard(limits, lambda: device(**kw))
    assert g().check_run(100) == (True, "")
    ok, why = g(free_ram_mb=500).check_run(100)
    assert not ok and "ram" in why
    ok, why = g(battery_pct=10).check_run(100)
    assert not ok and "battery" in why
    assert g(battery_pct=10, charging=True).check_run(100)[0] is True    # şarj olunarkən icazə verilir
    ok, why = g(thermal_ok=False).check_run(100)
    assert not ok and "thermal" in why
    ok, why = g().check_run(5000)
    assert not ok and "prompt" in why
    assert ResourceGuard(limits, None).check_run(10)[0] is False         # cihaz vəziyyəti naməlumdursa icazə verilmir


def test_download_plan_requires_consent_storage_and_unmetered_network_and_never_downloads():
    spec = LOCAL_MODEL_CATALOG[0]
    limits = ResourceLimits()
    p = plan_download(spec, device(), limits, consent_granted=False)
    assert p.allowed is False and p.needs_consent is True and p.size_mb == spec.approx_size_mb
    p = plan_download(spec, device(), limits, consent_granted=True)
    assert p.allowed is True and p.reasons == ()
    p = plan_download(spec, device(metered_network=True), limits, consent_granted=True)
    assert p.allowed is False and any("metered" in r for r in p.reasons)
    p = plan_download(spec, device(free_storage_mb=100), limits, consent_granted=True)
    assert p.allowed is False and any("storage" in r for r in p.reasons)
    p = plan_download(spec, device(battery_pct=5), limits, consent_granted=True)
    assert p.allowed is False and any("battery" in r for r in p.reasons)


def test_local_models_module_has_no_network_or_process_access():
    tree = ast.parse((ROOT / "agents" / "local_models.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m.split(".")[0] for m in mods if m} & {"socket", "http", "urllib", "requests", "ssl", "subprocess", "os", "shutil"}
