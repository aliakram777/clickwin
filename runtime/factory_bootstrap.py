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

from agents.agent_factory import AgentFactory, AgentHandle, FactoryAdmin, FactoryPolicy, FactoryView
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


@dataclass(frozen=True)
class FactoryStack:
    """Etibarlı server kodunun əlində olan dar imkanlar. Agentlərə VERİLMİR (agentlər yalnız `agent_handle(...)` nəticəsini alır).
    Burada yoxdur: imza obyektləri, `Company.admin`, `FactoryControl`/`FactoryRuntime`."""
    view: FactoryView
    owner_gate: OwnerGate            # sahib etibarnaməsi YALNIZ step-up ilə
    admin: FactoryAdmin              # yalnız imzalı sahib etibarnaməsi ilə işləyir
    dispatcher: AgentDispatcher
    team: TeamRunner
    agent_handle: Callable[[str], AgentHandle]


class TrustAnchor:
    """Şəxsi açar yalnız buradadır (`TrustedSigner` bağlanışı) və buradan çıxmır."""

    def __init__(self, config: SigningConfig, *, clock: Callable[[], float] = time.time) -> None:
        self._signer = TrustedSigner(config.key_id, config.seed, clock)
        ring = self._signer.verifier()
        for key_id, public in config.verify_keys:
            ring.add_public_key(key_id, public)
        for key_id in config.revoked_keys:
            ring.revoke_key(key_id)
        self.verifier: Keyring = ring   # YALNIZ yoxlama: Company/fabrik/agent qatına bu verilir

    def build_factory(self, *, company: Company, auth: AuthService, owners: Iterable[str], db_path: str | Path, audit_path: str | Path,
                      workspace_root: str | Path, team_db: str | Path, orchestrator_factory: Callable[[AgentLLMRunner], Orchestrator],
                      router: Any, approvals_reader: Any = None, memory: Callable[[], Any] | None = None, global_limit: Callable[[], float | None] | None = None,
                      clock: Callable[[], float] = time.time, policy: FactoryPolicy | None = None, allow_mock: bool = False) -> FactoryStack:
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
        return FactoryStack(factory.view, gate, factory.admin, dispatcher, team, factory.handle)
