# NAYMERA

Agent sistemi: bir **CEO agent** və onun idarə etdiyi **şöbə agentləri**. Hədəf məhsul (Android + Web +
Telegram, səs, internet/brauzer, sayt/proqram yaratma, virtual şirkət, trading) üçün plan:
[ROADMAP.md](ROADMAP.md).

> Status: mərhələ 1–5 hazırdır (180 test, CI yaşıl). Mövcud olan idarəetmə nüvəsidir: agent təsviri
> və reyestri, CEO və orkestrator, təhlükəsizlik qapısı, davamlı xərc kitabı. **Hələ real LLM, alət,
> brauzer, istifadəçi interfeysi, səs və trading yoxdur** — icra `runner` mock-udur. Mərhələ 6
> (asılılıq və retry) ayrıca PR-dadır. Cari vəziyyət: [STATUS.md](STATUS.md).

## Stack

- Python 3.12
- pytest
- SQLite
- PyYAML (`requirements.txt`)
- GitHub Actions (CI)

## Quraşdırma

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install pytest -r requirements.txt
python -m pytest -q
```

## Qovluqlar

- `agents/` — spec, factory, verify, registry, orchestrator, ceo
- `safety/` — gate (icazə, təsdiq, audit, stop), ledger (xərc kitabı)
- `tests/` — pytest testləri
- `.claude/skills/` — `test-gate`, `safety-layer`

## Əsas qaydalar

- Pul, xarici hesab və nəşr (publish) yalnız sahibin açıq təsdiqi ilə.
- API açarları yalnız mühit dəyişənlərində; `.env` git-ə düşmür.
- Gəlir haqqında heç bir iddia edilmir.

Ətraflı: [CLAUDE.md](CLAUDE.md), [ROADMAP.md](ROADMAP.md).
