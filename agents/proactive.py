"""Proaktiv zəka: hadisə, tapşırıq və layihə vəziyyətini təhlil edib istifadəçi soruşmadan TƏKLİF hazırlayır.

Qaydalar:
- Təkliflər YALNIZ təklifdir: `ActionProposal.executable` həmişə False; bu modul heç nəyi icra etmir, təsdiqləmir,
  yaddaşa yazmır, xərc rezerv etmir (yalnız oxuyur). İcra qərarı CEO/Broker/Gate/təsdiq yolundadır.
- Mənbə məlumatı (yaddaş mətni, tapşırıq başlıqları, araşdırma) etibarsızdır: yalnız təmizlənmiş qısa çıxarışlar
  (`safe_excerpt`) şablon mətnlərə daxil edilir.
- Lazımsız bildiriş yoxdur: əhəmiyyət səviyyəsi, cooldown/təkrar, eskalasiya, saatlıq/gündəlik limit, sakit saatlar,
  gündəlik xülasə (digest), ack/snooze/dismiss və müddət bitməsi (`NotificationPolicy`). Mock mənbəli nəticələr bildiriş
  yaratmır (yalnız açıq `include_mock` ilə, SİMULYASİYA işarəsi ilə).
- Çatdırma Android-first `NotificationChannel` interfeysi ilədir (`agents.mobile`); real push bu mərhələdə yoxdur.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from agents.mobile import IMPORTANCES, Notification, NotificationAction, NotificationChannel, clean_text
from agents.notify_store import NotificationStore  # noqa: F401  (yenidən ixrac)
from agents.research import ResearchReport

SENSITIVE = frozenset({"spend_money", "external_account", "publish"})
_BASE = {"low": 20, "medium": 50, "high": 75, "critical": 95}
_RANK = {name: i for i, name in enumerate(IMPORTANCES)}


def safe_excerpt(text: Any, cap: int = 80) -> str:
    """Etibarsız mətndən qısa, təmiz çıxarış (etiket/idarəedici simvolsuz)."""
    return clean_text(str(text), cap)


@dataclass(frozen=True)
class ActionProposal:
    action_id: str
    label_az: str
    requires_approval: bool = False
    executable: bool = False

    def __post_init__(self) -> None:
        if self.executable:
            raise ValueError("təklif edilən əməliyyat avtomatik icra oluna bilməz")
        NotificationAction(self.action_id, self.label_az)  # doğrulama


@dataclass(frozen=True)
class Suggestion:
    key: str
    kind: str
    title_az: str
    body_az: str
    importance: str
    priority: int
    evidence: tuple[str, ...]
    actions: tuple[ActionProposal, ...]
    created_at: float
    expires_at: float
    is_mock: bool = False

    def __post_init__(self) -> None:
        if self.importance not in IMPORTANCES:
            raise ValueError("importance yanlışdır")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not 0 <= self.priority <= 100:
            raise ValueError("priority 0..100 olmalıdır")
        if not self.key or len(self.key) > 128:
            raise ValueError("key yanlışdır")


@dataclass(frozen=True)
class Thresholds:
    approval_wait_s: float = 1800
    approval_expiry_s: float = 600
    budget_warn: float = 0.8
    budget_critical: float = 0.95
    due_soon_s: float = 48 * 3600
    stale_workflow_s: float = 3600
    suggestion_ttl_s: float = 86400

    def __post_init__(self) -> None:
        for n in ("approval_wait_s", "approval_expiry_s", "due_soon_s", "stale_workflow_s", "suggestion_ttl_s"):
            if getattr(self, n) <= 0:
                raise ValueError(f"{n} müsbət olmalıdır")
        if not 0 < self.budget_warn < self.budget_critical <= 1:
            raise ValueError("büdcə hədləri 0 < warn < critical ≤ 1 olmalıdır")


@dataclass(frozen=True)
class NotificationPolicy:
    min_importance: str = "medium"
    cooldown_s: float = 6 * 3600
    max_per_hour: int = 3
    max_per_day: int = 10
    critical_per_hour: int = 6
    quiet_start_min: int = 22 * 60
    quiet_end_min: int = 8 * 60
    utc_offset_min: int = 240  # Bakı: UTC+4
    digest_interval_s: float = 86400

    def __post_init__(self) -> None:
        if self.min_importance not in _RANK:
            raise ValueError("min_importance yanlışdır")
        if self.cooldown_s < 0 or self.digest_interval_s <= 0:
            raise ValueError("cooldown/digest müddəti yanlışdır")
        for n in ("max_per_hour", "max_per_day", "critical_per_hour"):
            v = getattr(self, n)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"{n} müsbət tam ədəd olmalıdır")
        if not -840 <= self.utc_offset_min <= 840:
            raise ValueError("utc_offset_min yanlışdır")
        for n in ("quiet_start_min", "quiet_end_min"):
            if not 0 <= getattr(self, n) <= 1439:
                raise ValueError(f"{n} 0..1439 olmalıdır")


def _priority(importance: str, urgency: int = 0) -> int:
    return min(100, _BASE[importance] + max(0, min(4, urgency)))


def _parse_due(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


class ProactiveAnalyzer:
    """YALNIZ OXUYUR: ledger, təsdiq oxuyucusu, workflow siyahısı, yaddaş oxuyucusu, stop bayrağı."""

    def __init__(self, *, clock: Callable[[], float] = time.time, ledger: Any = None, budget: Any = None,
                 approvals: Any = None, workflows: Any = None, memory: Any = None,
                 stop_path: str | Path | None = None, thresholds: Thresholds | None = None) -> None:
        self.clock, self.ledger, self.budget = clock, ledger, budget
        self.approvals, self.workflows, self.memory = approvals, workflows, memory
        self.stop_path = Path(stop_path) if stop_path is not None else None
        self.th = thresholds or Thresholds()

    def analyze(self) -> list[Suggestion]:
        out: list[Suggestion] = []
        for source in (self._stop, self._approvals, self._reconcile, self._budget, self._workflows, self._tasks):
            try:
                out.extend(source())
            except Exception:  # noqa: BLE001 — mənbə əlçatmazdırsa susuruq (səs-küy yaratmırıq)
                continue
        out.sort(key=lambda s: (-s.priority, s.key))
        return out

    # --- siqnallar ---
    def _mk(self, key: str, kind: str, title: str, body: str, importance: str, evidence: Sequence[str],
            actions: Sequence[ActionProposal], urgency: int = 0, ttl: float | None = None,
            expires_at: float | None = None, is_mock: bool = False) -> Suggestion:
        now = float(self.clock())
        return Suggestion(key, kind, clean_text(title, 120), clean_text(body, 600), importance,
                          _priority(importance, urgency), tuple(evidence), tuple(actions), now,
                          expires_at if expires_at is not None else now + (ttl or self.th.suggestion_ttl_s), is_mock)

    def _stop(self) -> list[Suggestion]:
        if self.stop_path is None or not self.stop_path.exists():
            return []
        return [self._mk("stop_active", "stop_active", "Stop bayrağı aktivdir",
                         "Bütün agent əməliyyatları dayandırılıb. Davam etmək üçün bayrağı yoxlayın.", "high", (),
                         (ActionProposal("open_status", "Vəziyyətə bax"),))]

    def _approvals(self) -> list[Suggestion]:
        if self.approvals is None:
            return []
        now, th, out = float(self.clock()), self.th, []
        for t in self.approvals.pending():
            age, remaining = now - t.created_at, t.expires_at - now
            if age < th.approval_wait_s or remaining <= 0:
                continue
            soon = remaining <= th.approval_expiry_s
            sensitive = t.permission in SENSITIVE
            importance = ("critical" if soon else "high") if sensitive else ("high" if soon else "medium")
            body = (f"{safe_excerpt(t.agent, 40)} agentinin {safe_excerpt(t.permission, 30)} əməliyyatı "
                    f"{int(age // 60)} dəqiqədir sahibin təsdiqini gözləyir (xərc {t.cost}). "
                    f"Təsdiqin vaxtı {max(1, int(remaining // 60))} dəqiqəyə bitir.")
            out.append(self._mk(f"approval:{t.id}", "approval_waiting", "Təsdiq gözlənilir", body, importance,
                                (t.id,), (ActionProposal("open_approval", "Təsdiq sorğusuna bax"),),
                                urgency=int(age // 3600), expires_at=t.expires_at))
        return out

    def _reconcile(self) -> list[Suggestion]:
        if self.ledger is None:
            return []
        return [self._mk(f"reconcile:{e.op_id}", "reconcile_needed", "Xərc uzlaşdırılmalıdır",
                         f"{safe_excerpt(e.agent, 40)} agenti üçün {e.amount:g} məbləğində xərc rezervi naməlumdur "
                         f"({safe_excerpt(e.op_id, 60)}). Əl ilə uzlaşdırma tələb olunur; rezerv avtomatik silinmir.",
                         "high", (e.op_id,), (ActionProposal("open_reconcile", "Uzlaşdırmaya bax"),))
                for e in self.ledger.unknown_entries()]

    def _budget(self) -> list[Suggestion]:
        if self.ledger is None or self.budget is None:
            return []
        th, out = self.th, []

        def band(used: float, limit: float) -> tuple[str, str] | None:
            if not limit or limit <= 0:
                return None
            r = used / limit
            if r >= th.budget_critical:
                return "crit", "critical"
            if r >= th.budget_warn:
                return "warn", "medium"
            return None

        scopes = []
        if getattr(self.budget, "global_limit", None) is not None:
            scopes.append(("global", "Ümumi büdcə", self.ledger.total(), self.budget.global_limit))
        for name, limit in dict(getattr(self.budget, "agent_limits", {}) or {}).items():
            scopes.append((name, f"{safe_excerpt(name, 40)} büdcəsi", self.ledger.agent_total(name), limit))
        for scope, label, used, limit in scopes:
            b = band(used, limit)
            if b is None:
                continue
            out.append(self._mk(f"budget:{scope}:{b[0]}", "budget_warning", f"{label} limitə yaxınlaşır",
                                f"{label}: {used:g} / {limit:g} istifadə olunub ({int(used / limit * 100)}%).", b[1],
                                (scope,), (ActionProposal("open_budget", "Büdcəyə bax"),)))
        return out

    def _workflows(self) -> list[Suggestion]:
        if self.workflows is None:
            return []
        now, th, out = float(self.clock()), self.th, []
        for j in self.workflows.list_jobs():
            if j.status == "running" and 0 < j.lease_expires < now:
                out.append(self._mk(f"workflow:{j.job_id}:stalled", "workflow_attention", "İş dayanıb",
                                    f"{safe_excerpt(j.job_id, 40)} işi icra olunur görünür, lakin proses cavab vermir "
                                    "(lease bitib). Təhlükəsiz davam etdirməni yoxlayın.", "high", (j.job_id,),
                                    (ActionProposal("open_job", "İşə bax"),)))
            elif j.status in ("interrupted", "partial", "failed") and now - j.updated_at >= th.stale_workflow_s:
                imp = "high" if j.status == "failed" else "medium"
                out.append(self._mk(f"workflow:{j.job_id}:{j.status}", "workflow_attention",
                                    "İş diqqət tələb edir",
                                    f"{safe_excerpt(j.job_id, 40)} işi '{j.status}' vəziyyətində qalıb. "
                                    "Davam etdirmək üçün baxın.", imp, (j.job_id,),
                                    (ActionProposal("open_job", "İşə bax"),)))
        return out

    def _tasks(self) -> list[Suggestion]:
        if self.memory is None:
            return []
        now, th, out = float(self.clock()), self.th, []
        for rec in self.memory.search(category="task", limit=100):
            meta = rec.meta if isinstance(rec.meta, dict) else {}
            due = _parse_due(meta.get("due"))
            if due is None or meta.get("done"):
                continue
            label = safe_excerpt(rec.content, 80)  # yaddaş mətni etibarsızdır: yalnız təmiz çıxarış
            if due < now:
                out.append(self._mk(f"task:{rec.id}:overdue", "task_due", "Tapşırığın vaxtı keçib",
                                    f"Gecikmiş tapşırıq: {label}", "high", (rec.id,),
                                    (ActionProposal("open_task", "Tapşırığa bax"),)))
            elif due - now <= th.due_soon_s:
                out.append(self._mk(f"task:{rec.id}:soon", "task_due", "Tapşırığın vaxtı yaxınlaşır",
                                    f"{int((due - now) // 3600)} saat qalıb: {label}", "medium", (rec.id,),
                                    (ActionProposal("open_task", "Tapşırığa bax"),)))
        return out

    def analyze_research(self, report: ResearchReport, include_mock: bool = False) -> list[Suggestion]:
        if report.is_mock and not include_mock:
            return []  # mock nəticə bildiriş yaratmır
        qh = hashlib.sha256(report.query.encode("utf-8")).hexdigest()[:12]
        mock_note = " SİMULYASİYA (mock): bu real internet nəticəsi deyil." if report.is_mock else ""
        conflicts = [c for c in report.claims if c.status == "conflicting"]
        if conflicts:
            return [self._mk(f"research:{qh}:conflict", "research_conflict", "Araşdırmada ziddiyyət tapıldı",
                             f"'{safe_excerpt(report.query, 60)}' üzrə mənbələr uyğunsuz məlumat verir "
                             f"({len(conflicts)} ziddiyyət). Qərar vermədən əvvəl mənbələri yoxlayın.{mock_note}",
                             "medium", tuple(s.domain for s in report.sources[:5]),
                             (ActionProposal("open_research", "Araşdırmaya bax"),), ttl=7 * 86400,
                             is_mock=report.is_mock)]
        if report.sources and all("stale" in s.flags for s in report.sources):
            return [self._mk(f"research:{qh}:stale", "research_stale", "Araşdırma mənbələri köhnədir",
                             f"'{safe_excerpt(report.query, 60)}' üzrə bütün mənbələr köhnədir; yenisini axtarın."
                             f"{mock_note}", "low", tuple(s.domain for s in report.sources[:5]),
                             (ActionProposal("open_research", "Araşdırmaya bax"),), ttl=7 * 86400,
                             is_mock=report.is_mock)]
        return []


def to_notification(s: Suggestion, *, now: float | None = None) -> Notification:
    """Təklifi Android-first, Azərbaycan dilində bildirişə çevirir (səs mətni ilə)."""
    created = s.created_at
    return Notification(
        id="n_" + hashlib.sha256(f"{s.key}|{created}".encode("utf-8")).hexdigest()[:20],
        title=clean_text(s.title_az, 120), body=clean_text(s.body_az, 600), importance=s.importance,
        priority=s.priority, category=s.kind, created_at=created, expires_at=max(s.expires_at, created + 1),
        dedupe_key=s.key,
        actions=tuple(NotificationAction(a.action_id, a.label_az, a.requires_approval) for a in s.actions[:4]),
        speech_text=f"{s.title_az}. {s.body_az}", is_mock=s.is_mock)


@dataclass(frozen=True)
class NotificationDecision:
    suggestion: Suggestion
    action: str  # notify | digest | defer | suppress
    reason: str


class ProactiveEngine:
    def __init__(self, analyzer: Any, store: NotificationStore, policy: NotificationPolicy | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.analyzer, self.store = analyzer, store
        self.policy = policy or NotificationPolicy()
        self.clock = clock

    # --- istifadəçi cavabları ---
    def acknowledge(self, key: str) -> None:
        self.store.mark(key, "acked")

    def snooze(self, key: str, seconds: float) -> None:
        self.store.mark(key, "snoozed", float(self.clock()) + float(seconds))

    def dismiss(self, key: str) -> None:
        self.store.mark(key, "dismissed")

    # --- qərar ---
    def _quiet(self, now: float) -> bool:
        p = self.policy
        local = int(((now + p.utc_offset_min * 60) % 86400) // 60)
        if p.quiet_start_min > p.quiet_end_min:
            return local >= p.quiet_start_min or local < p.quiet_end_min
        return p.quiet_start_min <= local < p.quiet_end_min

    def _decide(self, s: Suggestion, now: float, extra: dict[str, int]) -> tuple[str, str]:
        p, st = self.policy, self.store.state(s.key)
        rank = _RANK[s.importance]
        escalated = st is not None and st.last_sent > 0 and rank > _RANK[st.last_importance]
        if now >= s.expires_at:
            return "suppress", "müddəti bitib"
        if st is not None:
            if st.status == "dismissed":
                return "suppress", "istifadəçi rədd edib"
            if st.status == "snoozed" and now < st.until:
                return "suppress", "təxirə salınıb"
            if st.status == "acked" and not escalated:
                return "suppress", "istifadəçi təsdiqləyib (eskalasiya yoxdur)"
        recently = st is not None and st.last_sent > 0 and now - st.last_sent < p.cooldown_s
        if rank < _RANK[p.min_importance]:
            return ("suppress", "cooldown: gündəlik xülasədə artıq göstərilib") if recently else \
                ("digest", "aşağı əhəmiyyət: gündəlik xülasəyə")
        if recently and not escalated:
            return "suppress", "cooldown: təkrar bildiriş göndərilmir"
        if s.importance != "critical" and self._quiet(now):
            return "defer", "sakit saatlar"
        if s.importance == "critical":
            if self.store.sent_count(now - 3600, True) + extra["crit"] >= p.critical_per_hour:
                return "defer", "kritik bildiriş saatlıq limiti"
        else:
            if self.store.sent_count(now - 3600, False) + extra["hour"] >= p.max_per_hour:
                return "defer", "saatlıq bildiriş limiti"
            if self.store.sent_count(now - 86400, False) + extra["day"] >= p.max_per_day:
                return "digest", "gündəlik bildiriş limiti: xülasəyə"
        return "notify", "vacib və yeni"

    def _ordered(self) -> list[Suggestion]:
        return sorted(self.analyzer.analyze(), key=lambda s: (-s.priority, s.key))

    def plan(self) -> list[NotificationDecision]:
        """Quru qaçış: vəziyyəti dəyişmir (yalnız bu çağırış daxilində limitləri nəzərə alır)."""
        now, extra, out = float(self.clock()), {"crit": 0, "hour": 0, "day": 0}, []
        for s in self._ordered():
            action, reason = self._decide(s, now, extra)
            if action == "notify":
                extra["crit" if s.importance == "critical" else "hour"] += 1
                extra["day"] += 0 if s.importance == "critical" else 1
            out.append(NotificationDecision(s, action, reason))
        return out

    def run(self, channel: NotificationChannel) -> list[NotificationDecision]:
        now, extra, out = float(self.clock()), {"crit": 0, "hour": 0, "day": 0}, []
        for s in self._ordered():
            action, reason = self._decide(s, now, extra)
            if action == "digest":
                self.store.queue_digest(s.key, s.importance, s.title_az, now)
            elif action == "notify":
                try:
                    res = channel.deliver(to_notification(s, now=now))
                except ValueError:
                    action, reason = "suppress", "bildiriş qaydalara uyğun deyil"
                else:
                    if res.ok:
                        self.store.record_sent(s.key, s.importance, now)
                    else:
                        action, reason = "defer", f"çatdırıla bilmədi: {res.detail}"
            out.append(NotificationDecision(s, action, reason))
        return out

    def digest(self, channel: NotificationChannel) -> Notification | None:
        """Aşağı əhəmiyyətli məlumatların gündəlik xülasəsi (interval ərzində ən çox bir dəfə)."""
        now = float(self.clock())
        last = self.store.last_digest()
        if last > 0 and now - last < self.policy.digest_interval_s:
            return None
        items = self.store.digest_items(50)
        if not items:
            return None
        shown = "; ".join(clean_text(t, 60) for _, _, t in items[:5])
        more = f" və daha {len(items) - 5}" if len(items) > 5 else ""
        n = Notification(id=f"digest_{int(now)}", title="Gündəlik xülasə", body=(shown + more)[:590],
                         importance="low", priority=20, category="digest", created_at=now,
                         expires_at=now + 86400, dedupe_key=f"digest:{int(now // 86400)}",
                         actions=(NotificationAction("open_digest", "Hamısına bax"),),
                         speech_text=f"Gündəlik xülasə. {len(items)} məlumat.")
        if not channel.deliver(n).ok:
            return None
        for key, imp, _ in items:
            self.store.record_sent(key, imp, now, counted=False)
        self.store.clear_digest()
        self.store.set_last_digest(now)
        return n
