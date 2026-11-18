# MEMORY_REASONING — Mərhələ 18.3 (Memory Engine v2) və 18.4 (Reasoning/Planning scaffolds)

**Vəziyyət:** işləyən kitabxana qatı; heç nə icra etmir, şəbəkə/model/env istifadə etmir; istehsala qoşulmayıb. Smoke/regressiya testləri keçir; geniş test kampaniyası final mərhələyə saxlanılıb.

## 18.3 Memory Engine v2 (`memory/`)
- Dörd növ: `short_term` (TTL ≤7 gün), `long_term`, `episodic`, `project`. SQLite + FTS5 (diakritik qatlama: ö≈o, ü≈u, ç≈c, ş≈s; ə ayrıca), sorğu escape olunur, FTS5 yoxdursa LIKE ehtiyatı.
- Yazma səlahiyyəti aktora görə: OWNER/SYSTEM hər növ; ENGINE (nüvə) yalnız epizodik; AGENT yalnız short_term/project (mənşə agent, etibar low); **LLM yazmır**. Silmə yalnız sahib. Mənşə, etibar, mənbə, confidence, refs saxlanır.
- Sirr/idarəedici simvol rədd; dublikat və `op_id` idempotentliyi; `render_context` yaddaşı escape olunmuş ETİBARSIZ bloklarla verir (təlimat deyil).
- Epizodlar: `EpisodeRecorder` `KernelLoop(observer=...)` ilə hər tərəqqi tick-ini yazır (icraçı çıxışı yazılmır). `consolidate_episodes` deterministik xülasə (model yox) yaradır, orijinallar qalır.

## 18.4 Reasoning/Planning (`reasoning/`)
- `TemplatePlanner` (modelsiz, deterministik, açar sözlə), `ModelPlanner` (inyeksiya olunan `generate`; ciddi JSON sxemi, yalnız icazəli capability-lər, xəta ⇒ plan yox; məqsəd və yaddaş escape olunmuş etibarsız bloklarda).
- `Critic` + `GuardedPlanner` (bloklayan problem ⇒ plan yox), `CriteriaVerifier` (`nonempty`, `contains:`, `notcontains:`, `maxlen:`, `status:succeeded`; maşınla yoxlanmayan meyar ⇒ avtomatik «verified» YOXDUR).
- Hamısı `core.Planner`/`core.Verifier` portlarıdır: plan yenə `PlanValidator`-dan keçir, icra L1 təsdiqi ilə.

## Məhdudiyyətlər
Semantik (embedding) axtarış yoxdur; köhnə `agents/memory.MemoryService` ilə miqrasiya yoxdur; `ModelPlanner` real modelə qoşulmayıb (18.1 qatı/18.6 lokal runtime ilə birləşəcək); şablonlar sahib tərəfindən yazılmalıdır;
meyar formatı sadədir (mürəkkəb yoxlama üçün yoxlayıcı portu genişləndirilməlidir); konsolidasiya yalnız say xülasəsidir.

## 18.5 Təhlükəsiz handler kitabxanası (`runtime/core_handlers.py`)
`register_safe_handlers(registry, goals=..., memory=..., workspace=...)` yalnız dar, deterministik handler-ləri qeydə alır: `memory.recall`, `memory.consolidate`, `report.progress`, `workspace.list`.
Model/şəbəkə/shell/ödəniş/nəşr/kod icrası YOXDUR; hər biri yenə Broker + L1 təsdiqi + agent icazəsi (`read_files`/`write_files`) ilə işləyir. Resurs verilməyibsə uyğun handler qeydə alınmır.
Məhdudiyyət: `PlanStepSpec` arqument daşımır (18.0 müqaviləsi), ona görə handler-lər məqsəd/plan kontekstindən işləyir; parametrli handler-lər müqavilənin genişləndirilməsini tələb edir.
