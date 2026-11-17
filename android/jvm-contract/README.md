# JVM kontrakt layihəsi

Tətbiqin Android-dan asılı olmayan Kotlin qatını (`data/*`, `security/Messages.kt`) **JVM-də kompilyasiya edir** və sınayır:
şəbəkə xətaları (timeout, bağlantı kəsilməsi, 401/429/5xx), token yenilənməsi, hadisə sinxronizasiyası və — `tools/jvm_contract.py` ilə —
HƏQİQİ Python backend (loopback) ilə cütləşmə, söhbət, tarixçə, hadisələr, imzalı təsdiq.

Bu **APK build deyil**: Android API-ləri (Keystore, BiometricPrompt, Compose UI) burada yoxlanılmır. İmza testində Java `SHA256withECDSA`
istifadə olunur (Android Keystore ilə eyni DER formatı), lakin açar proqram açarıdır.

```bash
python3 tools/jvm_contract.py        # backend + gradle test (Maven Central lazımdır)
NAYMERA_RUN_JVM=1 pytest tests/test_jvm_contract.py
```
