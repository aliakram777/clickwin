# NAYMERA — Məhsul yol xəritəsi

Tarix: 2026-11-17 · Əsas: `claude/stage12-research-intelligence` (mərhələ 1–12; Mərhələ 13–16 üzərində işlənir; PR #1–#12 zəncirvari açıqdır, birləşdirilməyib).

Bu sənəd plandır, vədi yox. Gəlir, qazanc və ya gəlirli nəticə haqqında heç bir iddia yoxdur; trading itkiyə səbəb ola bilər
(bax: bölmə 6).

Təsdiqlənmiş məhsul: **Android APK (əsas istifadə platforması)** · yazılı və səsli danışıq (hədəf: təbii qadın səsi, Azərbaycan
dili) · autonom CEO və şöbə agentləri · internet araşdırması · proaktiv təkliflər · **Website & App Builder** · virtual
şirkət idarəetməsi · trading/birja inteqrasiyası · maliyyə, hesab və nəşr üçün təsdiq qapısı.
Web paneli köməkçi/admin kanalıdır; **Telegram əsas platforma deyil** (yalnız istəyə bağlı ikinci kanal ola bilər).

---

## 1. Mövcud imkanlar (mərhələ 1–12, 722 test, CI yaşıl)

| Qat | Modul | Nə edir |
|---|---|---|
| Nüvə | `agents/spec.py`, `factory.py`, `verify.py`, `registry.py` | Agent təsviri (YAML), yaratma, yoxlama, SQLite reyestr |
| Orkestrasiya | `orchestrator.py`, `ceo.py`, `workflow.py`, `durable.py` | Şöbəyə yönləndirmə, CEO planı, DAG, retry, davamlı workflow və təhlükəsiz davam etdirmə |
| Təhlükəsizlik | `safety/gate.py`, `broker.py`, `approvals.py`, `ledger.py` | İcazə/təsdiq/audit/stop; **Effect Broker** (yan təsirin yeganə yolu); hash-ə bağlı, vaxtlı, tək istifadəlik təsdiq; atomik büdcə rezervi |
| Alətlər | `safety/tools.py`, `workspace.py`, `webtools.py` | Allowlist + `@tool_handler`, workspace sərhədi, `SandboxAdapter` interfeysi (**real OS sandbox yoxdur**), mock/canlı veb alət təsvirləri |
| LLM | `agents/llm.py`, `llm_planner.py` | Provider interfeysi, `Secret`, limitlər, mock; model cavabı etibarsız, yalnız təklif |
| Yaddaş və şirkət | `memory.py`, `company.py` | Davamlı yaddaş (nəzarətli yazma, sirr yazılmır, etibarsız mətn); 8 departament, rol/səlahiyyət/büdcə, audit olunan ötürmə |
| **Araşdırma** | `agents/research.py` | Mənbə/tarix/etibarlılıq yoxlaması, çox mənbəli müqayisə, Azərbaycan dilində izah, yaddaşa yazma; mənbəsiz məlumat fakt kimi təqdim olunmur |
| **Proaktiv zəka** | `agents/proactive.py`, `notify_store.py` | Hadisə/tapşırıq/vəziyyət təhlili, əhəmiyyət və prioritet, səs-küy nəzarəti (cooldown, limit, sakit saat, digest); təkliflər icra olunmur |
| **Mobil interfeys** | `agents/mobile.py` | Android-first bildiriş DTO (versiyalı JSON), hadisə axını (cursor/ack), `VoiceProfile` (az-AZ, qadın, təbii), səs interfeysləri və mock-lar |

Həddlər: real LLM provayderi, real OS sandbox, canlı internet, API server, Android tətbiqi, real səs (TTS/STT), Builder və
trading **yoxdur**. Mock nəticələr hər yerdə mock kimi işarələnir.

## 2. Çatışmayan imkanlar

| İmkan | Vəziyyət | Asılılıq |
|---|---|---|
| API server (REST + SSE), cihaz cütləşməsi, autentifikasiya | yoxdur | EventStore, ApprovalGateway |
| **Android APK** (söhbət, təsdiq, bildiriş, səs) | yoxdur | API server |
| Real OS sandbox adapteri | yoxdur | — (canlı veb və Builder üçün şərtdir) |
| Canlı internet araşdırması | alət təsviri var, icra bağlı | OS sandbox, allowlist |
| Real LLM provayderi | yoxdur | sahib: provayder, açar (env), xərc limiti |
| Real səs (TTS/STT), təbii qadın səsi | interfeys və mock var | provayder seçimi, sahib təsdiqi |
| Push (FCM) | `NotificationChannel` interfeysi var | Android tətbiqi, sahib hesabı |
| Website & App Builder | plan var (bölmə 8) | sandbox, API, LLM, təsdiq |
| Trading/birja | yoxdur | risk mühərriki, paper rejimi |
| Müşahidə, sirr idarəsi, ehtiyat nüsxə | qismən (jurnallar) | — |

## 3. İnkişaf mərhələləri

Hər mərhələ ayrıca branch/PR, testlər əvvəl, `test-gate` ilə bağlanır. Xarici hesab, API açarı və ödənişli xidmət yalnız
sahibin açıq təsdiqi ilə qoşulur.

**Tamamlanıb (PR-lər açıqdır):** 6 asılılıq/retry · 7 Effect Broker · 8 təsdiq xidməti · 9 Tool Runtime və workspace ·
10 LLM adapteri (mock) · 11 yaddaş, virtual şirkət, davamlı workflow · **12 araşdırma, proaktiv zəka, Android-first interfeyslər**.

| # | Mərhələ | Nəticə | Çıxış meyarı |
|---|---|---|---|
| 13 | **API server** | REST + SSE; `EventStore` sinxronizasiyası; cihaz cütləşməsi; qısa ömürlü token; təsdiq API-si (`ApprovalGateway` yalnız autentifikasiyadan sonra) | Autentifikasiyasız sorğu rədd edilir; təsdiq yalnız cütləşmiş cihazdan |
| 14 | **OS sandbox adapteri** | Real izolyasiya (fayl, şəbəkə, proses, yaddaş, vaxt) + müstəqil attestasiya sınaqları | `REQUIRED_PROBES` müstəqil testlərlə sübut olunur |
| 15 | **Real LLM adapteri** | OpenAI/Anthropic `LLMProvider`; xərc ledger-ə; açar yalnız env | Mock ilə eyni kontrakt testləri; sızma yoxdur |
| 16 | **Android tətbiqi (APK)** | Söhbət, bildiriş (FCM), təsdiq (biometrik), offline kəş (EventStore), önizləmə | Cihaz/emulyator testləri; təsdiq yalnız toxunuş/biometrik |
| 17 | **Səsli danışıq** | STT/TTS provayderləri; təbii qadın səsi (az-AZ); push-to-talk | Səs heç vaxt təsdiq mənbəyi deyil; transkript etibarsızdır |
| 18 | **Canlı internet araşdırması** | `web.search/fetch` sandbox-da; allowlist, sürət limiti, büdcə | Mənbəsiz iddia fakt kimi göstərilmir; SSRF testləri |
| 19 | **Website & App Builder** | Bölmə 8 | Nəşr təsdiqsiz mümkün deyil |
| 20 | Web admin paneli | Köməkçi kanal: audit, xərc, təsdiq, kill switch | E2E testlər |
| 21 | Telegram (istəyə bağlı) | İkinci kanal: bildiriş və sadə komandalar; böyük təsdiqlər Android-də | Kontrakt testləri; sahib chat id-si |
| 22 | Trading — yalnız paper | Simulyasiya, risk mühərriki | Paper meyarları (bölmə 6) |
| 23 | Trading — canlı (şərti) | Yalnız sahib açıq aktivləşdirəndə | Bölmə 6 qaydaları |
| 24 | Sərtləşdirmə | Müşahidə, ehtiyat nüsxə, sirr rotasiyası, xaos testləri | Yoxlama siyahısı |

Sıra səbəbi: API və sandbox Android, canlı veb və Builder üçün şərtdir; trading sonda.

## 4. Texniki arxitektura

```
        Android APK (əsas)         Web admin (köməkçi)      Telegram (istəyə bağlı)
                \                        |                        /
                 +----- API server (REST + SSE, autentifikasiya, cihaz cütləşməsi) -----+
                                         |                  EventStore (seq/ack)  <-- bildirişlər
                  CEO agent ── şirkət (8 departament, rol/səlahiyyət/büdcə)  <-- Proaktiv mühərrik
                                         |                          |              (təklif, icra yox)
                      Orchestrator ── Workflow (DAG, retry, davamlı) |
                                         |                          Yaddaş (etibarsız mətn)
                    ┌──────────── EFFECT BROKER ────────────┐
                    │ stop · təkrar · büdcə rezervi · Gate · Təsdiq · audit │
                    └───────┬───────────────────────────────┘
          Tool Runtime (allowlist) ── alətlər: fayl (workspace) · araşdırma · kod/Builder · səs · trading
                    │                                   (network/shell/system/env yalnız OS sandbox ilə)
              LLMProvider (yalnız təklif; çıxış etibarsızdır)
```

- **Effect broker** yan təsirin yeganə yoludur; Gate, Ledger, Təsdiq onun nüvəsidir.
- **LLM təklif edir, kod qərar verir:** model, yaddaş, veb və səs transkripti etibarsız giriş sayılır; icazə, limit və risk deterministik koddadır.
- **Android-first:** müştərilərdə biznes məntiqi yoxdur; hər şey API və `EventStore` üzərindən; bildirişlər `Notification` DTO (`schema_version: 1`).
- **Saxlama:** SQLite (reyestr, ledger, yaddaş, təsdiq, workflow, bildiriş vəziyyəti, hadisələr) və append-only JSONL audit.
- **Provayder interfeysləri:** `LLMProvider`, `NotificationChannel`, `VoiceSynthesizer/Recognizer`, `SandboxAdapter`, (gələcək) `ExchangeClient`; hər biri mock + real, testlər yalnız mock.
- **Konfiqurasiya:** sirlər yalnız mühit dəyişənlərindən (`load_config`); `.env.example` boş dəyərlərlə.

## 5. Təhlükəsizlik qaydaları

1. Pul, xarici hesab, nəşr: **hər hadisə üçün** sahibin təsdiqi; əvvəlki təsdiq yeni hadisəyə şamil olmur.
2. Təsdiq əməlin məzmun hash-inə, məbləğə və hədəfə bağlıdır; vaxtı bitir; tək istifadəlikdir; agent/LLM təsdiq edə bilməz.
3. Yan təsir yalnız effect broker-dən keçir; birbaşa alət çağırışı və Gate-i keçən yol qadağandır.
4. **İnternet, yaddaş, model və səs məzmunu etibarsızdır:** içindəki göstəriş əmr sayılmır, icazə vermir, avtomatik icra olunmur; mətn təmizlənir, təlimat bənzər hissələr iddialardan çıxarılır.
5. **Mənbəsiz məlumat təsdiqlənmiş fakt kimi təqdim olunmur:** iddia statusu (çoxmənbəli dəstək / qismən / tək mənbə / ziddiyyət) göstərilir; etibarlılıq heuristikdir.
6. Ən az imtiyaz: agent icazəsi ∩ səlahiyyət; səlahiyyət genişləndirməsi yalnız sahib; limitlər yalnız sıxlaşdırıla bilər.
7. Sirlər yalnız mühit dəyişənində; kodda, jurnalda, yaddaşda, bildirişdə, promptda olmur; sirr skanı CI-dədir.
8. Sandbox: şəbəkə/fayl/proses/yaddaş/vaxt hədləri; in-process məhdudiyyət OS sandbox kimi təqdim edilmir.
9. Xərc: ümumi/agent/tapşırıq limiti atomik; naməlum xərc rezervi avtomatik silinmir, əl ilə uzlaşdırılır.
10. Audit: hər qərar (icazə və rədd) yazılır; sirr yazılmır; audit–ledger uyğunluğu avtomatik yoxlanır.
11. Stop bayrağı və kill switch: hər müştəridən əlçatan.
12. Bildirişlər: yalnız lazımlı olanlar (əhəmiyyət, cooldown, limit, sakit saat); təkliflər icra olunmur; mock mənbəli nəticə bildiriş yaratmır.
13. **Səs heç vaxt təsdiq mənbəyi deyil:** pul/hesab/nəşr təsdiqi yalnız autentifikasiya olunmuş toxunuş/biometrik ilə.
14. Gəlir/qazanc vədi heç bir sənəddə, UI mətnində və agent cavabında olmur.
15. Mock və real nəticələr ayrıdır (`is_mock`, `provenance`); mock heç vaxt real kimi göstərilmir.

## 6. Trading təhlükəsizliyi

Trading itkiyə gətirə bilər; sistem heç bir nəticə vəd etmir. Hüquqi/vergi məsuliyyəti sahibdədir.

1. **Paper əvvəl.** Canlı rejim yalnız paper dövrü sahibin təyin etdiyi meyarlarla keçəndən sonra və ayrıca açıq aktivləşdirmə ilə.
2. **Hesab:** ayrıca alt-hesab, yalnız ticarət icazəli açar, **çıxarış (withdraw) qeyri-aktiv**, IP icazə siyahısı; açar yalnız env-də.
3. **Risk mühərriki deterministik koddur** (LLM deyil): LLM yalnız təklif verir; birjaya birbaşa çıxış yoxdur.
4. **Hədlər:** maksimal sifariş, mövqe, gündəlik itki, açıq sifariş sayı, alət siyahısı; aşılsa bloklanır və audit-ə yazılır.
5. **Defolt qadağalar:** marja, leverac, törəmə alətlər, qısa satış.
6. **Təsdiq rejimləri:** hər sifariş üçün əl ilə (defolt) və ya sahibin imzaladığı, vaxt/hədlə məhdud "strategiya zərfi" (yalnız paper-dən sonra).
7. **Dövrə kəsici:** gündəlik itki, ardıcıl xəta, köhnə qiymət/anomaliya → avtomatik dayandırma; yenidən başlatma sahibin təsdiqi ilə.
8. **Düzgünlük:** idempotent `client_order_id`, birja ilə mütəmadi uzlaşdırma, uyğunsuzluqda dayandırma.
9. **Realizm:** paper-də komissiya, sürüşmə, gecikmə; backtest gələcək nəticə kimi təqdim olunmur.
10. **Şəffaflıq:** hər sifariş üçün səbəb, risk yoxlaması və təsdiq izi; hesabatlarda gəlir vədi və proqnoz yoxdur.
11. **Kill switch:** bütün açıq sifarişlərin ləğvi və yeni sifarişlərin bloku, Android-dən bir toxunuşla.
12. İlk canlı mərhələ simvolik, sahibin təyin etdiyi çox kiçik limitlə.

## 7. Android-first plan

**Ortaq:** bir backend API; Android tətbiqində biznes məntiqi yoxdur. Tək sahibli autentifikasiya, cihaz cütləşməsi, qısa ömürlü token, itmiş cihazın uzaqdan ləğvi.

| Sahə | Plan | Artıq hazır olan |
|---|---|---|
| Sinxronizasiya | SSE + `EventStore` cursor/ack, offline kəş | `agents/mobile.py: EventStore` (seq, ack, prune, secret/ölçü yoxlaması) |
| Bildirişlər | FCM push; kanal `NotificationChannel` | `Notification` DTO (versiyalı JSON, Azərbaycan dili, əməliyyat düymələri, `speech_text`), `InMemoryChannel/NullChannel` |
| Səs-küy nəzarəti | Əhəmiyyət, cooldown, saatlıq/gündəlik limit, sakit saat, digest, ack/snooze/dismiss | `ProactiveEngine` + `NotificationPolicy` |
| Təsdiq | Android-də biometrik təsdiq; `ApprovalGateway` yalnız autentifikasiyadan sonra | `ApprovalService` (hash, vaxt, tək istifadə), `ApprovalReader` |
| Səs | Hədəf: **təbii qadın səsi (az-AZ)**; push-to-talk; ucadan oxunan mətn təmizlənir (URL, sirr, uzun id) | `VoiceProfile`, `VoiceSynthesizer/Recognizer`, `speakable`, `SpeechPolicy` (səs təsdiq edə bilməz) |
| Araşdırma | Nəticələr mənbə/tarix/status ilə göstərilir; mock "SİMULYASİYA" | `render_az`, `ResearchReport` |
| Köməkçi kanallar | Web admin (audit, xərc, kill switch); Telegram yalnız istəyə bağlı | — |

Səs seçimi (provayder, səs adı) sahib qərarıdır; seçim real qadın səsinin təbiiliyi və az-AZ keyfiyyəti üzrə dinləmə testi ilə edilməlidir.

## 8. Gələcək: Website & App Builder (yalnız arxitektura və plan)

**Məqsəd:** istifadəçinin səsli və ya yazılı tapşırığı ilə sayt və tətbiq yaratmaq: kod yaz → test et → önizləmə göstər → təsdiqlə yayımla.
Bu mərhələdə kod yazılmır; aşağıdakı plan mərhələ 19-un əsasıdır və 14 (OS sandbox), 13 (API), 15 (LLM) şərtidir.

```
Android (səs/mətn) → tapşırıq qəbulu → spesifikasiya və plan (LLM TƏKLİFİ, doğrulanır) → SAHİB planı təsdiqləyir
   → layihə workspace-i (sandbox) → kod yaratma addımları (Tool Runtime: fayl alətləri; build/test sandbox-da)
   → test və lint (sandbox) → önizləmə (izolə, müvəqqəti, şəbəkəsiz statik/lokal server; Android WebView + ekran görüntüsü)
   → nəzərdən keçirmə/dəyişiklik dövrü → NƏŞR: Approval Service (publish) + Effect Broker → hədəf (allowlist) → geri qaytarma planı
```

| Addım | Qayda | Təhlükəsizlik |
|---|---|---|
| Qəbul | Səsli/yazılı tapşırıq; transkript etibarsızdır | Tapşırıq təklifdir; sirr daxil edilməsi rədd edilir |
| Plan | Səhifə/komponent siyahısı, texnologiya, asılılıqlar, xərc və vaxt qiyməti | Plan doğrulanır (ciddi sxem), sahib təsdiq edir; LLM təsdiq verə bilməz |
| Kod | Yalnız layihə workspace-i; şablon kitabxanası; kod yaratma `fs.write` ilə | Workspace sərhədi; çıxış ölçü/kvota; sirr yoxdur |
| Asılılıqlar | Allowlist və lisenziya yoxlaması; versiyalar bağlanır | Yeni asılılıq plan təsdiqində göstərilir; sandbox-da quraşdırma |
| Test | Vahid/inteqrasiya/ekran testləri sandbox-da; nəticə real çıxışla göstərilir | Test keçmədən "hazırdır" deyilmir (`test-gate`) |
| Önizləmə | İzolə, müvəqqəti, yalnız sahibə görünən | Xarici resurs çağırışı bloklanır; ekran görüntüsü artefakt kimi saxlanır |
| Nəşr | Hədəf allowlist-dən; məzmun hash-i təsdiqə bağlanır; dəyişərsə yeni təsdiq | `publish` həssas icazədir: hər nəşr üçün təsdiq, vaxt limiti, tək istifadə |
| Geri qaytarma | Əvvəlki versiyaya qayıtma; nəşr izi audit-də | Naməlum vəziyyətdə nəşr dayanır |
| Yaddaş | Layihə qərarları, versiyalar, təsdiqlər `Memory Service`-də (etibarsız mətn) | Yaddaş mətni əmr deyil |

**Qəbul meyarları:** (1) plan təsdiqsiz kod yaranmır; (2) bütün fayl/alət çağırışları broker-dən keçir; (3) testlər keçməyincə önizləmə "hazır" kimi göstərilmir;
(4) nəşr təsdiqsiz, hash dəyişdikdə və ya vaxtı bitmiş təsdiqlə mümkün deyil; (5) önizləmə və build şəbəkəsiz/izolə işləyir; (6) hər addım xərc büdcəsi və audit ilə.
**Qeyri-məqsədlər (bu mərhələdə):** kod yazmaq, real domen/hosting hesabı, ödənişli xidmət qoşmaq. **Açıq suallar:** hədəf texnologiyalar (statik sayt, React, mobil), hosting seçimi, şablon mənbəyi.

## 9. Test və qəbul meyarları

**Hər mərhələ:** testlər əvvəl; `pytest -q` yaşıl; CI yaşıl; `test-gate` hesabatı real nəticə ilə; STATUS.md yenilənir; PR sahibin təsdiqi ilə birləşir.

- **Gate-i keçməmək:** broker kənarında yan təsir yolu yoxdur (statik testlər); alət çağırışı Gate/büdcə olmadan icra olunmur.
- **Xərc:** limit aşımında runner/alət çağırılmır; paralel rezervasiya limiti aşmır; ledger–audit uyğunluğu avtomatik testdədir.
- **Təsdiq:** köhnə, dəyişdirilmiş, vaxtı keçmiş və təkrar istifadə olunan təsdiqlər rədd edilir; agent/LLM/səs təsdiq edə bilmir.
- **Etibarsız məzmun:** veb/yaddaş/model/səs mətnindəki əmr icra olunmur (qırmızı komanda testləri); təlimat bənzər cümlələr iddia olmur.
- **Araşdırma:** `supported` ən azı 2 müstəqil etibarlı mənbə tələb edir (tip səviyyəsində); ziddiyyət və köhnə məlumat işarələnir; mock və real ayrıdır; SSRF/daxili ünvan rədd edilir.
- **Proaktiv:** boş vəziyyətdə bildiriş yoxdur; cooldown/limit/sakit saat/digest işləyir; təkliflər icra olunmur.
- **Sirr:** repo, jurnal, yaddaş, bildiriş, hadisə axınında sirr yoxdur; env dəyişəni olmadan real provayder işə düşmür.
- **Etibarlılıq:** proses yenidən açılanda ledger/təsdiq/yaddaş/workflow/bildiriş vəziyyəti qalır; kill switch yük altında işləyir.
- **UI (mərhələ 16+):** Android cihaz/emulyator testləri, təsdiq yalnız toxunuş/biometrik; Web üçün E2E; hadisə sinxronizasiyası (cursor/ack) testləri.
- **Trading:** paper-də risk mühərriki hədləri testlə sübut olunub; canlıdan əvvəl sahibin yazılı təsdiqi; birja uzlaşdırma testləri.
- **Real xarici çağırış** testlərdə yoxdur; real provayder yoxlaması yalnız sahibin icazəsi ilə, ayrıca əl ilə.

## Sahibin qərarları (gözləyir)

1. LLM provayderi və aylıq xərc limiti; səs provayderi (təbii qadın səsi, az-AZ) və dinləmə testi.
2. Android: native (Kotlin) yoxsa PWA ilə başlamaq; push (FCM) hesabı.
3. Hosting (lokal, VPS) və domen; Builder üçün hədəf texnologiyalar.
4. Trading: birja, yurisdiksiya, ilk risk hədləri, paper meyarları.
5. PR #1–#12 birləşmə qaydası (hazırda zəncirvari base-lərlə açıqdır); `ROADMAP.md` PR #5-in branch-ında da mövcuddur, birləşmədə toqquşma ola bilər.

## Mərhələ 13 — Android API və tətbiq bünövrəsi (status)
- Backend API (`api/`): cihaz cütləşməsi, token fırlanması, söhbət, hadisə axını, bildirişlər, yaddaş, şirkət (yalnız oxu), CEO işləri, təsdiq (step-up), stop/resume, səs interfeysi (mock) — **hazırdır və testlənib (805 test)**.
- Android (`android/`): Kotlin + Compose layihəsi yazılıb, **APK build EDİLMƏYİB** (Android SDK mühitdə yoxdur). Növbəti addım: sahibin maşınında build və cihaz yoxlaması.
- Sonrakı: real səs (az-AZ qadın) provayderi, FCM push, Keystore-a bağlı biometrik açar, real LLM konfiqurasiyası — hamısı sahib təsdiqi tələb edir.

## Mərhələ 14 — Android inteqrasiyası və təhlükəsizlik (status)
- Backend: açıq açarlı cihaz cütləşməsi (Keystore P-256, sahiblik sübutu), imza ilə step-up, `/v1/capabilities` (mock/real fərqi), server qoruyucuları; real HTTP üzərində inteqrasiya testləri — **hazırdır və testlənib (852 test)**.
- Android: Keystore + biometrik imza, real HTTP müştərisi (timeout/backoff/xəta növləri), Əsas/Söhbət/Yaddaş/Bildirişlər/Agentlər/Tarixçə/Ayarlar, qaranlıq tema, az/en/ru/tr — yazılıb, **APK build EDİLMƏYİB və cihazda sınanmayıb** (Android SDK yoxdur).
- Növbəti: sahibin maşınında build və cihaz/emulyator sınağı; sonra real səs, FCM, real LLM və agent icraçısı (hamısı sahib təsdiqi tələb edir).

## Mərhələ 15 — Android build hazırlığı (status)
- **APK build edilmədi**: `dl.google.com` bloklu, Android SDK yoxdur (mühit dəyişikliyi təklifi sahib tərəfindən rədd edildi). Maneə, tələb olunan versiyalar və yerli addımlar `android/BUILD_STATUS.md`-dədir; imzalama/yayımlama qurulmayıb.
- Audit (`android/AUDIT.md`), JVM kontrakt layihəsi (Android-dan asılı olmayan Kotlin qatı həqiqətən kompilyasiya olundu, həqiqi backend ilə 27 test keçdi), telefon sınağı planı (`android/DEVICE_TEST.md`).
- Növbəti: sahibin maşınında `assembleDebug` və `DEVICE_TEST.md` sınağı; sonra real səs, FCM, real LLM/agent icraçısı və imzalama (hamısı sahib qərarı).

## Mərhələ 16 — Daimi hibrid AI beyni (status)
- Cloud və Local mühərrikləri ortaq interfeysdə, AUTO/CLOUD/LOCAL, təhlükəsiz fallback, təkrar icranın qarşısı, razılıq və məxfilik, ortaq oxu-yaddaş — **hazırdır və testlənib (927 Python testi, 34 Kotlin JVM testi)**; ətraflı `HYBRID_AI.md`.
- Real Cloud provayderi, real lokal model və real səs **yoxdur** (simulation/not_configured). APK build edilməyib; Android UI kodu cihazda sınanmayıb.
- Növbəti (hamısı sahib qərarı/razılığı ilə): Cloud provayderi adapteri (açar yalnız serverin env-ində, ödənişli istifadədən əvvəl razılıq), lokal modelin ölçülməsi (az/tr/ru keyfiyyəti) və cihaz/server runtime-ı, real səs, Android razılıq ekranı.
