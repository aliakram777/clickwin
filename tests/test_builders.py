"""Məhsul istehsalı bünövrəsi: Website Builder, App Builder, APK Builder və Video Studio üçün agent interfeysləri. Bu mərhələdə HEÇ BİRİ qurulmayıb:
hamısı NOT CONFIGURED, plan yalnız SİMULYASİYA; real alət, nəşr, imzalama və deployment YOXDUR."""
import ast
from pathlib import Path

import pytest

from agents.agent_roles import ROLE_CATALOG
from agents.builders import BUILDERS, BuilderRegistry, BuildPlan, NotConfigured
from agents.team import TeamPlan, TeamRunner
from tests.test_agent_runtime import world  # noqa: F401  (fixture)

LEAK = "sk-test-" + "FAKEKEY1234567890abcdef"
IDS = ("website", "app", "apk", "video")


def test_registry_lists_the_four_builders_all_not_configured():
    reg = BuilderRegistry()
    assert tuple(b.builder_id for b in reg.all()) == IDS and set(BUILDERS) == set(IDS)
    for st in reg.statuses():
        assert st.state == "not_configured" and st.can_publish is False and st.reason and st.required_tools
        assert all(v == "not_configured" for v in st.tools_state.values())
    assert reg.get("nope") is None


@pytest.mark.parametrize("bid", IDS)
def test_build_is_refused_because_no_tools_are_connected(bid):
    b = BuilderRegistry().get(bid)
    plan = b.plan("kiçik layihə")
    with pytest.raises(NotConfigured) as e:
        b.build(plan)
    assert "NOT CONFIGURED" in str(e.value)


@pytest.mark.parametrize("bid", IDS)
def test_plans_are_deterministic_simulations_with_safe_roles_and_explicit_approval_gates(bid):
    b = BuilderRegistry().get(bid)
    p1, p2 = b.plan("Müştəri üçün layihə"), b.plan("Müştəri üçün layihə")
    assert p1 == p2 and isinstance(p1, BuildPlan) and p1.simulation is True and p1.builder_id == bid
    ids = [s.stage_id for s in p1.steps]
    assert len(ids) == len(set(ids)) and all(d in ids[:i] for i, s in enumerate(p1.steps) for d in s.depends_on)
    assert {s.role_id for s in p1.steps} <= set(ROLE_CATALOG)
    planning = [s for s in p1.steps if s.phase == "planning"]
    assert planning and all(s.permission == "read_files" and not s.needs_approval for s in planning)
    release = [s for s in p1.steps if s.phase == "release"]
    assert release and all(s.needs_approval for s in release)                                   # nəşr/imzalama/deployment təsdiqsiz mümkün deyil
    assert p1.approvals and all(g.owner_only for g in p1.approvals)
    assert {g.permission for g in p1.approvals} <= {"publish", "external_account", "spend_money"}


def test_apk_plan_states_the_android_sdk_limit_and_that_signing_stays_with_the_owner():
    b = BuilderRegistry().get("apk")
    assert "Android SDK" in b.status().reason
    plan = b.plan("APK hazırla")
    actions = {g.action for g in plan.approvals}
    assert {"apk_signing", "distribution"} <= actions
    assert "keystore" in " ".join(plan.notes).lower() and "sahib" in " ".join(plan.notes).lower()


@pytest.mark.parametrize("brief", ["", "   ", "x" * 1200, "açar " + LEAK])
def test_briefs_are_validated(brief):
    for b in BuilderRegistry().all():
        with pytest.raises(ValueError):
            b.plan(brief)


@pytest.mark.parametrize("bid", IDS)
def test_only_planning_steps_become_a_team_plan_and_run_as_a_simulation(bid, world):
    w = world
    b = BuilderRegistry().get(bid)
    plan = b.plan("Layihə təsviri")
    team_plan = b.to_team_plan(plan)
    assert isinstance(team_plan, TeamPlan) and team_plan.source == "rules"
    assert all(s.permission == "read_files" for s in team_plan.stages) and len(team_plan.stages) == len([s for s in plan.steps if s.phase == "planning"])
    runner = TeamRunner(w.factory, w.disp, w.tmp / "team.db", w.audit, clock=w.clock)
    w.cloud._state, w.cloud.is_mock = "simulation", True
    rep = runner.run(team_plan)
    assert rep.status == "ok" and rep.is_mock is True and rep.product is not None and rep.product.is_mock is True
    assert all(a.permissions == frozenset({"read_files"}) for a in w.factory.view.agents())       # nə yazma, nə nəşr icazəsi


def test_builders_module_has_no_effects_network_env_or_security_core_access():
    text = (Path(__file__).resolve().parent.parent / "agents" / "builders.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"safety.broker", "safety.gate", "safety.tools", "safety.workspace", "safety.ledger", "safety.approvals", "socket", "subprocess", "os", "urllib.request", "agents.agent_runtime"}
    assert "environ" not in text and "getenv" not in text
