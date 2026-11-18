# MODEL_ROUTER — Mərhələ 18.1 (Provider qatı + ModelRouter)

NAYMERA-nı konkret bir AI modelindən asılı olmayan edən model qatı: vahid provider interfeysi, etibarlı reyestr, qərar planı (`plan_route`), dörd adapter, Broker/GuardedLLM/CostLedger ilə icra və data-konfiqurasiya.

**Vəziyyət (dürüst):** yalnız **oflayn/fixture** icra. Real API, real açar, real şəbəkə və ödəniş YOXDUR. Qat istehsal yığınına (`build_app`, `runtime/env.py`, `runtime/production.py`, `tools/serve.py`) **QOŞULMAYIB**.
Real provayder/APK/real cihaz ilə heç nə sınanmayıb.

## 1. Arxitektura

```
sorğu (ModelRunRequest) ──► BrokeredModelRunner.run  (runtime/brokered_models.py)
                               │  yoxlama: iç-içə run yox, başqa agentin konteksti yox, plan_id uyğunluğu (varsa)
                               ▼
                         EffectBroker.run  (stop bayrağı → idempotency `model:{op_id}` → Gate/audit → ledger)
                               │  yalnız Broker daxilində:
                               ▼
        ┌── hər cəhd ─► təzə snapshot-lar (büdcə: LedgerPort, razılıq: ConsentView, sağlamlıq: HealthTracker + provider breaker)
        │               plan_route(registry, RouteRequest, budget, unhealthy, prefer)   ← YALNIZ qərar planı (models/router.py)
        │               registry.resolve_for_router → reverify; namizədlə (locality, reality, payment) müqayisəsi
        │               _TrustedProviderView  (status reyestrdən; yalnız runner silahlandırdıqda)
        │               GuardedLLM(view, LLMConfig(qiymətlər descriptor-dan), ledger=LedgerPort) + SpendScope(agent, op_id.n)
        │                    └► adapter.complete  (models/adapters/*: inyeksiya olunmuş nəqliyyat)
        └── xəta ─► cəhd nəticəsinə görə istisna siyahısı → YENİDƏN plan (fallback avtomatik icazə DEYİL)
```

| Komponent | Fayl | Rolu |
|---|---|---|
| `ModelProvider` | `models/protocol.py` | `LLMProvider` ilə uyğun protokol (`name`, `is_mock`, `model`, `complete`) |
| `ModelDescriptor` (`CapabilityDescriptor`) | `models/descriptor.py` | model ID, provider, capability-lər, kontekst/çıxış limitləri, **locality**, **reality**, **payment** (hesablanır), qiymət, `max_data_class`, availability, sübut |
| `ModelRegistry` | `models/registry.py` | etibar daşıyan statusu müstəqil müəyyən edir; dəqiq-tip siyahısı; `reverify`; `disable` (geri açma yoxdur) |
| `plan_route` | `models/router.py` | qərar planı: seçim, rədd səbəbləri, təhlükəsiz imtina, deterministik `plan_id` |
| `ModelRoutingConfig` | `models/config.py` | yalnız model üstünlüyü (sıra) və əlavə capability tələbi |
| Adapterlər | `models/adapters/` | OpenAI-uyğun (sərtləşdirilmiş), Anthropic, Gemini, proses-daxili lokal; `fixtures.py` oflayn nəqliyyat |
| `BrokeredModelRunner` | `runtime/brokered_models.py` | icra zənciri |
| Mutasiya aləti | `tools/mutation_check_models.py` | əsas yoxlamaları pozur, testlərin uğursuz olduğunu yoxlayır |

## 2. Etibar modeli: üç AYRI ölçü

| Ölçü | Dəyərlər | Mənbə |
|---|---|---|
| `locality` | LOCAL / REMOTE | dəqiq adapter tipi + yenidən yoxlanmış endpoint (`validate_endpoint`) |
| `reality` | MOCK / SIMULATED / REAL | MOCK: `MockProvider` tipi. REAL: yalnız qeydli endpoint-tətbiq edən nəqliyyatla. Fixture nəqliyyat ⇒ SIMULATED. Proses-daxili lokal adapter HƏMİŞƏ SIMULATED |
| `payment` | FREE / PAID | qiymət metadatasından HESABLANIR (saxlanmır, elan edilə bilməz) |

Provider özünü etibarlı edə bilməz: `describe()` və `name` oxunmur; `is_mock` yalnız tipdən gözlənən dəyərlə uyğunluq üçün yoxlanır (uyğunsuzluq ⇒ rədd). Alt sinif/ördək tipi/xam Stage 17 `OpenAICompatibleProvider` modellər qatında etibarlı deyil. Qeydiyyatdan sonra dəyişdirmə `reverify` ilə aşkarlanır (plan və icra anında).

**Vahid:** qiymətlər «ledger vahidi»ndədir (`CostLedger`/`LLMConfig.price_*_per_1k` ilə eyni ədədi vahid). Ledger valyuta bildirmir, buna görə heç yerdə valyuta fərz edilmir.

## 3. Qərar qapıları (`plan_route`)

Hər model üçün səbəb kodları toplanır; heç biri qalmayanlar arasında sıralama aparılır:
`registration_tampered` · `unavailable:*` · `unhealthy:*` · `simulated_not_allowed` / `mock_not_allowed` · `missing_capability` · `data_class_exceeds_model_max` · `remote_forbidden_for_class` ·
`remote_not_allowed` · `consent_missing:*` · `context_too_small` · `budget_unknown` / `budget_zero` / `budget_exceeded`.

- **Məlumat sinfi:** təsnif edilməmiş/naməlum ⇒ UNCLASSIFIED (yalnız `SENSITIVE`-i qəbul edən lokal modelə; uzağa HEÇ VAXT). Mətndə sirr bənzər məzmun ⇒ SECRET (bəyan edilən sinif aşağı ola bilməz); SECRET heç bir modelə verilmir (plan imtina edir). Yaddaş konteksti ≥ INTERNAL. Uzaq model ≤ INTERNAL, lokal ≤ SENSITIVE.
- **Uzaq:** yalnız `allow_remote is True` + `cloud_paid_use` (yaddaş üçün əlavə `cloud_data_transfer`) razılığı.
- **Ödənişli:** büdcə məlum, > 0 və təxmini xərc (GuardedLLM rezerv düsturu, UTF-8 bayt) hədd daxilində olmalıdır. Naməlum/yanlış büdcə ⇒ ödənişli yoxdur.
- **Sıralama (deterministik):** `prefer` → REAL < SIMULATED < MOCK → LOCAL < REMOTE → təxmini xərc → `model_id`. Eyni giriş ⇒ eyni plan və `plan_id`; qeydiyyat sırasından asılı deyil.
- **Nəticə:** `selected` + təklif olunan fallback sırası, rədd edilənlərin səbəbləri, imtinada kod və səbəb sayğacı. `GRANTS_EXECUTION=False`; plan I/O etmir, rezerv etmir, çağırış etmir.

## 4. İcra zənciri və fallback

- Hər icra `EffectBroker.run` daxilindədir; defolt agent büdcəsi 0 (`SYSTEM_MODEL_SPEC`): ödənişli provider yalnız sahibin açıq verdiyi agent spec-i və ledger tavanı ilə mümkündür.
- `plan_id` imza/icazə DEYİL: verilən plan yalnız təzə planla müqayisə üçündür (dəyişdirilmiş ⇒ `invalid_plan`, köhnəlmiş ⇒ `stale_plan`); icra həmişə təzə planın seçiminə əsaslanır.
- Xərc yalnız Broker-in TƏK `CostLedger`-indən qurulan `LedgerPort` ilə; `SpendScope` agentin adına yazır. Ümumi tavan/ledger oxunmursa və ya breaker açıqdırsa ödənişli çağırış yoxdur.
- **Fallback:** hər cəhd YENİ plandır (bütün qapılar təkrarlanır, sorğunun mətni/sinfi/bayraqları eynidir, razılıq/büdcə/sağlamlıq təzə oxunur). Nəticəsi **naməlum** ödənişli cəhddən (timeout, yarımçıq/pozulmuş cavab, haqqı naməlum xəta) sonra heç bir başqa ödənişli provider çağırılmır (ikiqat xərc yoxdur); rezerv `unknown` olaraq qalır və sahib uzlaşdırması tələb edir. Haqqın tutulmadığı bəlli xətalardan (məs. 401) sonra başqa provayderə keçmək olar. Lokaldan uzağa yüksəlmə yalnız bütün qapılar (açıq `allow_remote`, razılıq, sinif, büdcə) keçdikdə mümkündür.
- Eyni `op_id` təkrar gələndə `duplicate` (yenidən çağırış/xərc yoxdur; əvvəlki mətn replay olunmur). Uğursuz əməliyyatın `op_id`-si də istifadə olunmuş sayılır: təkrar cəhd yeni `op_id` tələb edir.
- Başqa agentə aid kontekst bloku aşkar edilərsə icra Broker-ə çatmadan dayandırılır. Öz konteksti yalnız escape olunmuş `untrusted` blok kimi ötürülür; model çıxışı ETİBARSIZ məlumatdır (`UNTRUSTED_TEXT`).

## 5. İstifadə qaydası (oflayn nümunə)

```python
from agents.llm import Secret
from agents.spec import AgentSpec
from models import ModelRegistry, ModelCapability, DataClass
from models.adapters.anthropic import AnthropicProvider
from models.adapters.fixtures import FixtureTransport, anthropic_ok
from models.adapters.local import FixtureLocalRuntime, LocalRuntimeProvider
from runtime.brokered_models import BrokeredModelRunner, ModelRunRequest
from safety.broker import EffectBroker
from safety.ledger import CostLedger

registry = ModelRegistry()
caps = (ModelCapability.TEXT_GENERATION, ModelCapability.CHAT)
registry.register(LocalRuntimeProvider(FixtureLocalRuntime("lokal cavab"), "lokal-1"), model_id="local_free", provider="local",
                  capabilities=caps, context_tokens=8000, max_output_tokens=1000, max_data_class=DataClass.SENSITIVE)
registry.register(AnthropicProvider("fixture-model", Secret("<test-dəyəri>"), FixtureTransport(anthropic_ok("uzaq cavab"))), model_id="remote_a",
                  provider="anthropic", capabilities=caps, context_tokens=8000, max_output_tokens=1000, max_data_class=DataClass.INTERNAL,
                  price_in_per_1k=0.5, price_out_per_1k=1.0)                    # ledger vahidi; uzaq model üçün qiymət > 0 məcburidir

ledger = CostLedger("ledger.db", global_limit=10.0)                              # ümumi tavan məlum olmalıdır, əks halda ödənişli yoxdur
broker = EffectBroker("audit.jsonl", "STOP", ledger=ledger)
agent = AgentSpec(name="agent_a", purpose="p", permissions=["read_files"], inputs=["x"], outputs=["y"], cost_limit=1.0)
runner = BrokeredModelRunner(registry, broker, agent=agent, consents=my_consent_view, log_dir=".")

plan = runner.preview(ModelRunRequest("op-1", "salam", data_class=DataClass.INTERNAL, allow_remote=True, allow_simulated=True))   # yalnız qərar
result = runner.run(ModelRunRequest("op-1", "salam", data_class=DataClass.INTERNAL, allow_remote=True, allow_simulated=True))
```
`my_consent_view` — `granted(scope) -> bool` metodlu obyekt (`cloud_paid_use`, `cloud_data_transfer`); razılıq yoxdursa uzaq model seçilmir. `data_class` verilməsə UNCLASSIFIED (yalnız lokal).
`allow_simulated=True` yalnız oflayn/fixture üçündür; real provayder qoşulmayıb.

## 6. Konfiqurasiya

Konfiqurasiya YALNIZ sıra və əlavə capability tələbi verir (`models/config.py`). `allow_remote`, `allow_simulated`, razılıq, məlumat sinfi, büdcə/qiymət, `max_data_class`, endpoint, açar, adapter tipi, əlçatanlıq və s. **tanınmır və fayl rədd edilir**
(`security_key_forbidden`). Naməlum sahə, təkrar açar, yanlış tip/ölçü, naməlum capability və qeydsiz `model_id` ⇒ yükləmə uğursuz; `runner.use_config(...)` uğursuz olarsa əvvəlki konfiqurasiya qalır. Konfiqurasiya faylı oxumur və mühit dəyişəni istifadə etmir: mətni çağıran verir.

```json
{
  "schema_version": 1,
  "profiles": {
    "yerli_ilk":   {"prefer": ["local_free", "remote_a"]},
    "uzaq_ilk":    {"prefer": ["remote_a", "local_free"], "capabilities": ["chat"]}
  }
}
```
```python
from models import ModelRoutingConfig
runner.use_config(ModelRoutingConfig.from_json(text))              # reyestrə qarşı yoxlanır
runner.run(ModelRunRequest("op-2", "salam", profile="uzaq_ilk", allow_remote=True, data_class=DataClass.INTERNAL))
```
Modeli dəyişmək üçün yalnız konfiqurasiya mətni dəyişir. «Uzaq ilk» profili belə uzağı SEÇDİRƏ BİLMƏZ: `allow_remote`, razılıq, məlumat sinfi və büdcə qapıları yenə tətbiq olunur.

## 6a. Mərhələ 18.6: REAL lokal model — razılıq və cihaz resursları
- `plan_route` indi LOCAL+REAL modelə **`local_resource_use`** razılığı tələb edir (CLAUDE.md qayda 7); simulyasiya/mock lokal modellər real resurs işlətmir və tələb olunmur. Əvvəl bu qapı YOX idi (18.1 baxışında aşkarlandı və düzəldildi).
- **Resurs qoruyucusu:** `ResourceSnapshot` (boş RAM, batareya, şarj, istilik) + `agents.local_models.ResourceLimits` eşikləri + modelin `min_ram_mb`-i (descriptor, kataloqdan). Naməlum/yanlış tipli snapshot ⇒ `resources_unknown` (seçilmir); `ram_low`, `battery_low` (şarj olunursa yoxlanmır), `thermal_throttled`.
  `BrokeredModelRunner(device_state=..., resource_limits=...)` resursları HƏR cəhddə təzə oxuyur; mənbə verilməyibsə/xəta ⇒ REAL lokal model işləmir.
- `models/local_server.register_local_server`: `OpenAICompatProvider(kind="private")` + endpoint-tətbiq edən nəqliyyatla REAL lokal modeli qeydə alır (nəqliyyat real deyilsə RƏDD, səssiz SIMULATED yox); RAM təxmini `LOCAL_MODEL_CATALOG`-dan. Şəbəkəyə çıxmır, heç nə yükləmir.
- `runtime.brokered_models.make_text_generator`: `ModelPlanner` kimi istehlakçılar üçün `generate(system, prompt)`; hər çağırış unikal op_id ilə Broker zəncirindən keçir; defolt təsnif edilməmiş məlumat ⇒ yalnız lokal.
- Hələ YOXDUR: modelin yüklənməsi (yalnız `plan_download` planı), real lokal serverlə sınaq, real RAM/batareya mənbəyi (Android/server adapteri `device_state` verməlidir), Azərbaycan keyfiyyət ölçməsi.

## 7. Təhlükəsizlik məhdudiyyətləri (açıq)

- **Broker konteksti və view-nun silahlanma pəncərəsi proses-daxili vəziyyətdir, OS sərhədi DEYİL.** Eyni prosesdə ixtiyari kod icra edə bilən tərəf bunları özü qura bilər (`tests/test_models_audit.py` bunu «kanarya» kimi göstərir; xərc yenə ledger-dən keçir). Agent kodu bu prosesdə işləmir (ayrı proses: `AgentGateway`, Stage 17).
- «Agentlərarası sızma» yalnız **girişdə** yoxlanır (başqa agentin konteksti bloku). Model çıxışında başqa agentin məlumatını tanıyan aşkarlama YOXDUR.
- Router yalnız sahibin düzgün qeyd etdiyi descriptor-a (capability, qiymət, `max_data_class`) əsaslanır; səhv sahib konfiqurasiyasını avtomatik aşkar edə bilməz.
- Etibarlı adapter tipləri koddadır; yeni tip üçün kod dəyişikliyi və yenidən baxış lazımdır. Anthropic/Gemini telləri ictimai API sənədinə əsaslanır, real xidmətə qarşı **yoxlanmayıb**; fixture yalnız daxili ardıcıllığı sübut edir.
- Naməlum nəticəli rezervlər sahib uzlaşdırmasına qədər saxlanır (`resolve_unknown`, Stage 17).
- `plan_id` məzmun hash-idir (determinizm/müqayisə üçün); imza və ya təsdiq DEYİL.
- Qeydiyyat sonrası yalnız `disable` mümkündür; geri açma üçün yeni reyestr lazımdır.

## 8. Stage 17 köhnə yolundakı məlum boşluqlar (DÜZƏLDİLMƏYİB)

Xam `agents.providers.OpenAICompatibleProvider` (istifadəçi yolu `build_app`/`runtime/env.py`) bu mərhələdə **dəyişdirilməyib** və yeni etibarlı yolda istifadə olunmur:
(a) dərin iç-içə JSON `RecursionError` ilə çökür; (b) tək surrogat ehtiva edən cavab mətni aşağı axına buraxılır; (c) cavab ölçüsünə limit yoxdur (yalnız sonradan `GuardedLLM.max_response_chars`).
Yeni yol `models.adapters.openai_compat.OpenAICompatProvider` ilə bunları `LLMBadResponse`-a çevirir. `tests/test_models_audit.py` boşluğun köhnə yolda HƏLƏ mövcud olduğunu təsdiqləyir (bağlandıqda test sənədin yenilənməsini tələb edəcək).
Əvvəlki audit nəticələri (uid təkrar istifadəsi, `/tmp` sızması, nəqliyyat «seam» etibarı, ümumi müddət limiti) da açıq qalır.

## 9. Hazırda işləməyən / gələcək mərhələyə saxlanılanlar

- Real provayder, real açar, real şəbəkə, ödəniş: YOXDUR. İstehsal qoşulması (`build_app`, `runtime/env.py`, `tools/serve.py`) ayrıca sahib qərarı və Stage 17 sərhəd kodunun dəyişməsini tələb edir.
- Real lokal model işləmə mühiti (hazırda yalnız oflayn `FixtureLocalRuntime`); lokal server üçün `OpenAICompatProvider(kind="private")` mövcuddur, lakin real serverlə sınanmayıb.
- Alət çağırışı, görmə, embedding, axın (streaming): dəstəklənmir (capability kimi elan edilə bilməz).
- Nəticənin (`text`) saxlanması/replay (idempotent `duplicate` yalnız statusu verir), model çıxışında sızma aşkarlanması, `AiStore` ilə razılıq uyğunlaşdırıcısı, API/Android səthi, sağlamlığın davamlı saxlanması.
- Event jurnalı (`kernel/EventLog`) ilə marşrut hadisələrinin yazılması (18.0 sxemlərinə əlavə tələb edər).
- `AgentGateway.run_session` bağlı qalır; bu mərhələ onu aktivləşdirmir.

## 10. Testlər və mutasiya

`tests/test_models_registry.py`, `test_models_adapters.py`, `test_models_router.py`, `test_models_runner.py`, `test_models_config.py`, `test_models_audit.py` (+ `tests/test_paid_llm_entry_points.py`-də 2 yeni giriş nöqtəsi). Hamısı oflayn; `socket` partladılır.
`python tools/mutation_check_models.py` (kopyada işləyir) hər əsas yoxlamanı ayrıca pozur; sağ qalan mutant REAL BOŞLUQ sayılır (ekvivalentlik yalnız yazılı sübutla; `EQUIVALENT` siyahısı boşdur). `--list`, `--only` mövcuddur.
