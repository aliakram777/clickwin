# Hibrid AI beyni (Mərhələ 16)

NAYMERA həm **Cloud AI**, həm **Local AI** ilə işləməyə hazırdır; hər ikisi daimi qoşulu saxlanılır, lakin **hər sorğuya yalnız BİR mühərrik cavab verir** (paralel iki cavab yoxdur). Seçimi `HybridRouter` edir (`agents/hybrid.py`).

## Rejimlər
| Rejim | Davranış |
|---|---|
| **AUTO** (defolt) | Sadə tapşırıq → Local üstünlük; mürəkkəb tapşırıq (plan, uzun mətn, təhlil) → Cloud üstünlük. Real mühərrik simulyasiyadan üstündür. Seçilmiş mühərrik uğursuz olsa, **ardıcıl** olaraq digərinə keçilir (təkrar yoxdur). |
| **CLOUD** | Yalnız Cloud. Cloud yoxdursa `cloud_unavailable`: səssiz Local-a keçid YOXDUR. |
| **LOCAL** | Yalnız Local. Model yoxdursa `local_unavailable`, tapşırıq dəstəklənmirsə `unsupported_locally` (açıq bildirilir). |

## Təhlükəsiz fallback və təkrar icranın qarşısı
- Hər sorğunun `request_id`-si var. Təkrar eyni `request_id` **saxlanmış nəticəni** qaytarır (mühərrik yenidən çağırılmır); eyni anda gələn təkrar `in_progress` alır; eyni `request_id` başqa məzmunla `bad_request`.
- Uğursuz (heç bir nəticə yoxdur) sorğu eyni `request_id` ilə yenidən yönləndirilə bilər; nəticə bir dəfə bağlanır (`finish` atomikdir).
- Vaxt aşımı (`timeout_unknown`) Cloud-da sorğunun işlənmiş ola biləcəyini bildirir: fallback yalnız mətn yaradılmasıdır (yan təsir yoxdur), nəticə isə bir dəfə commit olunur.
- Android tərəfi: `client_request_id` ilə söhbət sorğuları idempotentdir (şəbəkə təkrarı mesajı ikiqat yazmır, mühərriki təkrar çağırmır).
- Dövrə kəsici: ardıcıl 3 uğursuzluqdan sonra mühərrik 30 s kənarlaşdırılır, sonra bir yoxlama sorğusu icazə verilir.
- `STOP` bayrağı bütün mühərrikləri dayandırır; `EngineBlocked` fallback etmir.

## Razılıq və məxfilik (defolt: HEÇ BİR razılıq yoxdur)
| Sahə | Nə üçün |
|---|---|
| `cloud_paid_use` | real (ödənişli ola bilən) Cloud provayderindən istifadə |
| `cloud_data_transfer` | yaddaşdan kontekstin Cloud-a göndərilməsi |
| `local_model_download` | lokal modelin yüklənməsi (yalnız plan; real yükləmə yoxdur) |
| `local_resource_use` | cihazda real modelin RAM/CPU/batareya istifadəsi |
- Razılıq **vermək** cihaz Keystore imzası (biometrik step-up) tələb edir; **geri götürmək** asandır. Söhbət mətni və səs razılıq, rejim və ya məxfilik dəyişə bilməz (testlə).
- Yaddaş Cloud-a yalnız `cloud_may_receive_memory` + icazəli kateqoriyalar (`approval`/`result` heç vaxt) + `cloud_data_transfer` razılığı ilə gedir; defolt: göndərilmir. Local həmin yaddaşı (oxu üzü) alır. Yaddaşa yazma/silmə qaydaları dəyişməyib (`MemoryService`; LLM yaza bilməz).
- Sirr bənzər mətn heç vaxt Cloud-a göndərilmir (`blocked_egress`).
- İnternet/mühərrik çıxışı etibarsız mətndir: heç nə icra etmir, təsdiq sayılmır. Pul, hesab, nəşr, deployment yolları (Approval/Broker/Gate/Ledger) dəyişməyib və `agents/hybrid.py` onları import etmir.

## Mühərriklər
- **CloudEngine**: provayderdən asılı olmayan; mövcud `GuardedLLM`-i sarır. API açarı yalnız serverin mühit dəyişənlərindədir (`load_config`), APK-da yoxdur. Real provayder bu mərhələdə YOXDUR: mock → `simulation`, provayder seçilməyibsə `not_configured`.
- **LocalEngine**: `LocalRuntime` interfeysi. İki variant nəzərdə tutulub: (a) **cihazdakı** model (Android; interfeys `OnDeviceLocal`, defolt `NotConfiguredLocal`), (b) **şəxsi server** (`PersonalServerRuntime`, nəqliyyat çağıran tərəfindən verilir, şəbəkə kodu yoxdur). Model/runtime yoxdursa `not_configured`: real imkan kimi göstərilmir.
- Resurs qoruyucusu (`agents/local_models.py`): RAM, batareya (şarj olunmayanda), istilik, sorğu ölçüsü; cihaz vəziyyəti naməlumdursa icazə verilmir. Yükləmə planı razılıq, yaddaş, ölçülü şəbəkə və batareyanı yoxlayır; **heç nə yüklənmir**.

## Lokal model araşdırması (TƏXMİNİ, ölçülməyib)
| Model (Q4) | Təxmini disk | Təxmini RAM | Sinif | Qeyd |
|---|---|---|---|---|
| SmolLM2 360M | ~250 MB | ~0.6 GB | zəif cihaz | ən kiçik, çox məhdud keyfiyyət |
| Qwen2.5 0.5B | ~400 MB | ~0.9 GB | zəif/orta | sadə təsnifat və qısa cavab |
| Llama 3.2 1B | ~800 MB | ~1.6 GB | orta | qısa cavablar |
| Qwen2.5 1.5B | ~1.0 GB | ~2 GB | orta | çoxdilli iddia edilir, az/tr yoxlanmayıb |
| Gemma 2 2B | ~1.6 GB | ~3 GB | orta/yüksək | lisenziya şərtləri yoxlanmalı |
| Llama 3.2 3B | ~2.0 GB | ~3.8 GB | yüksək | rəsmi dil siyahısında az/ru/tr yoxdur |
| Phi-3 mini 3.8B | ~2.3 GB | ~4.2 GB | yüksək | İngilis dilli, lisenziya yoxlanmalı |
İcraçı variantları: llama.cpp (GGUF), MediaPipe LLM Inference, MLC LLM, ExecuTorch; şəxsi serverdə llama.cpp/Ollama-uyğun HTTP. **Azərbaycan dili üçün heç bir kiçik modelin keyfiyyəti sübut olunmayıb** — kataloqda `az` dəstəyi iddia edilmir, ona görə Local AI Azərbaycan mətnini default olaraq dəstəkləmir (AUTO Cloud-a yönəldir; LOCAL açıq bildirir). Cədvəl araşdırma nəticəsidir, real cihazda ölçülməyib.

## Səs (`agents/voice.py`)
az (əsas), en, ru, tr; hədəf **təbii qadın səsi**. TTS/STT adapterləri **Local və Cloud ayrı** göstərilir. Heç bir real səs mühərriki yoxdur: qoşulmayan `not_configured`, mock `simulation` (nəticə `is_mock`). Real Cloud səsi `cloud_paid_use` + `cloud_data_transfer` tələb edir. Səs heç vaxt təsdiq mənbəyi deyil.

## API
`GET /v1/ai/status`, `POST /v1/ai/mode`, `POST /v1/ai/challenge` + `/v1/ai/consent` + `/v1/ai/privacy` (step-up), `GET /v1/voice/status`; `GET /v1/capabilities` içində `ai` bloku; söhbət cavabında `engine`, `notice`, `notice_code`.

## Məhdudiyyətlər
Real Cloud provayderi, real lokal model və real səs yoxdur (hamısı `simulation`/`not_configured`). Android-da razılıq vermə ekranı hələ yoxdur (backend-də var); cihazdakı Local AI yalnız interfeysdir. Dil/mürəkkəblik təsnifatı sadə heuristikadır. Android build edilməyib və cihazda sınanmayıb (SDK yoxdur).
