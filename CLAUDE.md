# CLAUDE.md — NAYMERA

Agent sistemi: CEO agent + şöbə agentləri. Stack: Python 3.12, pytest, SQLite.
Mərhələ 1-4 hazırdır: agent yaratma, təhlükəsizlik qapısı, reyestr, orkestrator, CEO agent, davamlı xərc kitabı (bax: STATUS.md).

## Mütləq qaydalar

1. **Pul, xarici hesab, nəşr — yalnız sahibin təsdiqi ilə.**
   - Pul: ödəniş, abunə, reklam büdcəsi, hər hansı xərc.
   - Xarici hesab: hesab açmaq, daxil olmaq, API/servis qeydiyyatı.
   - Nəşr: sosial şəbəkə, sayt, paket reyestri, e-poçt göndərişi, PR/release.
   - Təsdiq gəlməyibsə dayan və soruş. Əvvəlki təsdiq yeni hadisəyə şamil olmur.
2. **API açarları yalnız mühit dəyişənlərində.** Kodda, testdə, loqda, commit
   mesajında, fixture-də açar olmaz. `.env` `.gitignore`-dadır; yalnız
   `.env.example` (boş dəyərlərlə) commit edilə bilər.
3. **Gəlir haqqında iddia yoxdur.** Heç bir sənəddə, koddakı mətndə, agent
   cavabında "qazanc", "gəlir", "pul qazandırır" tipli vədlər/rəqəmlər yazma.
4. **"Hazırdır" deməzdən əvvəl** `test-gate` skill-ini işlət; real pytest
   nəticəsini göstər. Çalışdırmadığını "keçdi" kimi təqdim etmə.
5. Təhlükəli/geri qaytarılmaz əməliyyatdan əvvəl `safety-layer` skill-ini işlət.
6. Soruşulmayan kod yazma: tapşırığın çərçivəsindən kənara çıxma.

## Əmrlər

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install pytest -r requirements.txt
pytest                 # bütün testlər
pytest -q tests/test_x.py::test_name   # tək test
```

CI: `.github/workflows/ci.yml` (hər push və PR-də `pytest`).

## Qovluq xəritəsi

```
.
├── CLAUDE.md                  # bu fayl: qaydalar, əmrlər, xəritə
├── README.md                  # layihə təsviri
├── STATUS.md                  # cari vəziyyət (hər əhəmiyyətli işdən sonra yenilə)
├── requirements.txt           # iş vaxtı asılılıqları (pyyaml)
├── pytest.ini                 # testpaths, pythonpath
├── .gitignore                 # .env, .venv, logs/, *.db və s.
├── .github/workflows/ci.yml   # GitHub Actions: pytest
├── .claude/skills/
│   ├── test-gate/SKILL.md     # "hazırdır" qapısı
│   └── safety-layer/SKILL.md  # pul/hesab/nəşr/sirr təhlükəsizlik qatı
├── agents/
│   ├── spec.py                # AgentSpec (YAML) + doğrulama
│   ├── factory.py             # məqsəddən təsvir (LLM interfeysi, MockLLM)
│   ├── verify.py              # test tapşırığı ilə yoxlama -> logs/errors.jsonl
│   ├── registry.py            # SQLite agent reyestri (data/naymera.db)
│   ├── orchestrator.py        # şöbəyə yönləndirmə; icra yalnız gate ilə
│   └── ceo.py                 # CEO: plan, bölgü, toplama, yoxlama, hesabat
├── safety/gate.py             # icazə, xərc limiti, təsdiq, audit, stop
├── safety/ledger.py           # SQLite xərc kitabı (data/ledger.db) + audit uyğunluğu
└── tests/                     # pytest testləri (spec/factory/verify/gate/registry/orchestrator/ceo/ledger/cost_integration)
```

Yeni kod əlavə olunanda bu xəritəni yenilə.

## Üslub

- Python 3.12, tip göstəriciləri, sadə və oxunaqlı kod.
- SQLite: `sqlite3` standart kitabxanası; sorğularda həmişə parametr (`?`),
  string birləşdirmə yox.
- Testlər şəbəkəyə və real xarici servislərə çıxmır; SQLite üçün `:memory:`.
- Kiçik, mərkəzləşmiş dəyişikliklər; hər dəyişiklikdən sonra `pytest`.

## Git

- İnkişaf branch-ında işlə; `main`-ə birbaşa push yoxdur.
- PR yalnız sahib istəyəndə açılır.
- Commit mesajında sirr, şəxsi məlumat və gəlir iddiası olmaz.
