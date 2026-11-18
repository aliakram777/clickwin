"""Mərhələ 18.5: TƏHLÜKƏSİZ handler kitabxanası (Kernel Loop üçün). YALNIZ dar, deterministik, yan təsiri məhdud handler-lər: model, şəbəkə, shell, ödəniş, nəşr, kod icrası YOXDUR.

Hər handler `StepHandlerRegistry.register` ilə qeydə alınır və yenə Effect Broker + L1 təsdiqindən keçir (agent spec-i icazəni verməlidir). PlanStepSpec arqument daşımadığı üçün handler-lər
məqsəd/plan kontekstindən işləyir:
- `memory.recall` (read_files): məqsəd başlığına görə yaddaşda axtarır; nəticədə yalnız say/etibar paylanması və id-lər (etibarsız mətn əks olunmur).
- `memory.consolidate` (write_files): məqsədin epizodlarını deterministik xülasəyə çevirir (LONG_TERM, mənşə system).
- `report.progress` (write_files): planın addım vəziyyətini PROJECT yaddaşına hesabat kimi yazır (mənşə system).
- `workspace.list` (read_files): workspace kökündəki girişlərin sayı və təhlükəsiz adlar (workspace sərhədi `safety.workspace`).
Reyestr YALNIZ çağıran etibarlı server kodu tərəfindən doldurulur; istehsal yığınında avtomatik qeydiyyat yoxdur."""
from __future__ import annotations

import re
from typing import Any

from core.store import EngineGoalStore, OwnerGoalStore
from memory import MemActor, MemoryKind, MemoryStore
from runtime.brokered_core import HandlerContext, HandlerFailed, StepHandlerRegistry
from safety.workspace import Workspace

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,60}$")


def register_safe_handlers(registry: StepHandlerRegistry, *, goals: EngineGoalStore | OwnerGoalStore, memory: MemoryStore | None = None, workspace: Workspace | None = None,
                           project: str = "core") -> list[str]:
    """Verilən resurslara uyğun handler-ləri qeydə alır; qeydə alınan capability_id-ləri qaytarır. Resurs yoxdursa uyğun handler qeydə alınmır (fail-closed)."""
    done: list[str] = []

    if memory is not None:
        def recall(ctx: HandlerContext) -> str:
            got = goals.get_goal(ctx.request.goal_id)
            if got is None:
                raise HandlerFailed("məqsəd tapılmadı")
            hits = memory.search(got[0].title, mode="any", limit=5)
            by_trust = {t: sum(1 for h in hits if h.record.trust == t) for t in ("high", "medium", "low")}
            return f"{len(hits)} qeyd (high={by_trust['high']}, medium={by_trust['medium']}, low={by_trust['low']}): " + ", ".join(h.record.memory_id for h in hits)

        def consolidate(ctx: HandlerContext) -> str:
            res = memory.consolidate_episodes(MemActor.SYSTEM, project=project, subject_ref=ctx.request.goal_id, op_id=("consolidate." + ctx.request.op_id)[:90])
            return f"xülasə yazıldı: {res.record.memory_id}" if res else "epizod yoxdur"

        def progress(ctx: HandlerContext) -> str:
            plan = goals.current_plan(ctx.request.goal_id)
            if plan is None:
                raise HandlerFailed("aktiv plan yoxdur")
            text = f"Plan {plan.plan_id} v{plan.version} ({plan.status.value}): " + ", ".join(f"{s.step_id}={s.state.value}" for s in goals.steps(plan.plan_id))
            res = memory.write(MemActor.SYSTEM, MemoryKind.PROJECT, text, title=f"Tərəqqi: {plan.plan_id}"[:200], project=project, tags=("progress",), source="report.progress",
                               refs=(ctx.request.goal_id, plan.plan_id), op_id=("progress." + ctx.request.op_id)[:90])
            return f"hesabat yazıldı: {res.record.memory_id}"
        for cap, fn, perm in (("memory.recall", recall, "read_files"), ("memory.consolidate", consolidate, "write_files"), ("report.progress", progress, "write_files")):
            registry.register(cap, fn, permission=perm)
            done.append(cap)

    if workspace is not None:
        def ls(ctx: HandlerContext) -> str:
            try:
                names = workspace.list_dir(".")
            except Exception:                                        # noqa: BLE001
                raise HandlerFailed("workspace oxunmadı") from None
            safe = [n for n in names if _SAFE_NAME.match(n)][:20]
            return f"{len(names)} giriş; təhlükəsiz adlar: {', '.join(safe)}"
        registry.register("workspace.list", ls, permission="read_files")
        done.append("workspace.list")
    return done
