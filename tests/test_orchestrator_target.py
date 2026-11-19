"""Orkestrator: tapşırıq konkret agentə yönəldilə bilər (eyni şöbədə bir neçə agent olduqda)."""
from agents.orchestrator import Orchestrator, Task
from agents.registry import Registry
from agents.spec import AgentSpec


def spec(name):
    return AgentSpec(name=name, purpose="p", tools=[], permissions=["read_files"], inputs=["sorgu"], outputs=["netice"], cost_limit=1.0)


def make(tmp_path, calls):
    reg = Registry(":memory:")
    for n in ("agent_a1", "agent_b2"):
        reg.add(spec(n), "development")
    return Orchestrator(reg, lambda s, d: calls.append(s.name) or {"netice": s.name}, audit_path=tmp_path / "a.jsonl",
                        stop_path=tmp_path / "STOP", errors_path=tmp_path / "e.jsonl")


def test_targeted_task_runs_on_the_named_agent_only(tmp_path):
    calls = []
    o = make(tmp_path, calls)
    r = o.execute(Task("development", {"sorgu": "x"}, "read_files", 0.0, "t", agent="agent_b2"))
    assert r.status == "ok" and r.agent == "agent_b2" and calls == ["agent_b2"]
    r = o.execute(Task("development", {"sorgu": "x"}, "read_files", 0.0, "t"))        # hədəfsiz: köhnə davranış (ilk uyğun agent)
    assert r.agent == "agent_a1"


def test_targeting_an_agent_outside_the_department_or_unknown_is_unrouted(tmp_path):
    calls = []
    o = make(tmp_path, calls)
    assert o.execute(Task("development", {"sorgu": "x"}, "read_files", 0.0, "t", agent="agent_zzz")).status == "unrouted"
    assert o.execute(Task("research", {"sorgu": "x"}, "read_files", 0.0, "t", agent="agent_a1")).status == "unrouted"
    assert calls == []
