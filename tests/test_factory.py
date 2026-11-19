import pytest

from agents.factory import MockLLM, create_spec
from agents.spec import AgentSpec, SpecError

PURPOSE = "Müştəri suallarını kateqoriyalara ayıran agent"


def test_sample_purpose_creates_valid_spec():
    spec = create_spec(PURPOSE, MockLLM())
    assert isinstance(spec, AgentSpec)
    assert spec.purpose == PURPOSE
    assert spec.cost_limit > 0
    # ən az imtiyaz: həssas icazə verilmir
    assert not {"spend_money", "external_account", "publish"} & set(spec.permissions)


def test_empty_purpose_rejected():
    with pytest.raises(SpecError):
        create_spec("   ", MockLLM())


class ScriptedLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


GOOD = (
    "```yaml\nad: abc_agent\nməqsəd: x\nalətlər: []\nicazələr: []\n"
    "giriş: [a]\nçıxış: [b]\nxərc_limiti: 1\n```"
)


def test_code_fence_is_stripped():
    assert create_spec("x", ScriptedLLM([GOOD])).name == "abc_agent"


def test_retries_once_with_error_feedback():
    llm = ScriptedLLM(["bu yaml deyil: [", GOOD])
    spec = create_spec("x", llm)
    assert spec.name == "abc_agent"
    assert len(llm.prompts) == 2
    assert "xəta" in llm.prompts[1].lower()


def test_gives_up_after_max_attempts():
    llm = ScriptedLLM(["pis", "pis"])
    with pytest.raises(SpecError):
        create_spec("x", llm, max_attempts=2)
    assert len(llm.prompts) == 2
