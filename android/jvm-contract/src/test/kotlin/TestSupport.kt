import az.naymera.app.data.PairResponse
import az.naymera.app.data.TokenResponse
import az.naymera.app.data.TokenStore
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64

class MemoryTokenStore(override var serverUrl: String = "") : TokenStore {
    override var accessToken: String = ""
    override var refreshToken: String = ""
    override var eventCursor: Long = 0L
    var deviceId: String = ""
    var cleared = false

    override fun savePairing(r: PairResponse) {
        deviceId = r.deviceId
        accessToken = r.accessToken
        refreshToken = r.refreshToken
    }

    override fun saveTokens(t: TokenResponse) {
        accessToken = t.accessToken
        refreshToken = t.refreshToken
    }

    override fun clear() {
        accessToken = ""
        refreshToken = ""
        cleared = true
    }
}

/** Android Keystore simulyasiyası (proqram açarı): P-256, SHA256withECDSA, DER imza, X.509 açıq açar — Keystore ilə eyni formatlar. */
class SoftwareKey {
    private val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
    val publicKeyB64: String = Base64.getEncoder().encodeToString(pair.public.encoded)

    fun sign(message: ByteArray): String {
        val sig = Signature.getInstance("SHA256withECDSA").apply { initSign(pair.private); update(message) }
        return Base64.getEncoder().encodeToString(sig.sign())
    }
}

object Backend {
    val url: String? = System.getenv("NAYMERA_BACKEND_URL")
    private val codes = (System.getenv("NAYMERA_PAIR_CODES") ?: "").split(",").filter { it.isNotBlank() }.toMutableList()
    private val approvals = (System.getenv("NAYMERA_APPROVAL_IDS") ?: "").split(",").filter { it.isNotBlank() }.toMutableList()

    @Synchronized fun nextCode(): String = codes.removeAt(0)

    @Synchronized fun nextApproval(): String = approvals.removeAt(0)
}
