"""Test yardımçıları (B-3): hər sahib əməliyyatı üçün DƏQİQ əhatəli, bir-dəfəlik etibarnamə yaradır və real metodu çağırır.

İstehsal kodu bu yardımçılara bağlı DEYİL: `ScopedAgentFactory`/`ScopedCompany` yalnız testlərdə `OWNER`/`STRANGER` (OwnerSentinel) arqumentini hər çağırış üçün yeni etibarnamə ilə əvəz edir;
başqa arqument (saxta Principal, SYSTEM, vaxtı keçmiş və s.) olduğu kimi real obyektə ötürülür, yəni rədd testləri dəyişməzdir."""
from __future__ import annotations

from typing import Any

from agents.agent_factory import AgentFactory
from agents.company import COMPANY_OP_SCOPES, Company
from tests.factory_keys import HASH0, OwnerSentinel

_ALIASES = {"assign": "assign_task"}


class _ScopedFacet:
    def __init__(self, real: Any, company_mode: bool = False) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_company", company_mode)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if not callable(attr) or name.startswith("_") or name == "scope":
            return attr

        def call(*a: Any, **kw: Any) -> Any:
            hits = [i for i, x in enumerate(a) if isinstance(x, OwnerSentinel)] + [k for k, v in kw.items() if isinstance(v, OwnerSentinel)]
            if not hits:
                return attr(*a, **kw)
            args, kws = list(a), dict(kw)
            sentinel = next(x for x in list(a) + list(kw.values()) if isinstance(x, OwnerSentinel))
            rest_a = [x for x in args if not isinstance(x, OwnerSentinel)]
            rest_k = {k: v for k, v in kws.items() if not isinstance(v, OwnerSentinel)}
            if self._company:
                allowed = sorted(COMPANY_OP_SCOPES.get(name, {"?"}))
                op, subject, digest = allowed[0], (str(rest_a[0]) if rest_a and isinstance(rest_a[0], str) else ""), HASH0
            else:
                op, subject, digest = self._real.scope(_ALIASES.get(name, name), *rest_a, **rest_k)
            cred = sentinel.mint(op, subject, digest)
            args = [cred if isinstance(x, OwnerSentinel) else x for x in args]
            kws = {k: (cred if isinstance(v, OwnerSentinel) else v) for k, v in kws.items()}
            return attr(*args, **kws)
        return call


class ScopedAgentFactory(AgentFactory):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.control = _ScopedFacet(self.control)       # type: ignore[assignment]
        self.admin = _ScopedFacet(self.admin)           # type: ignore[assignment]


class ScopedCompany(Company):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.admin = _ScopedFacet(self.admin, company_mode=True)    # type: ignore[assignment]
