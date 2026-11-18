"""Android API — NAYMERA CORE (Mərhələ 18.11): məqsəd/plan/addım interfeysləri. `ApiApp` DƏYİŞDİRİLMİR: `CoreApiApp` onun alt sinfidir və yalnız `/v1/core/*` marşrutları əlavə edir.

Qaydalar:
- Hər marşrut etibarlı cihaz sessiyası tələb edir (mövcud token/rate-limit qaydaları). Bu sinif istehsal yığınına (`build_app`, `tools/serve.py`) QOŞULMAYIB və heç bir şəbəkə soketi açmır: ictimai/uzaq
  ifşa YOXDUR (server qatı `api/server.py` dəyişməyib, defolt loopback/HTTPS siyasəti qüvvədədir).
- Məqsədin vəziyyət dəyişməsi (aktivləşdirmə, dayandırma, ləğv, tamamlama, davam) və naməlum/uğursuz addımın həlli YALNIZ cihaz step-up sübutu ilə (Keystore imzası; səs/söhbət heç vaxt).
  Sübut əməliyyata, hədəfə və CARİ VERSİYAYA bağlıdır (hash); bir-dəfəlikdir; köhnə versiya üçün sübut etibarsızdır.
- Yeni məqsəd yalnız DRAFT yaranır (aktivləşdirmə step-up tələb edir); yaratma idempotentdir (`client_op`). Mətn sirr bənzərdirsə rədd edilir.
- L1 addım təsdiqləri MÖVCUD `/v1/approvals` yolu ilə (`ApprovalService`) verilir; bu sinif təsdiq ETMİR. `/v1/core/steps/awaiting` yalnız hansı addımın təsdiq gözlədiyini göstərir.
- İcraçı çıxışı və jurnal mətnləri ETİBARSIZDIR (`untrusted: true`, qısaldılır). `safety.broker/gate/tools/workspace/ledger` import EDİLMİR; env/şəbəkə YOXDUR."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from agents.mobile import clean_text
from api.app import ApiApp, _int, _no_secret, _ok, _text
from api.http import ApiError, Ctx, Request, Response, Router
from core.ports import StepState
from core.store import NotPermitted, OpConflict, StaleVersion, StoreError
from kernel import ContractError, Goal, GoalStatus, InvalidTransition, Origin, Priority
from kernel.common import SecretDetected

_CLIENT_OP = re.compile(r"^[a-z0-9][a-z0-9_\-]{7,63}$")
_RESOLUTIONS = ("retry", "failed", "succeeded")
_RESOLVABLE = (StepState.UNKNOWN, StepState.BLOCKED, StepState.FAILED)
TRANSITION_TARGETS = tuple(s.value for s in GoalStatus if s is not GoalStatus.DRAFT)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreApiApp(ApiApp):
    def __init__(self, *, core_owner: Any, autonomy: str = "L1", **kwargs: Any) -> None:
        if autonomy not in ("L0", "L1"):
            raise ValueError("autonomy yalnız L0/L1 ola bilər")
        self._core, self._autonomy = core_owner, autonomy        # super().__init__ marşrutları qurmazdan ƏVVƏL
        super().__init__(**kwargs)

    # ------------------------------------------------------------------ marşrutlar
    def _routes(self, r: Router) -> None:
        super()._routes(r)
        add = r.add
        add("GET", "/v1/core/status", self.core_status)
        add("GET", "/v1/core/goals", self.core_goals)
        add("POST", "/v1/core/goals", self.core_goal_create)
        add("GET", "/v1/core/goals/{id}", self.core_goal_get)
        add("POST", "/v1/core/goals/{id}/challenge", self.core_goal_challenge)
        add("POST", "/v1/core/goals/{id}/transition", self.core_goal_transition)
        add("GET", "/v1/core/steps/awaiting", self.core_steps_awaiting)
        add("POST", "/v1/core/steps/{plan_id}/{step_id}/challenge", self.core_step_challenge)
        add("POST", "/v1/core/steps/{plan_id}/{step_id}/resolve", self.core_step_resolve)
        add("GET", "/v1/core/journal", self.core_journal)

    # ------------------------------------------------------------------ DTO
    @staticmethod
    def _goal_dto(g: Goal, version: int) -> dict[str, Any]:
        return {"id": g.goal_id, "title": clean_text(g.title, 200), "description": clean_text(g.description, 1000), "status": g.status.value, "priority": g.priority.value,
                "origin": g.origin.value, "version": version, "created_at": g.created_at.isoformat(), "updated_at": g.updated_at.isoformat(), "untrusted": g.origin.untrusted}

    def _plan_dto(self, plan: Any) -> dict[str, Any]:
        recs = {s.step_id: s for s in self._core.steps(plan.plan_id)}
        return {"id": plan.plan_id, "version": plan.version, "status": plan.status.value, "origin": plan.origin.value, "untrusted": plan.origin.untrusted,
                "steps": [{"id": s.step_id, "title": clean_text(s.title, 200), "capability_id": s.capability_id, "depends_on": list(s.depends_on), "state": recs[s.step_id].state.value,
                           "attempt": recs[s.step_id].attempt, "verified": recs[s.step_id].verified, "summary": clean_text(recs[s.step_id].summary, 200), "error": clean_text(recs[s.step_id].error, 120),
                           "untrusted_output": True} for s in plan.steps]}

    def _load_goal(self, goal_id: str) -> tuple[Goal, int]:
        got = self._core.get_goal(goal_id)
        if got is None:
            raise ApiError(404, "not_found", "Məqsəd tapılmadı.")
        return got

    @staticmethod
    def _store_error(exc: Exception) -> ApiError:
        if isinstance(exc, SecretDetected):
            return ApiError(422, "secret_detected", "Mətn sirr bənzər məzmun ehtiva edir və qəbul olunmadı.")
        if isinstance(exc, (StaleVersion, InvalidTransition, OpConflict, NotPermitted)):
            return ApiError(409, getattr(exc, "code", "conflict"), clean_text(str(exc), 200))
        if isinstance(exc, ContractError):
            return ApiError(400, getattr(exc, "code", "bad_request"), clean_text(str(exc), 200))
        return ApiError(409, "conflict", "Əməliyyat qəbul olunmadı.")

    # ------------------------------------------------------------------ oxu
    def core_status(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"autonomy": self._autonomy, "stop_active": self.stop_path.exists(), "steps": self._core.health_counts(), "executes_without_owner_approval": False})

    def core_goals(self, req: Request, p: dict, ctx: Ctx) -> Response:
        status = req.query.get("status")
        if status is not None and status not in {s.value for s in GoalStatus}:
            raise ApiError(400, "bad_request", "status yanlışdır.")
        items = self._core.list_goals(GoalStatus(status) if status else None, _int(req.query.get("limit"), "limit", 1, 100, 50))
        return _ok({"goals": [self._goal_dto(g, v) for g, v in items]})

    def core_goal_get(self, req: Request, p: dict, ctx: Ctx) -> Response:
        g, v = self._load_goal(p["id"])
        plan = self._core.current_plan(g.goal_id)
        return _ok({"goal": self._goal_dto(g, v), "plan": self._plan_dto(plan) if plan else None})

    def core_steps_awaiting(self, req: Request, p: dict, ctx: Ctx) -> Response:
        out = []
        for g, _ in self._core.list_goals(GoalStatus.ACTIVE, 100):
            plan = self._core.current_plan(g.goal_id)
            if plan is None:
                continue
            titles = {s.step_id: s for s in plan.steps}
            for rec in self._core.steps(plan.plan_id):
                if rec.state is StepState.AWAITING_APPROVAL:
                    out.append({"goal_id": g.goal_id, "goal_title": clean_text(g.title, 200), "plan_id": plan.plan_id, "step_id": rec.step_id, "step_title": clean_text(titles[rec.step_id].title, 200),
                                "capability_id": titles[rec.step_id].capability_id, "attempt": rec.attempt + 1, "approval_description": f"core:{plan.plan_id}:v{plan.version}:{rec.step_id}"[:200]})
        return _ok({"steps": out, "note": "Təsdiq /v1/approvals vasitəsilə (step-up) verilir."})

    def core_journal(self, req: Request, p: dict, ctx: Ctx) -> Response:
        q = req.query
        rows = self._core.journal_entries(q.get("goal_id") or None, _int(q.get("limit"), "limit", 1, 200, 50), _int(q.get("after_seq"), "after_seq", 0, 10 ** 9, 0))
        return _ok({"entries": [{"seq": r["seq"], "ts": r["ts"], "goal_id": r["goal_id"], "plan_id": r["plan_id"], "step_id": r["step_id"], "kind": clean_text(r["kind"], 60),
                                 "detail": {k: clean_text(str(v), 120) for k, v in list(r["detail"].items())[:8]}, "untrusted": True} for r in rows]})

    # ------------------------------------------------------------------ yaratma (DRAFT, idempotent)
    def core_goal_create(self, req: Request, p: dict, ctx: Ctx) -> Response:
        b = req.json
        title = _text(b, "title", 200)
        desc = _text(b, "description", 2000) if b.get("description") else ""
        client_op = b.get("client_op")
        if not isinstance(client_op, str) or not _CLIENT_OP.match(client_op):
            raise ApiError(400, "bad_request", "client_op (8-64 kiçik hərf/rəqəm/_-) tələb olunur.")
        prio = b.get("priority", "normal")
        if prio not in {x.value for x in Priority}:
            raise ApiError(400, "bad_request", "priority yanlışdır.")
        _no_secret(title + " " + desc)
        goal_id = "goal_" + _h("api:" + client_op)[:16]            # client_op ⇒ deterministik id: təkrar sorğu eyni məqsədi qaytarır
        existing = self._core.get_goal(goal_id)
        if existing is not None:
            g0, v0 = existing
            if (g0.title, g0.description, g0.priority.value, g0.origin) == (clean_text(title, 200).strip(), clean_text(desc, 2000).strip(), prio, Origin.OWNER):
                return _ok({"goal": self._goal_dto(g0, v0), "duplicate": True}, 200)
            raise ApiError(409, "op_conflict", "client_op artıq FƏRQLİ məzmunla istifadə olunub.")
        try:
            goal = Goal.new(title, origin=Origin.OWNER, description=desc, priority=Priority(prio), goal_id=goal_id)
            self._core.create_goal(goal, f"api.goal.create:{client_op}")
        except Exception as exc:                                 # noqa: BLE001
            raise self._store_error(exc) from None
        self._audit("api_core_goal_created", device_id=ctx.session.device_id, goal_id=goal_id)
        return _ok({"goal": self._goal_dto(*self._core.get_goal(goal_id))}, 201)

    # ------------------------------------------------------------------ vəziyyət dəyişməsi (step-up)
    def _target(self, body: dict) -> str:
        to = body.get("to_status")
        if to not in TRANSITION_TARGETS:
            raise ApiError(400, "bad_request", f"to_status {list(TRANSITION_TARGETS)} olmalıdır.")
        return to

    def core_goal_challenge(self, req: Request, p: dict, ctx: Ctx) -> Response:
        to = self._target(req.json)
        g, v = self._load_goal(p["id"])
        ph = _h(f"{to}:{v}")
        ch = self.auth.create_challenge(ctx.session.device_id, "core_goal", g.goal_id, ph, to)
        # Müştəri YALNIZ göstərdiyi əməliyyatı imzalamalıdır: action/subject/payload_hash/decision qaytarılır (müştəri `to_status`+`version`-dan hash-i yenidən hesablayıb müqayisə edə bilər)
        return _ok({"challenge_id": ch.id, "nonce": ch.nonce, "expires_in": ch.expires_in, "action": "core_goal", "subject": g.goal_id, "payload_hash": ph, "decision": to,
                    "goal": self._goal_dto(g, v), "to_status": to})

    def core_goal_transition(self, req: Request, p: dict, ctx: Ctx) -> Response:
        to = self._target(req.json)
        g, v = self._load_goal(p["id"])
        method = self._step_up(ctx, req.json, "core_goal", g.goal_id, _h(f"{to}:{v}"), to)
        try:
            self._core.transition_goal(g.goal_id, GoalStatus(to), f"api.goal.t:{req.json['challenge_id'][:40]}", v, reason=f"android:{method}")
        except Exception as exc:                                 # noqa: BLE001
            raise self._store_error(exc) from None
        self._audit("api_core_goal_transition", device_id=ctx.session.device_id, goal_id=g.goal_id, to_status=to, method=method)
        return _ok({"goal": self._goal_dto(*self._load_goal(g.goal_id))})

    def _resolvable(self, plan_id: str, step_id: str) -> Any:
        got = self._core.get_plan(plan_id)
        rec = next((s for s in self._core.steps(plan_id) if s.step_id == step_id), None) if got else None
        if rec is None:
            raise ApiError(404, "not_found", "Addım tapılmadı.")
        if rec.state not in _RESOLVABLE:
            raise ApiError(409, "not_resolvable", "Addım həll oluna bilən vəziyyətdə deyil.")
        return rec

    @staticmethod
    def _resolution(body: dict) -> str:
        r = body.get("resolution")
        if r not in _RESOLUTIONS:
            raise ApiError(400, "bad_request", f"resolution {list(_RESOLUTIONS)} olmalıdır.")
        return r

    def core_step_challenge(self, req: Request, p: dict, ctx: Ctx) -> Response:
        res = self._resolution(req.json)
        rec = self._resolvable(p["plan_id"], p["step_id"])
        subject, ph = f"{p['plan_id']}/{p['step_id']}"[:128], _h(f"{res}:{rec.attempt}:{rec.state.value}")
        ch = self.auth.create_challenge(ctx.session.device_id, "core_step", subject, ph, res)
        return _ok({"challenge_id": ch.id, "nonce": ch.nonce, "expires_in": ch.expires_in, "action": "core_step", "subject": subject, "payload_hash": ph, "decision": res,
                    "step": {"plan_id": p["plan_id"], "step_id": p["step_id"], "state": rec.state.value, "attempt": rec.attempt}})

    def core_step_resolve(self, req: Request, p: dict, ctx: Ctx) -> Response:
        res = self._resolution(req.json)
        rec = self._resolvable(p["plan_id"], p["step_id"])
        method = self._step_up(ctx, req.json, "core_step", f"{p['plan_id']}/{p['step_id']}"[:128], _h(f"{res}:{rec.attempt}:{rec.state.value}"), res)
        try:
            ok = self._core.resolve_step(p["plan_id"], p["step_id"], res, "stepup-" + req.json["challenge_id"][:60], note=f"android:{method}")
        except Exception as exc:                                 # noqa: BLE001
            raise self._store_error(exc) from None
        if not ok:
            raise ApiError(409, "not_resolvable", "Addım həll oluna bilmədi.")
        self._audit("api_core_step_resolved", device_id=ctx.session.device_id, plan_id=p["plan_id"], step_id=p["step_id"], resolution=res, method=method)
        return _ok({"ok": True, "resolution": res})

