"""Etibarlı istehsal bootstrap-ı: imza açarını yükləyir, fabriki qurur və agentlərə/kanallara YALNIZ dar imkanlar paylayır (audit A-1..A-6).

Mühit dəyişənləri (yalnız server; `runtime/` qaydası: env yalnız burada oxunur, burada isə yalnız verilmiş `Mapping` istifadə olunur):
- `NAYMERA_SIGNING_KEY`      aktiv Ed25519 şəxsi açar: 32 təsadüfi baytın URL-təhlükəsiz base64-ü (43 simvol). Yaratmaq:
                             `python -c "from agents.factory_auth import generate_signing_seed as g; print(g())"` (nəticəni yalnız server mühitində saxlayın).
- `NAYMERA_SIGNING_KEY_ID`    aktiv açarın identifikatoru (məs. `k-2026-11`).
- `NAYMERA_VERIFY_KEYS`       köhnə (təqaüdə çıxmış) açıq açarlar: `id=base64pub,id2=base64pub` — rotasiya zamanı yalnız etibarnamə ömrü (maks. 15 dəq) qədər saxlanır.
- `NAYMERA_REVOKED_KEYS`      ləğv olunmuş açar id-ləri: `id1,id2` (kompromis halında dərhal; bu açarla imzalanmış HƏR etibarnamə rədd edilir).
Açar yoxdursa fabrik/Company imtiyazlı əməliyyatları BAĞLI qalır. Zəif/statik/yanlış formatlı açar yükləmə zamanı rədd edilir (`BootstrapError`).

`TrustAnchor` şəxsi açara sahibdir və heç bir yerə vermir; yalnız `verifier` (açıq açarlar) paylanır. `FactoryStack` istehsal ünvanıdır:
agentlərə baxan komponentlər (`AgentHandle`) imza obyektinə, `Company.admin`-ə və icra fasadına çata bilmir.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from agents.agent_factory import AgentFactory, FactoryPolicy, FactoryView
from runtime.agent_gateway import AgentGateway, NOBODY
from agents.agent_runtime import AgentDispatcher, AgentLLMRunner
from agents.company import Company
from agents.factory_auth import CredentialError, Keyring, TrustedSigner, decode_public_key, decode_signing_seed, valid_key_id
from agents.orchestrator import Orchestrator
from agents.team import TeamRunner
from api.auth import AuthService
from api.owner_gate import OwnerGate, receipt_consumer


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class SigningConfig:
    key_id: str
    seed: bytes = field(repr=False)
    verify_keys: tuple[tuple[str, bytes], ...] = ()
    revoked_keys: frozenset[str] = frozenset()


def _pairs(text: str) -> list[tuple[str, str]]:
    out = []
    for item in (x.strip() for x in text.split(",") if x.strip()):
        if "=" not in item:
            raise BootstrapError("NAYMERA_VERIFY_KEYS formatı: id=base64pub,id2=base64pub")
        k, v = item.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def load_signing_config(env: Mapping[str, str]) -> SigningConfig | None:
    """Heç bir imza dəyişəni yoxdursa None (imtiyazlı əməliyyatlar bağlı). Qismən/yanlış/zəif konfiqurasiya `BootstrapError` verir (açıq xəta)."""
    raw_key = (env.get("NAYMERA_SIGNING_KEY") or "").strip()
    key_id = (env.get("NAYMERA_SIGNING_KEY_ID") or "").strip()
    verify = (env.get("NAYMERA_VERIFY_KEYS") or "").strip()
    revoked = (env.get("NAYMERA_REVOKED_KEYS") or "").strip()
    if not (raw_key or key_id or verify or revoked):
        return None
    if not raw_key or not key_id:
        raise BootstrapError("imza konfiqurasiyası natamamdır: NAYMERA_SIGNING_KEY və NAYMERA_SIGNING_KEY_ID birlikdə tələb olunur")
    try:
        seed = decode_signing_seed(raw_key)
        retired = tuple((k, decode_public_key(v)) for k, v in _pairs(verify))
    except CredentialError as exc:
        raise BootstrapError(f"imza açarı rədd edildi: {exc}") from None
    revoked_ids = frozenset(x.strip() for x in revoked.split(",") if x.strip())
    ids = [key_id] + [k for k, _ in retired]
    if not all(valid_key_id(k) for k in ids) or not all(valid_key_id(k) for k in revoked_ids):
        raise BootstrapError("açar id-si yanlışdır (3-40 simvol: hərf, rəqəm, _ . -)")
    if len(set(ids)) != len(ids):
        raise BootstrapError("açar id-ləri təkrarlanır")
    if key_id in revoked_ids:
        raise BootstrapError("aktiv imza açarı ləğv olunmuş siyahıdadır")
    return SigningConfig(key_id, seed, retired, revoked_ids)


_CONSOLE_OPS = ("spawn", "assign", "cancel_task", "stop", "restart", "archive", "create_agent", "grant", "set_budget", "set_policy", "resolve_request", "delete")


class OwnerConsole:
    """Sahib tərəfli əməliyyatların dar səthi (etibarlı server kodu üçün). Hər əməliyyat imzalı etibarnamə tələb edir; `FactoryAdmin/Control/Runtime` və ya `AgentFactory`
    obyekti/bağlı metodu bu səthdə YOXDUR (yalnız eyni adlı sarğı funksiyalar). Agent kodu bu proseslə ümumiyyətlə eyni prosesdə deyil (`AgentGateway`)."""
    __slots__ = _CONSOLE_OPS

    def __init__(self, admin: Any) -> None:
        for name in _CONSOLE_OPS:
            fn = getattr(admin, name)

            def call(*a: Any, _fn: Callable[..., Any] = fn, **kw: Any) -> Any:
                return _fn(*a, **kw)
            call.__name__ = name
            object.__setattr__(self, name, call)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("dəyişdirilə bilməz")


@dataclass(frozen=True)
class FactoryStack:
    """Etibarlı SERVER kodunun əlindəki dar imkanlar; agent kodu ilə ÜMUMİ PROSESDƏ deyil. Agentlər yalnız `agents` (AgentGateway) ilə AYRI proseslərdə işləyir və
    yalnız RPC ilə danışır. Burada yoxdur: bağlı `AgentFactory` metodu, `factory.admin/control/runtime`, `Company`, `Registry`, `Keyring`, DB bağlantısı, imza/issuer obyekti.
    (`dispatcher`/`team` etibarlı icra mühərrikləridir və fabrikə istinad saxlayır: onlar da agent koduna VERİLMİR.)"""
    view: FactoryView
    owner_gate: OwnerGate            # sahib etibarnaməsi YALNIZ step-up ilə
    console: OwnerConsole            # imzalı sahib etibarnaməsi ilə işləyən dar əməliyyatlar
    dispatcher: AgentDispatcher
    team: TeamRunner
    agents: AgentGateway             # agent kodu üçün AYRI proses + minimal RPC


class TrustAnchor:
    """Şəxsi açar yalnız buradadır (`TrustedSigner` bağlanışı) və buradan çıxmır. Keyring idarəsi (açar əlavə/dondur/ləğv) yalnız burada saxlanır."""

    def __init__(self, config: SigningConfig, *, clock: Callable[[], float] = time.time) -> None:
        self._signer = TrustedSigner(config.key_id, config.seed, clock)
        ring = self._signer.verifier()
        self._control = ring.take_control()          # YEGANƏ tutacaq: ondan sonra heç kim keyring-i dəyişə bilməz
        for key_id, public in config.verify_keys:
            self._control.add_public_key(key_id, public)
        for key_id in config.revoked_keys:
            self._control.revoke_key(key_id)
        self._control.freeze()                       # etibarlı açar çoxluğu bootstrap-dan sonra dondurulur; yalnız ləğv (daraltma) qalır
        self.verifier: Keyring = ring                # YALNIZ yoxlama (mutator yoxdur)

    # --- control-plane (yalnız etibarlı server kodu; TrustAnchor agentlərə/FactoryStack-ə verilmir) ---
    def revoke_key(self, key_id: str) -> None:
        self._control.revoke_key(key_id)

    def revoke_credential(self, nonce: str) -> None:
        self._control.revoke_credential(nonce)

    def build_factory(self, *, company: Company, auth: AuthService, owners: Iterable[str], db_path: str | Path, audit_path: str | Path,
                      workspace_root: str | Path, team_db: str | Path, orchestrator_factory: Callable[[AgentLLMRunner], Orchestrator],
                      router: Any, approvals_reader: Any = None, memory: Callable[[], Any] | None = None, global_limit: Callable[[], float | None] | None = None,
                      clock: Callable[[], float] = time.time, policy: FactoryPolicy | None = None, allow_mock: bool = False,
                      program_root: str | Path | None = None, agent_isolation: dict | None = None) -> FactoryStack:
        if company._core.verifier is not self.verifier:   # Company eyni yoxlayıcı ilə qurulmalıdır (müstəqil yoxlama)
            raise BootstrapError("Company bu etibar çıpasının yoxlayıcısı (`anchor.verifier`) ilə qurulmalıdır")
        factory = AgentFactory(company, db_path, audit_path, workspace_root, owners=owners, verifier=self.verifier, allow_mock=allow_mock, clock=clock,
                               policy=policy)
        runner = AgentLLMRunner(factory, router, memory, allow_simulated=False, global_limit=global_limit)
        orchestrator = orchestrator_factory(runner)
        dispatcher = AgentDispatcher(factory, orchestrator, runner, issuer=self._signer.dispatcher_issuer(), system=self._signer.system_issuer(),
                                     approvals=approvals_reader)
        team = TeamRunner(factory, dispatcher, team_db, audit_path, system=self._signer.system_issuer(), clock=clock)
        gate = OwnerGate(auth, self._signer.owner_issuer(receipt_consumer(auth)))
        root = Path(program_root) if program_root is not None else Path(workspace_root) / "agent_programs"
        root.mkdir(parents=True, exist_ok=True)
        iso = {"drop_to": NOBODY, "require_os_drop": True, **(agent_isolation or {})}   # defolt: uid endirmə MƏCBURİDİR (qapalı qalır)

        def ports_for(agent_id: str) -> Any:
            return factory.handle(agent_id)

        def status_of(agent_id: str) -> str | None:
            a = factory.view.agent(agent_id)
            return a.status if a is not None else None
        agents = AgentGateway(ports_for, status_of, program_root=root, **iso)
        return FactoryStack(factory.view, gate, OwnerConsole(factory.admin), dispatcher, team, agents)
