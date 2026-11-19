"""Stage 18.14: Android debug APK workflow-nun STATİK yoxlaması (workflow-u işə SALMIR; real build yalnız GitHub Actions-da).
Təhlükəsizlik invariantları: yalnız oxuma icazəsi, secret/imza açarı/release/nəşr yoxdur, action-lar SHA-ya pinlənib, artefakt yalnız debug APK."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "android-debug-apk.yml"


def load():
    text = WF.read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_workflow_is_valid_yaml_with_the_expected_triggers_and_minimal_permissions():
    text, wf = load()
    on = wf.get("on", wf.get(True))                                   # PyYAML "on" -> True
    assert set(on) == {"workflow_dispatch", "pull_request"}            # push/schedule/release tetikləyicisi yoxdur
    assert wf["permissions"] == {"contents": "read"}                   # başqa icazə yoxdur (contents: write / id-token / packages / releases yoxdur)
    for job in wf["jobs"].values():
        assert "permissions" not in job or job["permissions"] == {"contents": "read"}
        assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] <= 60


def test_no_secrets_signing_keys_publishing_or_release_steps():
    text, wf = load()
    code = "\n".join(line.split(" #", 1)[0] if not line.lstrip().startswith("#") else "" for line in text.splitlines())   # şərhlər xaric
    lowered = code.lower()
    assert "secrets." not in lowered and "${{ secrets" not in lowered
    for banned in ("keystore", "storepass", "keypass", "signingconfig", "gh release", "softprops/action-gh-release", "create-release", "gh-pages",
                   "publish", "deploy", "playstore", "google-play", "firebase", "id-token", "contents: write", "packages: write", "git push", "docker push"):
        assert banned not in lowered, banned
    assert "assemblerelease" not in lowered and "bundle" not in lowered
    assert wf["jobs"]["build-debug-apk"]["steps"], "addım yoxdur"


def test_third_party_actions_are_pinned_to_full_commit_shas():
    text, wf = load()
    uses = re.findall(r"uses:\s*([^\s#]+)", text)
    assert uses
    for u in uses:
        name, _, ref = u.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), u                  # hərəkətli tag/branch deyil, dəyişməz SHA


def test_build_uses_jdk17_gradle_89_sdk_34_and_assemble_debug_only():
    text, wf = load()
    env = wf["env"]
    assert env["JAVA_VERSION"] == "17" and env["GRADLE_VERSION"] == "8.9"
    assert env["ANDROID_PLATFORM"] == "android-34" and env["ANDROID_BUILD_TOOLS"] == "34.0.0"
    assert "distribution: temurin" in text
    assert ":app:assembleDebug" in text and "-p android" in text
    assert "platforms;${{ env.ANDROID_PLATFORM }}" in text and "build-tools;${{ env.ANDROID_BUILD_TOOLS }}" in text
    app = (ROOT / "android" / "app" / "build.gradle.kts").read_text()
    assert "compileSdk = 34" in app and "VERSION_17" in app            # workflow-un tələb etdiyi SDK/JDK build faylı ilə uyğundur
    for sub in ("jvm-contract", "compose-check"):                      # JDK 17-də toolchain 21 tələb olunmur
        g = (ROOT / "android" / sub / "build.gradle.kts").read_text()
        assert "jvmToolchain(21)" not in g and "JVM_17" in g, sub


def test_focused_tests_run_before_the_build_and_apk_is_verified_then_uploaded_as_artifact_only():
    text, wf = load()
    names = [s.get("name", "") for s in wf["jobs"]["build-debug-apk"]["steps"]]
    order = {n: i for i, n in enumerate(names)}
    assert order["Focused tests (Python)"] < order["JVM contract tests (real Python backend on loopback)"] < order["Build debug APK"]
    assert order["Compose type-check"] < order["Build debug APK"] < order["Verify the APK (real file, valid package, debug-signed)"] < order["Upload debug APK (workflow artifact only)"]
    upload = [s for s in wf["jobs"]["build-debug-apk"]["steps"] if s.get("name") == "Upload debug APK (workflow artifact only)"][0]
    assert upload["with"]["path"] == "android/app/build/outputs/apk/debug/*.apk"
    assert upload["with"]["if-no-files-found"] == "error"              # APK yoxdursa iş QIRMIZIdır (səssiz keçmir)
    assert upload["with"]["retention-days"] <= 30
    assert "aapt2" in text and "apksigner" in text and "az.naymera.app" in text
    # Focused testlər mövcud fayllardır
    for t in re.findall(r"tests/(test_\w+\.py)", text):
        assert (ROOT / "tests" / t).exists(), t


def test_workflow_does_not_touch_production_security_settings():
    text, _ = load()
    for banned in ("NAYMERA_SIGNING_KEY", "NAYMERA_", "OPENAI", "ANTHROPIC", "API_KEY", "serve.py", "build_app("):
        if banned == "NAYMERA_":
            assert "NAYMERA_" not in text.replace("NAYMERA_GRADLE", ""), banned
        else:
            assert banned not in text, banned
