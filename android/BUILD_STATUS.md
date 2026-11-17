# NAYMERA Android — build statusu

**APK build EDİLMƏYİB.** Bu mühitdə debug APK yaratmaq mümkün olmadı; saxta APK yaradılmayıb və repoda `.apk`/`.aab`/`.jar`/`.keystore` yoxdur.

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
