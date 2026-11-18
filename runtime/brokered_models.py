"""Mərhələ 18.1-D: `plan_route` qərarını Effect Broker -> GuardedLLM -> vahid CostLedger zəncirində TƏHLÜKƏSİZ icra edir. YALNIZ oflayn/fixture adapterlər (real API/açar/şəbəkə yoxdur).

Mühafizə qatları (heç biri tək başına sərhəd sayılmır):
1. Hər icra `EffectBroker.run` daxilindədir (stop bayrağı, təkrar `op_id`, Gate icazəsi/audit, ledger). Executor yalnız Broker kontekstində işləyir; iç-içə `run` (başqa effektin daxilindən) rədd edilir.
2. `plan_route` hər cəhddə İCRA ANINDA yenidən qurulur (təzə büdcə/sağlamlıq/razılıq snapshot-ları); `plan_id` imza/icazə DEYİL — kənardan verilən plan yalnız təzə planla müqayisə
   üçündür, uyğunsuzluqda (köhnəlmiş/saxta) icra OLMUR; icra həmişə TƏZƏ planın seçiminə əsaslanır.
3. Provider yenidən yoxlanır (`ModelRegistry.resolve_for_router` -> reverify) və planın namizədi ilə (locality/reality/payment) müqayisə olunur. `GuardedLLM`-ə verilən provider təkcə
   reyestr statusunu göstərən `_TrustedProviderView`-dur (is_mock/name/model reyestrdən; adapterin öz elanından DEYİL) və yalnız runner tərəfindən silahlanan pəncərədə çağırıla bilər.
4. Xərc: yalnız bu runner-in Broker-in TƏK kitabından qurduğu `LedgerPort` ilə, `SpendScope` (agentin adına, sabit `op_id`) + `GuardedLLM` rezervi. Kitab/qlobal tavan naməlumdursa ödənişli provider çağırılmır.
5. Fallback AVTOMATİK İCAZƏ DEYİL: hər cəhd yeni plandır (bütün qapılar təkrarlanır). Nəticəsi naməlum ödənişli cəhddən (timeout/yarımçıq/pozulmuş cavab/haqqı naməlum xəta) sonra HEÇ BİR ödənişli provider
   yenidən çağırılmır (ikiqat xərc yoxdur); yalnız ödənişsiz lokal fallback mümkündür.
6. Agent konteksti: başqa agentə aid kontekst bloku aşkar edilərsə icra başlamır.
HƏDD (açıq): Broker konteksti və `_TrustedProviderView` pəncərəsi proses-daxili vəziyyətdir; eyni prosesdə ixtiyari kod icra edə bilən tərəfə qarşı OS sərhədi DEYİL (agent kodu ayrı prosesdədir).
Köhnə yol qeydi: Stage 17-nin xam `OpenAICompatibleProvider` sinfi (build_app/env yolu) dəyişdirilməyib və bu yeni etibarlı yolda İSTİFADƏ OLUNMUR; onun RecursionError, tək surrogat və cavab-ölçü
boşluqları köhnə yolda QALIR (yeni yol `models.adapters.openai_compat.OpenAICompatProvider` ilə sərtləşdirilib)."""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from agents.llm import (GuardedLLM, LLMBadRequest, LLMBlocked, LLMBudgetExceeded, LLMBudgetOverrun, LLMError, LLMProviderError, LLMRequest, LLMResponse, LLMConfig, LLMTooLarge,
                        LLMUnavailable, SpendScope, spend_scope)
from agents.spec import AgentSpec
from kernel.common import canonical_json, render_untrusted, sha256_hex
from models.config import ConfigError, ModelRoutingConfig
from models.descriptor import DataClass, ModelCapability, ModelDescriptor, Reality
from models.registry import ModelRegistry
from agents.local_models import DeviceState, ResourceLimits
from models.router import CONSENT_LOCAL, CONSENT_PAID, CONSENT_TRANSFER, BudgetSnapshot, HealthTracker, ResourceSnapshot, RoutePlan, RouteRequest, plan_route
from safety.broker import Effect, EffectBroker
from safety.execution_context import in_broker
from safety.ledger import LedgerPort

_OP_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
MAX_ATTEMPTS = 8

SYSTEM_MODEL_SPEC = AgentSpec(name="naymera_models", purpose="Model marşrutu: plan_route qərarının Broker nəzarəti ilə icrası", tools=[], permissions=["read_files"],
                              inputs=["sorgu"], outputs=["netice"], cost_limit=0.0)         # defolt büdcə 0: ödənişli provider yoxdur; sahib agent spec-ini açıq verir


class ConsentView(Protocol):
    def granted(self, scope: str) -> bool: ...


@dataclass(frozen=True)
class ContextBlock:
    owner_agent: str                 # bu kontekst hansı agentə aiddir (başqa agentin konteksti istifadə edilə bilməz)
    text: str


@dataclass(frozen=True)
class ModelRunRequest:
    op_id: str
    prompt: str
    system: str = ""
    max_output_tokens: int = 256
    capabilities: tuple[ModelCapability, ...] = (ModelCapability.TEXT_GENERATION,)
    data_class: Any = DataClass.UNCLASSIFIED
    context_blocks: tuple[ContextBlock, ...] = ()
    allow_remote: bool = False
    allow_simulated: bool = False
    timeout_s: float = 30.0
    prefer: tuple[str, ...] = ()
    profile: str = ""                # konfiqurasiya profili (yalnız sıra + əlavə capability); boşdursa yalnız `prefer`


@dataclass(frozen=True)
class Attempt:
    model_id: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class ModelRunResult:
    status: str                      # ok | refused | blocked | duplicate | error | stale_plan | invalid_plan | invalid_request | cross_agent_context | nested_run_denied | broker_required
    reason: str = ""
    text: str = ""                   # ETİBARSIZ model çıxışı (məlumat, təlimat deyil)
    model_id: str = ""
    provider: str = ""
    locality: str = ""
    payment: str = ""
    simulated: bool = False          # reality != REAL: real xidmət kimi göstərilmir
    cost: float = 0.0                # ledger vahidi
    attempts: tuple[Attempt, ...] = ()
    plan_id: str = ""

    UNTRUSTED_TEXT = True
    GRANTS_EXECUTION = False


class _TrustedProviderView:
    """GuardedLLM-in gördüyü provider: status reyestrdən (adapterin elanından deyil). Yalnız runner `_arm()` ilə silahlandırdıqda çağırıla bilər və hər çağırışda reyestr yenidən yoxlanır."""
    kind = "view"

    def __init__(self, registry: ModelRegistry, desc: ModelDescriptor, adapter: Any) -> None:
        self._registry, self._desc, self._adapter, self._armed = registry, desc, adapter, False

    @property
    def is_mock(self) -> bool:
        return self._desc.reality is Reality.MOCK

    @property
    def name(self) -> str:
        return self._desc.provider

    @property
    def model(self) -> str:
        return self._desc.model_name

    def _arm(self, on: bool) -> None:
        self._armed = on

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._armed:
            raise LLMProviderError("provider yalnız Broker daxilində runner tərəfindən silahlandırıla bilər", charged=False)
        if not self._registry.reverify(self._desc.model_id):
            raise LLMProviderError("provider qeydiyyatdan sonra dəyişdirilib: çağırış göndərilmədi", charged=False)
        return self._adapter.complete(request)


class BrokeredModelRunner:
    def __init__(self, registry: ModelRegistry, broker: EffectBroker, *, agent: AgentSpec = SYSTEM_MODEL_SPEC, consents: ConsentView | None = None,
                 health: HealthTracker | None = None, log_dir: str | Path, max_attempts: int = 4, config: ModelRoutingConfig | None = None,
                 device_state: Callable[[], DeviceState] | None = None, resource_limits: ResourceLimits | None = None) -> None:
        if broker.ledger is None:
            raise ValueError("Broker-in xərc kitabı (ledger) olmadan model icrası mümkün deyil")
        self._registry, self._broker, self._agent, self._consents = registry, broker, agent, consents
        self._port = LedgerPort(broker.ledger)                 # TƏK kitab: Broker-in öz kitabı, dar səth
        self._health = health or HealthTracker(time.monotonic)
        self._log = Path(log_dir) / "models.jsonl"
        self._log.parent.mkdir(parents=True, exist_ok=True)
        self._max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS))
        self._device_state, self._res_limits = device_state, resource_limits or ResourceLimits()
        self._cfg_lock = threading.Lock()
        self._config: ModelRoutingConfig | None = None
        if config is not None:
            self.use_config(config)

    def use_config(self, config: ModelRoutingConfig | None) -> None:
        """Konfiqurasiyanı dəyişir (kod dəyişmədən model üstünlüyü). Reyestrə qarşı yoxlanır; uğursuzluqda ƏVVƏLKİ konfiqurasiya qalır (fail-closed). Hər run başlanğıcda bir snapshot götürür."""
        if config is not None:
            if not isinstance(config, ModelRoutingConfig):
                raise ConfigError("ModelRoutingConfig tələb olunur", "bad_type")
            config.validate_against(self._registry)
        with self._cfg_lock:
            self._config = config

    # ------------------------------------------------------------------ giriş
    def run(self, request: ModelRunRequest, *, plan: RoutePlan | None = None) -> ModelRunResult:
        """`plan` (varsa) yalnız müqayisə üçündür: köhnəlmiş/saxta plan icranı DAYANDIRIR; icra heç vaxt verilən plana əsaslanmır."""
        if in_broker():
            return ModelRunResult("nested_run_denied", "başqa Broker effektinin daxilindən model icrası icazəli deyil")
        with self._cfg_lock:
            cfg = self._config                                   # run başlanğıcında snapshot: cəhdlər arasında dəyişmir
        bad = self._validate(request) or self._check_profile(request, cfg)
        if bad:
            return ModelRunResult("invalid_request", bad)
        if plan is not None and (not isinstance(plan, RoutePlan) or plan.plan_id != _recompute_plan_id(plan)):
            return ModelRunResult("invalid_plan", "plan_id plan məzmununa uyğun deyil (dəyişdirilib)")
        for b in request.context_blocks:
            if b.owner_agent != self._agent.name:
                return ModelRunResult("cross_agent_context", "başqa agentə aid kontekst bloku aşkar edildi: icra dayandırıldı")
        composed = _compose(request)
        sha = sha256_hex(canonical_json({"s": request.system, "p": composed, "c": [c.value for c in request.capabilities]}))
        effect = Effect(self._agent, "read_files", 0.0, f"model:{request.op_id}", "models:run", payload={"op": request.op_id, "sha256": sha})
        res = self._broker.run(effect, lambda: self._execute(request, composed, plan, cfg))
        if res.status == "ok" and isinstance(res.output, ModelRunResult):
            return res.output
        if res.status == "duplicate":
            return ModelRunResult("duplicate", "eyni op_id artıq icra olunub: yenidən xərc/çağırış yoxdur")
        return ModelRunResult("blocked" if res.status == "blocked" else "error", f"broker:{res.status}: {res.reason}"[:200])

    def preview(self, request: ModelRunRequest) -> RoutePlan:
        """Yalnız qərar planı (çağırış/rezerv/yazı YOXDUR): indiki təzə snapshot-larla. Nəticə icra icazəsi DEYİL; `run(plan=...)` yalnız köhnəlməni aşkar etmək üçün müqayisə edir."""
        with self._cfg_lock:
            cfg = self._config
        bad = self._validate(request) or self._check_profile(request, cfg)
        if bad:
            raise ValueError(bad)
        return self._plan(request, _compose(request), {}, False, cfg)

    def _check_profile(self, r: ModelRunRequest, cfg: ModelRoutingConfig | None) -> str:
        if not isinstance(r, ModelRunRequest) or r.profile == "":
            return ""
        return "" if cfg is not None and cfg.profile(r.profile) is not None else "naməlum konfiqurasiya profili (fail-closed)"

    def _plan(self, r: ModelRunRequest, composed: str, excluded: dict[str, str], paid_blocked: bool, cfg: ModelRoutingConfig | None) -> RoutePlan:
        prefer, caps = r.prefer, r.capabilities
        if r.profile and cfg is not None and (p := cfg.profile(r.profile)) is not None:
            prefer = p.prefer + tuple(m for m in r.prefer if m not in p.prefer)                       # konfiqurasiya yalnız SIRA verir
            caps = tuple(dict.fromkeys(tuple(r.capabilities) + p.capabilities))                      # yalnız ƏLAVƏ tələb: sorğunun tələbi azalmır
        return plan_route(self._registry, RouteRequest((r.system, composed), r.max_output_tokens, caps, r.data_class, bool(r.context_blocks), r.allow_remote,
                                                       r.allow_simulated, self._consent_snapshot()),
                          budget=self._budget(), unhealthy=self._unhealthy(excluded, paid_blocked), prefer=prefer, resources=self._resource_snapshot(), limits=self._res_limits)

    def _validate(self, r: ModelRunRequest) -> str:
        if not isinstance(r, ModelRunRequest) or not isinstance(r.op_id, str) or not _OP_RE.match(r.op_id):
            return "op_id yanlışdır"
        if not isinstance(r.prompt, str) or not r.prompt.strip() or not isinstance(r.system, str):
            return "prompt/system yanlışdır"
        if not isinstance(r.context_blocks, tuple) or not all(isinstance(b, ContextBlock) and isinstance(b.text, str) for b in r.context_blocks):
            return "context_blocks yanlışdır"
        if isinstance(r.timeout_s, bool) or not isinstance(r.timeout_s, (int, float)) or not 0 < r.timeout_s <= 300:
            return "timeout_s yanlışdır"
        return ""

    # ------------------------------------------------------------------ icra (yalnız Broker daxilində)
    def _execute(self, r: ModelRunRequest, composed: str, supplied: RoutePlan | None, cfg: ModelRoutingConfig | None = None) -> ModelRunResult:
        if not in_broker():                                    # birbaşa/Broker-siz çağırış: heç bir provider çağırışı yoxdur
            return ModelRunResult("broker_required", "icra yalnız Effect Broker daxilində mümkündür")
        attempts: list[Attempt] = []
        excluded: dict[str, str] = {}
        paid_blocked = False
        for n in range(self._max_attempts):
            plan = self._plan(r, composed, excluded, paid_blocked, cfg)                                                      # HƏR cəhddə təzə snapshot
            if n == 0 and supplied is not None and supplied.plan_id != plan.plan_id:
                return ModelRunResult("stale_plan", "verilən plan təzə planla uyğun deyil (köhnəlmiş snapshot): icra olunmadı", attempts=tuple(attempts), plan_id=plan.plan_id)
            if plan.status != "selected":
                summary = ",".join(f"{k}={v}" for k, v in plan.refusal_summary)
                return ModelRunResult("refused", f"{plan.refusal_code}: {summary}"[:200], attempts=tuple(attempts), plan_id=plan.plan_id)
            cand = plan.selected
            resolved = self._registry.resolve_for_router(cand.model_id)       # reverify
            d = resolved.descriptor if resolved else None
            if d is None or (d.locality.value, d.reality.value, d.payment.value) != (cand.locality, cand.reality, cand.payment):
                excluded[cand.model_id] = "registration_changed"
                attempts.append(Attempt(cand.model_id, "registration_changed"))
                continue
            outcome, out = self._call(r, composed, resolved.provider, d, cand, n)
            attempts.append(Attempt(cand.model_id, outcome, out if isinstance(out, str) else ""))
            if isinstance(out, ModelRunResult):
                return ModelRunResult(out.status, out.reason, out.text, out.model_id, out.provider, out.locality, out.payment, out.simulated, out.cost, tuple(attempts), plan.plan_id)
            if outcome in ("blocked", "overrun", "fatal"):
                return ModelRunResult("blocked" if outcome == "blocked" else "error", out or outcome, attempts=tuple(attempts), plan_id=plan.plan_id)
            excluded[cand.model_id] = outcome
            if outcome in ("unknown_charge",) and d.payment.value == "paid":
                paid_blocked = True                           # nəticəsi naməlum ödənişli cəhd: heç bir başqa ödənişli provider çağırılmır
        return ModelRunResult("refused", "fallback cəhdləri tükəndi", attempts=tuple(attempts))

    def _call(self, r: ModelRunRequest, composed: str, adapter: Any, d: ModelDescriptor, cand: Any, n: int) -> tuple[str, Any]:
        view = _TrustedProviderView(self._registry, d, adapter)
        cfg = LLMConfig(provider=d.provider, model=d.model_name, timeout_s=float(min(r.timeout_s, 120)), max_output_tokens=cand.output_cap,
                        max_prompt_chars=max(1, min(len(r.system) + len(composed), 200_000)), max_response_chars=min(1_000_000, cand.output_cap * 16 + 1000),
                        max_total_tokens=cand.est_input_tokens + cand.output_cap, price_in_per_1k=d.price_in_per_1k, price_out_per_1k=d.price_out_per_1k, cost_limit=None)
        guarded = GuardedLLM(view, cfg, log_path=self._log, stop_path=self._broker.stop_path, ledger=self._port)
        scope = SpendScope(self._agent.name, f"{r.op_id}.{n}", self._agent_limit(), self._port.global_limit)
        view._arm(True)
        try:
            with spend_scope(scope):
                res = guarded.complete(composed, system=r.system, max_output_tokens=cand.output_cap)
        except LLMBlocked as exc:
            return "blocked", str(exc)[:160]
        except LLMBudgetOverrun as exc:
            self._health.record(d.model_id, True)
            return "overrun", f"budget_overrun: {str(exc)[:140]}"
        except (LLMBudgetExceeded, LLMTooLarge, LLMBadRequest, LLMUnavailable) as exc:
            return "ledger_denied", str(exc)[:160]
        except LLMProviderError as exc:
            self._health.record(d.model_id, False)
            return ("failed_not_charged" if exc.charged is False else "unknown_charge"), str(exc)[:160]
        except LLMError as exc:                               # timeout, pozulmuş cavab və s.: haqq naməlum
            self._health.record(d.model_id, False)
            return "unknown_charge", f"{exc.code}: {str(exc)[:140]}"
        finally:
            view._arm(False)
        self._health.record(d.model_id, True)
        return "ok", ModelRunResult("ok", "", res.text, d.model_id, d.provider, d.locality.value, d.payment.value, d.reality is not Reality.REAL, float(res.cost))

    # ------------------------------------------------------------------ snapshot-lar (hər cəhddə təzə)
    def _agent_limit(self) -> float:
        pol = self._broker.policy
        base = self._agent.cost_limit
        return float(min(base, pol.agent_limits.get(self._agent.name, base)) if pol is not None else base)

    def _budget(self) -> BudgetSnapshot:
        try:
            gl = self._port.global_limit
            if gl is None:
                return BudgetSnapshot(None, None)             # ümumi tavan naməlum ⇒ büdcə naməlum ⇒ ödənişli provider yoxdur
            headroom = 0.0 if self._port.breaker() is not None else max(0.0, float(gl) - self._port.total())
            return BudgetSnapshot(headroom, max(0.0, self._agent_limit() - self._port.agent_total(self._agent.name)))
        except Exception:                                      # noqa: BLE001  kitab oxunmadı: naməlum
            return BudgetSnapshot(None, None)

    def _resource_snapshot(self) -> ResourceSnapshot | None:
        """Cihaz resursları HƏR cəhddə təzə oxunur; oxunmadı/xəta ⇒ None (REAL lokal model seçilmir)."""
        if self._device_state is None:
            return None
        try:
            s = self._device_state()
            return ResourceSnapshot(s.free_ram_mb, s.battery_pct, s.charging, s.thermal_ok)
        except Exception:                                      # noqa: BLE001
            return None

    def _consent_snapshot(self) -> frozenset:
        got = set()
        for scope in (CONSENT_PAID, CONSENT_TRANSFER, CONSENT_LOCAL):
            try:
                if self._consents is not None and self._consents.granted(scope) is True:
                    got.add(scope)
            except Exception:                                  # noqa: BLE001  razılıq oxunmadı: verilməyib sayılır
                pass
        return frozenset(got)

    def _unhealthy(self, excluded: dict[str, str], paid_blocked: bool) -> dict[str, str]:
        bad = dict(self._health.snapshot())
        for d in self._registry.descriptors():
            try:
                if self._port.provider_breaker(d.provider) is not None:
                    bad[d.model_id] = "provider_breaker_open"
            except Exception:                                  # noqa: BLE001
                bad[d.model_id] = "provider_breaker_unreadable"
            if paid_blocked and d.payment.value == "paid":
                bad[d.model_id] = "paid_blocked_after_unknown_charge"
        bad.update(excluded)
        return bad


def _compose(r: ModelRunRequest) -> str:
    blocks = [render_untrusted("context", b.text) for b in r.context_blocks]
    return ("\n\n".join(blocks) + "\n\n" + r.prompt) if blocks else r.prompt


def _recompute_plan_id(plan: RoutePlan) -> str:
    if plan.status == "selected":
        return sha256_hex(canonical_json(plan.to_dict()))
    body = {"status": "refused", "code": plan.refusal_code, "note": plan.data_class_note, "effective": plan.effective_data_class,
            "rejected": [[x.model_id, list(x.reasons)] for x in plan.rejected]}
    return sha256_hex(canonical_json(body))


def make_text_generator(runner: BrokeredModelRunner, *, prefix: str, max_output_tokens: int = 512, data_class: Any = DataClass.UNCLASSIFIED, allow_remote: bool = False,
                        allow_simulated: bool = False, profile: str = "", timeout_s: float = 60.0) -> Callable[[str, str], str]:
    """`generate(system, prompt) -> str` (ModelPlanner/Reasoning üçün): hər çağırış unikal op_id ilə `BrokeredModelRunner.run` (Broker + qapılar) və yalnız `ok` nəticənin mətnini qaytarır;
    imtina/xəta ⇒ `RuntimeError` (planlaşdırıcı bunu «plan yox» sayır). Mətn ETİBARSIZDIR. Defolt: təsnif edilməmiş məlumat ⇒ yalnız lokal; uzaq model YOXDUR."""
    if not re.match(r"^[A-Za-z0-9_.\-]{1,40}$", prefix or ""):
        raise ValueError("prefix yanlışdır")
    counter = {"n": 0}
    lock = threading.Lock()

    def generate(system: str, prompt: str) -> str:
        with lock:
            counter["n"] += 1
            n = counter["n"]
        op = f"{prefix}-{uuid.uuid4().hex[:12]}-{n}"
        res = runner.run(ModelRunRequest(op, prompt, system, max_output_tokens, data_class=data_class, allow_remote=allow_remote, allow_simulated=allow_simulated, timeout_s=timeout_s, profile=profile))
        if res.status != "ok":
            raise RuntimeError(f"model çağırışı uğursuz: {res.status}")
        return res.text
    return generate
