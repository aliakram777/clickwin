# NAYMERA Android (Kotlin + Jetpack Compose)

Android tətbiq skeleti: Material3 UI, yazılı söhbət, bildirişlər, tarixçə, cütləşmə, təsdiq (biometrik step-up), səs interfeysi (simulyasiya).
Backend: `../api/` (stdlib WSGI). **Bu mühitdə build edilməyib — bax `BUILD_STATUS.md`.**

## Texnologiya
Kotlin 2.0, Jetpack Compose + Material3, OkHttp, kotlinx-serialization, AndroidX Security (EncryptedSharedPreferences), BiometricPrompt.
Pulsuz və minimal asılılıq dəsti; reklam/analitika/ödəniş/üçüncü tərəf SDK-sı yoxdur.

## Təhlükəsizlik
- APK-da API açarı yoxdur: LLM/xarici servis açarları yalnız serverin mühit dəyişənlərindədir. Cihaz yalnız server tərəfindən verilən
  tokenləri və cihaz sirrini saxlayır (şifrələnmiş).
- HTTPS məcburidir; düz HTTP yalnız debug quruluşunda `10.0.2.2`/`localhost`/`127.0.0.1` üçün.
- Həssas əməliyyat təsdiqi yalnız biometrik/cihaz kilidi + HMAC sübutu ilə; səs və söhbət mətni təsdiq mənbəyi deyil.
- Söhbət/səs mətni etibarsız giriş kimi göstərilir və heç nə icra etmir. Mock cavablar "SİMULYASİYA" ilə işarələnir.

## Quruluş
`data/` (ApiClient, SessionStore, Models, EventSync) · `ui/` (ekranlar, tema) · `voice/` (STT/TTS interfeysləri, MockSpeech) · `security/` (StepUp).
