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
    return [p for p in AND.rglob("*") if p.is_file() and not ({"build", ".gradle"} & set(p.relative_to(AND).parts)) and p.suffix in {".kt", ".kts", ".xml", ".md", ".properties",
                                                                      ".json", ".txt", ".gradle", ".pro"}]


def test_project_structure_exists():
    for rel in ("settings.gradle.kts", "build.gradle.kts", "gradle.properties", "README.md", "BUILD_STATUS.md",
                "app/build.gradle.kts", "app/proguard-rules.pro", "app/src/main/AndroidManifest.xml",
                "app/src/main/res/values/strings.xml", "app/src/main/res/values/themes.xml",
                "app/src/main/res/xml/network_security_config.xml",
                "app/src/debug/res/xml/network_security_config.xml", "app/src/main/res/xml/locales_config.xml",
                "app/src/main/res/values-en/strings.xml", "app/src/main/res/values-ru/strings.xml",
                "app/src/main/res/values-tr/strings.xml"):
        assert (AND / rel).is_file(), rel
    names = {p.name for p in KT}
    for expected in ("MainActivity.kt", "NaymeraApp.kt", "Theme.kt", "ChatScreen.kt", "ChatViewModel.kt",
                     "NotificationsScreen.kt", "HistoryScreen.kt", "PairingScreen.kt", "ApiClient.kt",
                     "SessionStore.kt", "Models.kt", "EventSync.kt", "VoiceInterfaces.kt", "StepUp.kt",
                     "ApprovalScreen.kt", "DeviceKey.kt", "HomeScreen.kt", "MemoryScreen.kt", "AgentsScreen.kt",
                     "ApiFailure.kt", "UiText.kt"):
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
    """ApiApp + CoreApiApp (`/v1/core/*`) marşrutları: Android klienti hər ikisini istifadə edir (CoreApiApp istehsala QOŞULMAYIB, dev serveri ilə işləyir)."""
    import importlib.util
    import sys
    import tempfile
    from api.bootstrap import build_app
    with tempfile.TemporaryDirectory() as d:
        svc = build_app(Path(d))
        routes = {p for _, p, _ in svc.app.router.routes()}
    spec = importlib.util.spec_from_file_location("serve_core_dev", Path(__file__).resolve().parent.parent / "tools" / "serve_core_dev.py")
    mod = sys.modules.get("serve_core_dev") or importlib.util.module_from_spec(spec)
    sys.modules["serve_core_dev"] = mod
    if not hasattr(mod, "build_core_dev"):
        spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as d:
        dev = mod.build_core_dev(Path(d), demo_loop=False)
        return routes | {p for _, p, _ in dev.app.router.routes()}


def test_kotlin_models_cover_the_server_json_keys():
    models = text(next(p for p in KT if p.name == "Models.kt"))
    names = set(re.findall(r'@SerialName\("([a-z_]+)"\)', models))
    required = {"access_token", "refresh_token", "public_key", "signature", "backend_mode", "agent_runner", "voice_tts",
                "push", "step_up", "scheme", "device_id", "conversation_id", "user_message",
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
    assert "Yaddaş" in strings and "Agentlər" in strings


def test_session_storage_never_holds_server_or_third_party_keys():
    store = text(next(p for p in KT if p.name == "SessionStore.kt"))
    assert "EncryptedSharedPreferences" in store and "refresh_token" in store
    assert "device_secret" not in store and "privateKey" not in store  # şəxsi açar yalnız Android Keystore-dadır
    assert "api_key" not in store.lower() and "openai" not in store.lower() and "anthropic" not in store.lower()


def test_ui_has_the_required_screens():
    nav = text(next(p for p in KT if p.name == "NaymeraApp.kt"))
    for dest in ("home", "chat", "memory", "notifications", "agents", "history", "settings"):
        assert f'"{dest}"' in nav, dest
    chat = text(next(p for p in KT if p.name == "ChatScreen.kt"))
    assert "isMock" in chat and "OutlinedTextField" in chat or "TextField" in chat
    assert "Mic" in chat  # səsli ünsiyyət düyməsi
    assert "enabled = vm.voiceAvailable" in chat  # real STT yoxdursa düymə söndürülür (işləyirmiş kimi göstərilmir)


# --- Stage 14: Keystore, imza, yerlişdirmə (i18n), şəbəkə xətaları, mock/real fərqi ---

ALL_KT = "\n".join(text(p) for p in KT)


def strings_of(locale_dir):
    xml = text(SRC / "res" / locale_dir / "strings.xml")
    return dict(re.findall(r'<string name="([a-z0-9_]+)">([^<]*)</string>', xml))


def test_device_key_lives_in_android_keystore_behind_biometrics_and_never_leaves_the_device():
    key = text(next(p for p in KT if p.name == "DeviceKey.kt"))
    for needle in ("AndroidKeyStore", "KeyGenParameterSpec", "KeyProperties.PURPOSE_SIGN", "secp256r1", "SHA256withECDSA",
                   "setUserAuthenticationRequired(true)", "setInvalidatedByBiometricEnrollment(true)",
                   "Signature.getInstance", "initSign", "publicKeySpki", "BiometricPrompt.CryptoObject"):
        assert needle in key, needle
    for banned in ("SharedPreferences", "privateKey.encoded", "private.encoded", "getEncoded()", "ObjectOutputStream", "Log."):
        assert banned not in key, banned
    # şəxsi açar yoxdur: yalnız açıq açar göndərilir
    assert "PairRequest" in ALL_KT and "public_key" in text(next(p for p in KT if p.name == "Models.kt"))


def test_no_shared_secret_or_hmac_remains_anywhere_in_the_android_project():
    for banned in ("device_secret", "deviceSecret", "HmacSHA256", "SecretKeySpec", "Mac.getInstance"):
        assert banned not in ALL_KT, banned


def test_step_up_signs_with_the_keystore_key_and_matches_the_server_message_format():
    step = text(next(p for p in KT if p.name == "StepUp.kt"))
    msgs = text(next(p for p in KT if p.name == "Messages.kt"))
    assert "DeviceKey" in step and "CryptoObject" in step and '"naymera-step-up"' in msgs and 'joinToString("|")' in msgs
    assert "Messages.challengeMessage" in step
    assert 'method = "voice"' not in step and "voice" not in step.lower().replace("voice_", "")
    key = text(next(p for p in KT if p.name == "DeviceKey.kt"))
    assert '"naymera-pair|' in text(next(p for p in KT if p.name == "Messages.kt")) and "Messages.pairMessage" in key


def test_network_failures_timeouts_and_disconnects_are_modeled_explicitly():
    fail = text(next(p for p in KT if p.name == "ApiFailure.kt"))
    for kind in ("Offline", "Timeout", "Unauthorized", "Forbidden", "Conflict", "RateLimited", "Server", "Unavailable", "Unknown"):
        assert kind in fail, kind
    client = text(next(p for p in KT if p.name == "ApiClient.kt"))
    for needle in ("callTimeout", "connectTimeout", "readTimeout", "SocketTimeoutException", "UnknownHostException",
                   "ConnectException", "SSLException", "IOException", "Mutex", 'method == "GET"', "backoff"):
        assert needle in client, needle
    assert "followRedirects(false)" in client
    sync = text(next(p for p in KT if p.name == "EventSync.kt"))
    for needle in ("ConnectionState", "Connected", "Reconnecting", "Offline", "backoff", "401"):
        assert needle in sync, needle


def test_mock_and_real_backend_modes_are_visibly_distinguished_and_missing_features_are_not_faked():
    home = text(next(p for p in KT if p.name == "HomeScreen.kt"))
    for needle in ("backendMode", "mode_mock", "mode_real", "agentRunner", "feature_unavailable", "capabilities"):
        assert needle in home, needle
    agents = text(next(p for p in KT if p.name == "AgentsScreen.kt"))
    assert "agent_runner_missing" in agents and "agentRunner" in agents
    notif = text(next(p for p in KT if p.name == "NotificationsScreen.kt"))
    assert "push_unavailable" in notif
    client = text(next(p for p in KT if p.name == "ApiClient.kt"))
    assert "/v1/capabilities" in client and "/v1/memory" in client and "/v1/company/agents" in client


def test_azerbaijani_is_the_default_and_en_ru_tr_are_complete():
    az = strings_of("values")
    assert "Söhbət" in az.values() and len(az) >= 40
    for loc in ("values-en", "values-ru", "values-tr"):
        other = strings_of(loc)
        assert set(other) == set(az), (loc, set(az) ^ set(other))
        assert all(v.strip() for v in other.values()), loc
        same = [k for k in az if other[k] == az[k] and len(az[k]) > 6 and k != "app_name" and not k.startswith("lang_")]
        assert len(same) <= 8, (loc, same)  # tərcümə edilməmiş (kopya) sətirlər
    # sətir formatlaması: yer tutucular dillər arasında uyğundur
    for loc in ("values-en", "values-ru", "values-tr"):
        other = strings_of(loc)
        for k, v in az.items():
            assert sorted(re.findall(r"%\d\$[sd]", v)) == sorted(re.findall(r"%\d\$[sd]", other[k])), (loc, k)
    cfg = text(SRC / "res" / "xml" / "locales_config.xml")
    assert all(f'android:name="{l}"' in cfg for l in ("az", "en", "ru", "tr"))
    assert "android:localeConfig" in text(SRC / "AndroidManifest.xml")


def test_ui_has_no_hardcoded_user_visible_text():
    ui_files = [p for p in KT if p.parent.name == "ui" or p.name in {"NaymeraApp.kt"}]
    assert len(ui_files) >= 9
    for p in ui_files:
        c = re.sub(r"/\*.*?\*/|//[^\n]*", "", text(p), flags=re.S)
        assert not re.search(r"[əıöüşçğƏİÖÜŞÇĞ]", "".join(re.findall(r'"[^"\n]*"', c))), p.name
        assert not re.search(r'\bText\(\s*"', c), p.name
        assert not re.search(r'(label|placeholder|contentDescription|title|subtitle)\s*=\s*(\{\s*Text\()?"', c), p.name
        if p.name not in {"Theme.kt", "UiText.kt", "ChatViewModel.kt", "MainViewModel.kt"}:
            assert "stringResource(" in c or "uiText(" in c, p.name
    # tərcümə açarları mövcuddur
    keys = set(strings_of("values"))
    used = set(re.findall(r"R\.string\.([a-z0-9_]+)", ALL_KT))
    assert used <= keys, used - keys


def test_interface_is_dark_by_default_and_has_the_required_sections():
    theme = text(next(p for p in KT if p.name == "Theme.kt"))
    assert "dark: Boolean = true" in theme
    nav = text(next(p for p in KT if p.name == "NaymeraApp.kt"))
    for screen in ("HomeScreen", "ChatScreen", "MemoryScreen", "NotificationsScreen", "AgentsScreen", "HistoryScreen"):
        assert screen in nav, screen
    assert "setApplicationLocales" in ALL_KT  # tətbiq daxili dil seçimi (az/en/ru/tr)


def test_voice_stays_a_labeled_future_interface():
    voice = text(next(p for p in KT if p.name == "VoiceInterfaces.kt"))
    assert "MockSpeech" in voice and "isMock" in voice and "FEMALE" in voice and "az-AZ" in voice
    chat_vm = text(next(p for p in KT if p.name == "ChatViewModel.kt"))
    assert "stt.available" in chat_vm  # real STT yoxdursa transkript göndərilmir


# --- Stage 15: audit, build uyğunluğu, JVM kontrakt layihəsi ---

def test_kotlin_core_is_testable_without_android_and_has_no_api_below_26_traps():
    api = text(next(p for p in KT if p.name == "ApiClient.kt"))
    assert "interface TokenStore" in text(next(p for p in KT if p.name == "TokenStore.kt"))
    assert "class ApiClient(private val store: TokenStore" in api and "SessionStore" not in api
    assert "class SessionStore" in text(next(p for p in KT if p.name == "SessionStore.kt")) and ": TokenStore" in text(next(p for p in KT if p.name == "SessionStore.kt"))
    msg = text(next(p for p in KT if p.name == "Messages.kt"))
    assert "import android" not in msg and "import androidx" not in msg
    for name in ("ApiClient.kt", "EventSync.kt", "ApiFailure.kt", "Models.kt", "TokenStore.kt"):
        body = text(next(p for p in KT if p.name == name))
        assert "import android" not in body and "import androidx" not in body, name  # JVM-də kompilyasiya olunur
    key = text(next(p for p in KT if p.name == "DeviceKey.kt"))
    assert "StrongBoxUnavailableException" not in key and "ProviderException" in key  # API 28 sinfi API 26-da çökdürə bilər


def test_jvm_contract_project_compiles_the_real_kotlin_sources_and_ships_no_binaries():
    jvm = AND / "jvm-contract"
    for rel in ("settings.gradle.kts", "build.gradle.kts", "README.md", "src/test/kotlin/MessagesTest.kt",
                "src/test/kotlin/BackendContractTest.kt", "src/test/kotlin/NetworkFailureTest.kt", "src/test/kotlin/TestSupport.kt"):
        assert (jvm / rel).is_file(), rel
    build = text(jvm / "build.gradle.kts")
    assert "../app/src/main/java/az/naymera/app/data" in build and "security/Messages.kt" in build
    assert "com.android" not in build and "google()" not in build and "signingConfigs" not in build
    assert "NAYMERA_BACKEND_URL" in text(jvm / "src/test/kotlin/BackendContractTest.kt")
    assert (ROOT / "tools" / "jvm_contract.py").is_file()


def test_no_signing_material_or_release_automation_exists():
    for p in AND.rglob("*"):
        if p.is_file() and p.suffix in {".kts", ".gradle", ".properties", ".md"} and p.name != "BUILD_STATUS.md":
            c = text(p)
            assert "signingConfigs" not in c and "storePassword" not in c and "keyPassword" not in c, p
    gi = text(AND / ".gitignore")
    assert all(x in gi for x in ("*.jks", "*.keystore", "*.apk", "*.aab", "local.properties"))


def test_build_status_documents_exact_requirements_blocker_and_local_steps():
    s = text(AND / "BUILD_STATUS.md")
    for needle in ("build EDİLMƏYİB", "dl.google.com", "compileSdk 34", "build-tools;34.0.0", "platforms;android-34", "AGP 8.5.2",
                   "Gradle 8.7", "JDK 17", "Kotlin 2.0.20", "assembleDebug", "app-debug.apk", "JVM kontrakt", "sdkmanager",
                   "gradle wrapper", "imzalanmayıb"):
        assert needle in s, needle
    assert (AND / "AUDIT.md").is_file()
    audit = text(AND / "AUDIT.md")
    for needle in ("StrongBoxUnavailableException", "TokenStore", "Gradle wrapper", "launcher"):
        assert needle in audit, needle


def test_launcher_icon_is_declared_and_exists():
    assert 'android:icon="@drawable/ic_launcher"' in text(SRC / "AndroidManifest.xml")
    assert (SRC / "res" / "drawable" / "ic_launcher.xml").is_file()


# --- Stage 16: hibrid AI paneli, rejimlər, fallback, dürüst etiketlər ---

def test_ai_models_and_client_fallback_are_pure_kotlin_and_cover_the_server_contract():
    models = text(next(p for p in KT if p.name == "AiModels.kt"))
    names = set(re.findall(r'@SerialName\("([a-z_]+)"\)', models + text(next(p for p in KT if p.name == "Models.kt"))))
    assert {"mode", "active", "cloud", "local", "consents", "privacy", "voice", "state", "is_mock", "reason", "consent_needed",
            "supported_kinds", "cloud_may_receive_memory", "cloud_categories", "tts", "stt", "client_request_id", "engine",
            "notice", "notice_code"} <= names
    for needle in ("enum class AiMode", "Auto", "Cloud", "Local", "enum class EngineBadge", "Simulation", "NotConfigured", "Unavailable"):
        assert needle in models, needle
    fb = text(next(p for p in KT if p.name == "ClientFallback.kt"))
    for needle in ("interface OnDeviceLocal", "object NotConfiguredLocal", "fun decideRoute", "enum class ClientRoute", "OnDevice", "Unavailable"):
        assert needle in fb, needle
    for body in (models, fb):
        assert "import android" not in body and "import androidx" not in body


def test_on_device_local_ai_is_only_an_interface_without_downloads_or_embedded_models():
    fb = text(next(p for p in KT if p.name == "ClientFallback.kt"))
    assert "LocalState.NotConfigured" in fb or "NotConfigured" in fb
    for banned in ("DownloadManager", "HttpURLConnection", "OkHttp", "huggingface", ".gguf", "http://", "https://"):
        assert banned not in fb, banned
    assert not [p for p in AND.rglob("*") if p.is_file() and not ({"build", ".gradle"} & set(p.relative_to(AND).parts))
                and p.suffix in {".gguf", ".tflite", ".task", ".onnx", ".bin"}]


def test_ai_panel_shows_cloud_local_active_mode_voice_and_mode_selection():
    panel = text(next(p for p in KT if p.name == "AiPanel.kt"))
    for needle in ("ai_cloud", "ai_local", "ai_active_mode", "ai_voice", "ai_mode_auto", "ai_mode_cloud", "ai_mode_local",
                   "ai_state_simulation", "ai_state_not_configured", "ai_state_unavailable", "ai_state_ready", "AiMode.Auto",
                   "AiMode.Cloud", "AiMode.Local", "setMode", "onDevice", "ai_consent_needed", "ai_cloud_no_fallback"):
        assert needle in panel, needle
    assert "AiPanel(" in text(next(p for p in KT if p.name == "HomeScreen.kt"))
    client = text(next(p for p in KT if p.name == "ApiClient.kt"))
    assert "/v1/ai/status" in client and "/v1/ai/mode" in client and "client_request_id" in text(next(p for p in KT if p.name == "Models.kt"))


def test_chat_falls_back_only_on_connectivity_loss_and_reuses_the_client_request_id_on_retry():
    vm = text(next(p for p in KT if p.name == "ChatViewModel.kt"))
    for needle in ("decideRoute", "ClientRoute.OnDevice", "clientRequestId", "OnDeviceLocal", "AiMode", "device_fallback"):
        assert needle in vm, needle
    fb = text(next(p for p in KT if p.name == "ClientFallback.kt"))
    assert "ApiFailure.Offline" in fb and "ApiFailure.Timeout" in fb and "AiMode.Cloud" in fb


def test_state_and_notice_labels_are_honest_and_translated():
    az, en = strings_of("values"), strings_of("values-en")
    assert "NOT CONFIGURED" in az["ai_state_not_configured"] and "SİMULYASİYA" in az["ai_state_simulation"]
    assert "NOT CONFIGURED" in en["ai_state_not_configured"] and "SIMULATION" in en["ai_state_simulation"]
    for code in ("fallback", "consent_required", "simulation_only", "all_unavailable", "cloud_unavailable", "local_unavailable",
                 "unsupported_locally", "blocked", "blocked_egress", "in_progress", "bad_request", "device_fallback"):
        assert f"notice_{code}" in az, code
    uitext = text(next(p for p in KT if p.name == "UiText.kt"))
    assert "notice_" in uitext
