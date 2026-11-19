package az.naymera.app.domain

/**
 * FLAG_SECURE istinad sayğacı (Android-dan asılı deyil, yalnız əsas iplikdə istifadə olunur): bayraq birinci təhlükəsiz ekran
 * açılanda qoyulur, SON təhlükəsiz ekran bağlananda götürülür. Ekranlar arası keçiddə köhnə ekranın dispose-u yeni ekranın bayrağını silmir.
 */
object SecureFlagCounter {
    private var refs = 0

    /** true: bayraq İNDİ qoyulmalıdır (ilk istinad). */
    fun acquire(): Boolean { refs++; return refs == 1 }

    /** true: bayraq İNDİ götürülməlidir (son istinad). Artıq-azaltma sayğacı mənfiyə salmır. */
    fun release(): Boolean { if (refs == 0) return false; refs--; return refs == 0 }

    fun count(): Int = refs
    fun reset() { refs = 0 }
}
