"""Android API tətbiqi: marşrutlar və təhlükəsizlik qaydaları.

Qaydalar: (1) `health/pair/token/refresh` istisna olmaqla hər marşrut etibarlı sessiya tələb edir; (2) təsdiq və
dayandırmanın davamı yalnız cihaz step-up sübutu ilə (səs heç vaxt); (3) şirkət yalnız oxunur; (4) söhbət/səs mətni
etibarsızdır və heç nə icra/təsdiq etmir; (5) icra yalnız `JobService` -> CEO/Broker yolu ilə; (6) cavablarda sirr yoxdur.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agents.company import DEPARTMENTS
from agents.memory import Actor, MemoryDenied, detect_secrets
from agents.mobile import EventStore, VoiceProfile, clean_text
from api.auth import AuthError, AuthService, Session
from api.chat import MAX_TEXT, ChatService
from api.events import EventNotificationChannel
from api.http import ApiError, App, Ctx, Request, Response, Router
from api.jobs import JobConflict, JobNotFound, JobService
from safety.approvals import Principal

API_VERSION = 1
OWNER_MEMORY_CATEGORIES = ("project", "task", "decision", "note")
STEP_UP_METHODS = ("biometric", "device_credential")
_LANG = re.compile(r"^[a-z]{2}-[A-Z]{2}$")


def _int(value: Any, name: str, lo: int, hi: int, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ApiError(400, "bad_request", f"{name} tələb olunur.")
        return default
    try:
        n = int(value) if isinstance(value, str) else value
    except ValueError:
        raise ApiError(400, "bad_request", f"{name} tam ədəd olmalıdır.") from None
    if isinstance(n, bool) or not isinstance(n, int) or not lo <= n <= hi:
        raise ApiError(400, "bad_request", f"{name} {lo}..{hi} aralığında olmalıdır.")
    return n


def _text(body: dict, name: str, cap: int) -> str:
    v = body.get(name)
    if not isinstance(v, str):
        raise ApiError(400, "bad_request", f"{name} mətn olmalıdır.")
    if any(ch != "\n" and ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in v) or "​" in v:
        raise ApiError(400, "bad_request", f"{name} idarəedici simvol ehtiva edir.")
    if not v.strip():
        raise ApiError(400, "bad_request", f"{name} boş ola bilməz.")
    if len(v) > cap:
        raise ApiError(400, "bad_request", f"{name} çox uzundur (maks. {cap}).")
    return v


def _no_secret(text: str) -> None:
    if detect_secrets(text):
        raise ApiError(422, "secret_detected", "Mətn sirr bənzər məzmun ehtiva edir və qəbul olunmadı.")


def _ok(body: Any, status: int = 200) -> Response:
    return Response(status, body)


class ApiApp(App):
    def __init__(self, *, auth: AuthService, chat: ChatService, events: EventStore, stop_path: str | Path,
                 gateway: Any, reader: Any, memory: Any, company: Any, proactive: Any, jobs: JobService,
                 voice: Any, owner_id: str, audit_path: str | Path, llm_is_mock: bool = True,
                 clock: Callable[[], float] = time.time, device_rate: int = 120, https: bool = False) -> None:
        self.auth, self.chat, self.events = auth, chat, events
        self.stop_path, self.gateway, self.reader = Path(stop_path), gateway, reader
        self.memory, self.company, self.proactive, self.jobs, self.voice = memory, company, proactive, jobs, voice
        self.owner_id, self.audit_path, self.llm_is_mock = owner_id, Path(audit_path), llm_is_mock
        self.clock, self.device_rate = clock, device_rate
        router = Router()
        self._routes(router)
        super().__init__(router, https=https, authenticate=self._authenticate, on_error=self._internal_error)

    # --- ümumi ---
    def _audit(self, event: str, **fields: Any) -> None:
        row = {"ts": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), "event": event, **fields}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _internal_error(self, rid: str, kind: str) -> None:
        self._audit("api_internal_error", request_id=rid, error=kind)

    def _authenticate(self, token: str, req: Request) -> Session:
        session = self.auth.authenticate(token)
        self.auth.hit(f"dev:{session.device_id}", self.device_rate, 60)
        return session

    def _routes(self, r: Router) -> None:
        add = r.add
        add("GET", "/v1/health", self.health, "none")
        add("POST", "/v1/pair", self.pair, "none")
        add("POST", "/v1/token/refresh", self.refresh, "none")
        add("GET", "/v1/me", self.me)
        add("POST", "/v1/logout", self.logout)
        add("POST", "/v1/chat/messages", self.chat_send)
        add("GET", "/v1/chat/conversations", self.chat_list)
        add("GET", "/v1/chat/conversations/{id}/messages", self.chat_messages)
        add("GET", "/v1/events", self.events_get)
        add("POST", "/v1/events/ack", self.events_ack)
        add("GET", "/v1/notifications/preview", self.notif_preview)
        add("POST", "/v1/notifications/run", self.notif_run)
        add("POST", "/v1/notifications/{key}/ack", self.notif_ack)
        add("POST", "/v1/notifications/{key}/snooze", self.notif_snooze)
        add("POST", "/v1/notifications/{key}/dismiss", self.notif_dismiss)
        add("POST", "/v1/memory", self.memory_add)
        add("GET", "/v1/memory", self.memory_search)
        add("GET", "/v1/company/agents", self.company_agents)
        add("GET", "/v1/company/departments", self.company_departments)
        add("GET", "/v1/jobs", self.jobs_list)
        add("POST", "/v1/jobs", self.jobs_propose)
        add("GET", "/v1/jobs/{id}", self.jobs_get)
        add("POST", "/v1/jobs/{id}/run", self.jobs_run)
        add("GET", "/v1/approvals", self.approvals_list)
        add("POST", "/v1/approvals/{id}/challenge", self.approval_challenge)
        add("POST", "/v1/approvals/{id}/decision", self.approval_decision)
        add("GET", "/v1/system/status", self.system_status)
        add("POST", "/v1/system/stop", self.system_stop)
        add("POST", "/v1/system/resume/challenge", self.resume_challenge)
        add("POST", "/v1/system/resume", self.resume)
        add("GET", "/v1/voice/profile", self.voice_profile)
        add("POST", "/v1/voice/transcripts", self.voice_transcript)
        add("POST", "/v1/voice/synthesize", self.voice_synthesize)

    # --- giriş və sessiya ---
    def health(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"status": "ok", "api_version": API_VERSION})

    def pair(self, req: Request, p: dict, ctx: Ctx) -> Response:
        self.auth.hit(f"pair:{ctx.remote}", 20, 60)
        res = self.auth.pair(req.json.get("code"), req.json.get("device_name"), ctx.remote)
        return _ok({"device_id": res.device_id, "access_token": res.access_token, "refresh_token": res.refresh_token,
                    "device_secret": res.device_secret, "access_expires_in": res.access_expires_in})

    def refresh(self, req: Request, p: dict, ctx: Ctx) -> Response:
        t = self.auth.refresh(req.json.get("refresh_token"), ctx.remote)
        return _ok({"access_token": t.access_token, "refresh_token": t.refresh_token,
                    "access_expires_in": t.access_expires_in})

    def me(self, req: Request, p: dict, ctx: Ctx) -> Response:
        s = ctx.session
        return _ok({"device_id": s.device_id, "device_name": s.device_name, "owner_id": s.owner_id,
                    "api_version": API_VERSION})

    def logout(self, req: Request, p: dict, ctx: Ctx) -> Response:
        self.auth.logout(ctx.session.device_id)
        return _ok({"ok": True})

    # --- söhbət ---
    def _converse(self, text: str, cid: Any, mode: str, confidence: float | None) -> dict[str, Any]:
        if cid is not None and not isinstance(cid, str):
            raise ApiError(400, "bad_request", "conversation_id mətn olmalıdır.")
        try:
            return self.chat.send(text, cid, mode, confidence)
        except KeyError:
            raise ApiError(404, "not_found", "Söhbət tapılmadı.") from None

    def chat_send(self, req: Request, p: dict, ctx: Ctx) -> Response:
        text = _text(req.json, "text", MAX_TEXT)
        _no_secret(text)
        return _ok(self._converse(text, req.json.get("conversation_id"), "text", None))

    def chat_list(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"conversations": self.chat.store.conversations(_int(req.query.get("limit"), "limit", 1, 100, 50))})

    def chat_messages(self, req: Request, p: dict, ctx: Ctx) -> Response:
        if not self.chat.store.exists(p["id"]):
            raise ApiError(404, "not_found", "Söhbət tapılmadı.")
        after = _int(req.query.get("after"), "after", 0, 2**62, 0)
        limit = _int(req.query.get("limit"), "limit", 1, 200, 100)
        return _ok({"messages": self.chat.store.messages(p["id"], after, limit)})

    # --- hadisələr və bildirişlər ---
    def events_get(self, req: Request, p: dict, ctx: Ctx) -> Response:
        cursor = _int(req.query.get("cursor"), "cursor", 0, 2**62, 0)
        limit = _int(req.query.get("limit"), "limit", 1, 500, 100)
        evs = self.events.since(cursor, limit)
        return _ok({"events": [{"seq": e.seq, "type": e.type, "id": e.id, "ts": e.ts, "payload": e.payload,
                                "schema_version": e.schema_version} for e in evs],
                    "next_cursor": evs[-1].seq if evs else cursor,
                    "last_ack": self.events.last_ack(ctx.session.device_id)})

    def events_ack(self, req: Request, p: dict, ctx: Ctx) -> Response:
        seq = req.json.get("seq")
        try:
            self.events.ack(ctx.session.device_id, seq)
        except ValueError:
            raise ApiError(400, "bad_request", "seq yanlışdır.") from None
        return _ok({"ok": True, "last_ack": self.events.last_ack(ctx.session.device_id)})

    @staticmethod
    def _decision_dto(d: Any) -> dict[str, Any]:
        s = d.suggestion
        return {"key": s.key, "action": d.action, "reason": d.reason, "title": s.title_az,
                "importance": s.importance, "is_mock": s.is_mock}

    def notif_preview(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"decisions": [self._decision_dto(d) for d in self.proactive.plan()]})

    def notif_run(self, req: Request, p: dict, ctx: Ctx) -> Response:
        ds = self.proactive.run(EventNotificationChannel(self.events))
        return _ok({"delivered": sum(1 for d in ds if d.action == "notify"),
                    "decisions": [self._decision_dto(d) for d in ds]})

    def notif_ack(self, req: Request, p: dict, ctx: Ctx) -> Response:
        self.proactive.acknowledge(p["key"])
        return _ok({"ok": True})

    def notif_snooze(self, req: Request, p: dict, ctx: Ctx) -> Response:
        secs = _int(req.json.get("seconds"), "seconds", 1, 7 * 86400)
        self.proactive.snooze(p["key"], secs)
        return _ok({"ok": True})

    def notif_dismiss(self, req: Request, p: dict, ctx: Ctx) -> Response:
        self.proactive.dismiss(p["key"])
        return _ok({"ok": True})

    # --- yaddaş ---
    @staticmethod
    def _memory_dto(r: Any) -> dict[str, Any]:
        return {"id": r.id, "category": r.category, "project": r.project, "source": r.source,
                "provenance": r.provenance, "content": r.content, "status": r.status, "created_at": r.created_at,
                "date": r.date, "revision": r.revision, "untrusted": True}

    def memory_add(self, req: Request, p: dict, ctx: Ctx) -> Response:
        b = req.json
        cat = b.get("category")
        if cat not in OWNER_MEMORY_CATEGORIES:
            raise ApiError(400, "bad_request", f"category {list(OWNER_MEMORY_CATEGORIES)} olmalıdır.")
        content = _text(b, "content", 4000)
        _no_secret(content)
        project = b.get("project", "default")
        if not isinstance(project, str):
            raise ApiError(400, "bad_request", "project mətn olmalıdır.")
        try:
            res = self.memory.write(Actor(ctx.session.owner_id, "owner"), cat, content, project=project)
        except MemoryDenied as exc:
            raise ApiError(400, "memory_denied", clean_text(str(exc), 200)) from None
        return _ok({"created": res.created, "record": self._memory_dto(res.record)}, 201 if res.created else 200)

    def memory_search(self, req: Request, p: dict, ctx: Ctx) -> Response:
        q = req.query
        limit = _int(q.get("limit"), "limit", 1, 100, 20)
        try:
            recs = self.memory.search(q.get("query", ""), category=q.get("category"), project=q.get("project"),
                                      limit=limit)
        except MemoryDenied as exc:
            raise ApiError(400, "memory_denied", clean_text(str(exc), 200)) from None
        return _ok({"records": [self._memory_dto(r) for r in recs]})

    # --- şirkət: yalnız oxu ---
    def company_agents(self, req: Request, p: dict, ctx: Ctx) -> Response:
        out = []
        for a in self.company.agents():
            au = a.authority
            out.append({"name": a.name, "department": a.department, "role": a.role, "status": a.status,
                        "budget": a.budget,
                        "authority": {"permissions": sorted(au.permissions), "sensitive_ok": au.sensitive_ok,
                                      "delegate_to": sorted(au.delegate_to), "max_task_cost": au.max_task_cost}})
        return _ok({"agents": out})

    def company_departments(self, req: Request, p: dict, ctx: Ctx) -> Response:
        costs = self.company.department_costs()
        agents = self.company.agents()
        return _ok({"departments": [
            {"id": d, "name": label, "active_agents": sum(1 for a in agents if a.department == d),
             "step_cost": costs.get(d)} for d, label in DEPARTMENTS.items()]})

    # --- CEO işləri ---
    def _goal(self, body: dict) -> str:
        goal = _text(body, "goal", 1000)
        _no_secret(goal)
        return goal

    def jobs_list(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"jobs": self.jobs.list(_int(req.query.get("limit"), "limit", 1, 100, 50))})

    def jobs_propose(self, req: Request, p: dict, ctx: Ctx) -> Response:
        job = self.jobs.propose(self._goal(req.json))
        self._audit("api_job_proposed", device_id=ctx.session.device_id, job_id=job["id"], ok=job["ok"])
        return _ok({"job": job}, 201)

    def jobs_get(self, req: Request, p: dict, ctx: Ctx) -> Response:
        try:
            return _ok({"job": self.jobs.get(p["id"])})
        except JobNotFound:
            raise ApiError(404, "not_found", "İş tapılmadı.") from None

    def jobs_run(self, req: Request, p: dict, ctx: Ctx) -> Response:
        try:
            self._audit("api_job_run", device_id=ctx.session.device_id, job_id=p["id"])
            return _ok({"job": self.jobs.run(p["id"])})
        except JobNotFound:
            raise ApiError(404, "not_found", "İş tapılmadı.") from None
        except JobConflict:
            raise ApiError(409, "job_conflict", "İş icra oluna bilmir (etibarsız təklif və ya məşğul).") from None

    # --- təsdiq (step-up) ---
    @staticmethod
    def _approval_dto(t: Any) -> dict[str, Any]:
        return {"id": t.id, "agent": t.agent, "permission": t.permission, "cost": float(t.cost),
                "description": clean_text(t.description, 200), "payload_hash": t.payload_hash,
                "created_at": t.created_at, "expires_at": t.expires_at}

    def approvals_list(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"approvals": [self._approval_dto(t) for t in self.gateway.pending()]})

    def _pending_ticket(self, approval_id: str) -> Any:
        t = self.gateway.get(approval_id)
        if t is None or t.status == "expired":
            raise ApiError(404, "not_found", "Təsdiq tapılmadı.")
        if t.status != "pending":
            raise ApiError(409, "not_pending", "Təsdiq artıq həll olunub.")
        return t

    @staticmethod
    def _decision(body: dict) -> str:
        d = body.get("decision")
        if d not in ("approve", "deny"):
            raise ApiError(400, "bad_request", "decision approve|deny olmalıdır.")
        return d

    def approval_challenge(self, req: Request, p: dict, ctx: Ctx) -> Response:
        decision = self._decision(req.json)
        t = self._pending_ticket(p["id"])
        ch = self.auth.create_challenge(ctx.session.device_id, "approval", t.id, t.payload_hash, decision)
        return _ok({"challenge_id": ch.id, "nonce": ch.nonce, "expires_in": ch.expires_in,
                    "approval": self._approval_dto(t)})

    def _step_up(self, ctx: Ctx, body: dict, action: str, subject: str, payload_hash: str, decision: str) -> str:
        cid, proof, method = body.get("challenge_id"), body.get("proof"), body.get("method")
        if not isinstance(cid, str) or not isinstance(proof, str) or not isinstance(method, str):
            raise ApiError(400, "bad_request", "challenge_id, proof və method mətn olmalıdır.")
        if method == "voice":
            self._audit("api_stepup_voice_rejected", device_id=ctx.session.device_id, action=action, subject=subject)
            raise ApiError(403, "voice_not_allowed", "Səslə təsdiq mümkün deyil.")
        if method not in STEP_UP_METHODS:
            raise ApiError(400, "bad_request", "method yanlışdır.")
        self.auth.verify_proof(ctx.session.device_id, cid, action, subject, payload_hash, decision, proof)
        return method

    def approval_decision(self, req: Request, p: dict, ctx: Ctx) -> Response:
        decision = self._decision(req.json)
        t = self._pending_ticket(p["id"])
        method = self._step_up(ctx, req.json, "approval", t.id, t.payload_hash, decision)
        # Sahib yalnız step-up sübutundan SONRA yaradılır; səs/söhbət bu yola çatmır.
        principal = Principal(ctx.session.owner_id, "human", "android")
        note = f"android:{ctx.session.device_id}:{method}"
        out = (self.gateway.approve if decision == "approve" else self.gateway.deny)(t.id, principal, note)
        self._audit("api_approval_decision", device_id=ctx.session.device_id, approval_id=t.id, decision=decision,
                    method=method, ok=out.ok)
        if not out.ok:
            raise ApiError(409, "decision_rejected", clean_text(out.reason, 200))
        return _ok({"ok": True, "status": "approved" if decision == "approve" else "denied"})

    # --- sistem ---
    def system_status(self, req: Request, p: dict, ctx: Ctx) -> Response:
        return _ok({"stop_active": self.stop_path.exists(), "pending_approvals": len(self.reader.pending()),
                    "api_version": API_VERSION, "llm_is_mock": bool(self.llm_is_mock)})

    def system_stop(self, req: Request, p: dict, ctx: Ctx) -> Response:
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text("stop\n", encoding="utf-8")
        self._audit("api_stop", device_id=ctx.session.device_id)
        return _ok({"ok": True, "stop_active": True})

    def resume_challenge(self, req: Request, p: dict, ctx: Ctx) -> Response:
        ch = self.auth.create_challenge(ctx.session.device_id, "resume", "stop", "", "approve")
        return _ok({"challenge_id": ch.id, "nonce": ch.nonce, "expires_in": ch.expires_in})

    def resume(self, req: Request, p: dict, ctx: Ctx) -> Response:
        method = self._step_up(ctx, req.json, "resume", "stop", "", "approve")
        self.stop_path.unlink(missing_ok=True)
        self._audit("api_resume", device_id=ctx.session.device_id, method=method)
        return _ok({"ok": True, "stop_active": False})

    # --- səs ---
    def voice_profile(self, req: Request, p: dict, ctx: Ctx) -> Response:
        v = self.voice
        return _ok({"profile": VoiceProfile().to_dict(),
                    "tts": {"available": v is not None, "is_mock": True if v is None else bool(getattr(v, "is_mock", True)),
                            "provider": "" if v is None else str(getattr(v, "name", "unknown"))},
                    "stt": {"available": False, "is_mock": True, "provider": ""},
                    "voice_may_approve": False})

    def voice_transcript(self, req: Request, p: dict, ctx: Ctx) -> Response:
        b = req.json
        text = _text(b, "text", MAX_TEXT)
        conf = b.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            raise ApiError(400, "bad_request", "confidence 0..1 olmalıdır.")
        lang = b.get("language")
        if not isinstance(lang, str) or not _LANG.match(lang):
            raise ApiError(400, "bad_request", "language 'az-AZ' formatında olmalıdır.")
        _no_secret(text)
        # Transkript etibarsız söhbət girişidir: təsdiq/icra yolu yoxdur.
        return _ok(self._converse(text, b.get("conversation_id"), "voice", float(conf)))

    def voice_synthesize(self, req: Request, p: dict, ctx: Ctx) -> Response:
        text = _text(req.json, "text", 1000)
        if self.voice is None:
            raise ApiError(503, "tts_unavailable", "Səs sintezi mövcud deyil.")
        res = self.voice.synthesize(text, VoiceProfile())
        return _ok({"text": res.text, "profile": res.profile.to_dict(), "audio_ref": res.audio_ref,
                    "is_mock": bool(res.is_mock)})
