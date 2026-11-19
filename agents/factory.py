"""Məqsəd mətnindən agent təsviri yaradır. LLM interfeysi: Protocol (hələlik mock)."""
from __future__ import annotations

import re
from typing import Protocol

from agents.spec import AgentSpec, SpecError

MARKER = "MƏQSƏD:"

PROMPT = """Aşağıdakı məqsəd üçün agent təsvirini YAML ilə yaz.
Yalnız bu açarlar: ad, məqsəd, alətlər, icazələr, giriş, çıxış, xərc_limiti.
Ən az imtiyaz prinsipi: pul, xarici hesab və nəşr icazəsi vermə.
{marker} {purpose}"""

_TRANSLIT = str.maketrans(
    {"ə": "e", "ı": "i", "ö": "o", "ü": "u", "ç": "c", "ş": "sh", "ğ": "g"}
)
_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


class MockLLM:
    """Şəbəkəsiz, deterministik əvəzedici: ən az imtiyazlı təsvir qaytarır."""

    def complete(self, prompt: str) -> str:
        purpose = prompt.split(MARKER, 1)[1].strip().splitlines()[0]
        return (
            f"ad: {_slug(purpose)}\n"
            f"məqsəd: {purpose!r}\n"
            "alətlər: []\n"
            "icazələr: [read_files]\n"
            "giriş: [sorğu]\n"
            "çıxış: [nəticə]\n"
            "xərc_limiti: 1.0\n"
        )


def _slug(text: str) -> str:
    words = re.findall(r"\w+", text.lower().translate(_TRANSLIT))
    slug = re.sub(r"[^a-z0-9]+", "_", "_".join(words[:4])).strip("_")[:32].strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"agent_{slug}"[:32].strip("_")
    return slug if len(slug) >= 3 else "agent_yeni"


def create_spec(purpose: str, llm: LLMClient, max_attempts: int = 2) -> AgentSpec:
    if not purpose or not purpose.strip():
        raise SpecError("məqsəd boş ola bilməz")
    prompt = PROMPT.format(marker=MARKER, purpose=purpose.strip())
    last: SpecError | None = None
    for _ in range(max(1, max_attempts)):
        answer = llm.complete(prompt)
        try:
            return AgentSpec.from_yaml(_strip_fence(answer))
        except SpecError as exc:
            last = exc
            prompt += f"\n\nƏvvəlki cavabda xəta var: {exc}\nDüzəldib yenidən yaz."
    assert last is not None
    raise last


def _strip_fence(text: str) -> str:
    match = _FENCE.search(text)
    return match.group(1) if match else text
