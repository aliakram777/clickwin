# STATUS

Yenilənib: 2026-11-17

## Cari mərhələ
Mərhələ 8 — təsdiq xidməti (tamamlandı, PR təsdiq gözləyir). Əvvəlki PR-lər: #1–#7.

## Hazırdır
- Qurulum: README, CLAUDE.md, .gitignore, skill-lər (`test-gate`, `safety-layer`), CI
- `agents/spec.py` — AgentSpec (YAML: ad, məqsəd, alətlər, icazələr, giriş, çıxış, xərc_limiti) + doğrulama
- `agents/factory.py` — məqsəd mətnindən təsvir; LLM interfeysi (Protocol), hələlik `MockLLM`
- `agents/verify.py` — test tapşırığı ilə yoxlama; uğursuzluq `logs/errors.jsonl`-ə
- `safety/gate.py` — icazə siyahısı, xərc limiti, təsdiq qapısı (pul/xarici hesab/nəşr təsdiqsiz bloklanır), audit `logs/audit.jsonl`, stop bayrağı
- `agents/registry.py` — SQLite-da agent təsvirləri + şöbə: əlavə et, oxu, yenilə, siyahı
- `agents/orchestrator.py` — tapşırığı şöbə agentinə yönləndirir; hər icra `Gate` yoxlamasından keçir (icazə, limit, təsdiq, stop); xəta `logs/errors.jsonl`-ə
- `agents/ceo.py` — CEO: məqsədi `Planner` ilə təhlil edir (hələlik deterministik `RulePlanner`), şöbə agentlərinin girişlərini doldurur, tapşırıqları bölür, nəticələri toplayıb şöbə üzrə yoxlayır (`checks`), uğursuz agent hesabatı (`JobReport.failures/summary()`). Plan büdcəni aşırsa heç nə icra olunmur; stop bayrağı qalan addımları dayandırır. Hər icra Orchestrator -> Gate-dən keçir.
- `safety/ledger.py` — SQLite `CostLedger`: agent üzrə və ümumi xərc, `op_id` ilə təkrar qoruması (eyni əməliyyat iki dəfə hesablanmır), proses yenidən açılanda qalır; `reconcile()` icazə verilmiş audit sətirləri ilə kitabı müqayisə edir
- `Orchestrator(ledger=...)`: limit yoxlaması kitabdakı davamlı xərclə gate-dən keçir; xərc yalnız gate icazə verəndən sonra yazılır; təkrar `op_id` -> `duplicate` (icra və xərc yoxdur). `CEO.run(goal, job_id=...)` addımlara `op_id` verir. Ledger verilməsə əvvəlki davranış saxlanır.
- Mərhələ 5 — `safety/ledger.py`: rezerv modeli `reserved/committed/released` (+ `unknown` işarəsi), `BEGIN IMMEDIATE` ilə atomik `reserve()` (ümumi, agent və tapşırıq limiti; paralel bağlantılar limiti aşa bilmir), `commit/release/mark_unknown`, köhnə sxemin avtomatik miqrasiyası, `BudgetPolicy` (agent limiti spec limitindən yalnız aşağı ola bilər). `reconcile()` icazə verilmiş rezervi audit ilə yoxlayır, faktiki xərcin rezervi aşmasını bildirir.
- `Orchestrator(policy=..., actual_cost=...)`: təkrar yoxlama -> atomik rezerv (limit aşılsa runner çağırılmır) -> gate -> icra -> hesablaşma. Gate rədd etsə rezerv azad olunur. Uğurda faktiki xərc (reporter yoxdursa rezerv) yazılır; xətada `ExecutionError(cost=...)` real xərci bildirirsə yazılır, bildirmirsə rezerv qorunub `unknown` işarələnir (`ledger.unknown_entries()`, sonradan `commit`). `CEO` plan ümumi/tapşırıq limitini aşırsa heç nə icra etmir.
- Mərhələ 6 — `agents/workflow.py`: `PlanStep` DAG-ı; boş plan, təkrar id, özünə/olmayan asılılıq və dövr `PlanError` ilə rədd olunur; icra sırası asılılıq-əvvəl, elan sırası sabit.
- `CEO.run_workflow(goal, steps, job_id)`: addımlar sırayla icra olunur; uğursuz/bloklanmış/yoxlamadan keçməyən addımın asılıları `skipped`. Retry (`CEO(max_retries=0..2)`, default 2): yalnız `TransientError`/`TimeoutError`/`ConnectionError` və yalnız real xərc məlum olduqda (`TransientError(cost=...)`); hər cəhd unikal `op_id` (`job:addım:aN`), uğursuz cəhdin xərci yazılır, təkrar işə salma (`job_id`) `duplicate` olur. Xərc naməlumdursa avtomatik retry yoxdur, rezerv silinmir və `JobReport.needs_reconciliation` / `summary()` əl ilə uzlaşdırma tələb edir (`ledger.commit/release`).
- Büdcə rədləri (`ledger` limiti, CEO plan ümumi/tapşırıq limiti) audit jurnalına `allowed:false, source:"budget"` kimi yazılır (`safety.ledger.log_budget_denial`); `reconcile()` onları saymır. `safety/gate.py` dəyişməyib.
- Mərhələ 7 — `safety/broker.py`: `EffectBroker.run(Effect, executor, validate)` yan təsirin yeganə yoludur. Sıra: stop bayrağı (audit-li) -> təkrar -> atomik büdcə rezervi -> Gate (icazə/təsdiq/audit) -> icra -> hesablaşma. Rədd olunan effektdə icraçı çağırılmır (icazə, təsdiq, limit, stop, yanlış xərc). Təkrar `op_id` ledger ilə (ledger yoxdursa yaddaşda) bir dəfə icra olunur. Xərc naməlumdursa (xəta, yanlış hesabat, `KeyboardInterrupt`) rezerv qorunur, `unknown` işarələnir və yalnız əl ilə (`ledger.commit/release`) həll olunur. Gate xətası rezervi azad edib yuxarı ötürülür.
- `@broker_only` — alətləri bəzəyir: `EffectBroker.run` daxilindən kənarda çağırış `BrokerRequired` qaldırır (contextvar). `Orchestrator` runner-i yalnız broker-ə ötürülən lambda ilə çağırır; AST testi yeni birbaşa runner çağırışı əlavə olunsa düşür. Bir `EffectBroker` bir neçə orkestratorla paylaşıla bilər (`Orchestrator(broker=...)`). `ExecutionError/TransientError` indi `safety.broker`-dədir (`agents.orchestrator` yenidən ixrac edir). Gate və Ledger kodu dəyişməyib (ledger-ə yalnız `log_denial` əlavə olunub).
- Mərhələ 8 — `safety/approvals.py`: `ApprovalService` (SQLite `data/approvals.db`). Həssas effekt (pul/xarici hesab/nəşr) icradan əvvəl `pending_approval` olur (rezerv tutulmur, runner çağırılmır). Təsdiq unikal ID və effektin (agent, permission, cost, op_id, description, payload) kanonik JSON-unun **SHA-256** hash-inə bağlıdır; `expiry` (`ttl_seconds` 1..86400); **tək istifadəlik** (atomik `consume`); payload dəyişərsə təsdiq həmişəlik `invalidated`; DB qeydinin bütövlüyü (hash) yoxlanır. Saxta ID, təkrar istifadə, vaxtı bitmiş, rədd edilmiş və təsdiqlənməmiş təsdiq rədd olunur. Hər hadisə (`approval_requested/approved/denied/expired/consumed/rejected/invalidated/notify_failed`, `code` ilə) `audit.jsonl`-ə yazılır; `reconcile()` `event` sətirlərini saymır.
- Broker inteqrasiyası: `EffectBroker(approvals=svc.client)`. Həssas effekt üçün `op_id` məcburidir. Təsdiq Gate-in son qərarı anında (icazə və limit yoxlamalarından sonra) istifadə olunur, buna görə Gate `allowed` ⇔ təsdiq istifadə olunub; büdcə rədd etsə təsdiq istifadə olunmur. `Task/Effect.approval_id` istəyə bağlıdır (yoxdursa təsdiq hash ilə tapılır). `approver` və `approvals` birlikdə verilə bilməz.
- Rolların ayrılması: broker/orkestrator yalnız `ApprovalClient` (request/check/consume) görür; `ApprovalGateway` (approve/deny/pending/get) yalnız kanal adapterlərinə verilir. `Principal(kind)`: `human` (adapter autentifikasiyadan sonra), `mock` (yalnız `allow_mock=True`, audit-də `source_kind:"mock"`, `human:false`), `agent`/`system` həmişə rədd; qərar verən sahib siyahısında (boş siyahı ilə xidmət qurulmur) və agentin özü olmamalıdır. `ApprovalChannel.notify(ticket)` — Web/Telegram/Android adapterləri üçün interfeys.
- Testlər: `tests/test_{spec,factory,verify,gate,registry,orchestrator,ceo,ledger,cost_integration,ledger_budget,budget_integration,workflow,retry,broker,approvals,approval_broker}.py` (316 test)

## Məhdudiyyətlər
- Real LLM çağırışı yoxdur (yalnız mock); real API açarı və xarici çağırış istifadə olunmayıb.
- `runner` funksiyadır (mock); real agent icra mühiti və LLM hələ yoxdur.
- CEO təhlili açar-söz qaydalarıdır (LLM yoxdur); `run()` düz siyahıdır, asılılıq və retry yalnız `run_workflow()`-dadır. Retry arasında gözləmə (backoff) yoxdur.
- Ledger verilməyibsə xərc sayğacı yaddaşdadır (köhnə davranış); `BudgetPolicy` ledger tələb edir.
- Büdcə rədləri gate-dən keçmir, ayrıca audit sətri kimi yazılır; CEO-nun yaddaşdakı iş büdcəsi (`budget=`) rədd etməsi isə audit-ə yazılmır.
- Addımların girişi yalnız məqsəd mətnidir: asılılıq çıxışları növbəti addıma ötürülmür.
- Naməlum xərcli rezervlər yalnız əl ilə (`ledger.commit/release`) həll olunur; avtomatik silinmir, vaxt aşımı yoxdur.
- Faktiki xərc rezervdən çox ola bilər: həqiqət olaraq yazılır, `reconcile()` bunu bildirir (əvvəlcədən qarşısı alınmır).

- **Məhdudiyyət (mərhələ 7):** `agents/verify.py` test tapşırığı üçün `runner`-i birbaşa çağırır (broker-siz). Yalnız mock/sandbox runner ilə istifadə olunmalıdır; real alətlər `@broker_only` olduğundan orada işləməz. Statik test bu istisnanı sabitləyir.
- Təkrar `op_id` cəhdləri yeni audit sətri yaratmır (köhnə testlə uyğunluq); rədd qərarları (stop, büdcə, Gate) audit-ə yazılır.

- **Mərhələ 8 məhdudiyyətləri:** real kanal adapterləri (Web/Telegram/Android) və istifadəçi autentifikasiyası hələ yoxdur — yalnız interfeys və mock ilə test; `human` principal-ın həqiqiliyinə nüvə yox, adapter cavabdehdir. Rol ayrılığı proses daxilidir: agent kodu DB/fayllara çıxış alsa təsdiqi saxtalaşdıra bilər, ona görə sandbox (mərhələ 9) məcburidir. Payload `approvals.db`-də açıq saxlanır (sirr olmamalıdır). Eski `approver` callable (kimliksiz) geriyə uyğunluq üçün qalıb, real istifadədə `approvals` istifadə edin. Pending sorğu büdcəni əvvəlcədən yoxlamır. `CEO.run_workflow` təsdiqdən sonra davam etdirmə hələ yoxdur: təsdiqdən sonra eyni `job_id` ilə təkrar işə salanda tamamlanmış addımlar `duplicate` olur və asılıları keçilir; hələlik `Orchestrator.execute` ilə təkrar göndərin.

## Növbəti addımlar (təsdiq gözləyir)
- LLM əsaslı Planner (interfeys hazırdır, real API təsdiq tələb edir)
- Asılılıq çıxışlarının növbəti addıma ötürülməsi, retry gözləməsi (backoff)
- Naməlum xərcli rezervlərin avtomatik həlli (vaxt aşımı / hesabat)
- Real icra mühiti (sandbox) və LLM interfeysi

## Qeyd
Bu bölmədə gəlir/qazanc haqqında iddia yazılmır.
