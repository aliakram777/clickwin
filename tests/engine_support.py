"""YALNIZ TEST: `build_app`-ın fail-closed mühərrik yoxlamasında ördək tipli saxta mühərrikləri/nəqliyyatları dəqiq TİPLƏ icazəli edir.

Bu `build_app` API-sinin parametri DEYİL: `monkeypatch` ilə `api.bootstrap._TEST_ONLY_TYPES` təyin edilir (test sonunda geri qaytarılır). İstehsal kodu bunu təyin etmir (AST testi)
və pytest yüklənməyibsə qeyd edilmiş tip `RuntimeError` verir."""
from __future__ import annotations

import pytest

import api.bootstrap as bootstrap


@pytest.fixture
def trust_test_types(monkeypatch):
    def register(*types: type) -> None:
        monkeypatch.setattr(bootstrap, "_TEST_ONLY_TYPES", frozenset(types))
    return register
