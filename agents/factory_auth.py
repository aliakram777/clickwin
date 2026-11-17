"""Fabrik sərhədində autentifikasiya edilmiş səlahiyyətlər (F-1).

`Principal` sadəcə bir iddiadır: onu hər kəs `Principal("owner", "human", ...)` kimi YARADA bilər. Fabrik artıq belə iddiaya etibar etmir.
Fabrikdə yalnız `PrincipalAuthority` tərəfindən imzalanmış (`SignedPrincipal`) sahib/sistem/dispetçer şəxsiyyətləri qəbul olunur.

- `PrincipalAuthority` etibarlı kanal adapterinin (məs. Keystore/biometrik step-up-ı yoxlayan API qatı) və bootstrap-ın əlindədir; açar yalnız ondadır.
  Fabrikə eyni açarla qurulmuş nüsxə verilir (HMAC-SHA256; sahib siyahısı və imza ayrıca yoxlanır).
- Açar yoxdursa (`authority=None`) fabrikin BÜTÜN imtiyazlı əməliyyatları bağlı qalır (fail closed).
- İmza `id`, `kind`, `channel` sahələrini bağlayır; sahəni dəyişən və ya imzanı kopyalayan başqa şəxsiyyət yaradıla bilmir.
- `SignedPrincipal` daşıyıcı (bearer) etibarnamədir: onu yalnız etibarlı kanal adapteri saxlamalı və qısa ömürlü tutmalıdır.
  Prosesin daxilində açarı oxuya bilən ixtiyari kod qarşısında qoruma vəd edilmir (bu OS sandbox deyil).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

from safety.approvals import Principal

MIN_KEY_BYTES = 32
ROLES = ("owner", "system", "dispatcher", "mock_owner")   # imzanın bağlandığı səlahiyyət rolu


@dataclass(frozen=True)
class SignedPrincipal(Principal):
    """`PrincipalAuthority` tərəfindən imzalanmış şəxsiyyət. `proof` repr/müqayisəyə düşmür."""
    role: str = ""
    proof: str = field(default="", repr=False, compare=False)


class PrincipalAuthority:
    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
            raise ValueError(f"imza açarı ən azı {MIN_KEY_BYTES} bayt olmalıdır")
        self.__key = bytes(key)

    def __repr__(self) -> str:   # açar heç vaxt görünmür
        return "PrincipalAuthority(<key hidden>)"

    def __reduce__(self):  # pickle/copy ilə açarın çıxarılmasına icazə yoxdur
        raise TypeError("PrincipalAuthority serializasiya olunmur")

    def _mac(self, pid: str, kind: str, channel: str, role: str) -> str:
        msg = "\x1f".join(("naymera-principal-v1", pid, kind, channel, role)).encode("utf-8")
        return hmac.new(self.__key, msg, hashlib.sha256).hexdigest()

    def _issue(self, pid: str, kind: str, channel: str, role: str) -> SignedPrincipal:
        return SignedPrincipal(pid, kind, channel, role, self._mac(pid, kind, channel, role))

    # --- yalnız etibarlı bootstrap / kanal adapteri çağırır ---
    def owner(self, owner_id: str, channel: str = "owner-console") -> SignedPrincipal:
        """Autentifikasiya olunmuş sahib (kanal adapteri step-up yoxlamasından SONRA çağırmalıdır)."""
        return self._issue(owner_id, "human", channel, "owner")

    def mock_owner(self, owner_id: str, channel: str = "test") -> SignedPrincipal:
        """Test sahibi (`kind="mock"`): fabrik yalnız `allow_mock=True` olduqda qəbul edir; real sahib təsdiqi deyil."""
        return self._issue(owner_id, "mock", channel, "mock_owner")

    def system(self, system_id: str = "naymera", channel: str = "core") -> SignedPrincipal:
        return self._issue(system_id, "system", channel, "system")

    def dispatcher(self, dispatcher_id: str = "dispatcher", channel: str = "runtime") -> SignedPrincipal:
        """Agent icrasını idarə edən dispetçer: tapşırığı tutmaq və nəticə səlahiyyəti almaq üçün."""
        return self._issue(dispatcher_id, "system", channel, "dispatcher")

    def verify(self, principal: object, *roles: str) -> bool:
        if not isinstance(principal, SignedPrincipal) or not principal.proof:
            return False
        if roles and principal.role not in roles:
            return False
        if principal.role not in ROLES:
            return False
        expected = self._mac(principal.id, principal.kind, principal.channel, principal.role)
        return hmac.compare_digest(expected, principal.proof)
