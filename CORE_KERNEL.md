# CORE_KERNEL — Mərhələ 18.2-B (Goal Store + Kernel Loop, yalnız L0/L1)

NAYMERA CORE-un ilk icra bünövrəsi: `kernel.Goal`/`kernel.Plan` üçün davamlı saxlanc və **tək addımlıq, təsdiq-nəzarətli döngü**. Heç bir yan təsir birbaşa icra olunmur; hər icra Effect Broker → Gate → Ledger + mövcud `ApprovalClient` zəncirindən keçir.

**Vəziyyət (dürüst):** real handler yoxdur (istehsal reyestri boşdur), real model/API/şəbəkə/agent kodu icrası yoxdur, qat `build_app`/`runtime/env.py`-ə qoşulmayıb. Yalnız fixture handler-lərlə sınanıb.

## 1. Avtonomluq
| Səviyyə | Davranış |
|---|---|
| **L0** | təklif: plan yaradır, doğrulayır, READY edir, dayanır. Konstruktor L0-a icraçı/təsdiq/yoxlayıcı portu verilməsinə İCAZƏ VERMİR |
| **L1** | hər addım üçün sahibin ayrıca, hash-ə bağlı, bir-dəfəlik təsdiqi; icra yalnız Broker adapteri ilə |
| L2 / L3 | tipdə MÖVCUD DEYİL (`Autonomy("L2")` ValueError). L2 gələcək hədəf kimi qeyd olunur, aktiv deyil; L3 yol xəritəsində yoxdur |

## 2. Komponentlər
| Fayl | Rolu |
|---|---|
| `core/ports.py` | `Autonomy`, `StepState`, `StepRequest`, portlar (`Planner`, `StepExecutor`, `ApprovalPort`, `Verifier`), `FileStop` |
| `core/store.py` | SQLite `GoalStore`: məqsəd/plan/addım/jurnal/outbox; iki üz (`EngineGoalStore`, `OwnerGoalStore`) |
| `core/validator.py` | plan doğrulayıcısı (həssas icazə, ödənişli addım, capability, addım sayı) |
| `core/loop.py` | `KernelLoop.tick()/run()` |
| `runtime/brokered_core.py` | `StepHandlerRegistry`, `BrokeredApprovals`, `BrokeredStepExecutor` (Broker + `ApprovalClient`) |
| `tools/mutation_check_core.py` | mutasiya yoxlaması (kopyada işləyir) |

## 3. Goal Store
- Sənəd JSON + hash; oxuma zamanı kernel müqaviləsi ilə yenidən doğrulanır, hash uyğunsuzluğu `StoreIntegrityError`.
- İdempotent yazılar (`op_id`: eyni məzmun = dublikat, fərqli = `OpConflict`), optimistik `expected_version` (`StaleVersion`).
- **İki üz:** mühərrik üzü məqsədi aktivləşdirə/tamamlaya/ləğv edə/davam etdirə, plan yeniləyə və addımı sahib adından həll edə bilmir. Sahib üzü yalnız sahib qapısında saxlanmalıdır. Bu proses-daxili üz ayrılığıdır, **OS sərhədi DEYİL**; sahibin həqiqiliyi (cihaz imzası/step-up) bu saxlancın deyil, sahib qapısının məsuliyyətidir (`approval_ref` yalnız istinaddır).
- Addım vəziyyətləri: `pending → awaiting_approval → running → succeeded | failed | blocked | unknown`. `begin_step` atomik CAS + lease; lease bitmiş `running` ⇒ `unknown` (avtomatik təkrar YOXDUR; yan təsir baş vermiş ola bilər). `unknown`-un gec nəticəsi yazılmır. Həlli yalnız sahib (`resolve_step`).
- Hadisələr: goal/plan dəyişiklikləri `outbox`-a eyni tranzaksiyada yazılır və `flush_outbox(EventLog)` ilə (idempotent) kernel jurnalına köçürülür. Addım/döngü hadisələri append-only `journal` cədvəlindədir (UPDATE/DELETE trigger ilə bloklanır). Jurnalın hash zənciri və xarici anker YOXDUR (18.2-C).

## 4. Kernel Loop (hər tick)
`STOP?` → lease bərpası → ACTIVE məqsədlər (prioritet, yaranma vaxtı) → plan yoxdursa **təklif** (planlaşdırıcı etibarsızdır) → DRAFT plan **doğrulanır** (READY sahib təsdiqi deyil) → L0 burada dayanır → L1: növbəti uyğun addım (asılılıqlar uğurlu) → icra oluna bilməyən addım üçün təsdiq İSTƏNMİR, blok → təsdiq sorğusu → təsdiqlənibsə **STOP yenidən yoxlanır** → `begin_step` → icra → (ixtiyari) `Verifier` → nəticə yazılır.
- **STOP üstündür:** tick-in əvvəlində, planlaşdırıcıya müraciətdən əvvəl və sonra, doğrulamada, təsdiq sorğusundan əvvəl və icradan dərhal əvvəl; Broker də öz stop bayrağını ayrıca yoxlayır. STOP-da təsdiqlənmiş addım icra olunmur və təsdiq yanmır.
- **Heç nə avtomatik «tamamlandı» olmur:** plan yalnız hər addım uğurlu VƏ yoxlanmış olduqda COMPLETED olur; məqsədi **yalnız sahib** tamamlayır.
- Uğursuz/blok addım ⇒ məqsəd BLOCKED (mühərrik blokdan çıxara bilmir). `Exception` atan icraçı ⇒ UNKNOWN; `BaseException` yayılır, addım RUNNING qalır.
- `run(max_ticks)` məhduddur (≤1000), tərəqqi dayananda bitir.

## 5. Təsdiq bağlılığı (L1)
Təsdiq effektin kanonik hash-inə bağlıdır: agent, icazə, xərc=0, `op_id`, təsvir, **məqsəd/plan/versiya/plan hash/addım hash/capability/cəhd**. Plan və ya addım dəyişərsə təsdiq etibarsızdır; hər cəhd yeni `op_id` və yeni təsdiq tələb edir. Təsdiq yalnız Broker-in stop/təkrar/Gate/ledger mərhələləri keçildikdən SONRA icraçının daxilində `consume` olunur (bir-dəfəlik). Yanlış hash ilə istifadə cəhdi təsdiqi ləğv edir (mövcud Stage 17 davranışı). Defoltda yalnız `kind="human"` təsdiqi qəbul olunur; `mock` yalnız testdə açıq `accept_mock=True` ilə.

## 6. Qaydalar və qadağalar
- Həssas icazələr (`spend_money`, `external_account`, `publish`) və ödənişli addımlar (`cost_estimate_azn` > 0) bu mərhələdə BAĞLIDIR (plan rədd edilir).
- Handler yalnız etibarlı server kodu tərəfindən qeydə alınır (`read_files`/`write_files`/`run_tests`; `seal()` sonrası dəyişmir). İstehsalda heç bir handler qeydə alınmır.
- İcraçı çıxışı ETİBARSIZDIR: yalnız ≤500 simvol xülasə, sirr aşkarlanarsa redaksiya, heç vaxt təlimat kimi ayrışdırılmır.
- `core/` `safety`/`runtime`/`api`/`agents`, şəbəkə, mühit, model, agent kodu və `run_session` istifadə etmir (AST testi). `approve`/`deny` adlı atribut yoxdur.

## 7. Məhdudiyyətlər (açıq)
- Üz ayrılığı və Broker konteksti proses-daxilidir, OS sərhədi deyil.
- Broker xətasının növü `EffectResult.reason` prefiksindən oxunur (Broker dəyişdirilmir).
- Uğursuz təsdiq istifadəsi Broker-də `op_id`-ni «istifadə olunmuş» qoyur; yeni cəhd yeni `op_id` ilə olur.
- `unknown` addımlar sahib həllini gözləyir; naməlum ledger rezervləri mövcud `resolve_unknown` ilə həll olunur.
- Audit zənciri, Memory v2, qiymətləndirmə, scheduler/watchdog, real lokal runtime, OS sandbox: sonrakı mərhələlər (18.2-C, 18.3, 18.4, 18.8, 18.6, 18.9).
- Root/privileged testlər bu mərhələdə işlədilməyib (qərar: yalnız ayrıca təsdiqlənmiş təcrid olunmuş mühitdə).

## 8. Testlər
`tests/test_core_store.py`, `test_core_loop.py`, `test_core_brokered.py`, `test_core_audit.py`; mutasiya: `python tools/mutation_check_core.py` (sağ qalan mutant REAL BOŞLUQ sayılır).
