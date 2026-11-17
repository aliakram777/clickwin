# NAYMERA Android — build statusu

**APK build EDİLMƏYİB.** Bu mühitdə Android SDK yoxdur (`ANDROID_HOME`, `sdkmanager` yoxdur) və Google Maven
(`dl.google.com`) əlçatan deyil; Gradle asılılıqları yüklənə bilmir. Buna görə:

- Kotlin/Compose kodu **kompilyasiya edilməyib** və APK **yaradılmayıb**; heç bir `.apk`/`.aab`/`.jar`/`.keystore` repoda yoxdur.
- Layihənin düzgün işlədiyi bu mərhələdə **sübut olunmayıb**. Yoxlanılan yeganə şey `tests/test_android_project.py`-dəki statik
  testlərdir (quruluş, manifest, təhlükəsizlik konfiqurasiyası, açar yoxluğu, Kotlin endpoint/DTO ilə server kontraktı).
- Gradle wrapper (`gradlew`, `gradle-wrapper.jar`) commit edilməyib; sahib yerli maşında `gradle wrapper` ilə yaradır.

## Yerli build (sahibin maşınında)

1. Android Studio və JDK 17 quraşdırın; Android SDK 34 yükləyin.
2. `android/` qovluğunu açın (və ya `local.properties`-də `sdk.dir` təyin edin).
3. `gradle wrapper`, sonra `./gradlew :app:assembleDebug`.
4. Emulyatorda server: repo kökündə `python -m api.server pair` (kod), `python -m api.server serve`; tətbiqdə ünvan `http://10.0.2.2:8765`
   (düz HTTP yalnız debug quruluşunda və yalnız bu yerli hostlar üçün).

Release APK imzasızdır; imzalama açarı repoda saxlanılmır və sahib tərəfindən yerli təyin edilməlidir.

## Məlum məhdudiyyətlər

- Real səs tanıma və sintez YOXDUR: `MockSpeech` simulyasiyadır (`isMock=true`, UI-da "SİMULYASİYA").
- Push (FCM) yoxdur; hadisələr qısa sorğulama ilə alınır.
- Step-up: cihaz sirri `EncryptedSharedPreferences`-dədir və HMAC sübutu biometrik açılışdan sonra hesablanır; açılış tətbiq
  səviyyəlidir (Keystore açarına bağlı kriptoqrafik biometrik məhdudiyyət sonrakı mərhələdir). Server də həmin sirri saxlayır.
- UI cihazda yoxlanılmayıb (ekran görüntüsü, işləmə testi yoxdur).
