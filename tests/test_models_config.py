"""Mərhələ 18.1-E: model seçimi konfiqurasiyası — kod dəyişmədən üstünlük, təhlükəsizliyi zəiflətmir, yanlış konfiqurasiya fail-closed. Real API/ödəniş YOXDUR."""
from __future__ import annotations

import ast
import json
import pathlib
import threading

import pytest

import models.config as cfgmod
from agents.spec import AgentSpec
from kernel.common import ContractError, UnsupportedVersion
from models import ConfigError, DataClass, ModelCapability, ModelRegistry, ModelRoutingConfig
from models.adapters.fixtures import anthropic_ok, gemini_ok
from runtime.brokered_models import BrokeredModelRunner
from tests.test_models_runner import W, req

GOOD = {"schema_version": 1, "profiles": {"fast": {"prefer": ["mock_m", "local_free"]}, "cloud_first": {"prefer": ["remote_a", "remote_b"], "capabilities": ["chat"]}}}


def load(obj) -> ModelRoutingConfig:
    return ModelRoutingConfig.from_json(json.dumps(obj))


@pytest.fixture()
def w(tmp_path):
    return W(tmp_path, models={"local_free": None, "mock_m": None, "remote_a": [anthropic_ok("a")] * 5, "remote_b": [gemini_ok("b")] * 5})


# ------------------------------------------------------------------ yükləmə
def test_valid_config_roundtrip_hash_and_immutability():
    c = load(GOOD)
    assert [p.name for p in c.profiles] == ["cloud_first", "fast"] and c.profile("fast").prefer == ("mock_m", "local_free")
    assert c.profile("cloud_first").capabilities == (ModelCapability.CHAT,) and c.profile("nope") is None and c.profile(5) is None
    assert load(c.to_dict()).content_hash == c.content_hash == load({**GOOD, "profiles": dict(reversed(list(GOOD["profiles"].items())))}).content_hash
    assert c.GRANTS_EXECUTION is False
    with pytest.raises(Exception):
        c.profiles = ()                                                                 # type: ignore[misc]


@pytest.mark.parametrize("key", sorted(cfgmod.FORBIDDEN_KEYS))
def test_security_relevant_keys_are_rejected_at_every_level(key):
    for obj in ({**GOOD, key: True}, {**GOOD, "profiles": {"p": {"prefer": ["m1"], key: True}}}):
        with pytest.raises(ConfigError) as ei:
            load(obj)
        assert ei.value.code == "security_key_forbidden"
    with pytest.raises(ConfigError):
        load({**GOOD, key.upper(): 1})                                                  # böyük/kiçik hərf fərqi yan keçid deyil


@pytest.mark.parametrize("bad", [
    None, [], "x", 5, {}, {"schema_version": 1}, {"profiles": {}}, {"schema_version": 1, "profiles": {}}, {"schema_version": 1, "profiles": []},
    {"schema_version": 1, "profiles": {"p": {}}}, {"schema_version": 1, "profiles": {"p": {"prefer": []}}}, {"schema_version": 1, "profiles": {"p": {"prefer": "m1"}}},
    {"schema_version": 1, "profiles": {"p": {"prefer": ["m1", "m1"]}}}, {"schema_version": 1, "profiles": {"p": {"prefer": [5]}}}, {"schema_version": 1, "profiles": {"p": {"prefer": ["Bad ID"]}}},
    {"schema_version": 1, "profiles": {"p": {"prefer": ["m1"], "capabilities": ["tool_calling"]}}}, {"schema_version": 1, "profiles": {"p": {"prefer": ["m1"], "capabilities": ["chat", "chat"]}}},
    {"schema_version": 1, "profiles": {"Bad Name": {"prefer": ["m1"]}}}, {"schema_version": 1, "profiles": {"p": {"prefer": ["m1"], "extra": 1}}},
    {"schema_version": 1, "profiles": {"p": {"prefer": [f"m{i}" for i in range(17)]}}}, {"schema_version": 1, "profiles": {f"p{i}": {"prefer": ["m1"]} for i in range(17)}},
    {"schema_version": 1, "profiles": {"p": {"prefer": ["m1"]}}, "extra": 1}, {"schema_version": True, "profiles": {"p": {"prefer": ["m1"]}}},
    {"schema_version": "1", "profiles": {"p": {"prefer": ["m1"]}}}, {"schema_version": 1, "profiles": {"p": {"prefer": ["sk-" + "Ab1C" * 10]}}},
])
def test_invalid_config_is_rejected(bad):
    with pytest.raises(ContractError):
        ModelRoutingConfig.from_dict(bad)


def test_future_version_and_bad_json_text_rejected():
    with pytest.raises(UnsupportedVersion):
        load({**GOOD, "schema_version": 2})
    for text in ('{"a":1,"a":2}', "{", "[]", "null", '{"schema_version": NaN}', "x" * 40_000, b"{}", None):
        with pytest.raises(ContractError):
            ModelRoutingConfig.from_json(text)                                          # type: ignore[arg-type]


def test_unknown_model_ids_are_rejected_against_the_registry(w):
    load(GOOD).validate_against(w.registry)
    with pytest.raises(ConfigError) as ei:
        load({"schema_version": 1, "profiles": {"p": {"prefer": ["ghost"]}}}).validate_against(w.registry)
    assert ei.value.code == "unknown_model"
    assert not hasattr(ModelRoutingConfig, "register")                                  # konfiqurasiya model qeydiyyata ala bilmir


def test_config_module_is_pure_no_io_env_or_network():
    src = pathlib.Path(cfgmod.__file__).read_text()
    tree = ast.parse(src)
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"__future__", "dataclasses", "typing", "kernel.common", "models.descriptor", "models.registry"}
    for bad in ("open(", "os.", "environ", "socket", "pathlib", "subprocess"):
        assert bad not in src.replace("`", "")


# ------------------------------------------------------------------ icra: kodsuz dəyişmə
def test_changing_preferences_changes_the_model_without_code_changes(w):
    w.runner.use_config(load({"schema_version": 1, "profiles": {"main": {"prefer": ["mock_m", "local_free"]}}}))
    a = w.runner.run(req("c1", profile="main", prefer=()))
    w.runner.use_config(load({"schema_version": 1, "profiles": {"main": {"prefer": ["local_free", "mock_m"]}}}))        # yalnız data dəyişdi
    b = w.runner.run(req("c2", profile="main", prefer=()))
    assert (a.model_id, b.model_id) == ("mock_m", "local_free") and a.text != b.text
    w.runner.use_config(None)
    assert w.runner.run(req("c3", profile="main")).status == "invalid_request"          # konfiqurasiya götürüldü: profil yoxdur


def test_unknown_or_invalid_config_swap_keeps_the_previous_config(w):
    w.runner.use_config(load({"schema_version": 1, "profiles": {"main": {"prefer": ["mock_m"]}}}))
    with pytest.raises(ConfigError):
        w.runner.use_config(load({"schema_version": 1, "profiles": {"main": {"prefer": ["ghost"]}}}))
    with pytest.raises(ConfigError):
        w.runner.use_config({"schema_version": 1})                                       # type: ignore[arg-type]
    assert w.runner.run(req("k1", profile="main", prefer=())).model_id == "mock_m"       # əvvəlki saxlanıb
    assert w.runner.run(req("k2", profile="ghost")).status == "invalid_request"
    assert w.runner.run(req("k3", profile="Bad Name")).status == "invalid_request"


def test_config_cannot_weaken_privacy_consent_budget_or_remote_policy(w):
    w.runner.use_config(load({"schema_version": 1, "profiles": {"remote": {"prefer": ["remote_a", "remote_b", "local_free"]}}}))
    for kw in ({"allow_remote": False}, {"data_class": DataClass.SENSITIVE}, {"data_class": None}, {"data_class": "garbage"}):
        r = w.runner.run(req(f"p{len(kw)}{str(list(kw.values())[0])[:4]}", profile="remote", **kw))
        assert r.locality == "local" and w.calls("remote_a") == 0 and w.calls("remote_b") == 0, kw          # üstünlük uzağı seçdirə bilmir
    w.consents.granted_set.clear()
    assert w.runner.run(req("p-consent", profile="remote")).locality == "local" and w.calls("remote_a") == 0
    w.consents.granted_set.update({"cloud_paid_use", "cloud_data_transfer"})
    ok = w.runner.run(req("p-ok", profile="remote"))
    assert ok.model_id == "remote_a" and w.calls("remote_a") == 1                                           # bütün qapılar açıq olanda üstünlük işləyir
    zero = AgentSpec(name="agent_zero", purpose="p", tools=[], permissions=["read_files"], inputs=["x"], outputs=["y"], cost_limit=0.0)
    rz = BrokeredModelRunner(w.registry, w.broker, agent=zero, consents=w.consents, log_dir=w.tmp, config=w.runner._config)
    assert rz.run(req("p-budget", profile="remote")).locality == "local" and w.calls("remote_a") == 1        # büdcə 0: ödənişli yoxdur


def test_config_capabilities_only_add_requirements(w):
    w.runner.use_config(load({"schema_version": 1, "profiles": {"strict": {"prefer": ["local_free"], "capabilities": ["json_output"]}}}))
    r = w.runner.run(req("cap1", profile="strict"))
    assert r.status == "refused" and "missing_capability" in r.reason and w.calls("local_free") == 0         # əlavə tələb heç bir modeldə yoxdur
    w.runner.use_config(load({"schema_version": 1, "profiles": {"loose": {"prefer": ["local_free"], "capabilities": ["chat"]}}}))
    assert w.runner.run(req("cap2", profile="loose", capabilities=(ModelCapability.TEXT_GENERATION, ModelCapability.JSON_OUTPUT))).status == "refused"   # sorğunun tələbi azalmadı


def test_preview_and_run_agree_under_a_profile(w):
    w.runner.use_config(load(GOOD))
    r = req("pv", profile="fast", prefer=())
    plan = w.runner.preview(r)
    assert plan.selected.model_id == "mock_m" and w.runner.run(r, plan=plan).model_id == "mock_m"
    with pytest.raises(ValueError):
        w.runner.preview(req("pv2", profile="ghost"))


def test_config_swaps_during_parallel_runs_are_safe(w):
    cfgs = [load({"schema_version": 1, "profiles": {"main": {"prefer": order}}}) for order in (["mock_m", "local_free"], ["local_free", "mock_m"])]
    w.runner.use_config(cfgs[0])
    out, errors = [], []

    def worker(i):
        try:
            out.append(w.runner.run(req(f"par{i}", profile="main", prefer=())).model_id)
        except Exception as exc:                                                        # noqa: BLE001
            errors.append(exc)

    def swapper():
        for i in range(40):
            w.runner.use_config(cfgs[i % 2])
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)] + [threading.Thread(target=swapper)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(out) == 12 and set(out) <= {"mock_m", "local_free"}


def test_no_real_provider_is_activated_by_configuration(w):
    w.runner.use_config(load(GOOD))
    assert all(d.reality.value != "real" for d in w.registry.descriptors())
    assert ModelRegistry().descriptors() == ()
