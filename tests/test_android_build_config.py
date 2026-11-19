"""Stage 18.13: Android Gradle/manifest/resurs konfiqurasiyasının STATİK yoxlaması (SDK tələb etmir) + Compose tip-yoxlamasının (opt-in) icrası.
Bu testlər APK build ETMİR və onu əvəz etmir."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ANDROID = Path(__file__).resolve().parent.parent / "android"
APP = ANDROID / "app"
MAIN = APP / "src" / "main"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_kotlin_and_compose_plugin_versions_are_aligned_everywhere():
    root = read(ANDROID / "build.gradle.kts")
    kotlin = re.search(r'org.jetbrains.kotlin.android"\) version "([\d.]+)"', root).group(1)
    compose_plugin = re.search(r'org.jetbrains.kotlin.plugin.compose"\) version "([\d.]+)"', root).group(1)
    assert kotlin == compose_plugin                                   # Kotlin 2.x: Compose compiler plugin Kotlin ilə EYNİ versiya olmalıdır
    assert kotlin.startswith("2.")
    for sub in ("jvm-contract", "compose-check"):
        assert f'kotlin("jvm") version "{kotlin}"' in read(ANDROID / sub / "build.gradle.kts"), sub
    assert 'kotlin("plugin.compose") version "%s"' % kotlin in read(ANDROID / "compose-check" / "build.gradle.kts")


def test_agp_gradle_jdk_and_sdk_levels_are_consistent_and_documented():
    root, app = read(ANDROID / "build.gradle.kts"), read(APP / "build.gradle.kts")
    assert 'com.android.application") version "8.5.2"' in root
    assert "compileSdk = 34" in app and "targetSdk = 34" in app and "minSdk = 26" in app
    assert "JavaVersion.VERSION_17" in app and 'jvmTarget = "17"' in app
    status = read(ANDROID / "BUILD_STATUS.md")
    for need in ("Gradle 8.7+", "AGP 8.5.2", "platforms;android-34", "build-tools;34.0.0", "dl.google.com"):
        assert need in status, need


def test_compose_bom_matches_the_type_check_runtime_and_dependencies_are_pinned():
    app = read(APP / "build.gradle.kts")
    assert 'platform("androidx.compose:compose-bom:2024.09.00")' in app          # Compose 1.7.0 / Material3 1.3.0
    assert 'org.jetbrains.compose.material3:material3:1.7.0' in read(ANDROID / "compose-check" / "build.gradle.kts")
    for dep in re.findall(r'implementation\("([^"]+)"\)', app):
        bom_managed = dep.startswith("androidx.compose.") and dep.count(":") == 1                  # versiyanı BOM təyin edir
        assert (dep.count(":") == 2 or bom_managed) and not dep.endswith(":+") and "latest" not in dep, dep
    assert "signingConfigs" not in app and "storeFile" not in app and ".keystore" not in app    # imzalama/açar qurulmayıb
    assert "debuggable" not in app                                                              # release-də debug açılmır


def test_manifest_is_locked_down_and_only_the_launcher_activity_is_exported():
    m = read(MAIN / "AndroidManifest.xml")
    ns = "{http://schemas.android.com/apk/res/android}"
    root = ET.fromstring(m)
    app = root.find("application")
    assert app.get(ns + "allowBackup") == "false" and app.get(ns + "usesCleartextTraffic") == "false"
    exported = [(c.tag, c.get(ns + "name")) for c in app if c.get(ns + "exported") == "true"]
    assert exported == [("activity", ".MainActivity")]
    assert "android:debuggable" not in m and "<provider" not in m and "<receiver" not in m
    assert "android.permission.USE_BIOMETRIC" in m and "android.permission.INTERNET" in m
    # cleartext yalnız debug quruluşunda və yalnız yerli hostlar üçün
    debug_cfg = read(APP / "src" / "debug" / "res" / "xml" / "network_security_config.xml")
    assert set(re.findall(r">([^<]+)</domain>", debug_cfg)) == {"10.0.2.2", "localhost", "127.0.0.1"}
    assert "domain-config" not in read(MAIN / "res" / "xml" / "network_security_config.xml")


@pytest.mark.parametrize("locale", ["values", "values-en", "values-ru", "values-tr"])
def test_string_resources_follow_aapt_rules(locale):
    root = ET.parse(MAIN / "res" / locale / "strings.xml").getroot()
    names = [e.get("name") for e in root.findall("string")]
    assert len(names) == len(set(names))
    for e in root.findall("string"):
        t = e.text or ""
        assert not re.search(r"(?<!\\)'", t) or t.startswith('"'), (locale, e.get("name"))       # apostrof qaçırılmalıdır
        assert t[:1] not in "@?", (locale, e.get("name"))
        assert not re.search(r"%(?!\d\$[sd]|%)", t), (locale, e.get("name"), t)                  # yalnız mövqeli format


def test_keystore_and_biometric_integration_invariants():
    key = read(MAIN / "java/az/naymera/app/security/DeviceKey.kt")
    step = read(MAIN / "java/az/naymera/app/security/StepUp.kt")
    for needle in ('"AndroidKeyStore"', "PURPOSE_SIGN", "setUserAuthenticationRequired(true)", "setInvalidatedByBiometricEnrollment(true)",
                   "AUTH_BIOMETRIC_STRONG", "SHA256withECDSA", 'ECGenParameterSpec("secp256r1")'):
        assert needle in key, needle
    assert "setUserAuthenticationParameters(0," in key and "SDK_INT >= Build.VERSION_CODES.R" in key      # API 30+ hər istifadədə; köhnədə -1
    assert "setUserAuthenticationValidityDurationSeconds(-1)" in key
    assert "privateKey" not in key.replace("PrivateKey", "") and ".encoded" in key                    # yalnız açıq açar çıxarılır
    for needle in ("BIOMETRIC_STRONG", "CryptoObject", "prompt.authenticate(info, crypto)", "onAuthenticationSucceeded", 'METHOD = "biometric"'):
        assert needle in step, needle
    assert "DEVICE_CREDENTIAL" not in step and "BIOMETRIC_WEAK" not in step                          # zəif/PIN geri-çəkilişi yoxdur
    signer = read(MAIN / "java/az/naymera/app/ui/CoreViewModel.kt")
    assert "StepUp.sign(" in signer and "challenge.message" in signer                                  # imzalanan baytlar göstərilən SignableChallenge-dən


def test_all_navigation_routes_have_composables_and_resources():
    nav = read(MAIN / "java/az/naymera/app/NaymeraApp.kt")
    domain = read(MAIN / "java/az/naymera/app/domain/Navigation.kt")
    routes = set(re.findall(r'\("([a-z]+)", (?:true|false)\)', domain))
    assert routes == {"home", "assistant", "goals", "approvals", "memory", "journal", "settings"}
    for d in ("Home", "Assistant", "Goals", "Approvals", "Memory", "Journal", "Settings"):
        assert f"composable(Destination.{d}.route)" in nav, d                                         # hər təyinatın ekranı var
        assert f"Destination.{d} ->" in nav, d                                                         # ikon + etiket xəritələri tamdır
    assert "composable(Destination.GOAL_DETAIL)" in nav
    for extra in ("notifications", "agents", "capabilities", "history"):
        assert f'composable("{extra}")' in nav and f'"{extra}"' in nav, extra
    keys = {e.get("name") for e in ET.parse(MAIN / "res/values/strings.xml").getroot().findall("string")}
    used = set(re.findall(r"R\.string\.([a-z0-9_]+)", "\n".join(read(p) for p in MAIN.rglob("*.kt"))))
    assert used <= keys, used - keys


def test_every_kotlin_import_of_project_symbols_resolves():
    """Layihə daxili `az.naymera.app.*` import-ları mövcud simvola işarə etməlidir (kompilyator olmadan sürətli yoxlama)."""
    src = MAIN / "java"
    declared: set[str] = set()
    for p in src.rglob("*.kt"):
        text = read(p)
        pkg = re.search(r"^package ([\w.]+)", text, re.M).group(1)
        for m in re.finditer(r"^(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:private|internal|public|data|sealed|enum|abstract|open|const|inline|suspend|annotation|fun|interface|class|object|val|var|typealias)\s+)+(?:<[^>]+>\s*)?(?:[\w.<>?]+\.)?(\w+)", text, re.M):
            declared.add(f"{pkg}.{m.group(1)}")
        for m in re.finditer(r"^\s+(?:data object|object|data class|class)\s+(\w+)", text, re.M):
            declared.add(f"{pkg}.{m.group(1)}")
    for p in src.rglob("*.kt"):
        for imp in re.findall(r"^import (az\.naymera\.app\.[\w.]+)$", read(p), re.M):
            assert imp in declared or imp == "az.naymera.app.R", (p.name, imp)                        # R — AGP-nin yaratdığı sinifdir


@pytest.mark.skipif(not os.environ.get("NAYMERA_COMPOSE_CHECK"), reason="opt-in: NAYMERA_COMPOSE_CHECK=1 (gradle + Maven Central lazımdır)")
def test_compose_sources_type_check_against_compose_multiplatform():
    assert shutil.which("gradle")
    cmd = ["gradle", "--no-daemon", "-q", "-p", str(ANDROID / "compose-check"), "compileKotlin"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
