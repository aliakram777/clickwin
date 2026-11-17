# STATUS

Yenilənib: 2026-11-17

## Cari mərhələ
Mərhələ 2 — reyestr və orkestrator (tamamlandı, PR təsdiq gözləyir). Mərhələ 1: PR #1.

## Hazırdır
- Qurulum: README, CLAUDE.md, .gitignore, skill-lər (`test-gate`, `safety-layer`), CI
- `agents/spec.py` — AgentSpec (YAML: ad, məqsəd, alətlər, icazələr, giriş, çıxış, xərc_limiti) + doğrulama
- `agents/factory.py` — məqsəd mətnindən təsvir; LLM interfeysi (Protocol), hələlik `MockLLM`
- `agents/verify.py` — test tapşırığı ilə yoxlama; uğursuzluq `logs/errors.jsonl`-ə
- `safety/gate.py` — icazə siyahısı, xərc limiti, təsdiq qapısı (pul/xarici hesab/nəşr təsdiqsiz bloklanır), audit `logs/audit.jsonl`, stop bayrağı
- `agents/registry.py` — SQLite-da agent təsvirləri + şöbə: əlavə et, oxu, yenilə, siyahı
- `agents/orchestrator.py` — tapşırığı şöbə agentinə yönləndirir; hər icra `Gate` yoxlamasından keçir (icazə, limit, təsdiq, stop); xəta `logs/errors.jsonl`-ə
- Testlər: `tests/test_{spec,factory,verify,gate,registry,orchestrator}.py` (80 test)

## Məhdudiyyətlər
- Real LLM çağırışı yoxdur (yalnız mock); real API açarı və xarici çağırış istifadə olunmayıb.
- `runner` funksiyadır (mock); real agent icra mühiti və LLM hələ yoxdur.
- Yönləndirmə sadədir: şöbə + giriş uyğunluğu + icazə; CEO agent hələ yoxdur.
- Xərc sayğacı orkestrator yaddaşındadır (proses yenidən başlayanda sıfırlanır).

## Növbəti addımlar (təsdiq gözləyir)
- CEO agent (şöbələr arası yönləndirmə, planlama)
- Xərc sayğacının SQLite-da saxlanması
- Real icra mühiti (sandbox) və LLM interfeysi

## Qeyd
Bu bölmədə gəlir/qazanc haqqında iddia yazılmır.
