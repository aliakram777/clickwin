# STATUS

Yenilənib: 2026-11-17

## Cari mərhələ
Mərhələ 3 — CEO agent (tamamlandı, PR təsdiq gözləyir). Əvvəlki PR-lər: #1 (mərhələ 1), #2 (mərhələ 2).

## Hazırdır
- Qurulum: README, CLAUDE.md, .gitignore, skill-lər (`test-gate`, `safety-layer`), CI
- `agents/spec.py` — AgentSpec (YAML: ad, məqsəd, alətlər, icazələr, giriş, çıxış, xərc_limiti) + doğrulama
- `agents/factory.py` — məqsəd mətnindən təsvir; LLM interfeysi (Protocol), hələlik `MockLLM`
- `agents/verify.py` — test tapşırığı ilə yoxlama; uğursuzluq `logs/errors.jsonl`-ə
- `safety/gate.py` — icazə siyahısı, xərc limiti, təsdiq qapısı (pul/xarici hesab/nəşr təsdiqsiz bloklanır), audit `logs/audit.jsonl`, stop bayrağı
- `agents/registry.py` — SQLite-da agent təsvirləri + şöbə: əlavə et, oxu, yenilə, siyahı
- `agents/orchestrator.py` — tapşırığı şöbə agentinə yönləndirir; hər icra `Gate` yoxlamasından keçir (icazə, limit, təsdiq, stop); xəta `logs/errors.jsonl`-ə
- `agents/ceo.py` — CEO: məqsədi `Planner` ilə təhlil edir (hələlik deterministik `RulePlanner`), şöbə agentlərinin girişlərini doldurur, tapşırıqları bölür, nəticələri toplayıb şöbə üzrə yoxlayır (`checks`), uğursuz agent hesabatı (`JobReport.failures/summary()`). Plan büdcəni aşırsa heç nə icra olunmur; stop bayrağı qalan addımları dayandırır. Hər icra Orchestrator -> Gate-dən keçir.
- Testlər: `tests/test_{spec,factory,verify,gate,registry,orchestrator,ceo}.py` (96 test)

## Məhdudiyyətlər
- Real LLM çağırışı yoxdur (yalnız mock); real API açarı və xarici çağırış istifadə olunmayıb.
- `runner` funksiyadır (mock); real agent icra mühiti və LLM hələ yoxdur.
- CEO təhlili açar-söz qaydalarıdır (LLM yoxdur); addımlar bir-birindən asılı deyil, təkrar cəhd yoxdur.
- Xərc sayğacı orkestrator yaddaşındadır (proses yenidən başlayanda sıfırlanır).

## Növbəti addımlar (təsdiq gözləyir)
- LLM əsaslı Planner (interfeys hazırdır, real API təsdiq tələb edir)
- Addımlar arası asılılıq və uğursuz addımın təkrarı
- Xərc sayğacının SQLite-da saxlanması
- Real icra mühiti (sandbox) və LLM interfeysi

## Qeyd
Bu bölmədə gəlir/qazanc haqqında iddia yazılmır.
