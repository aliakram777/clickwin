# NAYMERA Android — build statusu

**APK build EDİLMƏYİB (Mərhələ 18.13 yenidən yoxladı: 2026-11-19, vəziyyət dəyişməyib).** Bu mühitdə debug APK yaratmaq mümkün olmadı; saxta APK yaradılmayıb və repoda `.apk`/`.aab`/`.jar`/`.keystore` yoxdur.

## Dəqiq maneə (Mərhələ 15 yoxlaması)
| Yoxlama | Nəticə |
|---|---|
| `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `sdkmanager`, `adb` | yoxdur |
| `https://dl.google.com/...` (Android SDK paketləri VƏ Google Maven: AGP, AndroidX, Compose) | şəbəkə siyasəti ilə bloklanıb (HTTP 000) |
| `https://maven.google.com` | yalnız yönləndirmə; faktiki yükləmə `dl.google.com`-dandır (bloklu) |
| Maven Central (`repo1.maven.org`), `services.gradle.org` | əlçatandır (`repo.maven.apache.org` və `plugins.gradle.org` zaman-zaman 429) |
| JDK 21, Gradle 8.14.3 | quraşdırılıb |

Android Gradle Plugin və AndroidX kitabxanaları yalnız Google Maven-dədir, SDK platforması yalnız `dl.google.com`-dadır; ona görə `dl.google.com` açılmadan build mümkün deyil. Mühit şəbəkəsinə `dl.google.com` əlavə etmək təklif olundu, lakin sahib tərəfindən RƏDD edildi; bu qərar yenidən soruşulmadı.

## Tələb olunan versiyalar
- **JDK 17** (və ya 21), **Gradle 8.7+** (AGP 8.5.2 üçün; wrapper-i `gradle wrapper --gradle-version 8.9` ilə yaradın)
- **AGP 8.5.2**, **Kotlin 2.0.20** (+ Compose compiler plugin 2.0.20), Compose BOM 2024.09.00
- Android SDK: `platforms;android-34` (**compileSdk 34**, targetSdk 34, minSdk 26), `build-tools;34.0.0`, `platform-tools`
- SDK lisenziyalarını (`sdkmanager --licenses`) SAHİB özü qəbul edir; bu iş avtomatlaşdırılmayıb.

## Yerli build addımları (sahibin maşınında)
```bash
# 1) SDK (Android Studio ilə və ya komanda sətri ilə)
sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"
echo "sdk.dir=/path/to/Android/Sdk" > android/local.properties      # git-də izlənmir
# 2) Gradle wrapper (binar fayl repoda saxlanmır)
cd android && gradle wrapper --gradle-version 8.9
# 3) Debug APK
./gradlew :app:assembleDebug
#    nəticə: app/build/outputs/apk/debug/app-debug.apk  (debug açarı ilə avtomatik imzalanır; bizim açar DEYİL)
# 4) Cihaza yükləmə (USB debugging açıq)
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
Backend və cihaz sınağı: `android/DEVICE_TEST.md`. Emulyatorda server ünvanı `http://10.0.2.2:8765` (düz HTTP yalnız debug quruluşunda və yalnız yerli hostlar üçün); real telefonda HTTPS tələb olunur.

Release APK **imzalanmayıb** və imzalama/yayımlama bu repoda qurulmayıb (nə `signingConfigs`, nə açar, nə mağaza hesabı). İmzalama sahibin yerli açarı ilə, sahibin qərarı ilə aparılmalıdır.

## Nə yoxlanılıb (ayrıca)
**JVM kontrakt** (`android/jvm-contract`, `python3 tools/jvm_contract.py`): tətbiqin Android-dan asılı olmayan Kotlin qatı (`data/*`, `security/Messages.kt`) JVM-də həqiqətən KOMPİLYASİYA olunub və 34 JUnit testi keçib (Mərhələ 16: hibrid AI müqaviləsi və fallback qərarı daxil) — həqiqi Python backend ilə (loopback): cütləşmə, söhbət, tarixçə, yaddaş, hadisə kursoru/ack, imzalı təsdiq (Java `SHA256withECDSA` → server `api/p256`), yanlış/başqa cihaz imzası, token fırlanması, paralel 401-də tək yenilənmə, təkrar istifadə aşkarlanması, çıxış; backend olmadan: bağlantı kəsilməsi, vaxt aşımı, GET-in backoff ilə təkrarı, POST-un təkrarlanmaması, 401/403/404/409/429/5xx xəritələməsi, yönləndirməyə əməl olunmaması, əlaqə vəziyyəti (Reconnecting/Offline/Connected).
**Python statik testlər** (`tests/test_android_project.py`): manifest, gradle, açar yoxluğu, tərcümə tamlığı, UI-da sərt mətn yoxluğu, server marşrut kontraktı.

## Nə YOXLANILMAYIB
- Android Gradle build, resurs/manifest birləşməsi, R8, lint: işləməyib.
- Android-a bağlı Kotlin kodu kompilyasiya olunmayıb: `SessionStore`, `DeviceKey`, `StepUp` (Keystore, BiometricPrompt), bütün Compose ekranları, `MainActivity`, `NaymeraApp`.
- Real cihaz/emulyator: açılış, UI, Keystore/biometrik davranışı, dil dəyişməsi — sınanmayıb.

## Məlum məhdudiyyətlər
- Real səs tanıma/sintez yoxdur (`MockSpeech`, "SİMULYASİYA"); push (FCM) yoxdur, hadisələr sorğulama ilədir.
- Biometrik qeydiyyatı olmayan cihazda cütləşmə/təsdiq bloklanır (cihaz kilidi ilə əvəzləmə yoxdur).
- Backend `mock` rejimindədir: real LLM və agent icraçısı qoşulmayıb; tətbiq bunu açıq göstərir.

## Mərhələ 18.13 — yoxlama nəticəsi (sübutlarla)
**Debug APK GENERASİYA EDİLMƏDİ.** `app/build/outputs/apk/debug/app-debug.apk` mövcud deyil; saxta APK yaradılmayıb.

| Yoxlama (2026-11-19) | Nəticə |
|---|---|
| `ANDROID_HOME`/`ANDROID_SDK_ROOT`, `sdkmanager`, `adb`, `android.jar`, AGP/AndroidX/Compose keşi | yoxdur |
| `dl.google.com` (HEAD) | HTTP 000 / proksi 403 — bloklu |
| `maven.google.com/...` (AGP 8.5.2, compose-bom) | HTTP 301 → `dl.google.com` (bloklu) |
| `repo.maven.apache.org`, `plugins.gradle.org`, `maven.google.com/web/` | 200 (lakin AGP/AndroidX/SDK burada YOXDUR) |

**Dəqiq çatışmayanlar:** (1) Google Maven (`dl.google.com/dl/android/maven2`): AGP 8.5.2, AndroidX, Compose BOM 2024.09.00, Material; (2) Android SDK (`dl.google.com/android/repository`): `platforms;android-34`, `build-tools;34.0.0`, `platform-tools`; (3) SDK lisenziyalarının sahib tərəfindən qəbulu. Əvvəlki mühit-şəbəkə təklifi sahib tərəfindən rədd edilmişdi və yenidən təklif OLUNMADI.
Mühitə əlavə ediləcək tək host: `dl.google.com` (sahib qərarı); SDK paketləri ~1 GB ola bilər.

### Əvəzində nə yoxlanıldı (APK DEYİL)
1. **Gradle konfiqurasiyası (statik, `tests/test_android_build_config.py`):** Kotlin 2.0.20 ≡ Compose compiler plugin 2.0.20 (hər üç Gradle layihəsində eyni); AGP 8.5.2 (Gradle ≥ 8.7, JDK 17+; yerli Gradle 8.14.3/JDK 21); compileSdk/targetSdk 34, minSdk 26; BOM 2024.09.00 (Compose 1.7.0 / Material3 1.3.0); asılılıqlar dəqiq versiyadadır; `signingConfigs`/açar yoxdur; manifestdə yalnız `MainActivity` exported, `allowBackup=false`, `usesCleartextTraffic=false`, düz HTTP yalnız debug və yalnız 10.0.2.2/localhost/127.0.0.1; 4 dildə `strings.xml` aapt2 qaydalarına uyğun (apostrof, mövqeli format, təkrar yoxdur).
2. **Compose tip-yoxlaması (`android/compose-check`, `python3 tools/compose_check.py`):** tətbiqin HƏQİQİ mənbələri (`data/`, `domain/`, `ui/` — bütün ekranlar, `NaymeraApp.kt`, `voice/`, `security/Messages|CoreChallenge`) JVM-də Compose Multiplatform 1.7.0 (androidx.compose.* paket adları) və Kotlin 2.0.20 + Compose compiler plugin ilə **uğurla KOMPİLYASİYA olundu** (`BUILD SUCCESSFUL`, 0 xəta). Yalnız Android-a məxsus simvollar stub-dadır: `R`, navigation-compose, lifecycle/ViewModel, `FragmentActivity`/`Window`, `StepUp`/`DeviceKey`, `SessionStore`, `Base64`, `Build`. Bu yoxlama aşkarladı və düzəltdi: `plan?.steps?.forEach` daxilində `plan` smart-cast riski; `Icons.Filled.Chat/Send` (deprecated → AutoMirrored); `FLAG_SECURE` yarış vəziyyəti (ekranlar arası keçiddə bayraq silinirdi → `SecureFlagCounter`).
3. **Bu yoxlamanın QAPSAMADIĞI:** AGP/aapt2 resurs linkləməsi və `R` yaradılması, manifest birləşdirmə, real androidx/navigation/lifecycle/biometric/security-crypto API imzaları (stub-lar əl ilə yazılıb), Hilt/R8 (release), lint, dex/paketləmə/imzalama, işə düşmə. Bunlar yalnız real `assembleDebug` ilə təsdiqlənir.

## Android Studio ilə təkrarlana bilən build (sahibin maşını)
1. Android Studio Koala (2024.1.1) və ya yeni; JDK 17+ (Studio-nun daxili JBR kifayətdir). SDK Manager: `Android SDK Platform 34`, `Build-Tools 34.0.0`, `Platform-Tools`; lisenziyaları özünüz qəbul edin.
2. `File → Open` → `android/` qovluğu. Gradle sync (AGP 8.5.2 üçün Gradle 8.7+; Studio uyğun wrapper təklif edir və ya `cd android && gradle wrapper --gradle-version 8.9`).
3. Komanda sətri alternativi: `echo "sdk.dir=$HOME/Android/Sdk" > android/local.properties && cd android && ./gradlew :app:assembleDebug`.
4. Gözlənilən nəticə: `android/app/build/outputs/apk/debug/app-debug.apk` (debug açarı ilə avtomatik imzalanır; bizim açar DEYİL).
5. Backend: `python3 tools/serve_core_dev.py` (127.0.0.1:8765, cütləşmə kodu konsolda); emulyatorda server `http://10.0.2.2:8765`, real cihazda `adb reverse tcp:8765 tcp:8765` və server `http://127.0.0.1:8765`.
6. Build xəta verərsə, xəta mətnini göndərin: tip-yoxlaması keçdiyi üçün gözlənilən risk resurs/manifest/asılılıq versiyalarındadır.
7. Sonra `android/DEVICE_TEST.md` M2 siyahısı.

## Mərhələ 18.14 — GitHub Actions debug APK workflow-u (HAZIRLANIB, HƏLƏ İŞƏ SALINMAYIB)
**Fayl:** `.github/workflows/android-debug-apk.yml` (adı: «Android debug APK»). **Bu workflow bu mühitdə icra edilə bilmədi; real APK hələ YOXDUR.** Uğur yalnız GitHub-da yaşıl icra + yüklənmiş `naymera-debug-apk` artefaktı ilə təsdiqlənəcək.

- **Tetikləyici:** əl ilə (`workflow_dispatch`) və yalnız `android/**` (və workflow/alətlər) dəyişən pull request. `push`, `schedule`, `release` tetikləyicisi YOXDUR.
- **İcazələr:** yalnız `contents: read`. Artefakt yükləmə əlavə icazə tələb etmir. Secret, imza açarı, `signingConfigs`, release/nəşr/deploy addımı YOXDUR; APK yalnız workflow artefaktıdır (14 gün).
- **Mühit:** `ubuntu-latest`, Temurin **JDK 17**, Gradle **8.9** (AGP 8.5.2 üçün ≥ 8.7; repoda wrapper yoxdur), Android SDK `platform-tools`, `platforms;android-34`, `build-tools;34.0.0` (`android-actions/setup-android` — SDK lisenziyalarını runner-də avtomatik qəbul edir; bu, workflow-u işə salan sahibin qərarıdır).
- **Sıra:** fokus Python testləri (android/core/signing/dev-server/workflow) → JVM kontrakt testləri (62, real Python backend) → Compose tip-yoxlaması → **`gradle -p android :app:assembleDebug --stacktrace --no-daemon`** → APK yoxlaması (`unzip` classes.dex/manifest, `aapt2 dump badging`: paket `az.naymera.app`, minSdk 26, `apksigner verify`) → `actions/upload-artifact` (`naymera-debug-apk`, `if-no-files-found: error`). Uğursuzluqda hesabat artefaktı (`android-build-reports`, 7 gün).
- **Action-lar tam commit SHA-sına pinlənib** (checkout v4.4.0, setup-java v4.9.1, setup-python v5.6.0, setup-android v3.2.2, setup-gradle v4.4.4, upload-artifact v4.6.2).
- **Uyğunsuzluq düzəldildi:** `android/jvm-contract` və `android/compose-check` `jvmToolchain(21)` tələb edirdi → JDK 17-də uğursuz olardı; indi JDK-dan asılı olmayan Java/Kotlin 17 hədəfi (62 JVM testi JDK 21-də yenidən keçdi).
- **Statik yoxlama:** `tests/test_android_workflow.py` (YAML, tetikləyicilər, minimal icazə, secret/imza/release/nəşr yoxdur, SHA pin, sıra, artefakt yalnız debug APK).

**Necə işə salmaq:** (1) workflow faylı uzaq repoya çıxarılmalıdır (bu sahibin qərarıdır: indi yalnız yerli işçi qovluqdadır, commit/push EDİLMƏYİB); (2) Actions → «Android debug APK» → Run workflow; (3) iş bitəndə «Artifacts» bölməsindən `naymera-debug-apk`; (4) uğursuz olarsa log/`android-build-reports` göndərin.
**Qalan risk:** AGP/aapt2 resurs linkləməsi, manifest birləşdirmə, real AndroidX API imzaları, dex/paketləmə ilk dəfə məhz bu icrada yoxlanılacaq; səhv çıxarsa düzəlişlər növbəti addımda aparılır. M3 real build uğur qazanmayınca başlanmır.
