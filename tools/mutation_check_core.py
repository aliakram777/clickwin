#!/usr/bin/env python3
"""Mərhələ 18.2-B: `core/` və `runtime/brokered_core.py` üçün MUTASİYA yoxlaması. Hər əsas təhlükəsizlik yoxlaması ayrıca pozulur; testlər uğursuz OLMALIDIR.

İSTİFADƏ: python tools/mutation_check_core.py [--only SUBSTR] [--list]
İşçi nüsxəyə toxunmur: layihə müvəqqəti qovluğa kopyalanır (`.git`, qorunan `.claude/skills/context-efficiency|delegate-reading`, keşlər xaric), mutant yalnız kopyada tətbiq olunur.
Çıxış: 0 = hamısı öldürülüb; 1 = sağ qalan mutant (REAL BOŞLUQ; ekvivalentlik yalnız `EQUIVALENT`-də yazılı sübutla, hazırda BOŞDUR); 2 = köhnəlmiş mutant və ya baza uğursuzluğu."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ["tests/test_core_store.py", "tests/test_core_loop.py", "tests/test_core_brokered.py", "tests/test_core_audit.py"]
EXCLUDE = {".git", ".venv", "__pycache__", ".pytest_cache", "data", "logs"}
PROTECTED = {Path(".claude/skills/context-efficiency"), Path(".claude/skills/delegate-reading")}
M: dict[str, tuple[str, str, str]] = {}


def m(name: str, file: str, old: str, new: str) -> None:
    assert name not in M, name
    M[name] = (file, old, new)


ST, LP, VA, BR, PT = "core/store.py", "core/loop.py", "core/validator.py", "runtime/brokered_core.py", "core/ports.py"
# --- store
m("st.engine_goal_matrix_off", ST, "if actor is Actor.ENGINE and (goal.status, to) not in ENGINE_GOAL:", "if False:")
m("st.engine_plan_matrix_off", ST, "if actor is Actor.ENGINE and (plan.status, to) not in ENGINE_PLAN:", "if False:")
m("st.ready_hash_off", ST, "if to is PlanStatus.READY and actor is Actor.ENGINE and validated_hash != plan.content_hash:", "if False:")
m("st.complete_needs_steps_off", ST, "if not rows or any(r[0] != StepState.SUCCEEDED.value for r in rows) or (actor is Actor.ENGINE and any(not r[1] for r in rows)):", "if False:")
m("st.engine_complete_needs_verified_off", ST, "or (actor is Actor.ENGINE and any(not r[1] for r in rows)):", ":")
m("st.new_goal_draft_only_off", ST, "if not isinstance(goal, Goal) or goal.status is not GoalStatus.DRAFT:", "if not isinstance(goal, Goal):")
m("st.plan_needs_active_goal_off", ST, "if goal.status is not GoalStatus.ACTIVE:\n                raise NotPermitted(\"plan yalnız", "if False:\n                raise NotPermitted(\"plan yalnız")
m("st.single_active_plan_off", ST, "                if p.status in _ACTIVE_PLAN:\n                    raise NotPermitted", "                if False:\n                    raise NotPermitted")
m("st.new_plan_draft_v1_off", ST, "if not isinstance(plan, Plan) or plan.status is not PlanStatus.DRAFT or plan.version != 1:", "if not isinstance(plan, Plan):")
m("st.goal_stale_version_off", ST, "            if version != expected_version:\n                raise StaleVersion()\n            if actor is Actor.ENGINE and (goal.status, to)", "            if False:\n                raise StaleVersion()\n            if actor is Actor.ENGINE and (goal.status, to)")
m("st.plan_stale_version_off", ST, "            if version != expected_version:\n                raise StaleVersion()\n            if actor is Actor.ENGINE and (plan.status, to)", "            if False:\n                raise StaleVersion()\n            if actor is Actor.ENGINE and (plan.status, to)")
m("st.op_conflict_off", ST, "if (row[0], row[1], row[2]) != (kind, subject, content_hash):", "if False:")
m("st.begin_version_check_off", ST, "if cur_version != version or plan.status not in (PlanStatus.READY, PlanStatus.EXECUTING):", "if False:")
m("st.begin_attempt_check_off", ST, "or int(row[1]) + 1 != attempt:", ":")
m("st.begin_state_check_off", ST, "if row is None or row[0] not in (StepState.PENDING.value, StepState.AWAITING_APPROVAL.value) or int(row[1]) + 1 != attempt:", "if row is None or int(row[1]) + 1 != attempt:")
m("st.recover_inverted", ST, "AND lease_until IS NOT NULL AND lease_until < ?", "AND lease_until IS NOT NULL AND lease_until > ?")
m("st.recover_all_running", ST, "AND lease_until IS NOT NULL AND lease_until < ?", "AND (lease_until IS NOT NULL OR 1=1) AND ? IS NOT NULL")
m("st.resolve_owner_only_off", ST, "        if actor is not Actor.OWNER:\n            raise NotPermitted(\"addımı", "        if False:\n            raise NotPermitted(\"addımı")
m("st.revise_owner_only_off", ST, "        if actor is not Actor.OWNER:\n            raise NotPermitted(\"plan yeniləməsi", "        if False:\n            raise NotPermitted(\"plan yeniləməsi")
m("st.resolve_ref_check_off", ST, "or not isinstance(approval_ref, str) or not _APPROVAL_REF.match(approval_ref):", "or False:")
m("st.secret_redaction_off", ST, "    except SecretDetected:", "    except ZeroDivisionError:")
m("st.goal_integrity_off", ST, "if goal.content_hash != row[2] or goal.goal_id != goal_id:", "if False:")
m("st.plan_integrity_off", ST, "if plan.content_hash != row[2] or plan.plan_id != plan_id or plan.version != row[0]:", "if False:")
m("st.journal_update_trigger_off", ST, "BEFORE UPDATE ON journal BEGIN", "BEFORE UPDATE ON journal WHEN 1=0 BEGIN")
m("st.journal_delete_trigger_off", ST, "BEFORE DELETE ON journal BEGIN", "BEFORE DELETE ON journal WHEN 1=0 BEGIN")
m("st.output_sha_trusted", ST, 'output_sha = output_sha if isinstance(output_sha, str) and re.fullmatch(r"[0-9a-f]{64}", output_sha) else digest', "output_sha = output_sha")
m("st.schema_version_off", ST, "if version > STORE_SCHEMA_VERSION:", "if False:")
m("st.finish_requires_running_off", ST, "AND state = ? AND op_id = ?\",\n                                   (state.value, summary, output_sha, 1 if verified else 0, error, self._ts(), plan_id, version, step_id, StepState.RUNNING.value, op_id))", "AND state != ? AND op_id = ?\",\n                                   (state.value, summary, output_sha, 1 if verified else 0, error, self._ts(), plan_id, version, step_id, StepState.RUNNING.value, op_id))")
m("st.finish_op_check_off", ST, "AND state = ? AND op_id = ?\",\n                                   (state.value, summary, output_sha, 1 if verified else 0, error, self._ts(), plan_id, version, step_id, StepState.RUNNING.value, op_id))", "AND state = ? AND op_id != ?\",\n                                   (state.value, summary, output_sha, 1 if verified else 0, error, self._ts(), plan_id, version, step_id, StepState.RUNNING.value, op_id))")
m("st.op_id_validation_off", ST, "    if not isinstance(op_id, str) or not _OP.match(op_id):", "    if False:")
m("st.outbox_not_flushed_marked", ST, '"UPDATE outbox SET flushed = 1 WHERE seq = ?"', '"UPDATE outbox SET flushed = 0 WHERE seq = ?"')
m("st.summary_unbounded", ST, "clean_text(\"summary\", raw[:SUMMARY_MAX], max_len=SUMMARY_MAX, multiline=True)", "raw")
# --- loop
m("lp.stop_at_tick_off", LP, "        if self._stop():                                          # STOP üstündür", "        if False:                                          # STOP üstündür")
m("lp.stop_after_planner_off", LP, "        if self._stop():                                            # planlaşdırıcı işləyərkən", "        if False:                                            # planlaşdırıcı işləyərkən")
m("lp.stop_before_planner_off", LP, "        if self._stop():\n            return TickResult(\"halted_stop\", gid)\n        try:\n            plan = self._planner.propose(goal)", "        if False:\n            return TickResult(\"halted_stop\", gid)\n        try:\n            plan = self._planner.propose(goal)")
m("lp.stop_in_validate_off", LP, "    def _validate(self, goal: Goal, plan: Plan) -> TickResult:\n        if self._stop():", "    def _validate(self, goal: Goal, plan: Plan) -> TickResult:\n        if False:")
m("lp.stop_before_approval_off", LP, "        if self._stop():\n            return TickResult(\"halted_stop\", gid, pid, step.step_id)\n        state = self._approvals.ensure(req)", "        if False:\n            return TickResult(\"halted_stop\", gid, pid, step.step_id)\n        state = self._approvals.ensure(req)")
m("lp.stop_before_execute_off", LP, "        if self._stop():                                            # təsdiqlənmiş addım da STOP altında", "        if False:                                            # təsdiqlənmiş addım da STOP altında")
m("lp.autonomy_unchecked", LP, "self._level = Autonomy(autonomy)", "self._level = Autonomy.L1 if str(autonomy) in ('L2', 'L3') else Autonomy(autonomy)")
m("lp.l0_executor_allowed", LP, "if self._level is Autonomy.L0 and (executor is not None or approvals is not None or verifier is not None):", "if False:")
m("lp.l1_ports_not_required", LP, "if self._level is Autonomy.L1 and (executor is None or approvals is None):", "if False:")
m("lp.stop_callable_unchecked", LP, "if not callable(stop):", "if False:")
m("lp.l0_executes", LP, "        if self._level is Autonomy.L0:\n            return TickResult(\"awaiting_owner_review\"", "        if False:\n            return TickResult(\"awaiting_owner_review\"")
m("lp.supports_check_off", LP, "        ok, why = self._executor.supports(step)\n        if not ok or", "        ok, why = True, \"\"\n        if False or")
m("lp.sensitive_perm_check_off", LP, "or set(step.required_permissions) - ALLOWED_STEP_PERMISSIONS or step.cost_estimate_azn > 0:", "or step.cost_estimate_azn > 0:")
m("lp.paid_check_off", LP, "or set(step.required_permissions) - ALLOWED_STEP_PERMISSIONS or step.cost_estimate_azn > 0:", "or set(step.required_permissions) - ALLOWED_STEP_PERMISSIONS:")
m("lp.approval_skipped", LP, "        state = self._approvals.ensure(req)\n", "        state = ApprovalState(\"approved\", \"forged\")\n")
m("lp.approved_check_off", LP, "        if state.status != \"approved\":", "        if False:")
m("lp.denied_check_off", LP, "        if state.status == \"denied\":", "        if False:")
m("lp.dependency_check_off", LP, "and all(by_id[d].state is StepState.SUCCEEDED for d in s.depends_on)), None)", "), None)")
m("lp.unknown_detection_off", LP, "if any(s.state is StepState.UNKNOWN for s in steps):", "if False:")
m("lp.executor_exception_propagates", LP, "        except Exception:                                           # noqa: BLE001  yan təsir istisna olunmur", "        except ZeroDivisionError:                                           # noqa: BLE001  yan təsir istisna olunmur")
m("lp.bad_outcome_check_off", LP, "if not isinstance(outcome, StepOutcome) or outcome.status not in _OUTCOMES:", "if False:")
m("lp.verification_failure_ignored", LP, "            if not verified:\n                state, error = StepState.FAILED, \"verification_failed\"", "            if False:\n                state, error = StepState.FAILED, \"verification_failed\"")
m("lp.verifier_error_ok", LP, "            except Exception:                                       # noqa: BLE001\n                verified = False", "            except Exception:                                       # noqa: BLE001\n                verified = True")
m("lp.plan_completes_without_verifier", LP, "if plan.status is PlanStatus.EXECUTING and self._verifier is not None and all(s.verified for s in steps):", "if plan.status is PlanStatus.EXECUTING:")
m("lp.goal_replan_after_completion", LP, "        if any(p.status is PlanStatus.COMPLETED for p in history):", "        if False:")
m("lp.invalid_plan_ready", LP, "        if res.ok:\n            self._store.transition_plan(plan.plan_id, PlanStatus.READY", "        if True:\n            self._store.transition_plan(plan.plan_id, PlanStatus.READY")
m("lp.replan_limit_off", LP, "> self._max_replans:\n            return self._block_goal(goal, version, \"replan_limit\")", "> 10**9:\n            return self._block_goal(goal, version, \"replan_limit\")")
m("lp.planner_failure_limit_off", LP, "        if n > self._max_replans:\n            return self._block_goal(goal, version, why)", "        if False:\n            return self._block_goal(goal, version, why)")
m("lp.op_id_without_attempt", LP, 'f"{pid}:{plan.version}:{step.step_id}:{attempt}"', 'f"{pid}:{plan.version}:{step.step_id}"')
m("lp.failed_step_not_blocking", LP, "        bad = next((s for s in steps if s.state in (StepState.FAILED, StepState.BLOCKED)), None)", "        bad = None")
m("lp.priority_ignored", LP, "_PRIORITY_RANK.get(gv[0].priority.value, 9), gv[0].created_at", "0, gv[0].created_at")
m("lp.planner_goal_check_off", LP, "if not isinstance(plan, Plan) or plan.goal_id != gid or plan.status is not PlanStatus.DRAFT or plan.version != 1:", "if not isinstance(plan, Plan):")
m("lp.planner_status_check_off", LP, "or plan.status is not PlanStatus.DRAFT or plan.version != 1:", ":")
m("lp.begin_step_ignored", LP, "        if not self._store.begin_step(pid, plan.version, step.step_id, op_id, attempt, self._lease):\n            return TickResult(\"step_race\", gid, pid, step.step_id)", "        self._store.begin_step(pid, plan.version, step.step_id, op_id, attempt, self._lease)")
m("lp.stop_flag_ignored_in_run", LP, "            if not r.progress:\n                break", "            if False:\n                break")
m("lp.run_unbounded", LP, "for _ in range(max(1, min(int(max_ticks), MAX_RUN_TICKS))):", "for _ in range(int(max_ticks)):")
m("lp.only_active_goals_off", LP, "return sorted(self._store.list_goals(GoalStatus.ACTIVE, 500)", "return sorted(self._store.list_goals(None, 500)")
# --- validator
m("va.sensitive_off", VA, "if set(s.required_permissions) - ALLOWED_STEP_PERMISSIONS:", "if False:")
m("va.paid_off", VA, "if s.cost_estimate_azn > 0:", "if False:")
m("va.no_capability_off", VA, "if s.capability_id is None:", "if False:")
m("va.supports_off", VA, "            elif self._supports is not None:", "            elif False:")
m("va.goal_active_off", VA, "if goal.status is not GoalStatus.ACTIVE:", "if False:")
m("va.goal_mismatch_off", VA, "if plan.goal_id != goal.goal_id:", "if False:")
m("va.too_many_off", VA, "if len(plan.steps) > self._max:", "if False:")
# --- brokered adapter
m("br.consume_skipped", BR, "            used = self._client.consume(approval_ref, digest.hash)", "            used = type('O', (), {'ok': True, 'reason': ''})()")
m("br.consume_result_ignored", BR, "            if not used.ok:\n                raise ApprovalUnusable", "            if False:\n                raise ApprovalUnusable")
m("br.mock_accepted", BR, "if not (ticket.human_approved or self._accept_mock) or not self._client.check(ticket.id, digest.hash).ok:", "if not self._client.check(ticket.id, digest.hash).ok:")
m("br.check_skipped", BR, "or not self._client.check(ticket.id, digest.hash).ok:", "or False:")
m("br.sensitive_registration", BR, "if permission not in ALLOWED_STEP_PERMISSIONS:", "if False:")
m("br.seal_off", BR, "        if self._sealed:\n            raise RuntimeError", "        if False:\n            raise RuntimeError")
m("br.dup_capability_off", BR, "if not callable(handler) or capability_id in self._h:", "if not callable(handler):")
m("br.supports_permission_off", BR, "        if perm not in self._spec.permissions:\n            return False, \"agent_lacks_permission\"", "        if False:\n            return False, \"agent_lacks_permission\"")
m("br.duplicate_to_success", BR, "            return StepOutcome(\"unknown\", error=\"duplicate_op_id\")", "            return StepOutcome(\"succeeded\", error=\"duplicate_op_id\")")
m("br.blocked_to_success", BR, "            return StepOutcome(\"blocked\", error=f\"broker_blocked:{res.reason}\"[:150])", "            return StepOutcome(\"succeeded\")")
m("br.unknown_to_failed", BR, "        return StepOutcome(\"unknown\", error=\"executor_error\")", "        return StepOutcome(\"failed\", error=\"executor_error\")")
m("br.handler_failed_to_unknown", BR, "            return StepOutcome(\"failed\", error=\"handler_failed\")", "            return StepOutcome(\"unknown\", error=\"handler_failed\")")
m("br.unusable_to_failed", BR, "            return StepOutcome(\"blocked\", error=\"approval_unusable\")", "            return StepOutcome(\"failed\", error=\"approval_unusable\")")
m("br.broker_bypassed", BR, "        res = self._broker.run(effect, run)", "        res = type('R', (), {'status': 'ok', 'output': run(), 'reason': ''})()")
m("br.cost_nonzero", BR, "effect = Effect(self._spec, perm, 0.0, request.op_id", "effect = Effect(self._spec, perm, 1.0, request.op_id")
m("br.approval_unavailable_as_approved", BR, "        except Exception:                                          # noqa: BLE001  təsdiq xidməti əlçatmaz: fail-closed\n            return ApprovalState(\"unavailable\")", "        except Exception:                                          # noqa: BLE001  təsdiq xidməti əlçatmaz: fail-closed\n            return ApprovalState(\"approved\", \"x\")")
m("br.cost_unknown_on_known_failure", BR, "        super().__init__(message, cost=0.0)\n\n\nclass ApprovalUnusable", "        super().__init__(message)\n\n\nclass ApprovalUnusable")
# --- ports
m("pt.l2_member", PT, "    L1 = \"L1\"          # hər addım", "    L2 = \"L2\"\n    L1 = \"L1\"          # hər addım")
m("pt.allowed_perms_widened", PT, 'ALLOWED_STEP_PERMISSIONS = frozenset({"read_files", "write_files", "run_tests"})', 'ALLOWED_STEP_PERMISSIONS = frozenset({"read_files", "write_files", "run_tests", "spend_money", "publish"})')
m("pt.payload_without_hashes", PT, '"plan_hash": self.plan_hash, "step_id": self.step.step_id,\n                "step_hash": self.step_hash,', '"plan_hash": "", "step_id": self.step.step_id,\n                "step_hash": "",')

EQUIVALENT: dict[str, str] = {}


def _copy_tree(dst: Path) -> None:
    def ignore(d: str, names: list[str]) -> set[str]:
        rel = Path(d).resolve().relative_to(ROOT)
        return {n for n in names if n in EXCLUDE or (rel / n) in PROTECTED}
    shutil.copytree(ROOT, dst, ignore=ignore, symlinks=True)


def _run(cwd: Path, timeout: int = 600) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(cwd)})
    return p.returncode == 0, (p.stdout.strip().splitlines() or [""])[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    names = [n for n in M if args.only in n]
    if args.list:
        print("\n".join(names))
        return 0
    with tempfile.TemporaryDirectory(prefix="naymera_mut_core_") as tmp:
        work = Path(tmp) / "repo"
        _copy_tree(work)
        ok, last = _run(work)
        if not ok:
            print(f"BAZA XƏTASI: mutasiyasız testlər keçmir ({last})")
            return 2
        killed, survived, stale = [], [], []
        for name in names:
            file, old, new = M[name]
            path = work / file
            original = path.read_text(encoding="utf-8")
            if old not in original:
                stale.append(name)
                print(f"STALE    {name}")
                continue
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                passed, last = _run(work)
            except subprocess.TimeoutExpired:
                passed, last = False, "timeout (öldürülmüş sayılır: sonsuz dövr)"
            finally:
                path.write_text(original, encoding="utf-8")
            (survived if passed else killed).append(name)
            print(f"{'SURVIVED' if passed else 'killed  '} {name:42s} {last}")
        print(f"\nmutantlar: {len(names)}  öldürülüb: {len(killed)}  sağ qalan: {len(survived)}  köhnəlmiş: {len(stale)}")
        for n in survived:
            print(("EKVIVALENT (sübutla): " if n in EQUIVALENT else "REAL BOŞLUQ: ") + n + (f" — {EQUIVALENT[n]}" if n in EQUIVALENT else ""))
        if stale:
            return 2
        return 1 if [n for n in survived if n not in EQUIVALENT] else 0


if __name__ == "__main__":
    raise SystemExit(main())
