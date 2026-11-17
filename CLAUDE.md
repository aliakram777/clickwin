# CLAUDE.md — NAYMERA

Agent sistemi: CEO agent + şöbə agentləri. Stack: Python 3.12, pytest, SQLite.
Mərhələ 1-10 hazırdır (7: Effect Broker; 8: təsdiq; 9: Tool Runtime; 10: LLM adapter — yalnız interfeys və mock): agent yaratma, təhlükəsizlik qapısı, reyestr, orkestrator, CEO agent, xərc kitabı, büdcə siyasəti, asılılıq qrafı və retry (bax: STATUS.md).

## Mütləq qaydalar

1. **Pul, xarici hesab, nəşr — yalnız sahibin təsdiqi ilə.**
   - Pul: ödəniş, abunə, reklam büdcəsi, hər hansı xərc.
   - Xarici hesab: hesab açmaq, daxil olmaq, API/servis qeydiyyatı.
   - Nəşr: sosial şəbəkə, sayt, paket reyestri, e-poçt göndərişi, PR/release.
   - Təsdiq gəlməyibsə dayan və soruş. Əvvəlki təsdiq yeni hadisəyə şamil olmur.
2. **API açarları yalnız mühit dəyişənlərində** (LLM üçün `agents.llm.load_config`, `Secret` ilə; loqda göstərilmir).
   Model cavabı etibarsız giriş sayılır: LLM yalnız plan/təklif yaradır, icra qərarını CEO/Broker/Gate/təsdiq verir. Kodda, testdə, loqda, commit
   mesajında, fixture-də açar olmaz. `.env` `.gitignore`-dadır; yalnız
   `.env.example` (boş dəyərlərlə) commit edilə bilər.
3. **Gəlir haqqında iddia yoxdur.** Heç bir sənəddə, koddakı mətndə, agent
   cavabında "qazanc", "gəlir", "pul qazandırır" tipli vədlər/rəqəmlər yazma.
4. **"Hazırdır" deməzdən əvvəl** `test-gate` skill-ini işlət; real pytest
   nəticəsini göstər. Çalışdırmadığını "keçdi" kimi təqdim etmə.
5. Təhlükəli/geri qaytarılmaz əməliyyatdan əvvəl `safety-layer` skill-ini işlət.
   Yeni yan təsirli kod (alət, agent icrası) yalnız `EffectBroker.run` üzərindən işləyir; alətləri `@broker_only` ilə bəzə.
   Alətlər yalnız `ToolRuntime` ilə (qeydiyyatlı allowlist, `@tool_handler`); shell/şəbəkə/sistem/env alətləri defoltda bağlıdır və yalnız təsdiqlənmiş OS sandbox adapteri ilə mümkündür. In-process məhdudiyyətləri OS sandbox kimi təqdim etmə.
   Həssas effektlər `ApprovalService` ilə təsdiqlənir: agentlər/broker `ApprovalGateway`-ə (approve/deny) çıxış almır; testdəki mock təsdiqi (`kind="mock"`) real istifadəçi təsdiqi kimi təqdim olunmur.
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
│   ├── orchestrator.py        # şöbəyə yönləndirmə; runner yalnız broker-in icra mərhələsində çağırılır
│   ├── workflow.py            # tapşırıq DAG-ı: doğrulama (dövr, olmayan asılılıq), sıra
│   ├── llm.py                 # LLMProvider interfeysi, Secret/konfiqurasiya (env), GuardedLLM limitləri, MockProvider
│   ├── llm_planner.py         # LLM yalnız TƏKLİF: plan və agent təsviri (ciddi doğrulama); icra CEO/Broker ilə
│   └── ceo.py                 # CEO: plan, bölgü, toplama, yoxlama, hesabat, run_workflow + retry
├── safety/gate.py             # icazə, xərc limiti, təsdiq, audit, stop
├── safety/broker.py           # Effect Broker: stop -> təkrar -> rezerv -> Gate -> icra -> hesablaşma; @broker_only
├── safety/approvals.py        # təsdiq xidməti: hash-ə bağlı, vaxtlı, tək istifadəlik; Client (broker) / Gateway (kanallar)
├── safety/tools.py            # Tool Runtime: allowlist, limitlər, SandboxAdapter interfeysi (real OS sandbox YOXDUR)
├── safety/workspace.py        # workspace fayl sərhədi (tətbiq səviyyəli) + fs.read/fs.write/fs.list
├── safety/ledger.py           # SQLite xərc kitabı: rezerv/təsdiq/azad, limitlər (data/ledger.db)
└── tests/                     # pytest testləri (spec/factory/verify/gate/registry/orchestrator/ceo/ledger/cost_integration/ledger_budget/budget_integration/workflow/retry/broker/approvals/approval_broker/workspace/tool_runtime/llm/llm_planner)
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
