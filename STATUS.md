# STATUS

Yenilənib: 2026-11-17

## Cari mərhələ
Mərhələ 1 — agent yaratma modulu (tamamlandı, PR təsdiq gözləyir).

## Hazırdır
- Qurulum: README, CLAUDE.md, .gitignore, skill-lər (`test-gate`, `safety-layer`), CI
- `agents/spec.py` — AgentSpec (YAML: ad, məqsəd, alətlər, icazələr, giriş, çıxış, xərc_limiti) + doğrulama
- `agents/factory.py` — məqsəd mətnindən təsvir; LLM interfeysi (Protocol), hələlik `MockLLM`
- `agents/verify.py` — test tapşırığı ilə yoxlama; uğursuzluq `logs/errors.jsonl`-ə
- `safety/gate.py` — icazə siyahısı, xərc limiti, təsdiq qapısı (pul/xarici hesab/nəşr təsdiqsiz bloklanır), audit `logs/audit.jsonl`, stop bayrağı
- Testlər: `tests/test_spec.py`, `test_factory.py`, `test_verify.py`, `test_gate.py`

## Məhdudiyyətlər
- Real LLM çağırışı yoxdur (yalnız mock); real API açarı və xarici çağırış istifadə olunmayıb.
- `verify` agenti `runner` funksiyası ilə işlədir; real agent icra mühiti hələ yoxdur.
- Gate hələ agent icrasına qoşulmayıb (ayrıca modul).

## Növbəti addımlar (təsdiq gözləyir)
- CEO agentin və şöbə agentlərinin dizaynı
- SQLite sxemi
- Gate-in agent icrasına qoşulması

## Qeyd
Bu bölmədə gəlir/qazanc haqqında iddia yazılmır.
