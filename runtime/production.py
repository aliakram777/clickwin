"""İstehsal yığınının YEGANƏ yığılma yeri (`tools/serve.py` bunu çağırır; testlər eyni funksiyanı sınayır).

TƏK `CostLedger`: Cloud LLM mühərriki, Effect Broker, büdcə siyasəti və sahib uzlaşdırması (`LedgerReconciler`) EYNİ kitab nüsxəsini (eyni `ledger.db`) istifadə edir. Ümumi xərc
tavanı (`global_limit`) kitabın özündədir və hər rezervi (agent, sistem söhbəti/plan, Broker) məcburən məhdudlaşdırır. Əvvəl Cloud mühərriki ayrı `llm_ledger.db` ilə qurulurdu:
bu, ümumi limiti və uzlaşdırmanı yan keçirdi.
Mühit dəyişənləri/şəbəkə yalnız `runtime/`-dadır; bu modul açar oxumur (açar `load_signing_config`/`build_engines` vasitəsilədir).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from api.bootstrap import Services, build_app
from runtime.env import RuntimeEngines, build_engines
from runtime.factory_bootstrap import TrustAnchor, load_signing_config
from safety.ledger import CostLedger, LedgerPort


def build_production(data_dir: str | Path, env: Mapping[str, str], *, owner_id: str = "owner", global_limit: float = 0.0, https: bool = False,
                     transport: Any = None, agent_isolation: dict | None = None) -> tuple[Services, RuntimeEngines, TrustAnchor | None]:
    """`BootstrapError` (qismən/zəif imza konfiqurasiyası) çağırana ötürülür. `global_limit=0.0` (defolt): heç bir ödənişli xərc yoxdur."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    config = load_signing_config(env)
    trust = TrustAnchor(config) if config is not None else None
    ledger = CostLedger(d / "ledger.db", verifier=trust.verifier if trust is not None else None, owners={owner_id}, global_limit=global_limit)
    engines = build_engines(env, log_dir=d, stop_path=d / "STOP", transport=transport, ledger=LedgerPort(ledger))     # Cloud mühərriki: EYNİ kitab, dar səth
    svc = build_app(d, owner_id=owner_id, global_limit=global_limit, https=https, cloud_engine=engines.cloud, local_engine=engines.local, trust=trust,
                    agent_isolation=agent_isolation, ledger=ledger)
    return svc, engines, trust
