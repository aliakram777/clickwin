"""Tool Runtime. Sandbox adapterləri burada TEST ƏVƏZEDİCİLƏRİDİR (real sandbox deyil); heç bir sistem əmri,
şəbəkə və ya xarici API işləmir. Fayl əməliyyatları yalnız pytest-in tmp qovluğundadır."""
import ast
import json
from pathlib import Path

import pytest

from agents.spec import AgentSpec
from safety.approvals import ApprovalService, Principal
from safety.broker import BrokerRequired, EffectBroker
from safety.ledger import BudgetPolicy, CostLedger, reconcile
from safety.tools import (
    ALL_ENFORCEMENTS, REQUIRED_PROBES, NullSandbox, ResourceLimits, RuntimePolicy, SandboxAttestation,
    SandboxResult, ToolError, ToolRegistry, ToolRuntime, ToolSpec, tool_handler,
)
from safety.workspace import Workspace, file_tools

ROOT = Path(__file__).resolve().parent.parent
MOCK = Principal("owner", "mock", "test")
ALL_BLOCKED = {p: "blocked" for p in REQUIRED_PROBES}


class FakeOSSandbox:
    """TEST ƏVƏZEDİCİSİ: real izolyasiya yoxdur, yalnız adapter müqaviləsini yoxlayır."""

    def __init__(self, probes=None, isolation="os", enforces=ALL_ENFORCEMENTS, output="sandbox-çıxışı",
                 ok=True, attest_exc=None, run_exc=None):
        self.probes = dict(ALL_BLOCKED if probes is None else probes)
        self.isolation, self.enforces = isolation, frozenset(enforces)
        self.output, self.ok, self.attest_exc, self.run_exc = output, ok, attest_exc, run_exc
        self.requests = []

    def attest(self):
        if self.attest_exc:
            raise self.attest_exc
        return SandboxAttestation("fake", self.isolation, self.enforces, self.probes)

    def run(self, request):
        self.requests.append(request)
        if self.run_exc:
            raise self.run_exc
        return SandboxResult(self.ok, self.output, "" if self.ok else "sandbox xətası")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def agent(tools, perms=("read_files", "write_files", "run_tests", "publish")):
    return AgentSpec(name="agent_a", purpose="p", tools=list(tools), permissions=list(perms),
                     inputs=["sorgu"], outputs=["netice"], cost_limit=10.0)


@pytest.fixture
def env(tmp_path):
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ws = Workspace(ws_root, max_file_bytes=200, max_workspace_bytes=1000)
    audit = tmp_path / "audit.jsonl"
    ledger = CostLedger(tmp_path / "ledger.db")
    svc = ApprovalService(tmp_path / "approvals.db", audit, owners={"owner"}, allow_mock=True)
    calls = {"pure": 0, "publish": 0}

    @tool_handler("math.add")
    def add(ctx, a, b):
        calls["pure"] += 1
        return a + b

    @tool_handler("post.send")
    def send(ctx, text):
        calls["publish"] += 1
        return {"sent": text}

    @tool_handler("big.out")
    def big(ctx):
        return "x" * 5000

    @tool_handler("slow.loop")
    def slow(ctx):
        while True:
            ctx.clock.t += 1.0  # saxta saat irəliləyir
            ctx.check_deadline()

    def build(policy=None, sandbox=None, limits=None, budget=None, extra=()):
        reg = ToolRegistry()
        for t in file_tools(ws):
            reg.register(t)
        reg.register(ToolSpec("math.add", "pure", "read_files", {"a": int, "b": int}, ("a", "b"), add))
        reg.register(ToolSpec("post.send", "pure", "publish", {"text": str}, ("text",), send))
        reg.register(ToolSpec("big.out", "pure", "read_files", {}, (), big))
        reg.register(ToolSpec("slow.loop", "pure", "read_files", {}, (), slow))
        reg.register(ToolSpec("proc.run", "exec", "run_tests", {"argv": str}, ("argv",), sandboxed=True))
        reg.register(ToolSpec("net.get", "network", "read_files", {"url": str}, ("url",), sandboxed=True))
        reg.register(ToolSpec("sys.change", "system", "write_files", {"what": str}, ("what",), sandboxed=True))
        reg.register(ToolSpec("env.read", "env", "read_files", {"name": str}, ("name",), sandboxed=True))
        for t in extra:
            reg.register(t)
        broker = EffectBroker(audit, tmp_path / "STOP", ledger=ledger, approvals=svc.client,
                              policy=budget)
        clock = Clock()
        rt = ToolRuntime(reg, broker, policy=policy or RuntimePolicy(), limits=limits or ResourceLimits(),
                         sandbox=sandbox, workspace=ws, clock=clock)
        return rt, clock

    return build, ws, audit, ledger, svc, calls, tmp_path


def rows(audit):
    p = Path(audit)
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def gate_rows(audit):
    return [r for r in rows(audit) if "event" not in r and r.get("source") is None]


def tool_denials(audit):
    return [r for r in rows(audit) if r.get("source") == "tool"]


ALL_TOOLS = ["fs.read", "fs.write", "fs.list", "math.add", "post.send", "big.out", "slow.loop",
             "proc.run", "net.get", "sys.change", "env.read"]


# --- allowlist ---

def test_unknown_tool_is_denied_and_audited(env):
    build, _, audit, ledger, _, _, _ = env
    rt, _ = build()
    res = rt.call(agent(ALL_TOOLS), "rm.rf", {})
    assert res.status == "blocked" and res.stage == "tool" and "naməlum" in res.reason
    assert ledger.entries() == [] and gate_rows(audit) == []  # broker-ə çatmayıb
    assert len(tool_denials(audit)) == 1 and tool_denials(audit)[0]["allowed"] is False


@pytest.mark.parametrize("name", [None, 5, "", "FS.READ", "../x", "a b"])
def test_malformed_tool_names_denied(env, name):
    build, _, _, _, _, _, _ = env
    rt, _ = build()
    assert rt.call(agent(ALL_TOOLS), name, {}).status == "blocked"


def test_tool_must_be_in_the_agents_own_allowlist(env):
    build, ws, audit, _, _, _, _ = env
    rt, _ = build()
    res = rt.call(agent(["fs.read"]), "fs.write", {"path": "a.txt", "content": "x"})
    assert res.status == "blocked" and "agentin alət siyahısında" in res.reason
    assert ws.list_dir(".") == []


def test_agent_without_permission_is_stopped_by_broker_gate(env):
    build, ws, audit, _, _, _, _ = env
    rt, _ = build()
    res = rt.call(agent(["fs.write"], perms=("read_files",)), "fs.write", {"path": "a.txt", "content": "x"})
    assert res.status == "blocked" and "icazə" in res.reason
    assert ws.list_dir(".") == []


# --- fayl alətləri broker-dən keçir ---

def test_file_tools_roundtrip_through_broker_with_audit_and_ledger(env):
    build, ws, audit, ledger, _, _, _ = env
    rt, _ = build()
    a = agent(["fs.read", "fs.write", "fs.list"])
    assert rt.call(a, "fs.write", {"path": "n.txt", "content": "salam"}).status == "ok"
    assert rt.call(a, "fs.read", {"path": "n.txt"}).output == "salam"
    assert rt.call(a, "fs.list", {"path": "."}).output == ["n.txt"]
    g = gate_rows(audit)
    assert [r["allowed"] for r in g] == [True, True, True]
    assert [r["description"] for r in g] == ["tool:fs.write", "tool:fs.read", "tool:fs.list"]
    assert len(ledger.entries()) == 3 and reconcile(ledger, audit).ok


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "a/../../x", ".env", "x\x00y"])
def test_path_attacks_blocked_before_broker_and_audited(env, path):
    build, ws, audit, ledger, _, _, tmp = env
    rt, _ = build()
    for tool, args in (("fs.write", {"path": path, "content": "z"}), ("fs.read", {"path": path})):
        res = rt.call(agent([tool]), tool, args)
        assert res.status == "blocked" and res.stage == "tool"
    assert ledger.entries() == [] and gate_rows(audit) == []
    assert len(tool_denials(audit)) == 2
    assert not (tmp / "outside.txt").exists()


def test_symlink_escape_blocked_end_to_end(env):
    import os
    build, ws, audit, _, _, _, tmp = env
    secret = tmp / "secret.txt"
    secret.write_text("gizli")
    os.symlink(secret, tmp / "ws" / "l.txt")
    rt, _ = build()
    res = rt.call(agent(["fs.read"]), "fs.read", {"path": "l.txt"})
    assert res.status == "blocked" and "gizli" not in json.dumps(res.__dict__, default=str)


def test_handler_error_is_reported_with_known_cost(env):
    build, _, audit, ledger, _, _, _ = env
    rt, _ = build()
    res = rt.call(agent(["fs.read"]), "fs.read", {"path": "yoxdur.txt"})  # yol qaydasındadır, fayl yoxdur
    assert res.status == "error" and res.cost_status == "committed"
    assert ledger.unknown_entries() == []


# --- arqument doğrulaması ---

@pytest.mark.parametrize("tool,args", [
    ("math.add", {"a": 1}),                      # çatışmayan
    ("math.add", {"a": 1, "b": 2, "c": 3}),      # artıq
    ("math.add", {"a": "1", "b": 2}),            # tip
    ("math.add", {"a": True, "b": 2}),           # bool int sayılmır
    ("math.add", {"a": 1.5, "b": 2}),
    ("fs.read", {"path": 5}),
    ("fs.write", {"path": "a", "content": "x" * 70000}),
    ("math.add", "a=1"),                          # xəritə deyil
])
def test_argument_validation(env, tool, args):
    build, _, audit, ledger, _, calls, _ = env
    rt, _ = build()
    res = rt.call(agent(ALL_TOOLS), tool, args)
    assert res.status == "blocked" and res.stage == "tool"
    assert calls["pure"] == 0 and ledger.entries() == []


# --- bağlı kateqoriyalar və sandbox ---

@pytest.mark.parametrize("tool", ["proc.run", "net.get", "sys.change", "env.read"])
def test_dangerous_kinds_closed_by_default_even_with_verified_sandbox(env, tool):
    build, _, audit, ledger, _, _, _ = env
    sb = FakeOSSandbox()
    rt, _ = build(sandbox=sb)
    args = {"proc.run": {"argv": "x"}, "net.get": {"url": "u"}, "sys.change": {"what": "w"},
            "env.read": {"name": "n"}}[tool]
    res = rt.call(agent(ALL_TOOLS), tool, args)
    assert res.status == "blocked" and "bağlıdır" in res.reason
    assert sb.requests == [] and ledger.entries() == []


def exec_args():
    return {"argv": "echo salam"}


def test_exec_denied_without_sandbox_adapter(env):
    build, _, _, ledger, _, _, _ = env
    rt, _ = build(policy=RuntimePolicy(allow_kinds=frozenset({"pure", "exec"})))  # sandbox verilməyib
    res = rt.call(agent(ALL_TOOLS), "proc.run", exec_args())
    assert res.status == "blocked" and "sandbox" in res.reason and ledger.entries() == []


def test_null_sandbox_always_denies(env):
    build, _, _, ledger, _, _, _ = env
    rt, _ = build(policy=RuntimePolicy(allow_kinds=frozenset({"pure", "exec"})), sandbox=NullSandbox())
    assert rt.call(agent(ALL_TOOLS), "proc.run", exec_args()).status == "blocked"
    assert rt.security_posture()["os_sandbox_verified"] is False


@pytest.mark.parametrize("sb,posture_verified", [
    (FakeOSSandbox(isolation="none"), False),
    (FakeOSSandbox(probes={**ALL_BLOCKED, "network": "allowed"}), False),
    (FakeOSSandbox(probes={k: v for k, v in ALL_BLOCKED.items() if k != "fs_escape"}), False),
    (FakeOSSandbox(probes={**ALL_BLOCKED, "memory": "error"}), False),
    (FakeOSSandbox(attest_exc=RuntimeError("attest düşdü")), False),
    (FakeOSSandbox(enforces=ALL_ENFORCEMENTS - {"memory"}), False),
    (FakeOSSandbox(enforces=ALL_ENFORCEMENTS - {"processes"}), True),  # ümumi baza var, exec üçün çatışmır
])
def test_unverified_or_weak_sandbox_is_denied(env, sb, posture_verified):
    build, _, _, ledger, _, _, _ = env
    rt, _ = build(policy=RuntimePolicy(allow_kinds=frozenset({"pure", "exec"})), sandbox=sb)
    res = rt.call(agent(ALL_TOOLS), "proc.run", exec_args())
    assert res.status == "blocked" and sb.requests == [] and ledger.entries() == []
    assert rt.security_posture()["os_sandbox_verified"] is posture_verified


def test_verified_sandbox_runs_through_broker_with_limits_and_clean_env(env):
    build, ws, audit, ledger, _, _, tmp = env
    sb = FakeOSSandbox(output="hazır")
    limits = ResourceLimits(timeout_s=3.0, memory_mb=64, max_output_bytes=1000)
    rt, _ = build(policy=RuntimePolicy(allow_kinds=frozenset({"pure", "exec"})), sandbox=sb, limits=limits)
    res = rt.call(agent(ALL_TOOLS), "proc.run", exec_args())
    assert res.status == "ok" and res.output == "hazır"
    req = sb.requests[0]
    assert req.tool == "proc.run" and req.args == exec_args()
    assert (req.limits.timeout_s, req.limits.memory_mb) == (3.0, 64)
    assert dict(req.env) == {} and Path(req.workspace_root) == (tmp / "ws").resolve()
    assert gate_rows(audit)[-1]["description"] == "tool:proc.run" and gate_rows(audit)[-1]["allowed"] is True
    assert rt.security_posture()["os_sandbox_verified"] is True


def test_sandbox_failure_and_oversized_output_become_errors(env):
    build, _, _, ledger, _, _, _ = env
    pol = RuntimePolicy(allow_kinds=frozenset({"pure", "exec"}))
    for sb, word in ((FakeOSSandbox(ok=False), "sandbox"), (FakeOSSandbox(run_exc=OSError("düşdü")), "OSError"),
                     (FakeOSSandbox(output="x" * 5000), "çıxış")):
        rt, _ = build(policy=pol, sandbox=sb, limits=ResourceLimits(max_output_bytes=1000))
        res = rt.call(agent(ALL_TOOLS), "proc.run", exec_args())
        assert res.status == "error" and word in res.reason
        assert res.output is None


def test_dangerous_tools_cannot_have_in_process_handlers():
    reg = ToolRegistry()
    for kind in ("exec", "network", "system", "env"):
        with pytest.raises(ValueError):
            reg.register(ToolSpec(f"t.{kind}", kind, "run_tests", {}, (), handler=lambda ctx: 1))
        with pytest.raises(ValueError):
            reg.register(ToolSpec(f"t.{kind}", kind, "run_tests", {}, ()))  # nə handler, nə sandboxed
    with pytest.raises(ValueError):
        reg.register(ToolSpec("t.both", "pure", "read_files", {}, (), handler=lambda ctx: 1, sandboxed=True))


# --- broker qaydaları ---

def test_budget_denial_never_runs_handler(env):
    build, _, audit, ledger, _, calls, _ = env

    @tool_handler("paid.calc")
    def paid(ctx):
        calls["pure"] += 100
        return 1

    rt2, _ = build(budget=BudgetPolicy(task_limit=0.1),
                   extra=[ToolSpec("paid.calc", "pure", "read_files", {}, (), paid, cost=0.5)])
    res = rt2.call(agent(["paid.calc"]), "paid.calc", {})
    assert res.status == "blocked" and calls["pure"] == 0 and ledger.entries() == []
    assert [r for r in rows(audit) if r.get("source") == "budget"]


def test_stop_flag_blocks_tool_calls(env):
    build, _, _, _, _, calls, tmp = env
    rt, _ = build()
    (tmp / "STOP").write_text("stop")
    assert rt.call(agent(["math.add"]), "math.add", {"a": 1, "b": 2}).status == "blocked"
    assert calls["pure"] == 0


def test_sensitive_tool_needs_owner_approval(env):
    build, _, audit, ledger, svc, calls, _ = env
    rt, _ = build()
    a = agent(["post.send"])
    first = rt.call(a, "post.send", {"text": "salam"}, op_id="op1")
    assert first.status == "pending_approval" and first.approval_id and calls["publish"] == 0
    assert svc.gateway.approve(first.approval_id, MOCK).ok
    ok = rt.call(a, "post.send", {"text": "salam"}, op_id="op1")
    assert ok.status == "ok" and ok.output == {"sent": "salam"} and calls["publish"] == 1
    assert rt.call(a, "post.send", {"text": "salam"}, op_id="op1").status == "duplicate"
    assert rt.call(a, "post.send", {"text": "başqa"}, op_id="op1").status == "duplicate"  # op_id artıq istifadə olunub
    changed = rt.call(a, "post.send", {"text": "başqa"}, op_id="op2")  # args dəyişdi -> yeni təsdiq
    assert changed.status == "pending_approval" and calls["publish"] == 1


def test_sensitive_tool_without_op_id_is_denied(env):
    build, _, _, _, _, calls, _ = env
    rt, _ = build()
    assert rt.call(agent(["post.send"]), "post.send", {"text": "x"}).status == "blocked"
    assert calls["publish"] == 0


def test_duplicate_tool_call_runs_once(env):
    build, _, _, ledger, _, calls, _ = env
    rt, _ = build()
    a = agent(["math.add"])
    assert rt.call(a, "math.add", {"a": 1, "b": 2}, op_id="x1").output == 3
    assert rt.call(a, "math.add", {"a": 1, "b": 2}, op_id="x1").status == "duplicate"
    assert calls["pure"] == 1


# --- limitlər ---

def test_output_size_limit(env):
    build, _, _, ledger, _, _, _ = env
    rt, _ = build(limits=ResourceLimits(max_output_bytes=1000))
    res = rt.call(agent(["big.out"]), "big.out", {})
    assert res.status == "error" and "çıxış" in res.reason and res.output is None
    assert res.cost_status == "committed"


def test_cooperative_timeout_is_transient_error(env):
    build, _, _, _, _, _, _ = env
    rt, clock = build(limits=ResourceLimits(timeout_s=3.0))
    res = rt.call(agent(["slow.loop"]), "slow.loop", {})
    assert res.status == "error" and "zaman" in res.reason and res.transient is True


def test_limits_are_validated():
    for kw in ({"timeout_s": 0}, {"memory_mb": -1}, {"max_output_bytes": 0}, {"timeout_s": True}):
        with pytest.raises(ValueError):
            ResourceLimits(**kw)
    with pytest.raises(ValueError):
        RuntimePolicy(allow_kinds=frozenset({"pure", "teleport"}))


def test_security_posture_is_honest(env):
    build, _, _, _, _, _, _ = env
    rt, _ = build()
    p = rt.security_posture()
    assert p["os_sandbox_verified"] is False
    assert "memory" in p["not_enforced_in_process"] and "network" in p["not_enforced_in_process"]
    assert "memory" not in p["in_process_enforced"]


# --- bypass olmur ---

def test_handlers_refuse_direct_calls_and_cross_tool_calls(env):
    build, ws, _, _, _, _, _ = env
    reg_tools = {t.name: t for t in file_tools(ws)}
    with pytest.raises(BrokerRequired):
        reg_tools["fs.write"].handler(None, path="a.txt", content="x")
    assert ws.list_dir(".") == []

    @tool_handler("evil.tool")
    def evil(ctx):
        # broker daxilindədir, amma başqa alətin handler-ini çağırır
        return reg_tools["fs.write"].handler(ctx, path="hack.txt", content="x")

    rt, _ = build(extra=[ToolSpec("evil.tool", "pure", "read_files", {}, (), evil)])
    res = rt.call(agent(["evil.tool"]), "evil.tool", {})
    assert res.status == "error" and "BrokerRequired" in res.reason
    assert ws.list_dir(".") == []


def test_registry_is_frozen_by_runtime_and_hides_handlers(env):
    build, _, _, _, _, _, _ = env
    rt, _ = build()
    with pytest.raises(ValueError):
        rt.registry.register(ToolSpec("late.tool", "pure", "read_files", {}, (), lambda ctx: 1))
    info = rt.registry.describe("fs.read")
    assert info.name == "fs.read" and not hasattr(info, "handler")
    assert "fs.read" in rt.registry.names() and rt.registry.describe("yoxdur") is None


def test_registry_rejects_duplicates_and_bad_specs():
    reg = ToolRegistry()
    ok = ToolSpec("a.b", "pure", "read_files", {}, (), lambda ctx: 1)
    reg.register(ok)
    with pytest.raises(ValueError):
        reg.register(ok)
    bad = [
        ToolSpec("Bad Name", "pure", "read_files", {}, (), lambda ctx: 1),
        ToolSpec("x.y", "teleport", "read_files", {}, (), lambda ctx: 1),
        ToolSpec("x.y", "pure", "root", {}, (), lambda ctx: 1),
        ToolSpec("x.y", "pure", "read_files", {"a": list}, (), lambda ctx: 1),
        ToolSpec("x.y", "pure", "read_files", {"a": int}, ("b",), lambda ctx: 1),
        ToolSpec("x.y", "pure", "read_files", {}, (), lambda ctx: 1, cost=-1),
    ]
    for t in bad:
        with pytest.raises(ValueError):
            reg.register(t)


def test_runtime_requires_a_broker(env):
    with pytest.raises(TypeError):
        ToolRuntime(ToolRegistry(), None)


def test_shipped_code_has_no_shell_network_or_env_access():
    banned_imports = {"subprocess", "socket", "requests", "urllib", "http", "httpx", "ctypes", "shlex",
                      "pty", "smtplib", "ftplib", "multiprocessing"}
    for name in ("tools.py", "workspace.py"):
        tree = ast.parse((ROOT / "safety" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not banned_imports & {a.name.split(".")[0] for a in node.names}, name
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned_imports, name
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"system", "popen", "environ", "getenv", "execv", "fork"}, (name, node.lineno)


def test_agents_package_cannot_reach_tool_internals():
    for path in (ROOT / "agents").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"safety.tools", "safety.workspace"}:
                pytest.fail(f"{path.name} alət daxili hissələrini import edir")


def test_mixed_calls_are_all_audited_and_reconcile(env):
    build, _, audit, ledger, _, _, _ = env
    rt, _ = build()
    a = agent(ALL_TOOLS)
    rt.call(a, "fs.write", {"path": "a.txt", "content": "x"})       # gate allowed
    rt.call(a, "fs.read", {"path": "../x"})                          # tool denial
    rt.call(a, "nonexistent", {})                                    # tool denial
    rt.call(a, "math.add", {"a": 1, "b": 2})                         # gate allowed
    rt.call(agent(["fs.write"], perms=("read_files",)), "fs.write", {"path": "b.txt", "content": "y"})  # gate denied
    allowed = [r for r in gate_rows(audit) if r["allowed"]]
    assert len(allowed) == 2 and len(tool_denials(audit)) == 2
    assert len([r for r in gate_rows(audit) if not r["allowed"]]) == 1
    assert reconcile(ledger, audit).ok
