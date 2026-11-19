"""Mərhələ 18.2-B: sərhəd auditi — `core/` təmizdir (yan təsir, şəbəkə, env, agent kodu, təsdiq ETMƏK yoxdur), Stage 17/18.0 toxunulmazdır, L2/L3 yoxdur."""
from __future__ import annotations

import ast
import pathlib

import core
import runtime.brokered_core as brokered

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE_DIR = pathlib.Path(core.__file__).parent
ALLOWED_STDLIB = {"__future__", "dataclasses", "datetime", "enum", "hashlib", "json", "pathlib", "re", "threading", "typing"}
ALLOWED_PROJECT_PREFIX = ("core", "kernel")
ALLOWED_PROJECT_EXACT = {"safety.dbconn"}
FORBIDDEN_TEXT = ("os.environ", "getenv", "subprocess", "socket", "urllib", "requests", "eval(", "exec(", "run_session", "AgentGateway", "EffectBroker", "CostLedger", "ApprovalGateway", "importlib")


def _imports(tree: ast.AST) -> list[str]:
    return [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]


def test_core_imports_are_allowlisted_and_contain_no_side_effect_primitives():
    for f in CORE_DIR.glob("*.py"):
        src = f.read_text()
        for m in _imports(ast.parse(src)):
            assert m in ALLOWED_STDLIB or m in ALLOWED_PROJECT_EXACT or m.split(".")[0] in ALLOWED_PROJECT_PREFIX, f"{f.name}: icazəsiz import {m}"
        for bad in FORBIDDEN_TEXT:
            assert bad not in src.replace("`", ""), f"{f.name}: {bad}"


def test_core_never_defines_or_calls_approve_deny_and_has_no_model_or_agent_calls():
    for f in CORE_DIR.glob("*.py"):
        tree = ast.parse(f.read_text())
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & {"approve", "deny", "complete", "route", "synthesize", "transcribe", "run_session", "decide"}, f.name


def test_adapter_imports_and_no_network_env_or_approval_authority():
    src = pathlib.Path(brokered.__file__).read_text()
    allowed = ALLOWED_STDLIB | {"agents.spec", "core.ports", "kernel.common", "kernel.plan", "safety.approvals", "safety.broker"}
    for m in _imports(ast.parse(src)):
        assert m in allowed, f"icazəsiz import {m}"
    for bad in ("os.environ", "getenv", "socket", "urllib", "subprocess", "run_session", "gateway", ".approve(", ".deny(", ".decide("):
        assert bad not in src, bad


def test_kernel_and_stage17_production_files_do_not_import_core():
    for rel in ("kernel/common.py", "kernel/goal.py", "kernel/plan.py", "kernel/events.py", "kernel/eventlog.py", "runtime/env.py", "api/bootstrap.py", "runtime/production.py",
                "tools/serve.py", "safety/broker.py", "safety/ledger.py", "safety/approvals.py"):
        mods = _imports(ast.parse((ROOT / rel).read_text()))
        assert not [m for m in mods if m.split(".")[0] == "core" or m == "runtime.brokered_core"], rel


def test_autonomy_is_limited_to_l0_l1_and_agent_gateway_session_stays_closed():
    assert {a.name for a in core.Autonomy} == {"L0", "L1"}
    from runtime.agent_gateway import AgentGateway
    assert hasattr(AgentGateway, "run_session")                                                    # mövcuddur; bu mərhələ onu çağırmır (yuxarıdakı AST testləri)


def test_engine_face_exposes_no_owner_only_operations():
    for name in ("resolve_step", "revise_plan"):
        assert not hasattr(core.EngineGoalStore, name)
    for name in ("begin_step", "finish_step", "recover_expired", "flush_outbox", "mark_awaiting", "block_step"):
        assert not hasattr(core.OwnerGoalStore, name)
