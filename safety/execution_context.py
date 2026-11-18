"""Effect Broker icra konteksti (audit A-8): ödənişli LLM çağırışı bu kontekstdən KƏNARDA işləməməlidir.

`EffectBroker` icraçını çağırmazdan əvvəl bayrağı qaldırır (Gate/Ledger/stop/təsdiq mərhələləri artıq keçilib). `GuardedLLM` real (mock olmayan) provayderi YALNIZ bu bayraq
qalxıbsa çağırır. Bu proses-daxili `ContextVar`-dır: eyni prosesdə ixtiyari kod onu özü qaldıra bilər, buna görə agent kodu AYRI prosesdə işləyir (`runtime/agent_gateway.py`).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_ACTIVE: ContextVar[bool] = ContextVar("naymera_broker_active", default=False)


def in_broker() -> bool:
    return _ACTIVE.get()


@contextmanager
def broker_context() -> Iterator[None]:
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
