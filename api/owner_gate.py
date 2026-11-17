"""Sahib şəxsiyyətinin YEGANƏ yaradılma yolu: `api/auth.py` step-up (cihazın Keystore açarı ilə biometrik imza).

`OwnerGate`:
- `challenge(...)`: server bir-dəfəlik `nonce` verir; əməliyyat adı `factory:` prefiksi ilə bağlanır (adi təsdiq step-up-ı admin şəxsiyyətinə çevrilə bilməz).
- `authenticate(...)`: cihazın imzasını `AuthService.verify_proof` ilə yoxlayır; YALNIZ uğurdan sonra qəbz `OwnerIssuer.from_step_up` ilə qısa ömürlü,
  imzalanmış sahib etibarnaməsinə çevrilir. İxtiyari `owner_id` ilə sahib yaratmaq imkanı yoxdur; uğursuz/təkrar/vaxtı keçmiş/başqa növ sübut -> etibarnamə yoxdur.
Bu modul env oxumur, şəbəkəyə çıxmır, `safety.broker/gate/tools/workspace/ledger` import etmir.
"""
from __future__ import annotations

from typing import Any

from agents.factory_auth import OwnerIssuer, SignedPrincipal
from api.auth import AuthService, Challenge

ACTION_PREFIX = "factory:"


def receipt_consumer(auth: AuthService):
    """`OwnerIssuer` üçün: qəbzi yalnız `factory:` növündə, bir dəfə qəbul edir."""
    return lambda receipt: auth.consume_receipt(receipt, ACTION_PREFIX)


class OwnerGate:
    def __init__(self, auth: AuthService, issuer: OwnerIssuer) -> None:
        self._auth, self._issuer = auth, issuer

    def challenge(self, device_id: str, operation: str, subject: str, payload_hash: str) -> Challenge:
        return self._auth.create_challenge(device_id, ACTION_PREFIX + operation, subject, payload_hash, "approve")

    def authenticate(self, device_id: str, challenge_id: Any, operation: str, subject: str, payload_hash: str, proof: Any) -> SignedPrincipal:
        receipt = self._auth.verify_proof(device_id, challenge_id, ACTION_PREFIX + operation, subject, payload_hash, "approve", proof)   # AuthError(403/429)
        return self._issuer.from_step_up(receipt)
