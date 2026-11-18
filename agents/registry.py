"""Agent təsvirlərinin SQLite reyestri: saxla, oxu, yenilə."""
from __future__ import annotations

import re
import functools
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agents.spec import AgentSpec
from safety.dbconn import connect

DEFAULT_DB_PATH = Path("data/naymera.db")
DEPARTMENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name       TEXT PRIMARY KEY,
    department TEXT NOT NULL,
    spec_yaml  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _serial(fn):
    """Reyestr əməliyyatları (yaz + commit) bağlantı kilidi altında: ipler arası implicit tranzaksiya çarpazlaşması yoxdur (audit B-5)."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._conn.lock:
            return fn(self, *a, **k)
    return wrapper


class RegistryError(Exception):
    """Reyestr əməliyyatı mümkün deyil (təkrar, tapılmadı, yanlış şöbə)."""


@dataclass(frozen=True)
class RegisteredAgent:
    spec: AgentSpec
    department: str
    created_at: str
    updated_at: str


class Registry:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect(str(db_path), isolation_level="")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @_serial
    def add(self, spec: AgentSpec, department: str) -> None:
        _check_department(department)
        spec.validate()
        now = _now()
        try:
            self._conn.execute(
                "INSERT INTO agents (name, department, spec_yaml, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (spec.name, department, spec.to_yaml(), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise RegistryError(f"agent artıq mövcuddur: {spec.name}") from exc
        self._conn.commit()

    @_serial
    def update(self, spec: AgentSpec, department: str | None = None) -> None:
        spec.validate()
        if department is not None:
            _check_department(department)
        current = self.get(spec.name)
        self._conn.execute(
            "UPDATE agents SET department = ?, spec_yaml = ?, updated_at = ? WHERE name = ?",
            (department or current.department, spec.to_yaml(), _now(), spec.name),
        )
        self._conn.commit()

    @_serial
    def get(self, name: str) -> RegisteredAgent:
        row = self._conn.execute(
            "SELECT spec_yaml, department, created_at, updated_at FROM agents WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise RegistryError(f"agent tapılmadı: {name}")
        return _record(row)

    @_serial
    def list(self, department: str | None = None) -> list[RegisteredAgent]:
        sql = "SELECT spec_yaml, department, created_at, updated_at FROM agents"
        params: tuple = ()
        if department is not None:
            sql += " WHERE department = ?"
            params = (department,)
        rows = self._conn.execute(sql + " ORDER BY name", params).fetchall()
        return [_record(r) for r in rows]


def _record(row: tuple) -> RegisteredAgent:
    return RegisteredAgent(AgentSpec.from_yaml(row[0]), row[1], row[2], row[3])


def _check_department(department: str) -> None:
    if not isinstance(department, str) or not DEPARTMENT_RE.match(department):
        raise RegistryError("şöbə: kiçik latın hərfi ilə başlamalı, 2-32 simvol (a-z, 0-9, _)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
