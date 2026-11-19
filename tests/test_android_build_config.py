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


# ----------------------------------------------------------------------------------------------------------------------
# Stage 18.15+: CI-nin real `compileDebugKotlin` addımı `SessionStore.kt`-də `DeviceKey` import-unun olmadığını üzə çıxardı
# (Unresolved reference: DeviceKey). Android-dan asılı fayllar JVM/Compose yoxlamalarında stub ilə əvəz olunduğu üçün gizli qalmışdı.
# ----------------------------------------------------------------------------------------------------------------------
_KT_DECL = re.compile(
    r"^(?:(?:data|sealed|enum|abstract|open|annotation|inline|value|fun)\s+)*"
    r"(?P<kind>class|object|interface|typealias)\s+(?P<name>\w+)", re.M)
_KT_FUN = re.compile(r"^(?:(?:inline|suspend|operator|infix)\s+)*fun\s+(?:<[^>]+>\s*)?(?P<recv>[\w.<>?]+\.)?(?P<name>\w+)\s*\(", re.M)
_KT_VAL = re.compile(r"^(?:const\s+)?(?:val|var)\s+(?:(?P<recv>[\w.<>?]+)\.)?(?P<name>\w+)\b", re.M)


def _strip_kotlin(text: str) -> str:
    """Şərhlər və sətir literalları çıxarılır: yalnız real kod yoxlanılır (şərhdə `DeviceKey` yazmaq istifadə sayılmır)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    return re.sub(r"^(?:import|package)\s[^\n]*$", "", text, flags=re.M)          # import/package sətirləri kod deyil (`ui.res` ≠ `res` funksiyası)


def find_missing_cross_package_imports(files: dict[str, str]) -> list[tuple[str, str, str]]:
    """Hər Kotlin faylı üçün: başqa paketdəki layihə simvolu (sinif/obyekt/interfeys/typealias, yuxarı səviyyəli fun/val) kodda istifadə olunur,
    lakin nə eyni paketdədir, nə dəqiq (explicit) və ya `.*` ilə import olunub, nə də tam-ixtisaslı (`az.naymera.app.ui.UiText`) yazılıb → (fayl, ad, paket).
    Genişləndirici funksiyalar (`fun Throwable.toUiText()`) nöqtə ilə çağırılsa da import tələb edir; digər simvollar üçün qarşısındakı nöqtə (üzv/ixtisaslı) istifadədir."""
    pkg_of: dict[str, str] = {}
    declared: dict[str, dict[str, bool]] = {}          # ad -> {paket: genişləndiricidirmi}
    for path, text in files.items():
        pkg = re.search(r"^package ([\w.]+)", text, re.M).group(1)
        pkg_of[path] = pkg
        code = _strip_kotlin(text)
        for m in _KT_DECL.finditer(code):
            if not re.match(r"\s*private\b", m.group(0)):
                declared.setdefault(m.group("name"), {})[pkg] = False
        for m in _KT_FUN.finditer(code):
            declared.setdefault(m.group("name"), {})[pkg] = declared.get(m.group("name"), {}).get(pkg, False) or bool(m.group("recv"))
        for m in _KT_VAL.finditer(code):
            declared.setdefault(m.group("name"), {})[pkg] = declared.get(m.group("name"), {}).get(pkg, False) or bool(m.group("recv"))
    problems: list[tuple[str, str, str]] = []
    for path, text in files.items():
        pkg, code = pkg_of[path], _strip_kotlin(text)
        imports = set(re.findall(r"^import ([\w.*]+)", text, re.M))
        for name, owners in declared.items():
            if pkg in owners:                                           # eyni paket: import lazım deyil
                continue
            if re.search(rf"^[ \t]+(?:[a-z]+\s+)*(?:val|var|fun|class|object|interface)\s+(?:<[^>]+>\s*)?(?:[\w.<>?]+\.)?{re.escape(name)}\b", code, re.M):
                continue                                                # fayl adı özü bəyan edir (üzv/lokal): xarici simvolu kölgələyir, import lazım deyil
            is_ext = any(owners.values())
            pattern = rf"(?<![\w]){re.escape(name)}\b" if is_ext else rf"(?<![\w.]){re.escape(name)}\b"
            for m in re.finditer(pattern, code):
                before = code[max(0, m.start() - 80):m.start()]
                if any(before.endswith(f"{o}.") for o in owners):       # tam-ixtisaslı istifadə
                    continue
                if any(f"{o}.{name}" in imports or f"{o}.*" in imports for o in owners):
                    break
                if is_ext and not before.endswith("."):                 # genişləndirici funksiyanın öz bəyanı/ad toqquşması deyil, çağırışı deyil
                    continue
                problems.append((path, name, sorted(owners)[0]))
                break
    return problems


def _real_kotlin_sources() -> dict[str, str]:
    return {str(p.relative_to(MAIN / "java")): read(p) for p in (MAIN / "java").rglob("*.kt")}


def test_session_store_imports_and_uses_the_real_device_key():
    """Reqressiya: `SessionStore` real `az.naymera.app.security.DeviceKey`-i import edir və çağırır (stub/saxta əvəz yoxdur)."""
    store = read(MAIN / "java/az/naymera/app/data/SessionStore.kt")
    assert re.search(r"^import az\.naymera\.app\.security\.DeviceKey$", store, re.M)
    assert "DeviceKey.exists()" in store and "DeviceKey.delete()" in store
    key = read(MAIN / "java/az/naymera/app/security/DeviceKey.kt")
    assert re.search(r"^package az\.naymera\.app\.security$", key, re.M) and re.search(r"^object DeviceKey\b", key, re.M)
    assert '"AndroidKeyStore"' in key and "PURPOSE_SIGN" in key                                          # saxta deyil: həqiqi Keystore açarı
    assert 'object DeviceKey' not in read(ANDROID / "compose-check" / "stubs" / "StubSessionStore.kt")  # stub DeviceKey-i təkrar bəyan etmir
    assert not (MAIN / "java/az/naymera/app/data/DeviceKey.kt").exists()                                  # ikinci (saxta) DeviceKey yoxdur
    assert sum(1 for t in _real_kotlin_sources().values() if re.search(r"^object DeviceKey\b", t, re.M)) == 1


def test_no_missing_cross_package_imports_in_the_real_sources():
    assert find_missing_cross_package_imports(_real_kotlin_sources()) == []


def test_missing_import_detector_flags_real_gaps_and_accepts_valid_forms():
    a = "package p.sec\n\nobject DeviceKey { fun exists() = true }\nfun Throwable.toKind(): Int = 1\nfun helper(): Int = 2\n"
    def run(use: str, extra: str = "") -> list:
        b = f"package p.data\n{extra}\nclass Store {{ {use} }}\n"
        return find_missing_cross_package_imports({"a.kt": a, "b.kt": b})
    assert run("val x = DeviceKey.exists()") == [("b.kt", "DeviceKey", "p.sec")]                         # SessionStore hadisəsi
    assert run("val x = DeviceKey.exists()", "import p.sec.DeviceKey") == []                              # dəqiq import
    assert run("val x = DeviceKey.exists()", "import p.sec.*") == []                                      # ulduzlu import
    assert run("val x = p.sec.DeviceKey.exists()") == []                                                  # tam-ixtisaslı (NaymeraApp/UiText hadisələri)
    assert run("fun f(k: p.sec.DeviceKey) {}") == []
    assert run("// DeviceKey.exists() şərhdə\n val s = \"DeviceKey\"") == []                              # şərh/sətir istifadə deyil
    assert run("val x = helper()") == [("b.kt", "helper", "p.sec")]                                       # yuxarı səviyyəli funksiya
    assert run("val x = helper()", "import p.sec.helper") == []
    assert run("fun f(e: Exception) = e.toKind()") == [("b.kt", "toKind", "p.sec")]                       # genişləndirici funksiya nöqtə ilə də import tələb edir
    assert run("fun f(e: Exception) = e.toKind()", "import p.sec.toKind") == []
    assert run("val x = foo.DeviceKey") == []                                                              # başqa obyektin üzvü (nöqtədən sonra): xəta deyil
    same = find_missing_cross_package_imports({"a.kt": a, "c.kt": "package p.sec\nval y = DeviceKey.exists()\n"})
    assert same == []                                                                                      # eyni paket
    shadow = "package p.data\nobject Own {\n    val helper = 1\n    fun use() = helper\n}\n"
    assert find_missing_cross_package_imports({"a.kt": a, "b.kt": shadow}) == []                         # fayl öz üzvünü bəyan edir (CoreChallenge.STEP_RESOLUTIONS)
    assert find_missing_cross_package_imports({"a.kt": a.replace("object DeviceKey", "private object DeviceKey"), "b.kt": "package p.data\nval x = DeviceKey\n"}) == []   # private simvol
