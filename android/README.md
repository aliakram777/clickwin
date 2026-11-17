# NAYMERA Android (Kotlin + Jetpack Compose)

Android tətbiq: qaranlıq Material3 UI; Əsas, Söhbət, Yaddaş, Bildirişlər, Agentlər, Tarixçə, Ayarlar; cütləşmə və təsdiq (Keystore + biometrik imza); səs interfeysi (simulyasiya); az (əsas), en, ru, tr.
Backend: `../api/` (stdlib WSGI). **Bu mühitdə build edilməyib — bax `BUILD_STATUS.md`.**

## Texnologiya
Kotlin 2.0, Jetpack Compose + Material3, OkHttp, kotlinx-serialization, AndroidX Security (EncryptedSharedPreferences), BiometricPrompt.
Pulsuz və minimal asılılıq dəsti; reklam/analitika/ödəniş/üçüncü tərəf SDK-sı yoxdur.

## Təhlükəsizlik
- APK-da API açarı yoxdur: LLM/xarici servis açarları yalnız serverin mühit dəyişənlərindədir. Cihaz yalnız server tərəfindən verilən
  tokenləri saxlayır (şifrələnmiş); imza açarı Keystore-dadır və cihazdan çıxmır.
- HTTPS məcburidir; düz HTTP yalnız debug quruluşunda `10.0.2.2`/`localhost`/`127.0.0.1` üçün.
- Cihaz açarı Android Keystore-dadır (P-256, hər imza üçün biometrik); serverə yalnız açıq açar göndərilir. Həssas əməliyyat təsdiqi yalnız server nonce-ı üzərində biometrik imza ilə; səs və söhbət mətni təsdiq mənbəyi deyil.
- Backend rejimi (`/v1/capabilities`): mock/real açıq göstərilir; backend-də olmayan imkan (agent icraçısı, push, real səs) işləyirmiş kimi göstərilmir.
- Şəbəkə: timeout, backoff (yalnız GET), tək-uçuşlu token yenilənməsi, əlaqə vəziyyəti; xətalar istifadəçi dilində göstərilir.
- Söhbət/səs mətni etibarsız giriş kimi göstərilir və heç nə icra etmir. Mock cavablar "SİMULYASİYA" ilə işarələnir.

## Quruluş
`data/` (ApiClient, ApiFailure, SessionStore, Models, EventSync) · `ui/` (ekranlar, tema, UiText) · `voice/` (STT/TTS interfeysləri, MockSpeech) · `security/` (DeviceKey, StepUp). Dillər: `res/values` (az), `values-en|ru|tr`.
