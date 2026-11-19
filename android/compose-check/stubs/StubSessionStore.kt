@file:Suppress("unused", "UNUSED_PARAMETER")
package az.naymera.app.data

import androidx.fragment.app.Context

class SessionStore(context: Context) : TokenStore {
    override var serverUrl: String = ""
    override val accessToken: String = ""
    override val refreshToken: String = ""
    override var eventCursor: Long = 0L
    val deviceId: String = ""
    val isPaired: Boolean = false
    override fun savePairing(r: PairResponse) {}
    override fun saveTokens(t: TokenResponse) {}
    override fun clear() {}
}
