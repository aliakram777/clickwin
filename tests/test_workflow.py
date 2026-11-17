import pytest

from agents.workflow import PlanError, PlanStep, execution_order, validate_plan


def s(id, deps=(), dept="dept_a"):
    return PlanStep(id, dept, "read_files", 0.0, tuple(deps))


def ids(steps):
    return [x.id for x in execution_order(steps)]


def test_dependencies_run_before_dependents():
    assert ids([s("c", ["a", "b"]), s("b", ["a"]), s("a")]) == ["a", "b", "c"]


def test_independent_steps_keep_declaration_order():
    assert ids([s("x"), s("y"), s("z")]) == ["x", "y", "z"]


def test_diamond():
    order = ids([s("d", ["b", "c"]), s("b", ["a"]), s("c", ["a"]), s("a")])
    assert order[0] == "a" and order[-1] == "d" and set(order[1:3]) == {"b", "c"}


def test_valid_plan_passes():
    validate_plan([s("a"), s("b", ["a"])])


@pytest.mark.parametrize("steps,word", [
    ([s("a", ["b"]), s("b", ["a"])], "dövr"),
    ([s("a", ["b"]), s("b", ["c"]), s("c", ["a"])], "dövr"),
    ([s("a", ["a"])], "özünə"),
    ([s("a", ["yoxdur"])], "olmayan"),
    ([s("a"), s("a")], "təkrar"),
    ([], "boş"),
])
def test_invalid_plans_rejected(steps, word):
    with pytest.raises(PlanError) as exc:
        validate_plan(steps)
    assert word in str(exc.value)
    with pytest.raises(PlanError):
        execution_order(steps)


@pytest.mark.parametrize("bad", [
    PlanStep("", "d", "read_files"),
    PlanStep("a", "", "read_files"),
    PlanStep("a", "d", "read_files", depends_on="b"),  # sətir, siyahı deyil
    PlanStep("a", "d", "read_files", depends_on=(1,)),
])
def test_malformed_steps_rejected(bad):
    with pytest.raises(PlanError):
        validate_plan([bad])


def test_cycle_does_not_block_acyclic_part_detection():
    with pytest.raises(PlanError) as exc:
        validate_plan([s("ok"), s("a", ["b"]), s("b", ["a"])])
    assert "a" in str(exc.value) and "b" in str(exc.value)
