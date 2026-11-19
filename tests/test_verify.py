import json

from agents.spec import AgentSpec
from agents.verify import VerifyTask, verify

SPEC = AgentSpec(
    name="tasnif_agenti",
    purpose="sual tasnifi",
    tools=[],
    permissions=["read_files"],
    inputs=["sual"],
    outputs=["kateqoriya"],
    cost_limit=1.0,
)


def good_runner(spec, data):
    return {"kateqoriya": "faktura" if "faktura" in data["sual"] else "digər"}


TASK = VerifyTask(
    name="faktura_testi",
    input={"sual": "faktura harada?"},
    check=lambda out: out["kateqoriya"] == "faktura",
)


def read(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_passing_task(tmp_path):
    log = tmp_path / "logs" / "errors.jsonl"
    result = verify(SPEC, TASK, good_runner, errors_path=log)
    assert result.ok
    assert not log.exists()


def test_wrong_output_is_logged(tmp_path):
    log = tmp_path / "logs" / "errors.jsonl"
    bad = VerifyTask("x", {"sual": "salam"}, lambda out: out["kateqoriya"] == "faktura")
    result = verify(SPEC, bad, good_runner, errors_path=log)
    assert not result.ok
    rows = read(log)
    assert len(rows) == 1
    assert rows[0]["agent"] == "tasnif_agenti"
    assert rows[0]["stage"] == "check"


def test_runner_exception_is_logged(tmp_path):
    log = tmp_path / "errors.jsonl"

    def boom(spec, data):
        raise RuntimeError("partladı")

    result = verify(SPEC, TASK, boom, errors_path=log)
    assert not result.ok
    row = read(log)[0]
    assert row["stage"] == "run"
    assert "partladı" in row["error"]


def test_missing_declared_output_is_logged(tmp_path):
    log = tmp_path / "errors.jsonl"
    result = verify(SPEC, TASK, lambda s, d: {"başqa": 1}, errors_path=log)
    assert not result.ok
    assert read(log)[0]["stage"] == "output"


def test_missing_declared_input_is_logged(tmp_path):
    log = tmp_path / "errors.jsonl"
    task = VerifyTask("x", {}, lambda out: True)
    result = verify(SPEC, task, good_runner, errors_path=log)
    assert not result.ok
    assert read(log)[0]["stage"] == "input"


def test_check_exception_counts_as_failure(tmp_path):
    log = tmp_path / "errors.jsonl"
    task = VerifyTask("x", {"sual": "a"}, lambda out: out["yoxdur"])
    assert not verify(SPEC, task, good_runner, errors_path=log).ok
    assert read(log)[0]["stage"] == "check"


def test_failures_append(tmp_path):
    log = tmp_path / "errors.jsonl"
    for _ in range(2):
        verify(SPEC, TASK, lambda s, d: {"kateqoriya": "x"}, errors_path=log)
    assert len(read(log)) == 2
