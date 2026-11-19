# OPS_API — Mərhələ 18.8 (Operations) və 18.11 (Android API: CORE)

**Vəziyyət:** işləyən kitabxana/API qatı, smoke/regressiya yoxlanıb. İstehsal yığınına (`build_app`, `tools/serve.py`, `runtime/env.py`) və ictimai şəbəkəyə QOŞULMAYIB; heç bir real API/şəbəkə/ödəniş/kod icrası yoxdur.

## 18.8 Operations (`ops/`, `core/lease.py`)
- **Lease heartbeat** (`core.LeaseKeeper`, `KernelLoop(heartbeat=True)`): icra müddətində lease yenilənir; yalnız RUNNING + eyni `op_id`. UNKNOWN-a keçmiş addımın lease-i yenilənmir.
- **Hash-zəncirli audit** (`ops.AuditChain`, `AuditMirror`): append-only (trigger + hash zənciri), `verify`, `anchor`/`verify_anchor`, sirr redaksiyası; `AuditMirror` Kernel Loop jurnalını davamlı kursorla köçürür.
  HƏDD: DB-yə yazma imkanı olan tərəf zənciri yenidən hesablaya bilər; tam qorunma ankerin sistemdən kənarda (sahib cihazı) saxlanmasını tələb edir. Xarici anker imzalama hələ yoxdur.
- **Scheduler** (`ops.Scheduler`): yalnız qeydli iş adları (`loop.tick`, `memory.purge`, `audit.mirror`, `watchdog.check`, `backup.run` kimi; reyestri server kodu verir), interval işlər, STOP üstündür, yığılma yoxdur, ardıcıl uğursuzluqda avto-dayandırma (sahib aktiv edir), ≤10 iş/çağırış.
- **Watchdog/bərpa** (`ops.Watchdog`): heartbeat, sağlamlıq hesabatı (yalnız saylar/bayraqlar), `recover()` (bitmiş lease ⇒ UNKNOWN, outbox flush, audit qeydi). Heç bir addımı yenidən İCRA ETMİR.
- **Ehtiyat nüsxə** (`ops.backup/verify_backup/restore`): sqlite3 backup API, manifest (sha256 + integrity_check), retention, 0700/0600, hədəfdə yazma, overwrite yalnız açıq. HƏDD: nüsxələr şifrələnmir; xarici saxlama yoxdur.

## 18.11 Android API (`api/core_api.py`, `CoreApiApp`)
`ApiApp` dəyişməyib; alt sinif `/v1/core/*` əlavə edir (hamısı sessiya tələb edir):
| Marşrut | Rolu |
|---|---|
| `GET /v1/core/status` | autonomy (L0/L1), STOP, addım sayları |
| `GET/POST /v1/core/goals` | siyahı; yaratma (yalnız DRAFT, `client_op` ilə idempotent, sirr 422) |
| `GET /v1/core/goals/{id}` | məqsəd + cari plan + addım vəziyyətləri (çıxış `untrusted_output`) |
| `POST /v1/core/goals/{id}/challenge` + `/transition` | vəziyyət dəyişməsi: **step-up** (cihaz imzası, CARİ versiyaya bağlı, bir-dəfəlik, səs yox) |
| `GET /v1/core/steps/awaiting` | təsdiq gözləyən addımlar (təsdiq özü mövcud `/v1/approvals` step-up yolu ilədir) |
| `POST /v1/core/steps/{plan}/{step}/challenge` + `/resolve` | UNKNOWN/BLOCKED/FAILED addımın sahib həlli (step-up) |
| `GET /v1/core/journal` | jurnal (etibarsız, qısaldılmış) |
Şəbəkə ifşası: sinif heç bir soket açmır və istehsal yığınına qoşulmayıb; server qatı (`api/server.py`) dəyişməyib.

## Məhdudiyyətlər
Kernel/Android bildiriş inteqrasiyası (push) yoxdur; Kotlin tərəfi bu kontraktı hələ istifadə etmir (APK build mühiti yoxdur); `PlanStepSpec` arqument daşımır; audit ankeri sahib tərəfindən saxlanmalıdır; scheduler işləri eyni ipdə işləyir (vaxt aşımı məcbur edilmir).
