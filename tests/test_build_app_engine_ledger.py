"""build_app() bölünmüş-kitab yan keçidi: real (mock olmayan) ödənişli mühərrik HEÇ VAXT ayrıca/səssiz yaradılmış kitabla işləməməlidir.

Bu testlər yalnız PR #24-də mövcud API-ləri istifadə edir (build_engines, CostLedger, LedgerPort, build_app) və PR #24 kodunda UĞURSUZ olur (build_app ikinci kitab yaradırdı)."""
from __future__ import annotations

import pytest

from agents.hybrid import CloudEngine, LocalEngine
from api.bootstrap import build_app
from runtime.env import build_engines
from runtime.production import build_production
from safety.ledger import CostLedger, LedgerPort
from tests.test_hybrid_router import Fake
from tests.test_providers import FakeTransport, env as cloud_env, ok_body

PAID = dict(NAYMERA_LLM_COST_LIMIT="5.0", NAYMERA_LLM_PRICE_IN_PER_1K="0.01", NAYMERA_LLM_PRICE_OUT_PER_1K="0.02")


def real_engines(tmp_path, ledger):
    return build_engines(cloud_env(**PAID), log_dir=tmp_path, stop_path=tmp_path / "STOP", transport=FakeTransport([ok_body()]), ledger=ledger)


def test_real_cloud_engine_without_an_explicit_ledger_is_rejected(tmp_path):
    own = CostLedger(tmp_path / "llm_ledger.db")
    eng = real_engines(tmp_path, LedgerPort(own))
    assert eng.cloud.status().state == "ready" and eng.cloud.status().is_mock is False
    with pytest.raises(ValueError, match="ledger="):
        build_app(tmp_path / "data", global_limit=0.04, cloud_engine=eng.cloud, local_engine=eng.local)


def test_rejected_build_does_not_leave_a_second_ledger_behind(tmp_path):
    eng = real_engines(tmp_path, LedgerPort(CostLedger(tmp_path / "llm_ledger.db")))
    with pytest.raises(ValueError):
        build_app(tmp_path / "data", global_limit=0.04, cloud_engine=eng.cloud)
    assert sorted(p.name for p in (tmp_path / "data").glob("*ledger*")) == []


def test_real_engine_with_a_different_ledger_is_rejected_for_raw_and_port(tmp_path):
    engine_ledger = CostLedger(tmp_path / "engine.db", global_limit=0.04)
    other = CostLedger(tmp_path / "other.db", global_limit=0.04)
    eng = real_engines(tmp_path, LedgerPort(engine_ledger))
    for given in (other, LedgerPort(other)):
        with pytest.raises(ValueError, match="EYNİ deyil"):
            build_app(tmp_path / "data", global_limit=0.04, cloud_engine=eng.cloud, ledger=given)


def test_real_engine_without_any_ledger_of_its_own_is_rejected(tmp_path):
    shared = CostLedger(tmp_path / "shared.db", global_limit=0.04)
    eng = real_engines(tmp_path, LedgerPort(shared))
    eng.cloud.client.ledger = None                                        # kitabsız real provayder
    with pytest.raises(ValueError, match="kitabı yoxdur"):
        build_app(tmp_path / "data", global_limit=0.04, cloud_engine=eng.cloud, ledger=shared)


def test_real_engine_on_the_same_backend_is_accepted_via_port_or_raw_ledger(tmp_path):
    shared = CostLedger(tmp_path / "shared.db", global_limit=0.04)
    eng = real_engines(tmp_path, LedgerPort(shared))
    for given in (shared, LedgerPort(shared)):
        svc = build_app(tmp_path / f"data{id(given)}", global_limit=0.04, cloud_engine=eng.cloud, local_engine=eng.local, ledger=given)
        assert svc.ledger is shared


def test_mock_and_not_configured_engines_need_no_ledger(tmp_path):
    build_app(tmp_path / "d1", cloud_engine=CloudEngine(None), local_engine=LocalEngine(None))
    mock = build_engines({"NAYMERA_CLOUD_PROVIDER": "mock"}, log_dir=tmp_path, stop_path=tmp_path / "STOP", ledger=None)
    assert mock.cloud.status().is_mock is True
    build_app(tmp_path / "d2", cloud_engine=mock.cloud, local_engine=mock.local)
    build_app(tmp_path / "d3", cloud_engine=Fake("cloud", is_mock=True), local_engine=Fake("local", state="not_configured"))


def test_build_production_remains_the_working_canonical_path(tmp_path):
    svc, engines, _ = build_production(tmp_path / "p", cloud_env(**PAID), global_limit=0.5, transport=FakeTransport([ok_body()]))
    assert engines.cloud.status().state == "ready" and svc.ledger.global_limit == 0.5
    assert sorted(p.name for p in (tmp_path / "p").glob("*ledger*")) == ["ledger.db"]
