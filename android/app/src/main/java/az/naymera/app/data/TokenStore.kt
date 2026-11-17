package az.naymera.app.data

/**
 * Sessiya saxlancı (yalnız server tokenləri). Android-dan asılı deyil: tətbiqdə `SessionStore` (şifrələnmiş), JVM sınaqlarında yaddaşdakı
 * nüsxə. Cihazın imza açarı burada DEYİL, Android Keystore-dadır.
 */
interface TokenStore {
    var serverUrl: String
    val accessToken: String
    val refreshToken: String
    var eventCursor: Long
    fun savePairing(r: PairResponse)
    fun saveTokens(t: TokenResponse)
    fun clear()
}
