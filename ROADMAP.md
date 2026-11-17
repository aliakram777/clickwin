# NAYMERA — Məhsul yol xəritəsi

Tarix: 2026-11-17 · Əsas: `claude/stage5-budget-policy` (mərhələ 1–5). Mərhələ 6 (asılılıq/retry) PR #6-dadır, birləşdirilməyib.

Bu sənəd plandır, vədi yox. Gəlir, qazanc və ya gəlirli nəticə haqqında heç bir iddia yoxdur;
trading itkiyə səbəb ola bilər (bax: bölmə 6).

Təsdiqlənmiş məhsul: Android + Web + Telegram · yazılı və səsli danışıq · autonom CEO və şöbə agentləri ·
internet araşdırması və brauzer · sayt/proqram yaratma · virtual şirkət idarəetməsi · trading/birja
inteqrasiyası · maliyyə, hesab və nəşr üçün təsdiq qapısı.

---

## 1. Mövcud imkanlar (mərhələ 1–5, 180 test, CI yaşıl)

| Modul | Nə edir |
|---|---|
| `agents/spec.py` | Agent təsviri (YAML) + doğrulama (ad, məqsəd, alətlər, icazələr, giriş/çıxış, xərc limiti) |
| `agents/factory.py` | Məqsəd mətnindən təsvir; `LLMClient` interfeysi, yalnız `MockLLM` |
| `agents/verify.py` | Agenti test tapşırığı ilə yoxlayır, uğursuzluğu `logs/errors.jsonl`-ə yazır |
| `agents/registry.py` | SQLite agent reyestri (şöbə ilə) |
| `agents/orchestrator.py` | Tapşırığı şöbə agentinə yönləndirir; icra yalnız Gate və büdcə rezervindən sonra |
| `agents/ceo.py` | Plan (`RulePlanner`), bölgü, toplama, yoxlama, uğursuz agent hesabatı, iş büdcəsi |
| `safety/gate.py` | İcazə siyahısı, xərc limiti, pul/hesab/nəşr təsdiq qapısı, audit, stop bayrağı |
| `safety/ledger.py` | SQLite xərc kitabı: reserved/committed/released, atomik limitlər (ümumi/agent/tapşırıq), təkrar-qoruma, audit uyğunluğu |
| CI | GitHub Actions, `pytest`, Python 3.12 |

Həddlər: bütün icra `runner` funksiyasıdır (mock). Real LLM, alət, brauzer, UI, səs, trading yoxdur.
Mərhələ 6 (PR #6): tapşırıq DAG-ı, məhdud retry, naməlum xərcdə əl ilə uzlaşdırma.

## 2. Çatışmayan imkanlar

| İmkan | Vəziyyət | Asılılıq |
|---|---|---|
| Real LLM adapteri | yoxdur | sahib: provayder + API açarı (env) |
| Alət icra mühiti (sandbox) | yoxdur | — |
| İnternet araşdırması, brauzer | yoxdur | alət mühiti, təsdiq xidməti |
| Mərkəzi təsdiq xidməti (UI ilə) | Gate-də mexanizm var, kanal yoxdur | server |
| Yaddaş, şirkət modeli (rollar, məqsədlər, tarixçə) | yoxdur | SQLite sxemi |
| Server (API) | yoxdur | — |
| Web, Telegram, Android | yoxdur | server, autentifikasiya |
| Səs (STT/TTS) | yoxdur | provayder |
| Sayt/proqram yaratma | yoxdur | sandbox, git/nəşr qapısı |
| Trading/birja | yoxdur | risk mühərriki, paper rejimi |
| Müşahidə (metrik, xəbərdarlıq) | yalnız jurnal faylları | — |
| Sirr idarəsi, çoxlu cihaz/autentifikasiya | yoxdur | server |

## 3. İnkişaf mərhələləri

Hər mərhələ ayrıca branch/PR, testlər əvvəl, `test-gate` ilə bağlanır. Xarici hesab, API açarı və
ödənişli xidmət yalnız sahibin açıq təsdiqi ilə qoşulur (bölmə 5).

| # | Mərhələ | Nəticə | Çıxış meyarı |
|---|---|---|---|
| 6 | Asılılıq + retry (PR #6) | DAG, məhdud retry, uzlaşdırma | PR yoxlanılıb və birləşdirilib |
| 7 | **Effect broker** (hər yan təsirin yeganə yolu) | Alət çağırışı = `Action`; Gate + büdcə + audit olmadan icra yoxdur | Gate-i keçməyin mümkünsüzlüyü testləri |
| 8 | **Təsdiq xidməti** | Gözləyən təsdiq növbəsi, hash-ə bağlı, vaxt aşımlı, tək istifadəlik | Köhnə/dəyişdirilmiş təsdiq rədd edilir |
| 9 | Alət mühiti | Alət reyestri, sandbox (fayl/şəbəkə hədləri), vaxt/yaddaş limiti | Sandbox-dan çıxış testləri |
| 10 | LLM adapteri | `LLMClient` real provayderlə; xərc ledger-ə yazılır; prompt-injection müdafiəsi | Mock ilə eyni kontrakt testləri |
| 11 | Yaddaş və virtual şirkət | Şirkət/şöbə/rol/məqsəd modeli, qısa və uzun yaddaş, CEO planlaşdırması (LLM Planner) | Plan → icra → hesabat dövrü |
| 12 | İnternet araşdırması və brauzer | Axtarış və səhifə oxuma (yalnız oxu), mənbə qeydi | Veb məzmunu əmr kimi icra olunmur |
| 13 | API server | HTTP + SSE/WebSocket, tək-sahibli autentifikasiya, cihaz cütləşməsi | Autentifikasiyasız sorğu rədd edilir |
| 14 | Web tətbiqi | Söhbət, agent paneli, təsdiq qutusu, xərc/audit görünüşü | E2E testlər |
| 15 | Telegram botu | Mətn + səs mesajı, düymə ilə təsdiq, yalnız sahib çatı | Saxta Telegram serveri ilə kontrakt testləri |
| 16 | Səsli danışıq | STT/TTS interfeysi, push-to-talk | Səs yalnız komanda; pul təsdiqi səslə deyil |
| 17 | Android | Web (PWA) üzərində, push bildiriş, biometrik təsdiq; sonra lazım olsa native | Cihaz testləri |
| 18 | Sayt/proqram yaratma | Sandbox-da kod yaz, test et, preview; nəşr ayrıca təsdiqlə | Nəşr təsdiqsiz mümkün deyil |
| 19 | Trading — **yalnız paper** | Simulyasiya, risk mühərriki, hesabat | Paper meyarları (bölmə 6) |
| 20 | Trading — canlı (şərti) | Yalnız sahib açıq aktivləşdirəndə və paper meyarları keçəndən sonra | Bölmə 6 qaydaları |
| 21 | Sərtləşdirmə | Müşahidə, ehtiyat nüsxə, sirr rotasiyası, yük/xaos testləri | Yoxlama siyahısı |

Sıra səbəbi: təsdiq/effect qatı hər xarici imkandan əvvəl gəlir; trading sonda və iki mərhələdə.

## 4. Texniki arxitektura

```
 Android(PWA)   Web    Telegram           ← müştərilər (nazik, məntiq yoxdur)
        \         |        /
         +---- API server (HTTP + SSE) ----+  autentifikasiya, cihaz cütləşməsi
                       |
                  CEO agent ── şöbə agentləri (registry)
                       |
                 Orchestrator
                       |
              ┌── EFFECT BROKER ──┐   ← yan təsirin yeganə yolu
              │ Gate · Büdcə (ledger) · Təsdiq xidməti · Audit │
              └───────┬───────────┘
        alətlər (sandbox): web/brauzer · kod/sayt · LLM · səs · trading
```

- **Effect broker:** hər alət çağırışı `Action` olur; icazə → büdcə rezervi → (həssassa) təsdiq → icra → hesablaşma.
  Mövcud `Gate` və `Ledger` bunun nüvəsidir; yeni kod onları keçmir.
- **LLM təklif edir, kod qərar verir:** LLM yalnız plan/mətn yaradır; icazə, limit, risk deterministik koddadır.
- **Saxlama:** SQLite (reyestr, ledger, yaddaş, təsdiq növbəsi); append-only JSONL audit. Çoxlu proses lazım olsa, sonra Postgres (ayrıca qərar).
- **Provayder interfeysləri:** `LLMClient`, `STT/TTS`, `SearchClient`, `BrowserTool`, `ExchangeClient`: hər biri mock + real; testlər yalnız mock.
- **Müştərilər:** Web əsas; Android əvvəl PWA, Telegram eyni API-nin başqa kanalı.
- **Proses modeli:** server uzun işləyən proses; iş növbəsi və stop bayrağı; hər iş `job_id` və `op_id` ilə izlənir.
- **Konfiqurasiya:** sirlər yalnız mühit dəyişənlərindən; `.env.example` boş dəyərlərlə.

## 5. Təhlükəsizlik qaydaları

1. Pul, xarici hesab, nəşr: **hər hadisə üçün** sahibin təsdiqi; əvvəlki təsdiq yeni hadisəyə şamil olmur.
2. Təsdiq əməlin məzmun hash-inə, məbləğə və hədəfə bağlıdır; vaxtı bitir; tək istifadəlikdir.
3. Yan təsir yalnız effect broker-dən keçir; birbaşa alət çağırışı və Gate-i keçən yol qadağandır.
4. Xarici məzmun (veb, e-poçt, fayl) **etibarsız məlumatdır**: içindəki göstəriş əmr sayılmır, icazə vermir.
5. Ən az imtiyaz: hər agentin icazə siyahısı, xərc limiti, alət siyahısı var; limitlər yalnız sıxlaşdırıla bilər.
6. Sirlər yalnız mühit dəyişənində; kodda, jurnalda, promptda, commit-də olmur; sirr skanı CI-dədir.
7. Sandbox: şəbəkə və fayl hədləri, vaxt/yaddaş limiti; brauzer ayrı profil, sahibin sessiyalarına çıxışsız.
8. Xərc: ümumi/agent/tapşırıq limiti atomik; naməlum xərc rezervi avtomatik silinmir, əl ilə uzlaşdırılır.
9. Audit: hər qərar (icazə və rədd) yazılır; sirr yazılmır; audit ilə ledger uyğunluğu avtomatik yoxlanır.
10. Stop bayrağı və kill switch: bütün agentləri dərhal dayandırır; hər müştəridən əlçatan.
11. Yeni hesab açmaq, qeydiyyat, CAPTCHA/ToS-u keçmək avtomatik edilmir; sahib təsdiqi tələb edir.
12. Nəşr (sayt, post, mesaj, release) yalnız sahibin baxıb təsdiqlədiyi məzmunla.
13. Gəlir/qazanc vədi heç bir sənəddə, UI mətnində və agent cavabında olmur.
14. Şəxsi məlumat minimal saxlanır; üçüncü tərəf provayderlərə yalnız lazım olan göndərilir.

## 6. Trading təhlükəsizliyi

Trading itkiyə gətirə bilər; sistem heç bir nəticə vəd etmir. Hüquqi/vergi məsuliyyəti sahibdədir.

1. **Paper əvvəl.** Canlı rejim yalnız paper dövrü sahibin əvvəlcədən təyin etdiyi meyarlarla (müddət, risk hədləri, uyğunluq) keçəndən sonra və sahibin ayrıca açıq aktivləşdirməsi ilə.
2. **Hesab:** ayrıca alt-hesab, yalnız ticarət icazəli açar, **çıxarış (withdraw) icazəsi qeyri-aktiv**, IP icazə siyahısı; açar yalnız mühit dəyişənində.
3. **Risk mühərriki deterministik koddur** (LLM deyil): LLM yalnız təklif verir, risk mühərriki və Gate qərar verir. LLM birjaya birbaşa çıxa bilməz.
4. **Hədlər:** maksimal sifariş ölçüsü, maksimal mövqe, gündəlik itki limiti, açıq sifariş sayı, alət siyahısı (allowlist). Hədlər aşılsa əməliyyat bloklanır və audit-ə yazılır.
5. **Defolt qadağalar:** marja, leverac, törəmə alətlər, qısa satış; yalnız sahib ayrıca siyasətlə açır.
6. **Təsdiq rejimləri:** (a) hər sifariş üçün əl ilə təsdiq (defolt); (b) sahibin imzaladığı, vaxt və hədlə məhdud "strategiya zərfi", yalnız paper-dən sonra. Zərfdən kənar hər şey (a) rejiminə düşür.
7. **Dövrə kəsici:** gündəlik itki limiti, ardıcıl xəta, qiymət/məlumat köhnəlməsi və ya anomaliya → avtomatik dayandırma; yenidən başlatma yalnız sahibin təsdiqi ilə.
8. **Düzgünlük:** `client_order_id` ilə idempotent sifariş, birja ilə mütəmadi uzlaşdırma (mövqe, balans, açıq sifarişlər), uyğunsuzluqda dayandırma.
9. **Realizm:** paper-də komissiya, sürüşmə və gecikmə modelləşdirilir; backtest nəticələri gələcək nəticə kimi təqdim olunmur.
10. **Şəffaflıq:** hər sifariş üçün səbəb, risk yoxlaması və təsdiq izi saxlanır; hesabatlarda gəlir vədi və ya proqnoz yoxdur.
11. **Kill switch:** bütün açıq sifarişlərin ləğvi və yeni sifarişlərin bloku, bir toxunuşla (Web/Telegram/Android).
12. İlk canlı mərhələ simvolik məbləğlə, sahibin müəyyən etdiyi çox kiçik limitlə başlayır.

## 7. Android / Web / Telegram planı

**Ortaq:** bir backend API; müştərilərdə biznes məntiqi yoxdur. Tək sahibli autentifikasiya, cihaz cütləşməsi,
qısa ömürlü token, sessiya ləğvi. Hər müştəri eyni təsdiq növbəsini göstərir.

| Kanal | Mərhələ | Əsas imkanlar | Təhlükəsizlik |
|---|---|---|---|
| Web | 14 | Söhbət, agent/şöbə paneli, təsdiq qutusu, xərc və audit, kill switch | Sessiya + CSRF/CORS sərt, təsdiq yalnız aydın düymə ilə |
| Telegram | 15 | Mətn, səs mesajı, inline düymə ilə təsdiq, bildirişlər | Yalnız sahibin chat id-si, webhook sirri, böyük məbləğ təsdiqi Web/Android-də |
| Səs | 16 | STT/TTS, push-to-talk | Səs yalnız komanda üçündür; pul/hesab/nəşr təsdiqi səslə deyil (açıq toxunuş/biometrik + məbləğ oxunuşu) |
| Android | 17 | Əvvəl PWA, push bildiriş, biometrik təsdiq; native yalnız PWA yetərsiz qalsa | Cihaz cütləşməsi, itmiş cihazı uzaqdan ləğv |

Sıra: Web → Telegram → Səs → Android. Hər biri əvvəlkinin API-sini təkrar istifadə edir.

## 8. Test və qəbul meyarları

**Hər mərhələ:** testlər əvvəl yazılır; `pytest -q` yaşıl; CI yaşıl; `test-gate` hesabatı real nəticə ilə; STATUS.md yenilənir; PR sahibin təsdiqi ilə birləşir.

**Ümumi qəbul meyarları**
- **Gate-i keçməmək:** broker üzərindən kənar yan təsir yolunun olmadığını yoxlayan testlər (alət çağırışı Gate/büdcə olmadan icra olunmur).
- **Xərc:** limit aşımında runner/alət çağırılmır; paralel rezervasiya limiti aşmır; ledger–audit uyğunluğu avtomatik testdədir.
- **Təsdiq:** köhnə, dəyişdirilmiş, vaxtı keçmiş və təkrar istifadə olunan təsdiqlər rədd edilir.
- **Prompt-injection:** veb/fayl məzmunundakı əmr icra olunmur (qırmızı komanda testləri).
- **Sirr:** repo və jurnallarda sirr yoxdur (skan); mühit dəyişəni olmadan real provayder işə düşmür.
- **Sandbox:** hədd aşımı (şəbəkə, fayl, vaxt) bloklanır.
- **Etibarlılıq:** proses yenidən açılanda ledger/təsdiq/yaddaş qalır; kill switch yük altında işləyir (xaos testi).
- **UI:** Web üçün E2E (Playwright); Telegram üçün saxta serverlə kontrakt testləri; Android üçün cihaz/emulyator testləri.
- **Trading:** paper rejimində risk mühərriki hədlərinin hamısı testlə sübut olunub; canlıdan əvvəl sahibin yazılı təsdiqi və paper hesabatı; birja uzlaşdırma testləri.
- **Real xarici çağırış** testlərdə yoxdur; real provayder yoxlaması yalnız sahibin icazəsi ilə, ayrıca əl ilə.

## Sahibin qərarları (gözləyir)

1. LLM provayderi və aylıq xərc limiti.
2. Hosting (lokal, VPS) və domen.
3. Trading: birja, yurisdiksiya, ilk risk hədləri, paper meyarları.
4. Android: PWA ilə kifayət, yoxsa native.
5. Mərhələ 6–7 sırası: PR #1–#6 birləşmə qaydası (hazırda zəncirvari base-lərlə açıqdır).
