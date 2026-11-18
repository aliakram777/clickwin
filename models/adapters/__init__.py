"""Provider adapterləri (Mərhələ 18.1-B). Hamısı `ModelProvider` protokoluna uyğundur və cavabı mövcud `LLMResponse` müqaviləsinə normallaşdırır.
Adapterlərin standart nəqliyyatı YOXDUR: nəqliyyat inyeksiya olunur (testdə oflayn fixture). Real API çağırışı, açar və istehsal qoşulması bu mərhələdə YOXDUR.
Etibar statusu (locality/reality/payment) adapterdən deyil, `ModelRegistry`-dən gəlir; adapter tipi yalnız `models/registry.py` cədvəlinə koddakı dəyişiklik kimi əlavə olunur."""
