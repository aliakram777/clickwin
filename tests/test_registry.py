import sqlite3

import pytest

from agents.registry import Registry, RegistryError
from agents.spec import AgentSpec, SpecError


def spec(name="tasnif_agenti", perms=("read_files",), limit=1.0):
    return AgentSpec(
        name=name,
        purpose="p",
        tools=[],
        permissions=list(perms),
        inputs=["sual"],
        outputs=["kateqoriya"],
        cost_limit=limit,
    )


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def test_add_and_get_roundtrip(reg):
    reg.add(spec(), "destek")
    got = reg.get("tasnif_agenti")
    assert got.spec == spec()
    assert got.department == "destek"


def test_duplicate_add_rejected(reg):
    reg.add(spec(), "destek")
    with pytest.raises(RegistryError):
        reg.add(spec(), "destek")


def test_get_missing_rejected(reg):
    with pytest.raises(RegistryError):
        reg.get("yoxdur_agent")


def test_update_changes_spec_and_keeps_created_at(reg):
    reg.add(spec(), "destek")
    before = reg.get("tasnif_agenti")
    reg.update(spec(perms=("read_files", "write_files"), limit=2.0))
    after = reg.get("tasnif_agenti")
    assert after.spec.permissions == ["read_files", "write_files"]
    assert after.spec.cost_limit == 2.0
    assert after.department == "destek"  # verilməyibsə saxlanır
    assert after.created_at == before.created_at
    assert after.updated_at >= before.updated_at


def test_update_can_move_department(reg):
    reg.add(spec(), "destek")
    reg.update(spec(), department="satis")
    assert reg.get("tasnif_agenti").department == "satis"


def test_update_missing_rejected(reg):
    with pytest.raises(RegistryError):
        reg.update(spec())


def test_list_filters_by_department_sorted(reg):
    reg.add(spec("b_agent"), "destek")
    reg.add(spec("a_agent"), "destek")
    reg.add(spec("c_agent"), "satis")
    assert [r.spec.name for r in reg.list("destek")] == ["a_agent", "b_agent"]
    assert [r.spec.name for r in reg.list()] == ["a_agent", "b_agent", "c_agent"]
    assert reg.list("yoxdur") == []


@pytest.mark.parametrize("bad", ["", "Destek", "d'; DROP TABLE agents;--", "a b", "x" * 40])
def test_bad_department_rejected(reg, bad):
    with pytest.raises(RegistryError):
        reg.add(spec(), bad)
    assert reg.list() == []


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "data" / "naymera.db"
    r1 = Registry(path)
    r1.add(spec(), "destek")
    r1.close()
    r2 = Registry(path)
    assert r2.get("tasnif_agenti").spec == spec()
    r2.close()


def test_corrupt_row_raises_spec_error(tmp_path):
    path = tmp_path / "r.db"
    r = Registry(path)
    r.add(spec(), "destek")
    r.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE agents SET spec_yaml = ?", ("ad: x\nicazələr: [root_access]\n",))
    conn.commit()
    conn.close()
    r = Registry(path)
    with pytest.raises(SpecError):
        r.get("tasnif_agenti")
    r.close()
