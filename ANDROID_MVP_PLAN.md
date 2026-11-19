# ANDROID_MVP_PLAN — Kotlin Android klienti (CORE API üzərində istifadə oluna bilən APK MVP)

**Vəziyyət:** M1 (data qatı + CORE challenge yoxlaması + JVM kontrakt testləri) YAZILIB və JVM-də real Python backend ilə yoxlanıb; M2 (Compose UI + MVVM, Stage 18.12) YAZILIB, lakin bu mühitdə KOMPİLYASİYA EDİLMƏYİB (Android SDK yoxdur); M3–M5 (cihaz biometrik testi, debug APK) hələ yoxdur. APK bu mühitdə build EDİLMİR (Android SDK yoxdur, `dl.google.com` bloklu: bax `android/BUILD_STATUS.md`); «APK hazırdır» deyilməyəcək.
Bu sənəd mövcud kodu (`android/`, Stage 13–15) və yeni backend kontraktını (`api/core_api.py`, 18.11) əsas götürür.

## 1. Mövcud vəziyyət (qurulub)
Kotlin + Compose layihəsi: cütləşmə (`PairingScreen`), söhbət, hadisə sinxronizasiyası, bildirişlər, yaddaş, agentlər, **təsdiq ekranı** (`ApprovalScreen`) və **step-up** (`StepUp`/`DeviceKey`: Keystore ECDSA P-256, `BiometricPrompt` + CryptoObject, yalnız güclü biometrik, səs yox),
`ApiClient` (OkHttp, retry/backoff, token yenilənməsi), `SessionStore`/`TokenStore` (EncryptedSharedPreferences), `android/jvm-contract/` (Android-dan asılı olmayan qatın JVM testləri; opt-in olaraq HƏQİQİ Python backend ilə).
Çatışmayan: CORE məqsəd/plan/addım ekranları, CORE DTO-ları və `ApiClient` metodları, CORE step-up mesajları.

## 2. MVP tərifi — «istifadə oluna bilən» nə deməkdir
Sahib telefonla: cütləşir → **vəziyyəti görür və STOP verə bilir** → **məqsəd yaradır** → **məqsədi step-up ilə aktivləşdirir/dayandırır/ləğv edir/tamamlayır** → planı və addım vəziyyətlərini izləyir →
**gözləyən addım təsdiqlərini** (hash-ə bağlı, biometrik) verir/rədd edir → **UNKNOWN/BLOCKED/FAILED addımı həll edir** → jurnala baxır → hadisə/bildiriş alır.
MVP-yə DAXİL DEYİL: səs, yaddaş v2 ekranı, ops paneli, lokal model yükləmə ekranı, tapşırıq yaratma üçün model/planlaşdırıcı seçimi, release imzalama, Play Store.

## 3. API ↔ Kotlin xəritəsi (yeni)
| Backend | `ApiClient` metodu | Ekran |
|---|---|---|
| `GET /v1/core/status` | `coreStatus()` | Əsas səhifə (autonomy L0/L1, STOP, saylar) |
| `GET /v1/core/goals` | `goals(status?)` | Məqsədlər |
| `POST /v1/core/goals` (`client_op` ilə idempotent) | `createGoal(title, description, priority, clientOp)` | Yeni məqsəd |
| `GET /v1/core/goals/{id}` | `goal(id)` | Məqsəd detalı (plan, addımlar; çıxış `untrusted_output`) |
| `POST …/challenge` + `…/transition` | `goalChallenge`, `goalTransition` | Aktivləşdir/Dayandır/Ləğv/Tamamla (step-up) |
| `GET /v1/core/steps/awaiting` + mövcud `/v1/approvals` | `awaitingSteps()`, `approvals()`, `approvalChallenge/Decision` | Təsdiqlər (məqsəd konteksti ilə) |
| `POST …/steps/{p}/{s}/challenge` + `/resolve` | `stepChallenge`, `stepResolve` | Addımı həll et (retry/failed/succeeded) |
| `GET /v1/core/journal` | `journal(goalId?, afterSeq)` | Jurnal |

**Step-up qaydası (vacib):** server challenge cavabında `action`, `subject`, `payload_hash`, `decision` qaytarır. Cihaz YALNIZ istifadəçiyə göstərilən əməliyyatı imzalayır: `Messages.challengeMessage(challenge_id, nonce, action, subject, payload_hash, decision)`;
əlavə olaraq klient `sha256("{to_status}:{version}")` (məqsəd) və `sha256("{resolution}:{attempt}:{state}")` (addım) hash-ini YENİDƏN hesablayıb serverin verdiyi ilə müqayisə edir; uyğun deyilsə imzalamır. `method` həmişə `biometric`.

## 4. Kotlin iş paketləri
1. **Data qatı** (`data/CoreModels.kt`, `ApiClient.kt` genişləndirilməsi): 10 DTO/endpoint, xəta xəritələməsi (409 stale/not_resolvable/duplicate; 422 sirr; 403 step-up; 429 lockout), `client_op` generatoru (UUID, kiçik hərf), mətnlər ETİBARSIZ (`untrusted` bayraqları göstərilir).
2. **Security** (`security/CoreStepUp.kt`): mövcud `StepUp.sign`-dan istifadə; yeni action-lar üçün hash yoxlaması; FLAG_SECURE təsdiq ekranlarında.
3. **UI** (Compose, mövcud Theme/UiText/Azərbaycan mətnləri): `GoalsScreen`, `GoalDetailScreen`, `CreateGoalSheet`, `AwaitingStepsSection` (mövcud `ApprovalScreen`-i genişləndirir), `ResolveStepSheet`, `JournalScreen`, `HomeScreen`-ə status/STOP kartı. Naviqasiya: alt panelə «Məqsədlər» tabı.
4. **ViewModel-lər:** `GoalsViewModel`, `GoalDetailViewModel` (polling + mövcud `EventSync`), UI vəziyyəti `Loading/Content/Error`; oflayn rejim: yalnız oxu keşi (yazı/təsdiq əməliyyatları oflayn İCRA OLUNMUR).
5. **Test:** `android/jvm-contract` genişləndirilməsi — CORE endpoint-ləri HƏQİQİ Python backend (`CoreApiApp`, loopback) ilə: yaratma idempotentliyi, step-up imzası (Java ECDSA), köhnə versiya sübutu rədd, voice rədd. Cihaz testi: `DEVICE_TEST.md`-ə CORE yoxlama siyahısı.

## 5. Təhlükəsizlik tələbləri
- APK-da API açarı, model açarı, imza açarı YOXDUR; token yalnız EncryptedSharedPreferences; şəxsi açar yalnız Keystore (biometrik, `setUserAuthenticationRequired`).
- `usesCleartextTraffic=false`; yalnız HTTPS (inkişaf üçün yalnız loopback/emulyator istisnası, release-də yox); sertifikat pin etmə sahib qərarıdır.
- `allowBackup=false`, exported komponent yoxdur, deep link yoxdur; ekran görüntüsü qadağası (FLAG_SECURE) təsdiq/step-up ekranlarında.
- Server mətni (başlıq, xülasə, jurnal) təlimat deyil: yalnız göstərilir, heç vaxt klientdə icra/təsdiq üçün istifadə olunmur. Səs/söhbət mətni təsdiq mənbəyi deyil.
- Klient hər şeyi göstərir, heç nəyi öz-özünə təsdiq etmir; STOP həmişə əlçatandır.

## 6. Server tərəfi: MVP üçün nə lazımdır
**Hazırdır:** `tools/serve_core_dev.py` — yalnız `127.0.0.1` (host seçimi/env yoxdur; cihaz üçün `adb reverse`), mövcud auth/cütləşmə/STOP/step-up dəyişməyib, demo L1 döngü yalnız dar handler-lərlə, hər addım sahib təsdiqi tələb edir; testlər: `tests/test_serve_core_dev.py`.
`CoreApiApp` istehsala QOŞULMAYIB. Cihazla işləmək üçün yalnız **inkişaf serveri** lazımdır: `tools/serve_core_dev.py` (loopback; mövcud `api/server.py` siyasəti, handler reyestri boş, autonomy L1, L1 təsdiqləri mövcud `ApprovalService` ilə). LAN/HTTPS ifşası və real cihazla sınaq SAHİB TƏSDİQİ tələb edir.
Backend kiçik əlavələri (asılı olmayan, paralel): push əvəzinə mövcud `/v1/events` polling-ə CORE hadisələri (məqsəd statusu dəyişdi, təsdiq gözləyir) əlavə etmək.

## 7. APK-nın çatdırılma yolu
Bu mühitdə build mümkün deyil. Seçimlər: **(a)** sahibin maşınında Android Studio + `android/BUILD_STATUS.md` addımları (tövsiyə, indi); **(b)** GitHub Actions-da debug APK build işi (SDK runner-də yüklənir) — CI dəyişikliyi və artefakt yayımı SAHİB TƏSDİQİ tələb edir; **(c)** konteyner əsaslı build. Release imzalama (açar, `signingConfigs`, Play Store) sahib qərarı olmadan qurulmur; debug APK yalnız debug açarı ilə.

## 8. Mərhələlər (build-first)
| M | Nəticə | Qəbul (smoke) |
|---|---|---|
| M1 ✅ | Data qatı (`CoreModels.kt`, `ApiClient` CORE metodları, `security/CoreChallenge.kt`) + `jvm-contract` CORE testləri (`CoreChallengeTest`, `CoreContractTest`) | JVM testləri real backend ilə keçir (45/45, oflayn Gradle) |
| M2 ✍️ | Panel, 7 təyinatlı naviqasiya (Əsas, AI Köməkçi, Məqsədlər, Təsdiqlər, Yaddaş, Jurnal, Ayarlar), məqsəd siyahısı/yaratma/detal/keçid, təsdiqlər, imza dialoqu, jurnal. `domain/` (Repository, `Load`/`ErrorKind` tipli vəziyyətlər, `GoalsModel`/`ApprovalsModel`/`DashboardModel`/`JournalModel`, `Destination`) JVM-də test olunur (59 JVM testi, real backend ilə); Compose ekranları YALNIZ mənbə səviyyəsində yoxlanıb (statik testlər) | UI kompilyasiyası sahibin maşınında (Android Studio) |
| M3 | Cihazda biometrik imza (M2-də məntiq və imza dialoqu yazılıb) → server qəbul edir | cihaz testi |
| M4 | UNKNOWN həlli, jurnal, status/STOP kartı | cihaz yoxlama siyahısı |
| M5 | Debug APK build (sahibin maşını/CI təsdiqi ilə) | APK qurulur və quraşdırılır; cihaz testi `DEVICE_TEST.md` |
M1 (data/security) və M2–M4 (UI) paralel yazıla bilər; serverdəki `serve_core_dev.py` və polling hadisələri asılı deyil.

## 9. Məlum məhdudiyyətlər və risklər
APK build/UI kompilyasiyası bu mühitdə yoxlanıla bilməz (yalnız Android-dan asılı olmayan qat JVM-də); push bildirişi yoxdur (polling); oflayn yazı yoxdur; Compose UI əl ilə cihaz testi tələb edir; AZ mətnləri mövcud `UiText` ilə uyğunlaşdırılmalıdır;
`PlanStepSpec` arqument daşımır (məqsəd yaratma sadədir); CORE API autentifikasiyası mövcud cütləşmə/token sisteminə bağlıdır (ayrıca rol yoxdur: cütləşmiş cihaz = sahib cihazı).

## 10. M2 (Stage 18.12) — nə yazılıb
- **Arxitektura:** Compose ekranlar → `CoreViewModel` (yalnız həyat dövrü) → `domain/` modelləri (Android-dan asılı deyil) → `CoreRepository` → `ApiCoreRepository` → mövcud M1 `ApiClient`. Vəziyyətlər tiplidir: `Load.Idle/Loading/Content/Failed(kind, stale)`; xətada SON məzmun saxlanılır (oflayn: yalnız oxu, yazı/imza oflayn İCRA OLUNMUR).
- **AI Köməkçi:** mövcud `ChatScreen`/`ChatViewModel` (AZ-ilk, tarixçə, yüklənmə/xəta/oflayn, mock/real etiketi, fallback qaydaları) təkrar istifadə olunub; qoşulmayan model işləyirmiş kimi göstərilmir.
- **Təhlükəsizlik:** imza axını iki addımlıdır: «Təsdiqlə/Başlat/…» yalnız `SignDialog` açır (dəqiq action/subject/decision/payload_hash göstərilir); biometrik yalnız dialoqda «Biometrik ilə imzala» basılandan sonra; `CoreChallenge` (məqsəd, addım, təsdiq) klient tərəfində hash-i yenidən hesablayır, uyğunsuzluqda imzalamır; `FLAG_SECURE` təsdiq/detal ekranlarında; STOP imza tələb etmir və həmişə əlçatandır; server mətni etibarsız kimi qeyd olunur.
- **Lokalizasiya:** az (defolt), en, ru, tr — 64 yeni açar hər dildə.
- **Test:** `DomainModelsTest` (10), `NavigationTest` (3), `DomainBackendTest` (1, real backend), + mövcud JVM testləri = 59/59; `tests/test_android_project.py` (statik: hardcoded mətn yoxdur, `confirm` yalnız dialoqdan, domen Android-dan asılı deyil).

## 11. Stage 18.13 — debug APK yoxlaması
**APK generasiya edilmədi** (Google Maven və Android SDK bu mühitdə əlçatan deyil: `dl.google.com` bloklu). Əvəzində: statik Gradle/manifest/resurs testləri, **Compose mənbələrinin JVM-də Compose Multiplatform 1.7.0 ilə həqiqi KOMPİLYASİYASI** (`tools/compose_check.py`, 0 xəta; yalnız Android-a məxsus simvollar stub-dır) və 62 JVM testi. Dəqiq çatışmayanlar və təkrarlana bilən Android Studio addımları: `android/BUILD_STATUS.md`; cihaz yoxlama siyahısı: `android/DEVICE_TEST.md`. M3 bu yoxlama (real `assembleDebug`) bitməyincə başlanmır.

## 12. Stage 18.14 — CI-da debug APK
`.github/workflows/android-debug-apk.yml` (JDK 17, Gradle 8.9, SDK 34, `assembleDebug`, fokus testlər, yalnız artefakt, secret/imza/release yoxdur). Hələ icra olunmayıb: **real APK yoxdur**; M3 real build uğur qazanana qədər başlanmır. Ətraflı: `android/BUILD_STATUS.md`.
