import pytest

from agents.spec import AgentSpec, SpecError

VALID = """
ad: tasnif_agenti
məqsəd: Gələn sualları kateqoriyalara ayırmaq
alətlər: [text_classify]
icazələr: [read_files]
giriş: [sual]
çıxış: [kateqoriya]
xərc_limiti: 1.5
"""


def test_valid_yaml_loads():
    spec = AgentSpec.from_yaml(VALID)
    assert spec.name == "tasnif_agenti"
    assert spec.tools == ["text_classify"]
    assert spec.permissions == ["read_files"]
    assert spec.inputs == ["sual"]
    assert spec.outputs == ["kateqoriya"]
    assert spec.cost_limit == 1.5


def test_yaml_roundtrip():
    spec = AgentSpec.from_yaml(VALID)
    assert AgentSpec.from_yaml(spec.to_yaml()) == spec


@pytest.mark.parametrize("bad_name", ["", "A", "Big Name", "1abc", "ab", "x" * 40])
def test_bad_name_rejected(bad_name):
    text = VALID.replace("tasnif_agenti", f"'{bad_name}'")
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(text)


def test_empty_purpose_rejected():
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(VALID.replace("Gələn sualları kateqoriyalara ayırmaq", "''"))


def test_unknown_permission_rejected():
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(VALID.replace("read_files", "root_access"))


@pytest.mark.parametrize("limit", ["-1", "'abc'", ".inf", "true"])
def test_bad_cost_limit_rejected(limit):
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(VALID.replace("1.5", limit))


def test_missing_field_rejected():
    text = "\n".join(l for l in VALID.splitlines() if not l.startswith("çıxış"))
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(text)


def test_unknown_field_rejected():
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(VALID + "api_key: abc\n")


def test_non_mapping_and_broken_yaml_rejected():
    with pytest.raises(SpecError):
        AgentSpec.from_yaml("- a\n- b\n")
    with pytest.raises(SpecError):
        AgentSpec.from_yaml("ad: [unclosed")


def test_tools_must_be_list_of_strings():
    with pytest.raises(SpecError):
        AgentSpec.from_yaml(VALID.replace("[text_classify]", "[1, 2]"))
