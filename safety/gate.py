"""Təhlükəsizlik qapısı: icazə siyahısı, xərc limiti, təsdiq qapısı, audit, stop bayrağı.

Qərar sırası: stop bayrağı -> xərc düzgünlüyü -> icazə siyahısı -> xərc limiti
-> təsdiq (həssas icazələr). Hər qərar logs/audit.jsonl-ə yazılır.
Yoxlama uğursuz olarsa (məs. təsdiqləyici xəta atsa) əməl bloklanır (fail closed).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agents.spec import SENSITIVE_PERMISSIONS, AgentSpec

DEFAULT_AUDIT_PATH = Path("logs/audit.jsonl")
DEFAULT_STOP_PATH = Path("logs/STOP")
_EPS = 1e-9


@dataclass(frozen=True)
class Action:
    permission: str
    cost: float = 0.0
    description: str = ""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


# Təsdiqləyici: sahibin qərarını qaytarır. Hər əməl üçün ayrıca çağırılır.
Approver = Callable[[Action], bool]


class Gate:
    def __init__(
        self,
        spec: AgentSpec,
        audit_path: str | Path = DEFAULT_AUDIT_PATH,
        stop_path: str | Path = DEFAULT_STOP_PATH,
        approver: Approver | None = None,
    ) -> None:
        self.spec = spec
        self.audit_path = Path(audit_path)
        self.stop_path = Path(stop_path)
        self.approver = approver
        self.spent = 0.0

    # --- stop bayrağı ---
    def request_stop(self) -> None:
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text("stop\n", encoding="utf-8")

    def clear_stop(self) -> None:
        self.stop_path.unlink(missing_ok=True)

    def stopped(self) -> bool:
        return self.stop_path.exists()

    # --- əsas yoxlama ---
    def check(self, action: Action) -> Decision:
        decision = self._decide(action)
        self._audit(action, decision)  # audit yazılmasa istisna: əməl icazə almır
        if decision.allowed:
            self.spent += action.cost
        return decision

    def _decide(self, action: Action) -> Decision:
        if self.stopped():
            return Decision(False, "stop bayrağı aktivdir")
        cost = action.cost
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) \
                or not math.isfinite(cost) or cost < 0:
            return Decision(False, "xərc yanlışdır (sonlu və mənfi olmayan rəqəm olmalıdır)")
        if action.permission not in self.spec.permissions:
            return Decision(False, f"icazə siyahısında yoxdur: {action.permission}")
        if self.spent + cost > self.spec.cost_limit + _EPS:
            return Decision(
                False,
                f"xərc limiti aşılır: {self.spent + cost:.4f} > {self.spec.cost_limit:.4f}",
            )
        if action.permission in SENSITIVE_PERMISSIONS and not self._approved(action):
            return Decision(False, f"sahibin təsdiqi yoxdur: {action.permission}")
        return Decision(True, "icazə verildi")

    def _approved(self, action: Action) -> bool:
        if self.approver is None:
            return False
        try:
            return self.approver(action) is True
        except Exception:  # noqa: BLE001 — təsdiq alınmadısa bloklanır
            return False

    def _audit(self, action: Action, decision: Decision) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": self.spec.name,
            "permission": action.permission,
            "cost": action.cost if isinstance(action.cost, (int, float)) and math.isfinite(action.cost) else str(action.cost),
            "description": action.description,
            "allowed": decision.allowed,
            "reason": decision.reason,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
