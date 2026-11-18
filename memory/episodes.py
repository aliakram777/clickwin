"""Epizod yazıcısı (Mərhələ 18.3): Kernel Loop-un `observer` portu üçün. Hər tərəqqi tick-i EPISODIC yaddaşa yazılır (aktor ENGINE: yalnız epizodik, mənşə system).
Yalnız əməliyyat metadatası (action, id-lər, qısa səbəb) saxlanır; icraçı çıxışı (ETİBARSIZ mətn) bura YAZILMIR. Xəta döngüyə təsir etmir."""
from __future__ import annotations

from memory.store import MemActor, MemoryKind, MemoryStore


class EpisodeRecorder:
    def __init__(self, store: MemoryStore, project: str = "core") -> None:
        self._store, self._project = store, project

    def __call__(self, result) -> None:                      # core.TickResult
        refs = tuple(x for x in (result.goal_id, result.plan_id, result.step_id) if x)
        text = f"{result.action} goal={result.goal_id} plan={result.plan_id} step={result.step_id} {result.detail}".strip()
        self._store.write(MemActor.ENGINE, MemoryKind.EPISODIC, text + f" @{self._store.now().isoformat()}", title=result.action, project=self._project,
                          tags=(result.action[:32],), source="kernel_loop", refs=refs)
