# Telefonda sınaq planı (sahib üçün; hələ icra olunmayıb)

Hazırlıq: `BUILD_STATUS.md`-dəki addımlarla debug APK yaradın və cihaza yükləyin. Telefonda ekran kilidi və ən azı bir barmaq izi/üz qeydiyyatı (güclü biometrik) olmalıdır.

1. **Server:** kompüterdə `python3 -m api.server pair --data-dir data/api` (birdəfəlik kod), sonra `python3 -m api.server serve --data-dir data/api`.
   Emulyatorda ünvan `http://10.0.2.2:8765`. Real telefonda yalnız HTTPS: TLS sertifikatı (`--tls-cert/--tls-key`) və ya HTTPS-li tunel/reverse-proxy; düz HTTP release-də bloklanır.
2. **Cütləşmə:** ünvan + kod → biometrik sorğu çıxmalıdır; uğurlu olduqda Əsas ekran açılır. Yanlış kod/ləğv edilmiş biometrik → xəta mətni.
3. **Rejim:** Əsas ekranda "SİMULYASİYA/Demo rejimi" nişanı və imkan cədvəli (agent icraçısı: Yoxdur, push: Yoxdur, STT: Yoxdur) görünməlidir.
4. **Söhbət/Tarixçə:** mesaj göndərin; cavab alın; Tarixçə-də söhbəti açın; tətbiqi bağlayıb açın (sessiya saxlanmalıdır).
5. **Hadisələr/Bildirişlər:** serverdə təsdiq sorğusu yaradın (məs. `tools/jvm_contract.py`-dəki kimi) → Bildirişlər ekranında təsdiq görünməlidir; push YOXDUR (tətbiq açıq olmalıdır).
6. **Təsdiq:** "Təsdiqlə" → biometrik; uğurlu olduqda server `approved`; təkrar cəhd → "artıq həll olunub". Biometriki ləğv edin → "ləğv edildi".
7. **Şəbəkə:** təyyarə rejimi → "Şəbəkə yoxdur"; server dayandırın → "Yenidən qoşulur…/Şəbəkə yoxdur", bərpa olunca avtomatik "Əlaqə var".
8. **Dil:** Ayarlar → az/en/ru/tr; bütün ekranlar dəyişməlidir.
9. **Çıxış:** cihaz ayrılır, açar silinir; köhnə token serverdə 401 verir.
10. **Barmaq izi əlavə edin** → köhnə açar etibarsız olmalıdır; yenidən cütləşdirin.
