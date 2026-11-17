# STATUS

Yenilənib: 2026-11-17

## Cari mərhələ
Mərhələ 5 — ümumi büdcə və xərc hesablaşması (tamamlandı, PR təsdiq gözləyir). Əvvəlki PR-lər: #1–#4.

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
- Testlər: `tests/test_{spec,factory,verify,gate,registry,orchestrator,ceo,ledger,cost_integration,ledger_budget,budget_integration}.py` (180 test)

## Məhdudiyyətlər
- Real LLM çağırışı yoxdur (yalnız mock); real API açarı və xarici çağırış istifadə olunmayıb.
- `runner` funksiyadır (mock); real agent icra mühiti və LLM hələ yoxdur.
- CEO təhlili açar-söz qaydalarıdır (LLM yoxdur); addımlar bir-birindən asılı deyil, təkrar cəhd yoxdur.
- Ledger verilməyibsə xərc sayğacı yaddaşdadır (köhnə davranış); `BudgetPolicy` ledger tələb edir.
- Limit aşımı (büdcə rezervi rədd edəndə) gate-ə çatmır: `audit.jsonl`-də yazılmır, nəticə `blocked` + səbəb kimi qayıdır.
- Naməlum xərcli rezervlər əl ilə (`ledger.commit/release`) həll olunur; avtomatik vaxt aşımı yoxdur.
- Faktiki xərc rezervdən çox ola bilər: həqiqət olaraq yazılır, `reconcile()` bunu bildirir (əvvəlcədən qarşısı alınmır).

## Növbəti addımlar (təsdiq gözləyir)
- LLM əsaslı Planner (interfeys hazırdır, real API təsdiq tələb edir)
- Addımlar arası asılılıq və uğursuz addımın təkrarı
- Naməlum xərcli rezervlərin avtomatik həlli (vaxt aşımı / hesabat)
- Real icra mühiti (sandbox) və LLM interfeysi

## Qeyd
Bu bölmədə gəlir/qazanc haqqında iddia yazılmır.
