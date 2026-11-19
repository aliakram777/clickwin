"""Test yardımçıları (A-7): naməlum rezerv YALNIZ dəqiq sahib təsdiqi ilə həll olunur; `commit/release` onu həll etmir."""
from __future__ import annotations

from safety.ledger import CostLedger, resolution_scope
from tests.factory_keys import T0, owner_cred, ring


def owned_ledger(path=":memory:", **kw) -> CostLedger:
    """Sahib doğrulayıcısı olan kitab (test etibar çıpası, sabit saat)."""
    return CostLedger(path, verifier=ring(lambda: T0), owners={"owner"}, clock=lambda: T0, **kw)


def owner_approval(op_id: str, actual: float | None = None):
    subject, digest = resolution_scope(op_id, actual)
    return owner_cred("resolve_unknown", subject, digest)


def owner_resolve(ledger: CostLedger, op_id: str, actual: float | None = None) -> bool:
    return ledger.resolve_unknown(op_id, actual, owner_approval(op_id, actual))
