"""Yaranan agenti test tapşırığı ilə yoxlayır; uğursuzluğu logs/errors.jsonl-ə yazır."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agents.spec import AgentSpec, SpecError

DEFAULT_ERRORS_PATH = Path("logs/errors.jsonl")

Runner = Callable[[AgentSpec, dict], Any]


@dataclass
class VerifyTask:
    name: str
    input: dict
    check: Callable[[Any], bool]


@dataclass
class VerifyResult:
    ok: bool
    stage: str = ""
    error: str = ""


def verify(
    spec: AgentSpec,
    task: VerifyTask,
    runner: Runner,
    errors_path: str | Path = DEFAULT_ERRORS_PATH,
) -> VerifyResult:
    result = _run(spec, task, runner)
    if not result.ok:
        log_error(Path(errors_path), spec.name, task.name, result)
    return result


def _run(spec: AgentSpec, task: VerifyTask, runner: Runner) -> VerifyResult:
    try:
        spec.validate()
    except SpecError as exc:
        return VerifyResult(False, "spec", str(exc))

    missing_in = [k for k in spec.inputs if k not in task.input]
    if missing_in:
        return VerifyResult(False, "input", f"tapşırıqda giriş yoxdur: {missing_in}")

    try:
        output = runner(spec, dict(task.input))
    except Exception as exc:  # noqa: BLE001 — agent xətası jurnala düşməlidir
        return VerifyResult(False, "run", f"{type(exc).__name__}: {exc}")

    if not isinstance(output, dict):
        return VerifyResult(False, "output", "çıxış xəritə (dict) deyil")
    missing_out = [k for k in spec.outputs if k not in output]
    if missing_out:
        return VerifyResult(False, "output", f"elan olunan çıxış yoxdur: {missing_out}")

    try:
        passed = bool(task.check(output))
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(False, "check", f"yoxlama xətası: {type(exc).__name__}: {exc}")
    if not passed:
        return VerifyResult(False, "check", "çıxış gözlənilənə uyğun deyil")
    return VerifyResult(True)


def log_error(path: Path, agent: str, task: str, result: VerifyResult) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "task": task,
        "stage": result.stage,
        "error": result.error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
