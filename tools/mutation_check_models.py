#!/usr/bin/env python3
"""Mərhələ 18.1-E: models/ qatı üçün MUTASİYA yoxlaması. Əsas təhlükəsizlik yoxlamalarını (hər biri ayrıca) pozur və testlərin uğursuz olduğunu yoxlayır.

İSTİFADƏ: python tools/mutation_check_models.py [--only SUBSTR] [--list]
Repo-nun işçi nüsxəsinə TOXUNMUR: layihəni müvəqqəti qovluğa kopyalayır (`.git`, qorunan `.claude/skills/context-efficiency|delegate-reading`, keşlər xaric), mutantı yalnız kopyada tətbiq edir.
Çıxış kodu: 0 = bütün mutantlar öldürülüb; 1 = sağ qalan mutant (REAL BOŞLUQ sayılır: ekvivalentlik iddiası yalnız `EQUIVALENT` siyahısında yazılı sübutla mümkündür, hazırda siyahı BOŞDUR);
2 = köhnəlmiş mutant (mənbə dəyişib, mətn tapılmadı). Şəbəkə/real API YOXDUR."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ["tests/test_models_registry.py", "tests/test_models_adapters.py", "tests/test_models_router.py", "tests/test_models_runner.py", "tests/test_models_config.py",
         "tests/test_models_audit.py", "tests/test_paid_llm_entry_points.py"]
EXCLUDE = {".git", ".venv", "__pycache__", ".pytest_cache", "data", "logs"}
PROTECTED = {Path(".claude/skills/context-efficiency"), Path(".claude/skills/delegate-reading")}

# ad -> (fayl, köhnə mətn, yeni mətn)
M: dict[str, tuple[str, str, str]] = {}


def m(name: str, file: str, old: str, new: str) -> None:
    assert name not in M, name
    M[name] = (file, old, new)


REG, DESC, ROUT, RUN, CFG = "models/registry.py", "models/descriptor.py", "models/router.py", "runtime/brokered_models.py", "models/config.py"
BASE, ANTH, GEM, LOC, OAI = "models/adapters/base.py", "models/adapters/anthropic.py", "models/adapters/gemini.py", "models/adapters/local.py", "models/adapters/openai_compat.py"

# --- registry
m("reg.exact_type_to_isinstance", REG, "traits = _TRUSTED_ADAPTERS.get(type(adapter))", "traits = next((t for k, t in _TRUSTED_ADAPTERS.items() if isinstance(adapter, k)), None)")
m("reg.is_mock_check_off", REG, 'if getattr(adapter, "is_mock", None) is not traits.expected_is_mock:', "if False:")
m("reg.real_always", REG, "reality = Reality.REAL if enforces_endpoint_kind(transport, kind) else Reality.SIMULATED", "reality = Reality.REAL")
m("reg.endpoint_recheck_off", REG, "        validate_endpoint(base, kind)                  # həmişə yenidən", "        pass                  # həmişə yenidən")
m("reg.reverify_always_true", REG, "        return (locality, reality, model_name) == (desc.locality, desc.reality, desc.model_name)", "        return True")
m("reg.available_without_real", REG, "if available and reality is Reality.REAL and evidence:", "if available and evidence:")
m("reg.available_without_evidence", REG, "if available and reality is Reality.REAL and evidence:", "if available and reality is Reality.REAL:")
m("reg.duplicate_adapter_off", REG, "if any(a is adapter for _, a in self._items.values()):", "if False:")
m("reg.duplicate_id_off", REG, "            if model_id in self._items:", "            if False:")
m("reg.untrusted_runtime_off", REG, 'if type(getattr(adapter, "runtime", None)) not in TRUSTED_LOCAL_RUNTIMES:', "if False:")
m("reg.raw_stage17_trusted", REG, "    AnthropicProvider: _Traits(", "    __import__('agents.providers', fromlist=['x']).OpenAICompatibleProvider: _Traits('http', expected_is_mock=False),\n    AnthropicProvider: _Traits(")
m("reg.resolve_without_reverify", REG, "        if not self.reverify(model_id):\n            return None\n        desc, adapter", "        desc, adapter")
m("reg.disable_noop", REG, "desc = replace(item[0], availability=Availability.DISABLED)", "desc = item[0]")
m("reg.local_always_real", REG, "return Locality.LOCAL, Reality.SIMULATED, model_name              # real", "return Locality.LOCAL, Reality.REAL, model_name              # real")
m("reg.locality_always_local", REG, "locality = Locality.REMOTE if kind == \"cloud\" else Locality.LOCAL", "locality = Locality.LOCAL")
# --- descriptor
m("desc.remote_price_off", DESC, "if not (self.price_in_per_1k > 0 and self.price_out_per_1k > 0):", "if False:")
m("desc.remote_class_off", DESC, "            if self.max_data_class.rank > MAX_REMOTE_CLASS.rank:", "            if False:")
m("desc.local_class_off", DESC, "        elif self.max_data_class.rank > MAX_LOCAL_CLASS.rank:", "        elif False:")
m("desc.payment_mismatch_off", DESC, 'if data["payment"] != obj.payment.value:', "if False:")
m("desc.available_rule_off", DESC, "if self.reality is not Reality.REAL or not ev:", "if False:")
m("desc.mock_rule_off", DESC, "if self.reality is Reality.MOCK and (self.locality is not Locality.LOCAL or self.price_in_per_1k or self.price_out_per_1k):", "if False:")
m("desc.output_vs_context_off", DESC, "if self.max_output_tokens > self.context_tokens:", "if False:")
m("desc.long_context_off", DESC, "if ModelCapability.LONG_CONTEXT in caps and self.context_tokens < LONG_CONTEXT_MIN:", "if False:")
m("desc.rank_default_zero", DESC, '.get(self.value, 99)', '.get(self.value, 0)')
m("desc.payment_always_free", DESC, "return Payment.PAID if (self.price_in_per_1k > 0 or self.price_out_per_1k > 0) else Payment.FREE", "return Payment.FREE")
m("desc.usable_always", DESC, "return self.availability in (Availability.AVAILABLE, Availability.SIMULATION)", "return True")
# --- router
m("rt.reverify_off", ROUT, "if not registry.reverify(d.model_id):", "if False:")
m("rt.secret_scan_off", ROUT, "    if any(detect_secrets(t) for t in texts):", "    if False:")
m("rt.remote_class_ban_off", ROUT, "if eff in (DataClass.UNCLASSIFIED, DataClass.SECRET):", "if False:")
m("rt.class_vs_model_max_off", ROUT, "if _CLASS_RANK[eff] > _CLASS_RANK[d.max_data_class]:", "if False:")
m("rt.simulated_gate_off", ROUT, "if d.reality is not Reality.REAL and not allow_sim:", "if False:")
m("rt.budget_unknown_off", ROUT, '                if limit is None:\n                    reasons.append("budget_unknown")', '                if False:\n                    reasons.append("budget_unknown")')
m("rt.budget_zero_off", ROUT, "elif limit <= 0:", "elif False:")
m("rt.budget_exceeded_off", ROUT, "elif est_cost > limit:", "elif False:")
m("rt.invalid_cap_ignored", ROUT, "    return None if cap is None else min(headroom, cap)", "    return headroom if cap is None else min(headroom, cap)")
m("rt.cap_ignored", ROUT, "    if b.request_cap is None:\n        return headroom", "    if b.request_cap is None or True:\n        return headroom")
m("rt.remote_allow_off", ROUT, "                if not allow_remote:", "                if False:")
m("rt.consent_off", ROUT, '                reasons += [f"consent_missing:{c}" for c in needs if c not in consents]', "                pass")
m("rt.allow_remote_truthy", ROUT, "allow_remote, allow_sim = request.allow_remote is True, request.allow_simulated is True", "allow_remote, allow_sim = bool(request.allow_remote), bool(request.allow_simulated)")
m("rt.memory_floor_off", ROUT, "if memory_context and _CLASS_RANK[eff]", "if False and _CLASS_RANK[eff]")
m("rt.unhealthy_off", ROUT, "if d.model_id in bad:", "if False:")
m("rt.unavailable_off", ROUT, "if not d.usable:", "if False:")
m("rt.capability_off", ROUT, "missing = sorted(c.value for c in caps - set(d.capabilities))", "missing = []")
m("rt.context_fit_off", ROUT, "if est_in + out_cap > d.context_tokens:", "if False:")
m("rt.tiebreak_id_removed", ROUT, "est_cost, d.model_id)", "est_cost)")
m("rt.reality_rank_ignored", ROUT, "_REALITY_RANK[d.reality], 0 if d.locality", "0, 0 if d.locality")
m("rt.locality_rank_ignored", ROUT, "0 if d.locality is Locality.LOCAL else 1, est_cost", "0, est_cost")
m("rt.cost_rank_ignored", ROUT, "else 1, est_cost, d.model_id)", "else 1, 0, d.model_id)")
m("rt.prefer_ignored", ROUT, "key = (order.get(d.model_id, len(order)),", "key = (0,")
m("rt.rejected_unsorted", ROUT, "rej = tuple(sorted(rejected, key=lambda x: x.model_id))", "rej = tuple(rejected)")
m("rt.unclassified_rank_zero", ROUT, "DataClass.UNCLASSIFIED: 2, DataClass.SECRET: 99", "DataClass.UNCLASSIFIED: 0, DataClass.SECRET: 99")
m("rt.secret_rank_low", ROUT, "DataClass.UNCLASSIFIED: 2, DataClass.SECRET: 99", "DataClass.UNCLASSIFIED: 2, DataClass.SECRET: 2")
m("rt.byte_cost_estimate_chars", ROUT, 'est_in = sum(len(t.encode("utf-8", "replace")) for t in texts) + _INPUT_TOKEN_OVERHEAD', "est_in = sum(len(t) for t in texts) + _INPUT_TOKEN_OVERHEAD")
m("rt.output_clamp_off", ROUT, "out_cap = min(out_req, d.max_output_tokens)", "out_cap = out_req")
m("rt.fallbacks_all_selected", ROUT, "tuple(c for _, c in eligible[1:])", "tuple(c for _, c in eligible)")
m("rt.health_threshold_off", ROUT, "if n >= self._threshold:", "if False:")
m("rt.health_cooldown_ignored", ROUT, "if t > now}", "}")
m("rt.paid_free_budget_check_swapped", ROUT, "if d.payment is Payment.PAID:\n                if limit is None:", "if d.locality is Locality.REMOTE:\n                if limit is None:")
# --- adapters
m("ad.size_limit_off", BASE, "    if len(body) > limit:", "    if False:")
m("ad.recursion_off", BASE, "    except (ValueError, UnicodeDecodeError, RecursionError):", "    except (ValueError, UnicodeDecodeError):")
m("ad.surrogate_off", BASE, '        value.encode("utf-8")                                # tək', '        pass                                # tək')
m("ad.endpoint_recheck_off", BASE, '            validate_endpoint(self.base_url, "cloud")', "            pass")
m("ad.auth_status_off", BASE, "    if status in (401, 403):", "    if False:")
m("ad.5xx_charged_false", BASE, 'server xətası", charged=None', 'server xətası", charged=False')
m("ad.429_charged_unknown", BASE, 'sürət limiti", charged=False', 'sürət limiti", charged=None')
m("ad.secret_type_off", BASE, "        if not isinstance(api_key, Secret):", "        if False:")
m("ad.response_model_unvalidated", BASE, "return value if isinstance(value, str) and _MODEL_NAME.match(value) else fallback", "return value if isinstance(value, str) else fallback")
m("ad.token_bool_ok", BASE, "return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else fallback", "return v if isinstance(v, int) and v >= 0 else fallback")
m("ad.token_negative_ok", BASE, "return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else fallback", "return v if isinstance(v, int) and not isinstance(v, bool) else fallback")
m("ad.status_type_off", BASE, "    if isinstance(status, bool) or not isinstance(status, int):", "    if False:")
m("ad.response_type_off", BASE, "        if not isinstance(resp, HttpResponse):", "        if False:")
m("ad.timeout_to_notcharged", BASE, 'raise ProviderTimeout("provayder vaxt aşımı", charged=None, retryable=True)', 'raise ProviderTimeout("provayder vaxt aşımı", charged=False, retryable=True)')
m("ad.model_name_check_off", BASE, "    if not isinstance(model, str) or not _MODEL_NAME.match(model):", "    if False:")
m("ad.anthropic_type_off", ANTH, ' or b.get("type") != "text":', ":")
m("ad.anthropic_empty_content_ok", ANTH, "if not isinstance(blocks, list) or not blocks:", "if not isinstance(blocks, list):")
m("ad.gemini_part_type_off", GEM, ' or set(p) - {"text"} or "text" not in p:', ' or "text" not in p:')
m("ad.gemini_no_candidates_ok", GEM, "if not isinstance(cands, list) or not cands or not isinstance(cands[0], dict):", "if False:")
m("ad.local_runtime_trust_off", LOC, "    if type(runtime) not in TRUSTED_LOCAL_RUNTIMES:", "    if False:")
m("ad.local_loaded_off", LOC, "            if not self.runtime.loaded():", "            if False:")
m("ad.local_empty_reply_ok", LOC, "        if not text:", "        if False:")
m("ad.local_error_leak", LOC, 'raise LLMProviderError("lokal model icra xətası", charged=False, retryable=True) from None', "raise")
m("ad.openai_json_precheck_off", OAI, "            parse_json_body(resp)", "            pass")
m("ad.openai_surrogate_off", OAI, "        safe_text(out.text)", "        pass")
m("ad.openai_recursion_off", OAI, "        except RecursionError:", "        except ZeroDivisionError:")
# --- runner
m("run.nested_off", RUN, "        if in_broker():\n            return ModelRunResult(\"nested_run_denied\"", "        if False:\n            return ModelRunResult(\"nested_run_denied\"")
m("run.cross_agent_off", RUN, "            if b.owner_agent != self._agent.name:", "            if False:")
m("run.broker_required_off", RUN, "        if not in_broker():                                    # birbaşa", "        if False:                                    # birbaşa")
m("run.stale_plan_off", RUN, "if n == 0 and supplied is not None and supplied.plan_id != plan.plan_id:", "if False:")
m("run.plan_id_recompute_off", RUN, "if plan is not None and (not isinstance(plan, RoutePlan) or plan.plan_id != _recompute_plan_id(plan)):", "if False:")
m("run.view_armed_off", RUN, "        if not self._armed:", "        if False:")
m("run.view_reverify_off", RUN, "        if not self._registry.reverify(self._desc.model_id):", "        if False:")
m("run.descriptor_compare_off", RUN, "if d is None or (d.locality.value, d.reality.value, d.payment.value) != (cand.locality, cand.reality, cand.payment):", "if d is None:")
m("run.paid_block_after_unknown_off", RUN, '            if outcome in ("unknown_charge",) and d.payment.value == "paid":', "            if False:")
m("run.paid_unhealthy_off", RUN, '            if paid_blocked and d.payment.value == "paid":', "            if False:")
m("run.budget_unknown_unlimited", RUN, "                return BudgetSnapshot(None, None)             # ümumi tavan", "                return BudgetSnapshot(1e9, 1e9)             # ümumi tavan")
m("run.budget_error_unlimited", RUN, "        except Exception:                                      # noqa: BLE001  kitab oxunmadı: naməlum\n            return BudgetSnapshot(None, None)", "        except Exception:                                      # noqa: BLE001  kitab oxunmadı: naməlum\n            return BudgetSnapshot(1e9, 1e9)")
m("run.breaker_ignored", RUN, "headroom = 0.0 if self._port.breaker() is not None else", "headroom = 0.0 if False else")
m("run.consent_always", RUN, "if self._consents is not None and self._consents.granted(scope) is True:", "if True:")
m("run.ledger_none", RUN, "stop_path=self._broker.stop_path, ledger=self._port)", "stop_path=self._broker.stop_path, ledger=None)")
m("run.stop_path_none", RUN, "stop_path=self._broker.stop_path, ledger=self._port)", "stop_path=None, ledger=self._port)")
m("run.agent_limit_unbounded", RUN, "return float(min(base, pol.agent_limits.get(self._agent.name, base)) if pol is not None else base)", "return 1e9")
m("run.no_exclusion_after_failure", RUN, "            excluded[cand.model_id] = outcome", "            pass")
m("run.not_charged_for_unknown", RUN, 'return ("failed_not_charged" if exc.charged is False else "unknown_charge")', 'return ("failed_not_charged" if True else "unknown_charge")')
m("run.context_not_escaped", RUN, '    return ("\\n\\n".join(blocks) + "\\n\\n" + r.prompt) if blocks else r.prompt', '    return ("\\n\\n".join(b.text for b in r.context_blocks) + "\\n\\n" + r.prompt) if blocks else r.prompt')
m("run.idempotency_key_random", RUN, 'f"model:{request.op_id}", "models:run"', 'f"model:{request.op_id}:{time.time()}", "models:run"')
m("run.spend_scope_shared_agent", RUN, "scope = SpendScope(self._agent.name,", 'scope = SpendScope("llm_shared",')
m("run.broker_bypassed", RUN, "res = self._broker.run(effect, lambda: self._execute(request, composed, plan, cfg))",
  'res = (lambda: type("R", (), {"status": "ok", "output": (__import__("safety.execution_context", fromlist=["x"]).broker_context().__enter__(), self._execute(request, composed, plan, cfg))[1]})())()')
m("run.config_validate_off", RUN, "            config.validate_against(self._registry)", "            pass")
m("run.unknown_profile_ok", RUN, 'return "" if cfg is not None and cfg.profile(r.profile) is not None else "naməlum konfiqurasiya profili (fail-closed)"', 'return ""')
m("run.config_caps_override", RUN, "caps = tuple(dict.fromkeys(tuple(r.capabilities) + p.capabilities))", "caps = p.capabilities or r.capabilities")
m("run.config_prefer_ignored", RUN, "prefer = p.prefer + tuple(m for m in r.prefer if m not in p.prefer)", "prefer = r.prefer")
m("run.config_not_snapshotted", RUN, "            cfg = self._config                                   # run başlanğıcında snapshot: cəhdlər arasında dəyişmir", "            cfg = None")
m("run.simulated_flag_off", RUN, "d.locality.value, d.payment.value, d.reality is not Reality.REAL, float(res.cost))", "d.locality.value, d.payment.value, False, float(res.cost))")
m("run.view_is_mock_from_adapter", RUN, "        return self._desc.reality is Reality.MOCK", "        return bool(getattr(self._adapter, \"is_mock\", False))")
# --- config
m("cfg.forbidden_off", CFG, "        if bad:\n            raise ConfigError(f\"{where}", "        if False:\n            raise ConfigError(f\"{where}")
m("cfg.prefer_dupes_off", CFG, "if not prefer or len(set(prefer)) != len(prefer):", "if not prefer:")
m("cfg.prefer_empty_ok", CFG, "if not prefer or len(set(prefer)) != len(prefer):", "if len(set(prefer)) != len(prefer):")
m("cfg.unknown_model_off", CFG, "            if unknown:\n                raise ConfigError", "            if False:\n                raise ConfigError")
m("cfg.profile_limit_off", CFG, "if not isinstance(raw, dict) or not raw or len(raw) > MAX_PROFILES:", "if not isinstance(raw, dict) or not raw:")
m("cfg.version_off", CFG, 'if isinstance(data["schema_version"], bool) or data["schema_version"] != CONFIG_SCHEMA_VERSION:', "if False:")
m("cfg.cap_dupes_off", CFG, "if len(set(caps)) != len(caps):", "if False:")
m("cfg.forbidden_case_sensitive", CFG, "k.lower() in FORBIDDEN_KEYS", "k in FORBIDDEN_KEYS")
m("cfg.profile_lookup_any", CFG, "return next((p for p in self.profiles if p.name == name), None) if isinstance(name, str) else None", "return self.profiles[0] if self.profiles else None")

m("rt.unknown_class_to_public", ROUT, 'declared, note = DataClass.UNCLASSIFIED, "unknown_data_class"', 'declared, note = DataClass.PUBLIC, "unknown_data_class"')
m("rt.nonstr_class_to_public", ROUT, "declared = DataClass(declared) if isinstance(declared, str) else DataClass.UNCLASSIFIED", "declared = DataClass(declared) if isinstance(declared, str) else DataClass.PUBLIC")
m("rt.default_class_public", ROUT, "data_class: Any = DataClass.UNCLASSIFIED                 # defolt", "data_class: Any = DataClass.PUBLIC                 # defolt")
m("rt.text_size_cap_off", ROUT, "    if sum(len(t.encode(\"utf-8\", \"replace\")) for t in texts) > MAX_TEXT_BYTES:", "    if False:")
m("rt.output_range_off", ROUT, "or not 1 <= out_req <= MAX_OUTPUT_REQUEST:", "or False:")
m("rt.invalid_request_type_off", ROUT, "    if not isinstance(request, RouteRequest):", "    if False:")
m("run.default_agent_budget", RUN, "outputs=[\"netice\"], cost_limit=0.0)", "outputs=[\"netice\"], cost_limit=5.0)")
m("run.op_id_regex_off", RUN, "if not isinstance(r, ModelRunRequest) or not isinstance(r.op_id, str) or not _OP_RE.match(r.op_id):", "if not isinstance(r, ModelRunRequest):")
m("run.timeout_unbounded", RUN, "timeout_s=float(min(r.timeout_s, 120))", "timeout_s=float(r.timeout_s)")
m("run.timeout_validation_off", RUN, "or not 0 < r.timeout_s <= 300:", "or False:")
m("run.total_tokens_unbounded", RUN, "max_total_tokens=cand.est_input_tokens + cand.output_cap,", "max_total_tokens=10**9,")
m("run.refusal_hides_reasons", RUN, 'return ModelRunResult("refused", f"{plan.refusal_code}: {summary}"[:200], attempts=tuple(attempts), plan_id=plan.plan_id)', 'return ModelRunResult("refused", "", attempts=tuple(attempts), plan_id=plan.plan_id)')

# Ekvivalent mutantlar: yalnız yazılı sübutla. HAZIRDA BOŞ: heç bir sağ qalan mutant ekvivalent elan edilməyib.
EQUIVALENT: dict[str, str] = {}


def _copy_tree(dst: Path) -> None:
    def ignore(d: str, names: list[str]) -> set[str]:
        rel = Path(d).resolve().relative_to(ROOT)
        return {n for n in names if n in EXCLUDE or (rel / n) in PROTECTED}
    shutil.copytree(ROOT, dst, ignore=ignore, symlinks=True)


def _run(cwd: Path, tests: list[str], timeout: int = 600) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests], cwd=cwd, capture_output=True, text=True, timeout=timeout,
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
    with tempfile.TemporaryDirectory(prefix="naymera_mut_") as tmp:
        work = Path(tmp) / "repo"
        _copy_tree(work)
        ok, last = _run(work, TESTS)
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
                passed, last = _run(work, TESTS)
            except subprocess.TimeoutExpired:
                passed, last = False, "timeout (öldürülmüş sayılır: sonsuz dövr)"
            finally:
                path.write_text(original, encoding="utf-8")
            (survived if passed else killed).append(name)
            print(f"{'SURVIVED' if passed else 'killed  '} {name:42s} {last}")
        print(f"\nmutantlar: {len(names)}  öldürülüb: {len(killed)}  sağ qalan: {len(survived)}  köhnəlmiş: {len(stale)}")
        real = [n for n in survived if n not in EQUIVALENT]
        for n in survived:
            print(("EKVIVALENT (sübutla): " if n in EQUIVALENT else "REAL BOŞLUQ: ") + n + (f" — {EQUIVALENT[n]}" if n in EQUIVALENT else ""))
        if stale:
            return 2
        return 1 if real else 0


if __name__ == "__main__":
    raise SystemExit(main())
