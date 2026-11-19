# KNOWN_LIMITATIONS — açıq məhdudiyyətlər, çatışmayan modullar, texniki borc

Yenilənib: 2026-11-18 · Strategiya: **build-first** (əvvəlcə imkanlar qurulur; sistem-geniş test kampaniyası əsas arxitektura tamamlandıqdan sonra). Hər mərhələdə yalnız əsas smoke/regressiya və təhlükəsizlik yoxlamaları işlədilir.

## 1. Mərhələ vəziyyəti
| Mərhələ | Vəziyyət |
|---|---|
| 17 (PR #18–#28) | CI yaşıl, birləşdirilməyib. Son audit: SAFE WITH RESTRICTIONS |
| 18.0 `kernel/` (PR #29) | CI yaşıl, birləşdirilməyib |
| 18.1 `models/` + `runtime/brokered_models.py` | yerli iş ağacında, **commit olunmayıb**, sahib baxışı gözləyir |
| 18.2-B `core/` + `runtime/brokered_core.py`; 18.3 `memory/`; 18.4 `reasoning/` | ayrı worktree/branch (`claude/stage18-2b-goal-store`), **commit olunmayıb** |

## 2. Çatışmayan modullar (hələ qurulmayıb)
~~Memory Engine v2~~ (18.3 qurulub) · ~~Reasoning scaffolds~~ (18.4 qurulub; real modelə qoşulmayıb) · Autonomous Research (real fetch; yalnız mock/inyeksiya var) · Agent qualification və `run_session` · Software Engineering Engine ·
Self-Evolution (Gap Analyzer, təklif paketi) · real lokal model runtime adapteri · Ops (scheduler, watchdog, backup, resurs idarəsi) · ~~hash-zəncirli audit~~ (18.8 qurulub; xarici anker imzalama yoxdur) · ~~Ops: scheduler/watchdog/backup~~ (18.8) · ~~Android CORE API~~ (18.11 qurulub, istehsala qoşulmayıb) · OS sandbox · istehsal handler kitabxanası ·
Goal/Plan üçün API/Android səthi (APK build edilməyib; Android SDK yoxdur) · Evaluation Harness və Azərbaycan dəsti (sahib iştirakı ilə).

## 3. Texniki borc və məlum məhdudiyyətlər
- **18.6 (18.1 ağacında):** REAL lokal model qapıları (razılıq + resurs) əlavə edildi; real lokal serverlə sınaq, model yükləmə və real cihaz resurs mənbəyi (Android/server adapteri) hələ yoxdur.
- **18.8/18.11:** audit ankeri sistemdən kənarda saxlanmalıdır (imzalama yoxdur); backup şifrələnmir; scheduler işləri eyni ipdə (vaxt aşımı yox); push bildirişi və Kotlin istehlakçısı yoxdur; `CoreApiApp` istehsala qoşulmayıb (qoşulma ayrıca təsdiq).
- **18.5:** yalnız 4 dar handler; `PlanStepSpec` arqument daşımır ⇒ parametrli capability yoxdur (kernel müqaviləsi genişlənməlidir); handler-lər avtomatik qeydə alınmır.
- **18.3/18.4:** semantik axtarış yoxdur; köhnə `agents/memory` ilə miqrasiya yoxdur; konsolidasiya yalnız say xülasəsidir; `ModelPlanner` real modelə qoşulmayıb; meyar formatı sadədir; yalnız smoke testlər.
- **Stage 17 köhnə provider yolu:** xam `OpenAICompatibleProvider` — dərin JSON `RecursionError`, tək surrogat, cavab ölçüsü limiti yoxdur (düzəldilməyib; 18.1 yolu sərtləşdirilib). Digər açıq: uid təkrar istifadəsi, paylaşılan `/tmp`, nəqliyyat «seam» etibarı, ümumi müddət limiti.
- **Proses-daxili ayrılıqlar OS sərhədi DEYİL:** Broker `ContextVar`, `LedgerPort`, `EngineGoalStore`/`OwnerGoalStore` üzləri, `_TrustedProviderView`.
- **18.2-B:** lease yenilənməsi (heartbeat) yoxdur — uzun addım lease-dən çox çəkərsə `unknown` olur; planlaşdırıcı xəta sayğacı yaddaşdadır (restartda sıfırlanır); Broker xəta növü `reason` prefiksindən oxunur; `unknown` addımlar sahib həllini gözləyir; jurnalda hash zənciri yoxdur.
- **18.0:** `EventLog` dəyişdirməni aşkar edir, qarşısını almır; `OwnerDecision` kriptoqrafik doğrulanmır (yalnız istinad).
- **18.1:** Anthropic/Gemini formatları real API-yə qarşı yoxlanmayıb; qat istehsala (`build_app`/`env`) qoşulmayıb; `ENTRY_POINTS` testinə 2 sətir əlavə edilib; qiymətlər «ledger vahidi»ndədir (ledger valyuta bildirmir).
- **Lokal AI:** Azərbaycan dili keyfiyyəti sübut olunmayıb; resurs rəqəmləri təxminidir, ölçülməyib.
- **18.14 (CI APK):** `.github/workflows/android-debug-apk.yml` yazılıb və statik test olunub, lakin NEÇƏ DƏ İŞƏ SALINMAYIB (GitHub-a çıxarılmayıb; commit/push yoxdur) → real debug APK hələ yoxdur; `ubuntu-latest` runner-də ilk real build resurs/manifest/asılılıq xətaları üzə çıxara bilər; setup-android SDK lisenziyalarını avtomatik qəbul edir (sahib qərarı); action SHA-ları yoxlanıb (git ls-remote), lakin yenilənmələr əl ilə aparılmalıdır; debug APK müvəqqəti runner debug açarı ilə imzalanır (yenidən build ilə imza dəyişir → yenilənmə əvəzinə sil/yenidən qur).
- **18.13 (APK):** debug APK GENERASİYA EDİLMƏYİB: Google Maven/Android SDK əlçatan deyil. Compose mənbələri yalnız JVM-də (Compose Multiplatform 1.7.0 + stub-lar) kompilyasiya olunub — AGP/aapt2/manifest-birləşdirmə/real androidx imzaları/R8/lint yoxlanılmayıb; `RECORD_AUDIO` və `POST_NOTIFICATIONS` icazələri manifestdə elan olunub, lakin real səs/push yoxdur (ən az imtiyaz prinsipi üçün gələcəkdə yenidən baxılmalıdır, 18.13-də dəyişdirilməyib).
- **M2 (Android UI, 18.12):** Compose ekranları, `NaymeraApp` naviqasiyası və `BiometricSigner` bu mühitdə KOMPİLYASİYA EDİLMƏYİB və işə salınmayıb (Android SDK/Maven Google yoxdur): yalnız `domain/` məntiqi JVM-də, qalanı statik mətn testləri ilə yoxlanıb — sintaksis/API xətaları ola bilər; APK YOXDUR. Ekran görüntüləri/dizayn cihazda yoxlanmayıb; geniş ekran/planşet üçün ayrıca layout yoxdur (yalnız ağırlıqlı Column); oflayn rejim yalnız yaddaşdakı son məzmunu göstərir (diskdə keş yoxdur); məqsəd/təsdiq siyahısı polling/əl ilə yenilənir (CORE hadisələri `/v1/events`-ə yazılmır); ru/tr/en tərcümələri maşın-səviyyəlidir, mədəni/dil yoxlaması edilməyib; AI Köməkçi ekranı mövcud söhbət qatını istifadə edir (Core hədəf-yaratma axını yoxdur).
- **M1 (Android data qatı):** UI/ViewModel/naviqasiya və APK build YOXDUR (SDK bu mühitdə yoxdur); JVM testləri proqram P-256 açarı ilə (Keystore/BiometricPrompt cihazda yoxlanmayıb); `CoreApiApp` istehsala QOŞULMAYIB (yalnız `tools/serve_core_dev.py`); dev server HTTP loopback-dır, real cihaz üçün `adb reverse`, LAN/HTTPS ifşası sahib qərarıdır; cütləşmə sürət limiti (20/dəq/ünvan) JVM test paketini ortaq cihazlarla məhdudlaşdırır; CORE hadisələri `/v1/events`-ə hələ yazılmır (polling `GET /v1/core/*` ilə); `PlanStepSpec` arqument daşımır.
- **İşin saxlanması:** 18.1 və 18.2-B commit olunmayıb; container sıfırlanarsa itər.

## 4. Təhlükəli/xarici təsirli imkanlar — təhlükəsizlik yoxlaması olmadan AKTİVLƏŞDİRİLMİR
Agent/yaradılmış kod icrası (`run_session`, sandbox) · xarici şəbəkə/internet · real model API və ödəniş · hesab/giriş · nəşr/deploy/merge · gizli müşahidə və ya icazəsiz sistem girişi · sahib təsdiqini/audit izini keçən hər yol.
`build-first` bunlara icazə DEYİL: onlar yalnız ayrıca təsdiqlənmiş təcrid olunmuş mühitdə və sahib qərarı ilə.

## 5. Sistem-geniş test kampaniyası üçün yığılan siyahı (indi işlədilmir)
İnteqrasiya (core + models + memory + agents) · funksional ssenarilər · performans (lokal model sürəti/RAM/enerji) · etibarlılıq (qəza, SIGKILL, bərpa, uzun işləmə) · təhlükəsizlik (red-team, injection, sızma, escalation) ·
root/privileged izolyasiya təcrid olunmuş mühitdə · Azərbaycan qiymətləndirməsi · CI (push/PR) · mutasiya yenilənməsi yalnız dəyişmiş təhlükəsizlik kodunda · tapıntıların ciddiliyə görə düzəldilməsi və yoxlanması.
