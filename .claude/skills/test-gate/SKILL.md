---
name: test-gate
description: İşi "hazırdır", "tamamdır", "işləyir" kimi təqdim etməzdən əvvəl istifadə et. Testləri real işlədir və faktiki nəticəni göstərir.
---

# test-gate

"Hazırdır" demək üçün qapı. Atlama.

## Addımlar

1. Mühiti yoxla: `python3 --version` (hədəf 3.12) və `python3 -m pytest --version`.
   pytest yoxdursa: `pip install pytest`.
2. Testləri işlət: `python3 -m pytest -q`
3. **Real çıxışı** istifadəçiyə göstər (son sətirlər: keçən/uğursuz say, müddət).
4. Nəticəyə görə:
   - Hamısı keçdi → "hazırdır" demək olar, çıxışı qeyd edərək.
   - Uğursuzluq var → "hazırdır" demə. Uğursuz testləri və səbəbi göstər.
   - Test işləmədi/toplanmadı → bunu açıq de; "keçdi" demə.

## Qadağalar

- Nəticəni təxmin etmə, xatırlamadan yazma, uyduraraq yazma.
- Testi keçirmək üçün onu silmə, atlama (`skip`) və ya zəiflətmə.
- CI haqqında iddia etmək üçün onun real statusunu yoxla; yoxlamamısansa de.

## Hesabat şablonu

```
Komanda: python3 -m pytest -q
Nəticə:  <real çıxış>
Hökm:    keçdi / uğursuz / işləmədi
```
