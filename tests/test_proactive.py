"""Proaktiv analiz və bildiriş siyasəti. Təkliflər yalnız TƏKLİFDİR: heç biri avtomatik icra olunmur. Heç bir real
push və ya xarici çağırış yoxdur."""
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.durable import WorkflowStore
from agents.memory import Actor, MemoryService
from agents.mobile import InMemoryChannel
from agents.proactive import (
    ActionProposal, NotificationPolicy, NotificationStore, ProactiveAnalyzer, ProactiveEngine, Suggestion,
    Thresholds, safe_excerpt, to_notification,
)
from agents.research import FetchedPage, build_report
from agents.workflow import PlanStep
from safety.approvals import ApprovalService
from safety.ledger import BudgetPolicy, CostLedger

ROOT = Path(__file__).resolve().parent.parent
OWNER = Actor("owner", "owner")


def utc(h, m=0, d=17):
    return datetime(2026, 11, d, h, m, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def world(tmp_path):
    clock = Clock(utc(8))  # 12:00 Bakı vaxtı (UTC+4)
    ledger = CostLedger(tmp_path / "ledger.db")
    svc = ApprovalService(tmp_path / "approvals.db", tmp_path / "audit.jsonl", owners={"owner"}, ttl_seconds=3600,
                          clock=clock, allow_mock=True)
    wf = WorkflowStore(tmp_path / "wf.db", clock=clock, lease_s=60)
    mem = MemoryService(tmp_path / "mem.db", tmp_path / "mem_audit.jsonl", clock=clock)
    pol = BudgetPolicy(global_limit=10.0, agent_limits={"agent_a": 5.0})

    def analyzer(**kw):
        opts = dict(clock=clock, ledger=ledger, budget=pol, approvals=svc.reader, workflows=wf,
                    memory=mem.reader, stop_path=tmp_path / "STOP", thresholds=Thresholds())
        opts.update(kw)
        return ProactiveAnalyzer(**opts)

    return analyzer, clock, ledger, svc, wf, mem, tmp_path


def kinds(sugs):
    return sorted(s.kind for s in sugs)


# --- boş vəziyyət: bildiriş yoxdur ---

def test_quiet_world_produces_no_suggestions(world):
    analyzer, *_ = world
    assert analyzer().analyze() == []


# --- siqnallar və əhəmiyyət ---

def request(svc, op="op1", perm="spend_money", cost=0.5):
    return svc.client.request("agent_a", perm, cost, op, "ödəniş", {"x": 1})


def test_waiting_approvals_become_suggestions_only_after_the_wait_threshold(world):
    analyzer, clock, _, svc, *_ = world
    t = request(svc)
    clock.advance(600)
    assert analyzer().analyze() == []                      # hələ tez
    clock.advance(1300)                                    # 1900s > 1800s
    [s] = analyzer().analyze()
    assert (s.kind, s.importance, s.key) == ("approval_waiting", "high", f"approval:{t.id}")
    assert s.evidence == (t.id,) and "Təsdiq" in s.title_az and "spend_money" in s.body_az
    assert s.is_mock is False and s.expires_at > s.created_at


def test_approval_about_to_expire_is_critical_and_nonsensitive_is_lower(world):
    analyzer, clock, _, svc, *_ = world
    request(svc, "op1")
    request(svc, "op2", perm="read_files")
    clock.advance(3100)  # 500s qalıb < 600s
    by_perm = {("spend_money" in s.body_az): s for s in analyzer().analyze()}
    assert by_perm[True].importance == "critical" and by_perm[False].importance in ("medium", "high")


def test_unknown_cost_reservations_need_manual_reconciliation(world):
    analyzer, _, ledger, *_ = world
    ledger.reserve("j1:b:a0", "agent_a", 0.5)
    ledger.mark_unknown("j1:b:a0")
    [s] = analyzer().analyze()
    assert (s.kind, s.importance, s.key) == ("reconcile_needed", "high", "reconcile:j1:b:a0")
    assert "j1:b:a0" in s.evidence and any(a.action_id == "open_reconcile" for a in s.actions)


@pytest.mark.parametrize("spent,expected", [(7.9, []), (8.2, [("budget_warning", "medium")]),
                                             (9.7, [("budget_warning", "critical")])])
def test_global_budget_thresholds(world, spent, expected):
    analyzer, _, ledger, *_ = world
    ledger.record("o1", "other_agent", spent)
    assert [(s.kind, s.importance) for s in analyzer().analyze()] == expected


def test_budget_bands_have_distinct_keys_so_escalation_can_renotify(world):
    analyzer, _, ledger, *_ = world
    ledger.record("o1", "other_agent", 8.2)
    k1 = analyzer().analyze()[0].key
    ledger.record("o2", "other_agent", 1.5)
    k2 = analyzer().analyze()[0].key
    assert k1 != k2


def test_agent_budget_threshold(world):
    analyzer, _, ledger, *_ = world
    ledger.record("o1", "agent_a", 4.3)  # 86% / 5.0
    assert [(s.kind, s.key) for s in analyzer().analyze()] == [("budget_warning", "budget:agent_a:warn")]


def test_stalled_and_failed_workflows_are_reported_only_after_a_grace_period(world):
    analyzer, clock, _, _, wf, *_ = world
    steps = [PlanStep("a", "dept_a", "read_files", 0.5)]
    wf.create("j1", "g", steps)
    wf.finish("j1", "interrupted")
    assert analyzer().analyze() == []
    clock.advance(4000)  # > stale_workflow_s
    [s] = analyzer().analyze()
    assert (s.kind, s.importance, s.key) == ("workflow_attention", "medium", "workflow:j1:interrupted")
    wf.create("j2", "g", steps)
    wf.finish("j2", "failed")
    clock.advance(4000)
    assert {x.key: x.importance for x in analyzer().analyze()}["workflow:j2:failed"] == "high"
    wf.create("j3", "g", steps)
    wf.acquire("j3")
    clock.advance(200)  # lease bitdi: proses çökə bilər
    assert "workflow:j3:stalled" in {x.key for x in analyzer().analyze()}


def test_finished_workflows_are_not_reported(world):
    analyzer, clock, _, _, wf, *_ = world
    wf.create("j1", "g", [PlanStep("a", "dept_a", "read_files", 0.5)])
    wf.finish("j1", "ok")
    clock.advance(10000)
    assert analyzer().analyze() == []


def test_due_tasks_use_memory_as_untrusted_data(world):
    analyzer, clock, _, _, _, mem, _ = world
    mem.write(OWNER, "task", "Hesabatı bitir <script>alert(1)</script>\x1b[31m", meta={"due": "2026-11-17T20:00:00Z"})
    mem.write(OWNER, "task", "Gecikmiş iş", meta={"due": "2026-11-16T10:00:00Z"})
    mem.write(OWNER, "task", "Uzaq iş", meta={"due": "2026-12-30"})
    mem.write(OWNER, "task", "Bitmiş iş", meta={"due": "2026-11-16", "done": True})
    mem.write(OWNER, "task", "Vaxtsız iş")
    sugs = analyzer().analyze()
    assert sorted((s.importance, s.kind) for s in sugs) == [("high", "task_due"), ("medium", "task_due")]
    assert all("<" not in s.title_az + s.body_az and "\x1b" not in s.title_az + s.body_az for s in sugs)
    assert all("script" not in (s.title_az + s.body_az).lower() or "alert" not in (s.title_az + s.body_az)
               for s in sugs)  # etibarsız HTML/skript mətni təmizlənir


def test_stop_flag_is_surfaced(world):
    analyzer, _, _, _, _, _, tmp = world
    (tmp / "STOP").write_text("stop")
    [s] = analyzer().analyze()
    assert (s.kind, s.importance) == ("stop_active", "high")


LIVE = [FetchedPage("https://stat.gov.az/a", "A", "Azərbaycan iqtisadi artım 2023 ilində 1.1 faiz olub.", "2024-03-01", "live", 0),
        FetchedPage("https://blog.other.org/b", "B", "Azərbaycan iqtisadi artım 2023 ilində 3.5 faiz olub.", "2024-02-01", "live", 0)]


def test_research_conflicts_are_suggested_but_mock_results_are_not(world):
    analyzer, *_ = world
    now = utc(8)
    live = build_report("Azərbaycan iqtisadi artım 2023", LIVE, now=now)
    mock = build_report("Azərbaycan iqtisadi artım 2023", [FetchedPage(p.url, p.title, p.text, p.published, "mock", 0)
                                                           for p in LIVE], now=now)
    [s] = analyzer().analyze_research(live)
    assert (s.kind, s.importance, s.is_mock) == ("research_conflict", "medium", False)
    assert analyzer().analyze_research(mock) == []                          # mock nəticə bildiriş yaratmır
    [m] = analyzer().analyze_research(mock, include_mock=True)
    assert m.is_mock is True and "SİMULYASİYA" in m.body_az


def test_priority_follows_importance_and_actions_are_never_executable(world):
    analyzer, clock, ledger, svc, *_ = world
    ledger.record("o1", "other_agent", 9.7)
    request(svc)
    clock.advance(3100)
    sugs = analyzer().analyze()
    ranks = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    by_imp = sorted(sugs, key=lambda s: -s.priority)
    assert [ranks[s.importance] for s in by_imp] == sorted([ranks[s.importance] for s in by_imp], reverse=True)
    assert all(0 <= s.priority <= 100 for s in sugs)
    assert all(a.executable is False for s in sugs for a in s.actions)
    with pytest.raises(ValueError):
        ActionProposal("run_now", "İndi icra et", requires_approval=True, executable=True)


def test_safe_excerpt_neutralizes_untrusted_text():
    out = safe_excerpt("Salam <b>dünya</b>\x00\n\x1b[31m " + "x" * 300, cap=40)
    assert "<" not in out and "\x00" not in out and "\n" not in out and len(out) <= 40


def test_proactive_module_never_executes_or_approves():
    tree = ast.parse((ROOT / "agents" / "proactive.py").read_text(encoding="utf-8"))
    forbidden_imports = {"agents.orchestrator", "agents.ceo", "safety.broker", "safety.tools", "safety.gate",
                         "agents.llm", "agents.company", "safety.approvals"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden_imports, node.module
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"approve", "deny", "consume", "execute", "run", "commit", "release",
                                     "reserve", "write", "delete", "update"}, (node.attr, node.lineno)


# --- oxu-yalnız üzlər ---

def test_approval_reader_and_workflow_listing_are_read_only(world):
    _, clock, _, svc, wf, *_ = world
    request(svc)
    assert not any(hasattr(svc.reader, n) for n in ("approve", "deny", "consume", "request", "check"))
    assert len(svc.reader.pending()) == 1 and svc.reader.get(svc.reader.pending()[0].id) is not None
    wf.create("j1", "g", [PlanStep("a", "dept_a", "read_files", 0.5)])
    [info] = wf.list_jobs()
    assert (info.job_id, info.status) == ("j1", "created")
    assert [j.job_id for j in wf.list_jobs(statuses={"ok"})] == []


# --- səs-küy nəzarəti ---

def sug(key="k1", importance="medium", now=None, expires_in=86400, kind="test"):
    now = utc(8) if now is None else now
    return Suggestion(key, kind, f"Başlıq {key}", f"Mətn {key}", importance, {"low": 20, "medium": 50, "high": 75,
                      "critical": 95}[importance], ("e1",), (), now, now + expires_in)


class Fake:
    def __init__(self, items=()):
        self.items = list(items)

    def analyze(self):
        return list(self.items)


@pytest.fixture
def engine_env(tmp_path):
    clock = Clock(utc(8))
    channel = InMemoryChannel()

    def build(items=(), **policy):
        fake = Fake(items)
        store = NotificationStore(tmp_path / "notif.db", clock=clock)
        eng = ProactiveEngine(fake, store, NotificationPolicy(**policy), clock=clock)
        return eng, fake, store

    return build, clock, channel, tmp_path


def actions(decisions):
    return {d.suggestion.key: d.action for d in decisions}


def test_important_suggestion_is_notified_once_and_delivered_as_android_notification(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug("a", "high")])
    d = eng.run(ch)
    assert actions(d) == {"a": "notify"} and len(ch.delivered) == 1
    n = ch.delivered[0]
    assert (n.platform, n.locale, n.importance, n.dedupe_key) == ("android", "az", "high", "a")
    assert n.speech_text and "http" not in n.speech_text
    assert actions(eng.run(ch)) == {"a": "suppress"} and len(ch.delivered) == 1   # təkrar yoxdur


def test_cooldown_dedupes_and_expires(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug("a")], cooldown_s=3600)
    assert actions(eng.run(ch))["a"] == "notify"
    clock.advance(1800)
    d = eng.run(ch)
    assert actions(d)["a"] == "suppress" and "cooldown" in d[0].reason
    clock.advance(2000)
    assert actions(eng.run(ch))["a"] == "notify" and len(ch.delivered) == 2


def test_escalation_bypasses_cooldown(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug("a", "medium")], cooldown_s=3600)
    eng.run(ch)
    fake.items = [sug("a", "critical")]
    clock.advance(60)
    d = eng.run(ch)
    assert actions(d)["a"] == "notify" and ch.delivered[-1].importance == "critical"


def test_low_importance_goes_to_a_daily_digest_not_to_push(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug(f"l{i}", "low") for i in range(7)], digest_interval_s=86400)
    assert set(actions(eng.run(ch)).values()) == {"digest"} and ch.delivered == []
    digest = eng.digest(ch)
    assert digest is not None and digest.importance == "low" and len(ch.delivered) == 1
    assert digest.body.count("l") >= 1 and len(digest.actions) <= 4
    assert eng.digest(ch) is None                      # interval ərzində ikinci digest yoxdur
    eng.run(ch)
    clock.advance(86400 + 10)
    fake.items = [sug("l9", "low", now=clock.t)]
    eng.run(ch)
    assert eng.digest(ch) is not None


def test_rate_limits_defer_the_excess_and_critical_has_its_own_allowance(engine_env):
    build, clock, ch, _ = engine_env
    items = [sug(f"m{i}", "medium") for i in range(4)] + [sug(f"c{i}", "critical") for i in range(2)]
    eng, fake, _ = build(items, max_per_hour=2, max_per_day=10, critical_per_hour=3)
    d = actions(eng.run(ch))
    assert sorted(v for k, v in d.items() if k.startswith("m")) == ["defer", "defer", "notify", "notify"]
    assert all(v == "notify" for k, v in d.items() if k.startswith("c"))
    clock.advance(3700)
    again = actions(eng.run(ch))
    assert sorted(v for k, v in again.items() if k.startswith("m")).count("notify") == 2


def test_daily_cap(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug(f"m{i}", "medium") for i in range(5)], max_per_hour=10, max_per_day=3)
    d = actions(eng.run(ch))
    assert list(d.values()).count("notify") == 3 and list(d.values()).count("digest") == 2


def test_quiet_hours_hold_noncritical_until_morning(engine_env):
    build, clock, ch, _ = engine_env
    clock.t = utc(19, 30)  # 23:30 Bakı
    eng, fake, _ = build([sug("m", "medium"), sug("c", "critical")], utc_offset_min=240)
    d = actions(eng.run(ch))
    assert d == {"m": "defer", "c": "notify"}
    clock.t = utc(4, 30, d=18)  # 08:30 Bakı
    fake.items = [sug("m", "medium", now=clock.t)]
    assert actions(eng.run(ch)) == {"m": "notify"}


def test_acknowledge_snooze_and_dismiss_silence_a_suggestion(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug("a"), sug("b"), sug("c")], cooldown_s=60)
    eng.run(ch)
    eng.acknowledge("a")
    eng.snooze("b", 7200)
    eng.dismiss("c")
    clock.advance(500)
    d = actions(eng.run(ch))
    assert d == {"a": "suppress", "b": "suppress", "c": "suppress"}
    clock.advance(7200)
    d = actions(eng.run(ch))
    assert d["b"] == "notify" and d["a"] == "suppress" and d["c"] == "suppress"
    fake.items = [sug("a", "critical")]
    assert actions(eng.run(ch)) == {"a": "notify"}  # eskalasiya təsdiqlənmiş bildirişi də qaytarır


def test_expired_suggestions_are_never_sent(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug("a", "high", expires_in=100)])
    clock.advance(500)
    assert actions(eng.run(ch)) == {"a": "suppress"} and ch.delivered == []


def test_failed_delivery_is_retried_and_not_counted_as_sent(engine_env):
    build, clock, ch, _ = engine_env
    eng, fake, _ = build([sug("a", "high")], max_per_hour=1)
    ch.fail = True
    d = eng.run(ch)
    assert actions(d) == {"a": "defer"} and "çatdırıla bilmədi" in d[0].reason
    ch.fail = False
    assert actions(eng.run(ch)) == {"a": "notify"}


def test_plan_is_a_dry_run_and_state_survives_restart(engine_env):
    build, clock, ch, tmp = engine_env
    eng, fake, store = build([sug("a", "high")], cooldown_s=3600)
    assert actions(eng.plan()) == {"a": "notify"} and actions(eng.plan()) == {"a": "notify"}  # dəyişiklik yoxdur
    eng.run(ch)
    eng2, _, _ = build([sug("a", "high")], cooldown_s=3600)  # yeni proses, eyni fayl
    assert actions(eng2.run(ch)) == {"a": "suppress"}


def test_policy_validation():
    for bad in (dict(min_importance="yox"), dict(cooldown_s=-1), dict(max_per_hour=0), dict(utc_offset_min=2000),
                dict(quiet_start_min=2000)):
        with pytest.raises(ValueError):
            NotificationPolicy(**bad)


def test_to_notification_is_azerbaijani_android_ready_and_marks_mock(engine_env):
    s = Suggestion("k", "approval_waiting", "Təsdiq gözlənilir", "Ödəniş sorğusu gözləyir. Bax https://x.example/a",
                   "high", 80, ("e1",), (ActionProposal("open_approval", "Sorğuya bax"),), utc(8), utc(8) + 3600,
                   is_mock=True)
    n = to_notification(s, now=utc(8))
    assert (n.platform, n.locale, n.category, n.is_mock) == ("android", "az", "approval_waiting", True)
    assert [a.action_id for a in n.actions] == ["open_approval"] and "bağlantı" in n.speech_text
    assert json.loads(n.to_json())["voice"]["gender"] == "female"
