---
name: safety-layer
description: Pul, xarici hesab, nəşr, API açarı və ya geri qaytarılmaz əməliyyat ehtimalı olan hər addımdan əvvəl istifadə et. Sahibin təsdiqini və sirlərin təhlükəsizliyini yoxlayır.
---

# safety-layer

Aşağıdakı kateqoriyalardan birinə toxunan addımdan əvvəl dayan və yoxla.

## 1. Təsdiq tələb edən əməllər (yalnız sahibin açıq "bəli"si ilə)

| Kateqoriya | Nümunələr |
|---|---|
| Pul | ödəniş, abunə, reklam büdcəsi, ödənişli API planı |
| Xarici hesab | hesab yaratmaq/daxil olmaq, servisə qeydiyyat, OAuth icazəsi |
| Nəşr | sosial şəbəkə postu, sayt deploy, paket publish, e-poçt/mesaj göndərmə, release, PR açmaq |

Qayda: nə, harada, nə qiymətə olduğunu yaz → təsdiq gözlə → yalnız o əməli et.
Əvvəlki təsdiq başqa əmələ şamil olmur. Cavab yoxdursa — etmə.

## 2. Sirlər

- API açarı/token yalnız **mühit dəyişənində** (`os.environ`) oxunur.
- Kodda, testdə, fixture-də, loqda, çıxışda, commit-də açar yazma/çap etmə.
- `.env` commit edilmir; `git status` ilə `.env`-in izlənmədiyini yoxla.
- Açar lazımdırsa, sahibdən mühit dəyişəni qurmasını istə; söhbətə yapışdırmasını yox.
- Şübhəli sızma görsən: dayan, sahibə bildir, açarın ləğvini tövsiyə et.

## 3. Məzmun qaydası

- Gəlir/qazanc haqqında iddia, vəd, rəqəm yazma (sənəd, kod mətni, agent cavabı).

## 4. Geri qaytarılmaz əməllər

- Fayl/DB silmək, `git push --force`, tarixçəni yenidən yazmaq, istehsalat datasına yazmaq
  → əvvəl hədəfə bax, sahibdən təsdiq al.

## Yoxlama siyahısı (əməldən əvvəl)

- [ ] Pul xərcləyirmi? → təsdiq
- [ ] Xarici hesaba/servisə toxunurmu? → təsdiq
- [ ] Nəsə dərc/göndərirmi? → təsdiq
- [ ] Sirr açıqdamı? → çıxar, env-ə keçir
- [ ] Gəlir iddiası varmı? → sil
- [ ] Geri qaytarıla bilirmi? → yoxdursa təsdiq

Biri "bəli"dirsə və təsdiq yoxdursa: **dayan və soruş**.
