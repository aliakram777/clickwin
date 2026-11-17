# Agent fabrikası, çoxagentli axın və real AI runtime (Mərhələ 17)

NAYMERA ixtisaslaşmış agentlər yarada, idarə edə və komanda kimi işlədə bilər. Hamısı mövcud təhlükəsizlik qatlarının **üzərindədir** (Company → Orchestrator → Effect Broker → Gate / Approval / Ledger); onları keçmir.

## Rollar (`agents/agent_roles.py`)
Proqramçı, Veb dizayner, Android developer, Video creator, Araşdırmaçı, Test mühəndisi, Data analyst, Layihə meneceri. Hər şablon: ixtisas, vəzifələr, **sistem təlimatı** (etibarsız məlumat və təsdiq qaydaları daxilində), icazə siyasəti, tapşırıq növləri, elan olunan alətlər (**hamısı `not_configured`**), yaddaş siyasəti (Cloud-a ötürmə defolt yox, yazma yalnız `result`/`note`).

## Agent (`agents/agent_factory.py`)
Unikal `agt_…` ID və ad, rol, ixtisas, sistem təlimatı, icazələr, **ayrı iş sahəsi** (`workspaces/<id>/`), büdcə limiti, tapşırıq siyahısı, yaddaş siyasəti, status (`active/stopped/archived/deleted`), hesabat. Hər əməliyyat (qəbul və rədd) audit jurnalındadır; sirr jurnala düşmür.
- **Yaradılış:** NAYMERA (sistem) yalnız **məhdud** agent yaradır: `read_files`, büdcə **0 AZN**, ötürmə yox (`Company.register_baseline_agent`). Sahib şablon daxilində `write_files/run_tests` (və yalnız `web_designer/android_developer/video_creator` üçün `publish`, `sensitive_ok` ilə) verə bilər. Pul və xarici hesab icazəsi heç bir şablonda yoxdur. Limitlər: ümumi/rol üzrə agent sayı, növbə uzunluğu; sahib avtonom yaradılışı söndürə bilər.
- **Agent özünü artıra bilmir:** `AgentHandle` yalnız `submit_result`, `handoff`, `request_permission`, `info` verir. İcazə **sorğusu** yalnız qeyd olunur; həll yalnız sahibdədir (`resolve_request`). Büdcə tavanı (`global_budget_cap`, defolt 0) yalnız sahib tərəfindən qaldırılır.
- **Həyat dövrü:** `stop` (reversible; effektiv icazələr boşaldılır), `restart` (əvvəlki səlahiyyəti bərpa edir, yenisini vermir; sahibin dayandırdığını yalnız sahib), `archive` (geri qayıtmaz), `delete` (yalnız sahib, yalnız arxivlənmişi; audit saxlanılır). Ardıcıl uğursuzluqda (3) agent sistem tərəfindən avtomatik dayandırılır.
- **Tapşırıq:** `assign` — rol növü, tutulan icazə, xərc (agentin `max_task_cost`, büdcə, 0 AZN), ölçü, sirr yoxlanır; `op_id` ilə idempotentdir.
- **Agentlərarası ötürmə:** `Company.handoff` səlahiyyəti (`delegate_to`, tutulan icazə, xərc) + payload yoxlaması: sirr rədd, ölçü limiti, təlimat bənzərliyi **işarələnir**, mətn `<untrusted_agent_output>` blokuna bükülür; dərinlik və dövrə nəzarəti; dayandırılmış agent ötürə bilməz.

## İcra (`agents/agent_runtime.py`)
`AgentDispatcher.execute` tapşırığı **atomik** tutur (təkrar icra yoxdur), `Orchestrator.execute(Task(agent=…))` ilə Broker/Gate/Ledger yoluna verir; həssas icazə (`publish`) Gate-də **sahib təsdiqi** gözləyir (`awaiting_approval`), təsdiqdən sonra EYNİ `op_id` ilə davam edir. `AgentLLMRunner` yalnız **mətn** alır (HybridRouter, AUTO/CLOUD/LOCAL); alət, fayl, şəbəkə, ödəniş, nəşr çağırışı yoxdur. Nəticə yoxlanır (ölçü, sirr, boşluq; təlimat bənzərliyi işarələnir) və agentin hesabatı yazılır. `prefetch` müstəqil tapşırıqların AI mətnini **paralel** alır (yan təsirsiz); icra/qeyd ardıcıldır.

## Çoxagentli axın (`agents/team.py`)
`TeamPlanner` məqsədi mərhələlərə bölür (qaydalar; LLM planı yalnız TƏKLİFdir: ciddi JSON, rol allowlist, DAG, LLM icazə seçə bilməz). `TeamRunner` agentləri seçir/yaradır (məhdud), asılılığı olmayan mərhələləri paralel generasiya edir, asılılıqları ardıcıl icra edir, uğursuz mərhələni eyni roldakı başqa agentə **bir dəfə** ötürür, əvəzedici yoxdursa asılı mərhələləri `skipped` edir, nəticələri yoxlayıb **yekun məhsul** (`FinalProduct`) və CEO üçün hesabat (`render_report_az`) qaytarır. Eyni plan təkrar işə salınsa heç nə iki dəfə icra olunmur. Plan xərci büdcə tavanını aşırsa başlamır. Həssas mərhələ üçün agent avtomatik yaradılmır.

## Məhsul bünövrəsi (`agents/builders.py`)
Website Builder, App Builder, APK Builder, Video Studio interfeysləri: **hamısı `not_configured`**, `plan()` deterministik SİMULYASİYA planı (planning → build → release), `build()` həmişə `NotConfigured`. Nəşr/imzalama/deployment mərhələləri `needs_approval` və `owner_only`; APK üçün Android SDK məhdudiyyəti və imzalama açarının sahibdə qalması qeyd olunur. `to_team_plan` yalnız planlaşdırma mərhələsini çoxagentli plana çevirir.

## Real AI runtime (`agents/providers.py`, `runtime/`)
OpenAI-uyğun provayder adapteri (nəqliyyat inyeksiya olunur; `agents/` şəbəkə kodu daşımır), real nəqliyyat `runtime/transport.py` (yönləndirmə yox, ölçü limiti, timeout). Endpoint siyasəti: Cloud yalnız HTTPS + ictimai DNS adı; şəxsi server loopback/özəl şəbəkə (HTTP) və ya TLS. `runtime/env.py` mühit dəyişənlərindən mühərrik qurur (`.env.example`): real Cloud yalnız **açar + baza URL + model + büdcə limiti > 0 + qiymətlər + xərc kitabı** olduqda `ready` olur (əks halda `not_configured`, səbəb göstərilir), istifadə üçün yenə `cloud_paid_use` razılığı lazımdır. Açar yalnız `Secret` ilə serverdə oxunur; repr/log/xəta mətnində yoxdur. Server: `python3 tools/serve.py`.

## Məhdudiyyətlər
- Real provayder/model ilə sınaq **aparılmayıb** (açar yoxdur, ödəniş yoxdur); adapter fake nəqliyyat və loopback test serveri ilə sınanıb. Real OpenAI-uyğun serverlə uyğunluq (müxtəlif cavab formatları) cihazda/serverdə yoxlanmalıdır.
- Agentlər yalnız MƏTN istehsal edir; fayl yazma, test icrası, sayt/APK/video istehsalı və nəşr alətləri qoşulmayıb. Plan nəticələri qaralamadır.
- İcra qatındakı saxlancılar (Company/Registry/Ledger/Approvals) tək-iplik bağlıdır: `AgentDispatcher` icranı ardıcıl edir, paralel yalnız AI generasiyasıdır.
- Rol/plan heuristikası sadədir (açar söz əsaslı); LLM planı yalnız simulyasiya/mock ilə sınanıb.
- API/Android-da agent idarəetmə ekranı yoxdur (agent siyahısı hələ yalnız oxunur); sahib əməliyyatları kodla/konsolla.
- APK build edilməyib (Android SDK yoxdur).
