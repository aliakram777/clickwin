# KERNEL_CONTRACTS — Mərhələ 18.0 (Core Contracts)

`kernel/` NAYMERA Master Intelligence Kernel üçün **dəyişməz, versiyalı, doğrulanan məlumat müqavilələridir**. Bu paket heç nə İCRA ETMİR və heç bir İCAZƏ VERMİR
(hər müqavilədə `GRANTS_EXECUTION = False`). İcra qərarını yalnız mövcud Broker/Gate/Ledger/sahib təsdiqi yolu verir (Mərhələ 17); Mərhələ 17 sərhədlərinə toxunulmayıb.

## Müqavilələr
| Modul | Tip | Qısa təsvir |
|---|---|---|
| `kernel/goal.py` | `Goal`, `AcceptanceCriterion` | məqsəd, prioritet, status keçidləri, valideyn, qəbul meyarları, büdcə REFERANSI (`BudgetRef`, təyin etmir) |
| `kernel/plan.py` | `Plan`, `PlanStepSpec` | sıralı addımlar, yalnız əvvəlki addıma asılılıq, gözlənilən nəticə, yoxlama meyarı, versiya (`revise`), həssas icazə ⇒ sahib təsdiqi tələbi |
| `kernel/events.py` | `Event`, `EntityRef`, `EVENT_SCHEMAS` | `event_id`, `event_type`, varlıq, UTC vaxt, `correlation_id`, `op_id`, ciddi növə-xas yük sxemi |
| `kernel/capability.py` | `Capability`, `FieldSpec` | giriş/çıxış müqaviləsi, tələb olunan icazələr (yalnız tələb), risk, əlçatanlıq, sübut |
| `kernel/proposal.py` | `ImprovementProposal`, `RiskAssessment`, `OwnerDecision` | boşluq, təklif, sübut, risk, təsirlənən komponentlər, test meyarları; sahib qərarı AYRI obyekt |
| `kernel/eventlog.py` | `EventLog` | SQLite, yalnız-əlavə, idempotent (`op_id`), kursorlu oxuma, oxuma zamanı hash yoxlaması |
| `kernel/common.py` | köməkçilər | xəta tipləri, `Origin`, `RiskLevel`, vaxt/mətn/id yoxlamaları, kanonik JSON, `render_untrusted`, `Evidence`, `BudgetRef` |

## Qaydalar
- **Dəyişməzlik:** `frozen` dataclass, `tuple` sahələr; keçidlər yeni nüsxə qaytarır (`transition`, `revise`, `with_decision`).
- **Ciddi sxem:** çatışmayan VƏ naməlum sahə rədd; təkrar JSON açarı, NaN/Infinity, 32 KB-dan böyük JSON rədd. `schema_version` (indi 1): gələcək/naməlum versiya `UnsupportedVersion` ilə rədd (fail-closed); köhnə versiyalar üçün `common.MIGRATIONS` reyestri hazırdır (indi boşdur).
- **UTC:** yalnız timezone-aware vaxt qəbul olunur (naive rədd), UTC-yə çevrilir, `…Z` formatında yazılır.
- **Mətn:** NFC, idarəedici/bidi/surrogate simvollar rədd, Azərbaycan hərfləri və emoji qorunur. Sirr bənzər məzmun (`agents.memory.detect_secrets`) rədd edilir; xəta mesajı dəyəri GÖSTƏRMİR.
- **Etibarsız kontekst:** `Origin` (owner/system/agent/llm/external) mənşəni qeyd edir; sərbəst mətn məlumatdır. Modelə/agentə verilərkən yalnız `render_untrusted` (escape olunmuş blok) ilə.
- **Sahib təsdiqi təklif məzmunundan AYRIDIR:** `ImprovementProposal.content_hash` yalnız məzmundan hesablanır (status/təsdiq/vaxt daxil deyil). `OwnerDecision` konkret hash-ə bağlıdır; məzmun dəyişərsə (`revise`) hash dəyişir, status DRAFT-a və təsdiq PENDING-ə qayıdır, köhnə qərar keçmir. Qərarsız `approved/rejected` qurulmur; sənəd dəyişdirilərsə (`hash_mismatch`) yüklənmir. Qərar yalnız «bu məzmuna baxıldı» deməkdir (`SCOPE="proposal_content_only"`): merge/deploy/xərc/icra təsdiqi DEYİL; qərarın həqiqiliyi (cihaz imzası/step-up) sahib qapısının məsuliyyətidir, `approval_ref` yalnız ona istinaddır.
- **Qorunan komponentlər:** `safety/`, `runtime/`, `.github/`, auth/fabrik faylları, `kernel/`-ə toxunan təklifin riski ən azı `high` olmalıdır.
- **Dürüstlük:** `Capability` `AVAILABLE` yalnız sübutla `VERIFIED` ola bilər; simulyasiya/qurulmamış real kimi göstərilmir.
- **Event jurnalı:** eyni `op_id` + eyni məzmun = dublikat (yeni sətir yoxdur); eyni `op_id` + fərqli məzmun = `IdempotencyConflict`; `UPDATE/DELETE` trigger ilə bloklanır; SQLite `safety.dbconn.LockedConnection` ilə, parametrli sorğular.
- **İmport sərhədi:** `kernel/` yalnız stdlib, `agents.spec`, `agents.memory`, `safety.dbconn` import edir (AST testi). Broker/Gate/Ledger/Approvals/Tools/runtime/api, şəbəkə, env, subprocess, `AgentGateway.run_session` YOXDUR.

## Məhdudiyyətlər (dürüst)
- Bu mərhələdə yalnız MÜQAVİLƏLƏR və jurnal var: Goal Store, planlaşdırıcı, kernel döngüsü, provayder qatı, Gap Analyzer və s. sonrakı mərhələlərdir (18.1+).
- Jurnal iplik-təhlükəsizdir, lakin çox-proses yarışı üçün yalnız SQLite kilidlərinə və `UNIQUE` məhdudiyyətlərinə arxalanır; tam sınaq aparılmayıb. Jurnaldakı hash yoxlaması dəyişdirməni AŞKAR edir, qarşısını almır (fayl sahibi trigger-i silə bilər).
- Mənfi pul dəyərləri rədd edilir, lakin `BudgetRef` büdcə TƏYİN etmir: səlahiyyət `CostLedger`-dədir.
- `OwnerDecision`-un kriptoqrafik doğrulaması bu mərhələdə YOXDUR (yalnız formal istinad).
- APK/real provayder/real cihaz ilə əlaqə yoxdur; heç biri sınanmayıb.
