"""Sistem səviyyəli (agent olmayan) LLM çağırışları — söhbət, planlaşdırma — da Effect Broker üzərindən (audit A-8).

`BrokeredRouter` `HybridRouter` ilə eyni `route(...)` səthini verir, lakin hər çağırış `EffectBroker.run` daxilində icra olunur: stop bayrağı -> Gate (icazə/audit) ->
Ledger rezervi -> icra. Real (mock olmayan) provayder `GuardedLLM` səviyyəsində ayrıca YALNIZ Broker kontekstində çağırılır, buna görə bu sarğı olmadan ödənişli Cloud
söhbət/plan üçün işləmir (bloklanır). Broker effektinin xərci 0-dır: faktiki xərc `GuardedLLM`-in Ledger rezervində (agent əhatəsi yoxdursa `llm` hesabı) izlənir.
`read_files` icazəsi burada "yan təsirsiz oxu/məsləhət" mənasındadır (yeni icazə növü əlavə edilməyib).
"""
from __future__ import annotations

import hashlib
from typing import Any, Sequence

from agents.hybrid import RouteResult
from agents.spec import AgentSpec
from safety.broker import Effect, EffectBroker

SYSTEM_LLM_SPEC = AgentSpec(name="naymera_llm", purpose="Sistem söhbət və plan sorğuları (Broker nəzarəti ilə)", tools=[], permissions=["read_files"],
                            inputs=["sorgu"], outputs=["netice"], cost_limit=5.0)


class BrokeredRouter:
    def __init__(self, router: Any, broker: EffectBroker, spec: AgentSpec = SYSTEM_LLM_SPEC) -> None:
        self._router, self._broker, self._spec = router, broker, spec

    def route(self, request_id: str, kind: str, prompt: str, system: str = "", context: Sequence[Any] = (), allow_mock: bool = True,
              allow_paid: bool = True) -> RouteResult:
        sha = hashlib.sha256(f"{kind}\n{prompt}".encode("utf-8")).hexdigest() if isinstance(prompt, str) else ""
        effect = Effect(self._spec, "read_files", 0.0, "", f"llm:{str(kind)[:20]}", payload={"request_id": str(request_id)[:80], "kind": str(kind)[:20], "prompt_sha256": sha})
        res = self._broker.run(effect, lambda: self._router.route(request_id, kind, prompt, system, context, allow_mock, allow_paid))
        if res.status == "ok" and isinstance(res.output, RouteResult):
            return res.output
        return RouteResult("blocked", str(request_id)[:80], reason=f"broker:{res.status}: {res.reason}"[:200])

    def __getattr__(self, name: str) -> Any:           # status/store/health kimi oxu səthləri birbaşa
        return getattr(self._router, name)
