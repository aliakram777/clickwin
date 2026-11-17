"""Android layihəsinin statik yoxlaması. DİQQƏT: bu mühitdə Android SDK yoxdur və APK build EDİLMƏYİB; testlər yalnız
layihə quruluşunu, təhlükəsizlik konfiqurasiyasını və server ilə kontraktı yoxlayır — APK-nın işlədiyini sübut etmir."""
import re
import subprocess
from pathlib import Path

import pytest

from agents.memory import detect_secrets
from api.app import ApiApp  # noqa: F401  (marşrut cədvəli üçün)

ROOT = Path(__file__).resolve().parent.parent
AND = ROOT / "android"
APP = AND / "app"
SRC = APP / "src" / "main"
KT = sorted(SRC.rglob("*.kt")) if SRC.exists() else []


def text(path):
    return Path(path).read_text(encoding="utf-8")


def all_text_files():
    return [p for p in AND.rglob("*") if p.is_file() and p.suffix in {".kt", ".kts", ".xml", ".md", ".properties",
                                                                      ".json", ".txt", ".gradle", ".pro"}]


def test_project_structure_exists():
    for rel in ("settings.gradle.kts", "build.gradle.kts", "gradle.properties", "README.md", "BUILD_STATUS.md",
                "app/build.gradle.kts", "app/proguard-rules.pro", "app/src/main/AndroidManifest.xml",
                "app/src/main/res/values/strings.xml", "app/src/main/res/values/themes.xml",
                "app/src/main/res/xml/network_security_config.xml",
                "app/src/debug/res/xml/network_security_config.xml"):
        assert (AND / rel).is_file(), rel
    names = {p.name for p in KT}
    for expected in ("MainActivity.kt", "NaymeraApp.kt", "Theme.kt", "ChatScreen.kt", "ChatViewModel.kt",
                     "NotificationsScreen.kt", "HistoryScreen.kt", "PairingScreen.kt", "ApiClient.kt",
                     "SessionStore.kt", "Models.kt", "EventSync.kt", "VoiceInterfaces.kt", "StepUp.kt",
                     "ApprovalScreen.kt"):
        assert expected in names, expected


def test_gradle_declares_a_real_application_with_a_minimal_free_dependency_set():
    gradle = text(APP / "build.gradle.kts")
    assert 'applicationId = "az.naymera.app"' in gradle and "compileSdk" in gradle and "minSdk" in gradle
    assert "isMinifyEnabled = true" in gradle
    deps = re.findall(r'(?:implementation|debugImplementation|testImplementation)\(\s*(?:platform\()?"([^"]+)"', gradle)
    assert deps
    allowed_prefixes = ("androidx.", "org.jetbrains.kotlin", "org.jetbrains.kotlinx", "com.squareup.okhttp3",
                        "com.google.android.material", "junit:junit")
    assert all(d.startswith(allowed_prefixes) for d in deps), deps
    banned = ("firebase", "crashlytics", "admob", "play-services", "billing", "revenuecat", "sentry", "mixpanel",
              "facebook", "amplitude", "appsflyer", "adjust", "segment", "onesignal")
    blob = (gradle + text(AND / "build.gradle.kts") + text(AND / "settings.gradle.kts")).lower()
    assert not [b for b in banned if b in blob]


def test_manifest_is_locked_down():
    m = text(SRC / "AndroidManifest.xml")
    for perm in ("INTERNET", "RECORD_AUDIO", "POST_NOTIFICATIONS", "USE_BIOMETRIC"):
        assert f"android.permission.{perm}" in m, perm
    assert 'android:allowBackup="false"' in m and 'android:usesCleartextTraffic="false"' in m
    assert "android:networkSecurityConfig" in m and "android:debuggable" not in m
    assert 'android:exported="true"' in m and m.count('android:exported="true"') == 1  # yalnız MainActivity
    for risky in ("READ_SMS", "READ_CONTACTS", "ACCESS_FINE_LOCATION", "SYSTEM_ALERT_WINDOW", "READ_EXTERNAL_STORAGE",
                  "WRITE_EXTERNAL_STORAGE", "CAMERA", "READ_PHONE_STATE"):
        assert risky not in m, risky


def test_cleartext_is_allowed_only_for_local_debug_hosts():
    release = text(APP / "src" / "main" / "res" / "xml" / "network_security_config.xml")
    assert 'cleartextTrafficPermitted="false"' in release and 'cleartextTrafficPermitted="true"' not in release
    debug = text(APP / "src" / "debug" / "res" / "xml" / "network_security_config.xml")
    domains = re.findall(r"<domain[^>]*>([^<]+)</domain>", debug)
    assert set(domains) <= {"10.0.2.2", "localhost", "127.0.0.1"} and domains
    for p in all_text_files():
        if p.name == "network_security_config.xml" or p.suffix == ".md":
            continue
        for url in re.findall(r"http://[^\s\"')]+", text(p)):
            if url.startswith(("http://schemas.android.com/", "http://www.w3.org/")):  # XML ad sahələri (şəbəkə deyil)
                continue
            assert re.match(r"http://(10\.0\.2\.2|localhost|127\.0\.0\.1)\b", url), (p.name, url)


def test_no_secrets_or_api_keys_are_embedded_in_the_android_project():
    for p in all_text_files():
        content = text(p)
        assert detect_secrets(content) == [], p
        assert not re.search(r"(?i)(api[_-]?key|secret[_-]?key|client[_-]?secret)\s*=\s*\"[^\"]+\"", content), p
    for p in KT:
        c = text(p)
        assert "BuildConfig" not in c or "API_KEY" not in c, p
        assert not re.search(r'"(sk-|AKIA|ghp_|xox)[A-Za-z0-9]', c), p


def test_no_apk_or_fake_build_artifacts_are_committed():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert not [f for f in out if f.endswith((".apk", ".aab", ".jar", ".keystore", ".jks"))]
    status = text(AND / "BUILD_STATUS.md")
    assert "build EDİLMƏYİB" in status and "Android SDK" in status


def test_api_client_endpoints_exist_on_the_server():
    from api.app import ApiApp as _A  # noqa: F401
    routes = _server_routes()
    used = set(re.findall(r'"(/v1/[A-Za-z0-9_/{}\-]+)"', text(next(p for p in KT if p.name == "ApiClient.kt"))))
    assert used
    for path in used:
        norm = re.sub(r"\{[^}]+\}", "{id}", path)
        assert any(re.sub(r"\{[^}]+\}", "{id}", r) == norm for r in routes), path


def _server_routes():
    import tempfile
    from api.bootstrap import build_app
    with tempfile.TemporaryDirectory() as d:
        svc = build_app(Path(d))
        return {p for _, p, _ in svc.app.router.routes()}


def test_kotlin_models_cover_the_server_json_keys():
    models = text(next(p for p in KT if p.name == "Models.kt"))
    names = set(re.findall(r'@SerialName\("([a-z_]+)"\)', models))
    required = {"access_token", "refresh_token", "device_secret", "device_id", "conversation_id", "user_message",
                "assistant_message", "speech_text", "is_mock", "provenance", "input_mode", "importance", "priority",
                "dedupe_key", "schema_version", "events", "next_cursor", "challenge_id", "nonce", "payload_hash",
                "approvals", "expires_in", "stop_active", "pending_approvals", "untrusted"}
    assert required <= names, required - names


def test_voice_layer_is_separate_labeled_and_never_reaches_approvals():
    voice = text(next(p for p in KT if p.name == "VoiceInterfaces.kt"))
    for needle in ("interface SpeechToText", "interface TextToSpeech", "isMock", "az-AZ", "FEMALE", "MockSpeech"):
        assert needle in voice, needle
    for p in KT:
        if p.name in {"StepUp.kt", "ApprovalScreen.kt"}:  # təsdiq kodu səs qatından təcrid olunub
            c = text(p)
            for banned in ("SpeechToText", "TextToSpeech", "SpeechRecognizer", "VoiceInterfaces", 'method = "voice"'):
                assert banned not in c, (p.name, banned)
    step = text(next(p for p in KT if p.name == "StepUp.kt"))
    assert "BiometricPrompt" in step and '"voice"' not in step and "biometric" in step
    strings = text(SRC / "res" / "values" / "strings.xml")
    assert "SİMULYASİYA" in strings and "Söhbət" in strings and "Bildirişlər" in strings and "Tarixçə" in strings


def test_session_storage_never_holds_server_or_third_party_keys():
    store = text(next(p for p in KT if p.name == "SessionStore.kt"))
    assert "EncryptedSharedPreferences" in store and "refresh_token" in store and "device_secret" in store
    assert "api_key" not in store.lower() and "openai" not in store.lower() and "anthropic" not in store.lower()


def test_ui_has_the_required_screens():
    nav = text(next(p for p in KT if p.name == "NaymeraApp.kt"))
    for dest in ("chat", "notifications", "history", "settings"):
        assert f'"{dest}"' in nav, dest
    chat = text(next(p for p in KT if p.name == "ChatScreen.kt"))
    assert "isMock" in chat and "OutlinedTextField" in chat or "TextField" in chat
    assert "Mic" in chat  # səsli ünsiyyət düyməsi
