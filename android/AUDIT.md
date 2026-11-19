# Android layihəsi auditi (Mərhələ 15)

Metod: bütün Kotlin/Gradle/manifest/resurs faylları oxundu; Android-dan asılı olmayan Kotlin qatı JVM-də kompilyasiya və sınaqdan keçirildi (`jvm-contract`). Android-a bağlı hissələr kompilyasiya OLUNMAYIB (SDK yoxdur) — bunlar yalnız nəzərdən keçirilib.

## Tapılanlar və düzəldilənlər
| # | Tapıntı | Təsir | Vəziyyət |
|---|---|---|---|
| 1 | `DeviceKey` `StrongBoxUnavailableException` (API 28 sinfi) tuturdu; minSdk 26 | API 26–27 cihazlarda sinif yüklənməsi/çökmə riski | Düzəldildi: ümumi `ProviderException` |
| 2 | `ApiClient`/`EventSync` konkret `SessionStore`-dan (Android `Context`) asılı idi | JVM-də sınaq mümkün deyildi | Düzəldildi: `TokenStore` interfeysi; `SessionStore` onu həyata keçirir |
| 3 | İmzalanan mesaj formatı Android-a bağlı fayllarda (`StepUp`, `DeviceKey`) idi | Server ilə uyğunluq sınanmırdı | Düzəldildi: `security/Messages.kt` + dillərarası vektorlar |
| 4 | OkHttp `callTimeout` aşımı `InterruptedIOException` verir (Timeout kimi tanınmırdı) | Yanlış xəta mətni | Düzəldildi (`Timeout`) |
| 5 | Timeout/backoff dəyərləri sərt idi | Sınaq mümkün deyildi | Düzəldildi: `ApiTimeouts`, `EventSync.backoffBaseMs` |
| 6 | Tətbiqdə launcher ikonu yox idi | Defolt/boş ikon | Düzəldildi: vektor launcher ikonu (adaptive ikon deyil) |

## Açıq risklər (build zamanı yoxlanmalı)
- **Gradle wrapper** repoda yoxdur (binar fayl qadağandır); `gradle wrapper` ilə yaradılmalıdır.
- `androidx.security:security-crypto:1.1.0-alpha06` alfa buraxılışdır (`MasterKey.Builder` üçün); stabil olana keçid dəyərləndirilməlidir.
- Compose: `Icons.Filled.Send/Chat` AutoMirrored variantları üçün deprecation xəbərdarlığı ola bilər; `material-icons-extended` böyük asılılıqdır (R8 kiçildir).
- Rus dilində say formaları (plurals) istifadə olunmur (`%1$d mesaj` tipli sətirlər); lint xəbərdarlığı ola bilər.
- `Theme.Material3.Dark.NoActionBar` + `AppCompatActivity`: tema AppCompat-dan törəyir, yoxlanılmalıdır.
- `proguard-rules.pro` serializer qaydaları yalnız `data` paketini saxlayır; release-də R8 sınağı aparılmayıb.
- Keystore: `setUserAuthenticationParameters(0, AUTH_BIOMETRIC_STRONG)` və `setInvalidatedByBiometricEnrollment(true)` real cihazlarda (OEM fərqləri, StrongBox) sınanmalıdır; barmaq izi dəyişəndə cihaz yenidən cütləşdirilməlidir.

## Kompilyasiya statusu
| Hissə | JVM-də kompilyasiya | Android-da |
|---|---|---|
| `data/Models`, `ApiFailure`, `ApiClient`, `EventSync`, `TokenStore` | ✅ + 27 test | — |
| `security/Messages` | ✅ + test | — |
| `data/SessionStore`, `security/DeviceKey`, `security/StepUp` | ❌ | sınanmayıb |
| `ui/*`, `NaymeraApp`, `MainActivity`, `voice/*` | ❌ | sınanmayıb |
| Manifest, resurslar, gradle | statik testlər | build işləməyib |
