# NAYMERA

Agent sistemi: bir **CEO agent** və onun idarə etdiyi **şöbə agentləri**.

> Status: qurulum mərhələsi. Hələ iş görən agent kodu yoxdur — yalnız layihə
> iskeleti, qaydalar və CI var. Cari vəziyyət: [STATUS.md](STATUS.md).

## Stack

- Python 3.12
- pytest
- SQLite
- GitHub Actions (CI)

## Quraşdırma

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install pytest
pytest
```

## Əsas qaydalar

- Pul, xarici hesab və nəşr (publish) yalnız sahibin açıq təsdiqi ilə.
- API açarları yalnız mühit dəyişənlərində; `.env` git-ə düşmür.
- Gəlir haqqında heç bir iddia edilmir.

Ətraflı: [CLAUDE.md](CLAUDE.md) və `.claude/skills/`.
