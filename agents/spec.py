"""Agent təsviri (YAML) və doğrulama."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

# Təsdiq tələb edən (həssas) icazələr — bax: safety/gate.py
SENSITIVE_PERMISSIONS = frozenset({"spend_money", "external_account", "publish"})
KNOWN_PERMISSIONS = frozenset({"read_files", "write_files", "run_tests"}) | SENSITIVE_PERMISSIONS

# YAML açarı -> sahə adı
YAML_KEYS = {
    "ad": "name",
    "məqsəd": "purpose",
    "alətlər": "tools",
    "icazələr": "permissions",
    "giriş": "inputs",
    "çıxış": "outputs",
    "xərc_limiti": "cost_limit",
}
_FIELD_TO_KEY = {v: k for k, v in YAML_KEYS.items()}


class SpecError(ValueError):
    """Agent təsviri yanlışdır."""


@dataclass
class AgentSpec:
    name: str
    purpose: str
    tools: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    cost_limit: float = 0.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise SpecError("ad: kiçik latın hərfi ilə başlamalı, 3-32 simvol (a-z, 0-9, _)")
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise SpecError("məqsəd boş ola bilməz")
        for attr in ("tools", "permissions", "inputs", "outputs"):
            _check_str_list(attr, getattr(self, attr))
        unknown = set(self.permissions) - KNOWN_PERMISSIONS
        if unknown:
            raise SpecError(f"naməlum icazə: {sorted(unknown)}")
        if not self.inputs or not self.outputs:
            raise SpecError("giriş və çıxış boş ola bilməz")
        limit = self.cost_limit
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            raise SpecError("xərc_limiti rəqəm olmalıdır")
        if not math.isfinite(limit) or limit < 0:
            raise SpecError("xərc_limiti sonlu və mənfi olmayan rəqəm olmalıdır")
        self.cost_limit = float(limit)

    @classmethod
    def from_dict(cls, data: Any) -> "AgentSpec":
        if not isinstance(data, dict):
            raise SpecError("təsvir açar-dəyər xəritəsi olmalıdır")
        unknown = set(data) - set(YAML_KEYS)
        if unknown:
            raise SpecError(f"naməlum sahə: {sorted(map(str, unknown))}")
        missing = [k for k in YAML_KEYS if k not in data]
        if missing:
            raise SpecError(f"çatışmayan sahə: {missing}")
        return cls(**{YAML_KEYS[k]: v for k, v in data.items()})

    @classmethod
    def from_yaml(cls, text: str) -> "AgentSpec":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError(f"YAML xətası: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def load(cls, path: str | Path) -> "AgentSpec":
        return cls.from_yaml(Path(path).read_text(encoding="utf-8"))

    def to_yaml(self) -> str:
        data = {_FIELD_TO_KEY[f]: getattr(self, f) for f in _FIELD_TO_KEY}
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _check_str_list(attr: str, value: Any) -> None:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise SpecError(f"{_FIELD_TO_KEY[attr]} sətirlər siyahısı olmalıdır")
