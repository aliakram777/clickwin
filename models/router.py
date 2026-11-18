"""ModelRouter.plan_route (Mərhələ 18.1-C): YALNIZ QƏRAR PLANI. Heç bir provider çağırışı, şəbəkə, pul xərci, büdcə rezervi, fallback icrası YOXDUR.

Giriş (registry + sorğu + büdcə snapshot-u + sağlamlıq snapshot-u) eyni olduqda nəticə (və `plan_id`) eynidir: saat, təsadüfilik, I/O, dəyişən qlobal vəziyyət istifadə olunmur.
Seçim yalnız `ModelRegistry`-nin müstəqil müəyyən etdiyi descriptor-lara əsaslanır; provider-in öz elanları oxunmur. Qeydiyyatdan sonra dəyişdirilmiş provider (`reverify`) rədd edilir.

Fail-closed qaydalar:
- Məlumat sinfi: təsnif edilməmiş/naməlum ⇒ UNCLASSIFIED (yalnız yetərli lokal modelə, uzağa HEÇ VAXT); mətndə sirr bənzər məzmun ⇒ SECRET (bəyan edilən sinif ondan aşağı ola bilməz); SECRET heç bir modelə verilmir;
  yaddaş konteksti ən azı INTERNAL sayılır.
- Uzaq provider yalnız `allow_remote=True` + `cloud_paid_use` razılığı (+ yaddaş üçün `cloud_data_transfer`) + məlumat sinfi ≤ modelin maksimumu ilə.
- Ödənişli (payment=PAID) provider yalnız ledger büdcəsi MƏLUM və > 0, qiymət təxmini həddi aşmadıqda. Büdcə naməlum/yanlış ⇒ ödənişli provider yoxdur. Vahid: `CostLedger`/`LLMConfig.price_*` ilə EYNİ ədədi vahid (valyuta fərz edilmir).
- SIMULATED/MOCK provider yalnız `allow_simulated=True` olduqda və planda `simulated=True` ilə (real kimi göstərilmir).
Sıralama (deterministik): `prefer` sırası, REAL < SIMULATED < MOCK, LOCAL < REMOTE, təxmini maksimum xərc, `model_id`. Sıralama yalnız qapıları keçmiş namizədlər arasındadır.
Heç bir uyğun provider yoxdursa plan `refused` olur (təhlükəsiz imtina) və hər provider üçün rədd səbəbləri göstərilir.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from agents.llm import _INPUT_TOKEN_OVERHEAD          # GuardedLLM xərc rezervi ilə EYNİ yuxarı hədd düsturu (Stage 17 sabiti oxunur, dəyişdirilmir)
from agents.local_models import ResourceLimits
from agents.memory import detect_secrets
from kernel.common import canonical_json, sha256_hex
from models.descriptor import DataClass, Locality, ModelCapability, ModelDescriptor, Payment, Reality
from models.registry import ModelRegistry

CONSENT_PAID = "cloud_paid_use"
CONSENT_TRANSFER = "cloud_data_transfer"
CONSENT_LOCAL = "local_resource_use"                  # cihazda/şəxsi serverdə REAL lokal model işlətmək (CLAUDE.md qayda 7)
MAX_TEXT_BYTES = 2_000_000
MAX_OUTPUT_REQUEST = 1_000_000

_CLASS_RANK = {DataClass.PUBLIC: 0, DataClass.INTERNAL: 1, DataClass.SENSITIVE: 2, DataClass.UNCLASSIFIED: 2, DataClass.SECRET: 99}
_REALITY_RANK = {Reality.REAL: 0, Reality.SIMULATED: 1, Reality.MOCK: 2}


@dataclass(frozen=True)
class RouteRequest:
    """Marşrut sorğusu. Sahələr yumşaq doğrulanır: yanlış dəyər istisna yox, `refused` plan (fail-closed) verir."""
    texts: tuple[str, ...]                                   # system/prompt/kontekst: həm xərc, həm sirr skanı üçün
    max_output_tokens: int
    capabilities: tuple[ModelCapability, ...] = (ModelCapability.TEXT_GENERATION,)
    data_class: Any = DataClass.UNCLASSIFIED                 # defolt: təsnif edilməmiş (fail-closed)
    includes_memory_context: bool = False
    allow_remote: bool = False
    allow_simulated: bool = False
    granted_consents: frozenset = frozenset()                # snapshot: `cloud_paid_use`, `cloud_data_transfer`


@dataclass(frozen=True)
class BudgetSnapshot:
    """Ledger büdcəsinin snapshot-u (CostLedger ilə eyni vahid). `None` = naməlum (ledger yoxdur/oxunmadı) ⇒ ödənişli provider seçilmir."""
    ledger_headroom: Any = None
    request_cap: Any = None


@dataclass(frozen=True)
class ResourceSnapshot:
    """Cihaz resurslarının snapshot-u (RAM, batareya, istilik). `None` = naməlum ⇒ REAL lokal model seçilmir (fail-closed)."""
    free_ram_mb: Any = None
    battery_pct: Any = None
    charging: Any = None
    thermal_ok: Any = None


def resource_problem(snap: Any, limits: ResourceLimits, model_min_ram_mb: int) -> str | None:
    """Səbəb kodu və ya None. Naməlum/yanlış tipli sahib ⇒ `resources_unknown`. Eşiklər `agents.local_models.ResourceLimits`-dən (batareya: şarj olunursa yoxlanmır)."""
    if not isinstance(snap, ResourceSnapshot):
        return "resources_unknown"
    ram, bat, chg, thm = snap.free_ram_mb, snap.battery_pct, snap.charging, snap.thermal_ok
    if (isinstance(ram, bool) or not isinstance(ram, (int, float)) or not math.isfinite(ram) or isinstance(bat, bool) or not isinstance(bat, (int, float))
            or not 0 <= bat <= 100 or not isinstance(chg, bool) or not isinstance(thm, bool)):
        return "resources_unknown"
    need = max(limits.min_free_ram_mb, int(model_min_ram_mb))
    if ram < need:
        return f"ram_low:{int(ram)}<{need}"
    if bat < limits.min_battery_pct and not chg:
        return f"battery_low:{int(bat)}<{limits.min_battery_pct}"
    if not thm:
        return "thermal_throttled"
    return None


@dataclass(frozen=True)
class Candidate:
    model_id: str
    provider: str
    locality: str
    reality: str
    payment: str
    simulated: bool                       # reality != REAL: real xidmət kimi göstərilmir
    est_input_tokens: int
    output_cap: int
    est_max_cost: float                   # GuardedLLM rezerv düsturu ilə (ledger vahidi); FREE üçün 0
    required_consents: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "provider": self.provider, "locality": self.locality, "reality": self.reality, "payment": self.payment, "simulated": self.simulated,
                "est_input_tokens": self.est_input_tokens, "output_cap": self.output_cap, "est_max_cost": self.est_max_cost, "required_consents": list(self.required_consents)}


@dataclass(frozen=True)
class Rejection:
    model_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RoutePlan:
    status: str                                       # "selected" | "refused"
    selected: Candidate | None
    fallbacks: tuple[Candidate, ...]                  # TƏKLİF olunan sıra; heç biri çağırılmayıb. İcrada qapılar yenidən yoxlanılır (18.1-D)
    rejected: tuple[Rejection, ...]
    effective_data_class: str
    data_class_note: str
    refusal_code: str
    refusal_summary: tuple[tuple[str, int], ...]
    selection_reason: str
    plan_id: str

    GRANTS_EXECUTION = False                          # plan icra icazəsi VERMİR
    EXECUTES_NOTHING = True

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "selected": self.selected.to_dict() if self.selected else None, "fallbacks": [c.to_dict() for c in self.fallbacks],
                "rejected": [{"model_id": r.model_id, "reasons": list(r.reasons)} for r in self.rejected], "effective_data_class": self.effective_data_class,
                "data_class_note": self.data_class_note, "refusal_code": self.refusal_code, "refusal_summary": [list(x) for x in self.refusal_summary],
                "selection_reason": self.selection_reason}


def _finite_nonneg(v: Any) -> float | None:
    return float(v) if not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 else None


def _budget_limit(b: Any) -> float | None:
    """Ödənişli provider üçün tavan. Naməlum (None), yanlış tipli/sonsuz/mənfi dəyər ⇒ None (ödənişli provider seçilmir). Verilmiş, lakin yanlış `request_cap` da naməlum sayılır."""
    if not isinstance(b, BudgetSnapshot):
        return None
    headroom = _finite_nonneg(b.ledger_headroom)
    if headroom is None:
        return None
    if b.request_cap is None:
        return headroom
    cap = _finite_nonneg(b.request_cap)
    return None if cap is None else min(headroom, cap)


def _refuse(code: str, note: str = "", effective: str = DataClass.UNCLASSIFIED.value, rejected: tuple[Rejection, ...] = ()) -> RoutePlan:
    body = {"status": "refused", "code": code, "note": note, "effective": effective, "rejected": [[r.model_id, list(r.reasons)] for r in rejected]}
    return RoutePlan("refused", None, (), rejected, effective, note, code, (), f"refused: {code}", sha256_hex(canonical_json(body)))


def _effective_class(declared: Any, texts: tuple[str, ...], memory_context: bool) -> tuple[DataClass, str]:
    note = ""
    if not isinstance(declared, DataClass):
        try:
            declared = DataClass(declared) if isinstance(declared, str) else DataClass.UNCLASSIFIED
        except ValueError:
            declared, note = DataClass.UNCLASSIFIED, "unknown_data_class"
    eff = declared
    if any(detect_secrets(t) for t in texts):
        if eff is not DataClass.SECRET:
            note = "secret_detected_overrides_declared_class"
        eff = DataClass.SECRET
    if memory_context and _CLASS_RANK[eff] < _CLASS_RANK[DataClass.INTERNAL]:
        eff, note = DataClass.INTERNAL, note or "memory_context_floor_internal"
    if eff is DataClass.UNCLASSIFIED and not note:
        note = "unclassified_treated_as_sensitive_local_only"
    return eff, note


def plan_route(registry: ModelRegistry, request: RouteRequest, *, budget: BudgetSnapshot | None = None, unhealthy: Mapping[str, str] | None = None,
               prefer: Sequence[str] = (), resources: ResourceSnapshot | None = None, limits: ResourceLimits | None = None) -> RoutePlan:
    """Qərar planı qaytarır. Heç nə çağırmır/rezerv etmir/yazmır. `prefer`: yalnız SIRA (uyğunsuz modeli uyğun edə bilməz)."""
    if not isinstance(request, RouteRequest):
        return _refuse("invalid_request", "RouteRequest tələb olunur")
    texts = request.texts if isinstance(request.texts, (tuple, list)) else None
    if texts is None or not texts or not all(isinstance(t, str) for t in texts):
        return _refuse("invalid_request", "texts: boş olmayan mətn siyahısı tələb olunur")
    texts = tuple(texts)
    if sum(len(t.encode("utf-8", "replace")) for t in texts) > MAX_TEXT_BYTES:
        return _refuse("request_too_large")
    out_req = request.max_output_tokens
    if isinstance(out_req, bool) or not isinstance(out_req, int) or not 1 <= out_req <= MAX_OUTPUT_REQUEST:
        return _refuse("invalid_request", "max_output_tokens yanlışdır")
    try:
        caps = frozenset(ModelCapability(c) for c in request.capabilities)
    except (ValueError, TypeError):
        return _refuse("invalid_request", "naməlum capability")
    if not caps:
        return _refuse("invalid_request", "ən azı bir capability tələb olunur")
    memory_ctx = request.includes_memory_context is True
    eff, note = _effective_class(request.data_class, texts, memory_ctx)
    consents = frozenset(c for c in (request.granted_consents if isinstance(request.granted_consents, (set, frozenset, tuple, list)) else ()) if isinstance(c, str))
    allow_remote, allow_sim = request.allow_remote is True, request.allow_simulated is True
    limit = _budget_limit(budget)
    res_limits = limits if isinstance(limits, ResourceLimits) else ResourceLimits()
    bad = unhealthy if isinstance(unhealthy, Mapping) else {}
    est_in = sum(len(t.encode("utf-8", "replace")) for t in texts) + _INPUT_TOKEN_OVERHEAD
    order = {m: i for i, m in enumerate(p for p in prefer if isinstance(p, str))}

    descs = registry.descriptors()                                          # qeydiyyat sırası: nəticə ondan ASILI OLMAMALIDIR (aşağıda sıralanır)
    if not descs:
        return _refuse("no_models_registered", "", eff.value)
    eligible: list[tuple[tuple, Candidate]] = []
    rejected: list[Rejection] = []
    for d in descs:
        reasons: list[str] = []
        if not registry.reverify(d.model_id):
            reasons.append("registration_tampered")                       # qeydiyyatdan sonra dəyişdirilib: digər meyarlara baxılmır
        else:
            if not d.usable:
                reasons.append(f"unavailable:{d.availability.value}")
            if d.model_id in bad:
                reasons.append("unhealthy:" + "".join(ch for ch in str(bad[d.model_id])[:40] if ch.isalnum() or ch in "_-:. "))
            if d.reality is not Reality.REAL and not allow_sim:
                reasons.append(f"{d.reality.value}_not_allowed")
            missing = sorted(c.value for c in caps - set(d.capabilities))
            if missing:
                reasons.append("missing_capability:" + ",".join(missing))
            if _CLASS_RANK[eff] > _CLASS_RANK[d.max_data_class]:
                reasons.append(f"data_class_exceeds_model_max:{eff.value}>{d.max_data_class.value}")
            out_cap = min(out_req, d.max_output_tokens)
            if est_in + out_cap > d.context_tokens:
                reasons.append("context_too_small")
            needs: list[str] = []
            if d.locality is Locality.LOCAL and d.reality is Reality.REAL:             # REAL lokal model: razılıq + cihaz resursları (batareya/istilik/RAM); naməlum ⇒ seçilmir
                needs.append(CONSENT_LOCAL)
                if CONSENT_LOCAL not in consents:
                    reasons.append(f"consent_missing:{CONSENT_LOCAL}")
                problem = resource_problem(resources, res_limits, d.min_ram_mb)
                if problem:
                    reasons.append(problem)
            if d.locality is Locality.REMOTE:
                if eff in (DataClass.UNCLASSIFIED, DataClass.SECRET):
                    reasons.append(f"remote_forbidden_for_class:{eff.value}")
                if not allow_remote:
                    reasons.append("remote_not_allowed")
                needs.append(CONSENT_PAID)
                if memory_ctx:
                    needs.append(CONSENT_TRANSFER)
                reasons += [f"consent_missing:{c}" for c in needs if c not in consents]
            est_cost = est_in * d.price_in_per_1k / 1000 + out_cap * d.price_out_per_1k / 1000          # GuardedLLM rezerv düsturu
            if d.payment is Payment.PAID:
                if limit is None:
                    reasons.append("budget_unknown")
                elif limit <= 0:
                    reasons.append("budget_zero")
                elif est_cost > limit:
                    reasons.append("budget_exceeded")
        if reasons:
            rejected.append(Rejection(d.model_id, tuple(reasons)))
            continue
        cand = Candidate(d.model_id, d.provider, d.locality.value, d.reality.value, d.payment.value, d.reality is not Reality.REAL, est_in, out_cap,
                         est_cost if d.payment is Payment.PAID else 0.0, tuple(needs))
        key = (order.get(d.model_id, len(order)), _REALITY_RANK[d.reality], 0 if d.locality is Locality.LOCAL else 1, est_cost, d.model_id)
        eligible.append((key, cand))
    rej = tuple(sorted(rejected, key=lambda x: x.model_id))
    if not eligible:
        counts: dict[str, int] = {}
        for r in rej:
            for x in r.reasons:
                k = x.split(":", 1)[0]
                counts[k] = counts.get(k, 0) + 1
        code = "secret_data_not_allowed" if eff is DataClass.SECRET else "no_eligible_provider"
        plan = _refuse(code, note, eff.value, rej)
        return RoutePlan("refused", None, (), rej, eff.value, note, code, tuple(sorted(counts.items())), f"refused: {code}", plan.plan_id)
    eligible.sort(key=lambda x: x[0])
    first = eligible[0]
    why = (f"{first[1].model_id}: tələb olunan capability-lər var; sinif {eff.value} ≤ model maksimumu; "
           f"{'lokal' if first[1].locality == 'local' else 'uzaq'}, {first[1].reality}, {first[1].payment}; sıra açarı(prefer,reality,locality,xərc)={list(first[0][:4])}")
    plan = RoutePlan("selected", first[1], tuple(c for _, c in eligible[1:]), rej, eff.value, note, "", (), why, "")
    return RoutePlan(plan.status, plan.selected, plan.fallbacks, plan.rejected, plan.effective_data_class, plan.data_class_note, "", (), why,
                     sha256_hex(canonical_json(plan.to_dict())))


class HealthTracker:
    """Provider sağlamlığı (kiçik dövrə kəsici). `plan_route`-dan KƏNARDA: `record` vəziyyəti dəyişir, `snapshot()` plana verilən təmiz mapping-dir.
    Saat inyeksiya olunur (deterministik test). ardıcıl `threshold` uğursuzluqdan sonra `cooldown_s` müddətində model sağlam sayılmır."""

    def __init__(self, clock: Callable[[], float], threshold: int = 3, cooldown_s: float = 30.0) -> None:
        if not callable(clock) or isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1 or not (isinstance(cooldown_s, (int, float)) and cooldown_s > 0):
            raise ValueError("HealthTracker parametrləri yanlışdır")
        self._clock, self._threshold, self._cooldown = clock, threshold, float(cooldown_s)
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def record(self, model_id: str, ok: bool) -> None:
        if ok:
            self._fails.pop(model_id, None)
            self._open_until.pop(model_id, None)
            return
        n = self._fails.get(model_id, 0) + 1
        self._fails[model_id] = n
        if n >= self._threshold:
            self._open_until[model_id] = self._clock() + self._cooldown

    def snapshot(self) -> dict[str, str]:
        now = self._clock()
        return {m: "circuit_open" for m, t in sorted(self._open_until.items()) if t > now}
