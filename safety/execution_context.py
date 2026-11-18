"""Effect Broker icra konteksti (audit A-8): ödənişli LLM çağırışı bu kontekstdən KƏNARDA işləməməlidir.

`EffectBroker` icraçını çağırmazdan əvvəl bayrağı qaldırır (Gate/Ledger/stop/təsdiq mərhələləri artıq keçilib). `GuardedLLM` real (mock olmayan) provayderi YALNIZ bu bayraq
qalxıbsa çağırır.

HƏDDİ AÇIQ BİLDİRİŞ (bu OS təhlükəsizlik sərhədi DEYİL):
- Bu proses-daxili `ContextVar`-dır. Eyni prosesdə ixtiyari Python kodu icra edə bilən tərəf `broker_context()` / `_ACTIVE`-i özü qaldıra və ya `GuardedLLM`-i keçib provayderi birbaşa
  çağıra bilər. Yəni bu yoxlama YANLIŞLIQLA/səhvən (unudulmuş, yan keçilmiş, yeni əlavə olunmuş) Broker-siz ödənişli çağırışa qarşı BÜTÖVLÜK mühafizəsidir, qəsdən hücumçuya qarşı yox.
- Qəsdən hücumçuya qarşı sərhəd yalnız PROSES sərhədidir: agent kodu bu prosesdə işləmir, AYRI prosesdə (`runtime/agent_gateway.py`) və yalnız minimal RPC ilə danışır; API açarı
  yalnız server mühitindədir. Server prosesinin özündə icra olunan etibarlı kod (və ya ona yüklənmiş plagin) bu mühafizəni aşa bilər.
- Kontekst iplərə/async tapşırıqlara görədir: Broker icraçısının başlatdığı başqa ip kontekst almır (bu fail-closed: orada ödənişli çağırış bloklanır).
- İstehsal giriş nöqtələrinin siyahısı `tests/test_paid_llm_entry_points.py`-də AST ilə yoxlanılır: yeni çağırış nöqtəsi əlavə olunsa test sinifləndirmə tələb edərək uğursuz olur.
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
