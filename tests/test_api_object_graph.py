"""ApiApp/HTTP serverindən çatılan REAL obyekt qrafı: imza açarı, imtiyazlı fabrik/Company, xam kitab və uzlaşdırma ora çatmır.

DÜRÜST QEYD: bu proses-daxili obyekt qrafı yoxlamasıdır (atribut/slot/bağlanış/`__self__` yolları). Python sarğıları ixtiyari proses-daxili koda (məs. `gc.get_objects()`, modul reyestrləri,
`sys.modules`) qarşı TƏHLÜKƏSİZLİK SƏRHƏDİ DEYİL: bu test yalnız səhvən yaranan paylaşmaları (API obyektinin açar/kitab tutması) tutur. Sərhəd yalnız proses sərhədidir (agent kodu ayrı prosesdə)."""
from __future__ import annotations

import pytest

from agents.factory_auth import generate_signing_seed
from api.server import create_server
from runtime.production import build_production
from tests.test_providers import FakeTransport, env as cloud_env, ok_body
from tests.test_security_boundary import reach

FORBIDDEN_FOR_API = {"Ed25519PrivateKey", "TrustedSigner", "TrustAnchor", "OwnerIssuer", "SystemIssuer", "DispatcherIssuer", "KeyringControl", "OwnerGate", "OwnerConsole",
                     "CompanyAdmin", "Company", "AgentFactory", "FactoryAdmin", "FactoryControl", "FactoryRuntime", "AgentGateway", "AgentDispatcher",
                     "CostLedger", "LedgerReconciler"}


@pytest.fixture
def prod(tmp_path):
    e = cloud_env(NAYMERA_LLM_COST_LIMIT="5.0", NAYMERA_LLM_PRICE_IN_PER_1K="0.01", NAYMERA_LLM_PRICE_OUT_PER_1K="0.02")
    e.update({"NAYMERA_SIGNING_KEY": generate_signing_seed(), "NAYMERA_SIGNING_KEY_ID": "k-grp-001"})
    svc, engines, anchor = build_production(tmp_path / "data", e, global_limit=1.0, transport=FakeTransport([ok_body()]))
    yield svc, engines
    svc.factory.agents.close()


def types_of(root):
    return {type(o).__name__ for o in reach(root)}


def test_the_walker_has_teeth_the_trusted_container_reaches_the_privileged_objects(prod):
    svc, engines = prod
    reached = types_of(svc) & FORBIDDEN_FOR_API
    assert {"Ed25519PrivateKey", "CostLedger", "LedgerReconciler", "OwnerGate", "CompanyAdmin", "AgentFactory"} <= reached


def test_api_app_object_graph_contains_no_signing_key_ledger_reconciler_or_privileged_facades(prod):
    svc, _ = prod
    assert types_of(svc.app) & FORBIDDEN_FOR_API == set()


def test_http_server_object_graph_is_equally_clean(prod, tmp_path):
    svc, _ = prod
    httpd = create_server(svc, "127.0.0.1", 0)
    try:
        assert types_of(httpd) & FORBIDDEN_FOR_API == set()
    finally:
        httpd.server_close()


def test_the_execution_layer_sees_only_the_narrow_ledger_port(prod):
    svc, engines = prod
    port = svc.app.chat.engine.router._broker.ledger
    for obj in (port, engines.cloud.client.ledger):
        assert type(obj).__name__ == "LedgerPort" and not any(hasattr(obj, n) for n in ("record", "expire_stale", "resolve_unknown", "close", "_conn"))
    assert "CostLedger" not in types_of(svc.app) | types_of(engines.cloud)


def test_what_remains_reachable_from_the_api_is_the_documented_set(prod):
    svc, _ = prod
    allowed = {"ApprovalGateway", "EffectBroker", "LedgerPort", "Keyring", "LockedConnection", "Connection"}
    interesting = {"ApprovalGateway", "EffectBroker", "LedgerPort", "Keyring", "LockedConnection", "Connection"}
    assert (types_of(svc.app) & (FORBIDDEN_FOR_API | interesting)) <= allowed
