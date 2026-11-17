package az.naymera.app.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Cihazdakı sessiya: yalnız server tərəfindən verilən tokenlər. APK-da heç bir server, LLM və ya üçüncü tərəf açarı saxlanmır;
 * cihazın imza açarı burada DEYİL, Android Keystore-dadır (`DeviceKey`) və cihazdan çıxmır.
 * Dəyərlər Android Keystore ilə şifrələnmiş SharedPreferences-dədir; ehtiyat nüsxəsi söndürülüb.
 */
class SessionStore(context: Context) {
    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context,
        "naymera_session",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var serverUrl: String
        get() = prefs.getString("server_url", "") ?: ""
        set(v) = prefs.edit().putString("server_url", v).apply()

    val accessToken: String get() = prefs.getString("access_token", "") ?: ""

    val refreshToken: String get() = prefs.getString("refresh_token", "") ?: ""

    val deviceId: String get() = prefs.getString("device_id", "") ?: ""

    var eventCursor: Long
        get() = prefs.getLong("event_cursor", 0L)
        set(v) = prefs.edit().putLong("event_cursor", v).apply()

    val isPaired: Boolean get() = refreshToken.isNotEmpty() && DeviceKey.exists()

    fun savePairing(r: PairResponse) {
        prefs.edit()
            .putString("device_id", r.deviceId)
            .putString("access_token", r.accessToken)
            .putString("refresh_token", r.refreshToken)
            .apply()
    }

    fun saveTokens(t: TokenResponse) {
        prefs.edit().putString("access_token", t.accessToken).putString("refresh_token", t.refreshToken).apply()
    }

    /** Çıxışda tokenlər və cihaz açarı silinir. */
    fun clear() {
        prefs.edit().clear().apply()
        DeviceKey.delete()
    }
}
