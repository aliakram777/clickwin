#!/usr/bin/env python3
"""JVM kontrakt sınağı: HƏQİQİ Python backend (`CoreDevServer`: loopback, mock rejimi, müvəqqəti məlumat qovluğu, CORE API ilə) qurur və Kotlin qatını
`gradle test` ilə ona qarşı icra edir. APK build DEYİL. Heç bir real açar/hesab/ödəniş/xarici şəbəkə yoxdur; Gradle `--offline` ilə də işləyə bilər (asılılıqlar keşdə olduqda: `NAYMERA_GRADLE_OFFLINE=1`)."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("serve_core_dev", ROOT / "tools" / "serve_core_dev.py")
scd = importlib.util.module_from_spec(_spec)
sys.modules["serve_core_dev"] = scd
_spec.loader.exec_module(scd)

from kernel import Goal, GoalStatus, Origin  # noqa: E402
from core import StepState  # noqa: E402

PAIR_CODES = 24
APPROVALS = 4


def _seed(dev) -> dict[str, str]:
    """UNKNOWN vəziyyətli addım (sahib həll etməlidir): məqsəd -> aktiv -> plan -> addım icrası uğursuz/naməlum."""
    gid = "goal_" + hashlib.sha256(b"jvm-seed").hexdigest()[:16]
    dev.faces.owner.create_goal(Goal.new("JVM seed demo məqsəd", origin=Origin.OWNER, goal_id=gid), "jvm.seed.create")
    dev.faces.owner.transition_goal(gid, GoalStatus.ACTIVE, "jvm.seed.activate", 1)
    for _ in range(3):
        dev.loop.tick()
    plan = dev.faces.engine.current_plan(gid)
    step = plan.steps[0].step_id
    dev.faces.engine.begin_step(plan.plan_id, 1, step, "core.jvmseed01", 1, 60)
    dev.faces.engine.finish_step(plan.plan_id, 1, step, "core.jvmseed01", StepState.UNKNOWN, "", "", False, "executor_exception")
    return {"NAYMERA_SEED_GOAL": gid, "NAYMERA_SEED_PLAN": plan.plan_id, "NAYMERA_SEED_STEP": step}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        with scd.CoreDevServer(tmp, demo_loop=True, autorun_loop=False) as srv:
            codes = [srv.call(lambda d: d.services.auth.create_pairing_code()) for _ in range(PAIR_CODES)]
            approvals = [srv.call(lambda d, i=i: d.services.approvals.client.request("fin_agent", "spend_money", 0.5, f"jvm{i}", "JVM kontrakt sınağı", {"i": i}).id)
                         for i in range(APPROVALS)]
            seed = srv.call(_seed)
            env = dict(os.environ, NAYMERA_BACKEND_URL=srv.url, NAYMERA_PAIR_CODES=",".join(codes), NAYMERA_APPROVAL_IDS=",".join(approvals), **seed)
            cmd = ["gradle", *(["--offline"] if os.environ.get("NAYMERA_GRADLE_OFFLINE") else []), "--no-daemon", "-p", str(ROOT / "android" / "jvm-contract"), "cleanTest", "test"]
            return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
