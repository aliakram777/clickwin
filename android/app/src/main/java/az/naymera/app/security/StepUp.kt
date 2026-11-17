package az.naymera.app.security

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

/**
 * Həssas əməliyyat təsdiqi (step-up). Cihaz sirri ilə HMAC sübutu YALNIZ uğurlu biometrik/cihaz kilidi açılışından sonra
 * hesablanır. Server sübutu yoxlayır (api/auth.py): sübut əməliyyatın action, subject, payload_hash və qərarına bağlıdır.
 * Metod həmişə "biometric"dir; başqa mənbə (söhbət mətni və s.) bu yola çıxmır.
 */
object StepUp {
    const val METHOD = "biometric"

    private const val AUTHENTICATORS =
        BiometricManager.Authenticators.BIOMETRIC_STRONG or BiometricManager.Authenticators.DEVICE_CREDENTIAL

    fun canAuthenticate(activity: FragmentActivity): Boolean =
        BiometricManager.from(activity).canAuthenticate(AUTHENTICATORS) == BiometricManager.BIOMETRIC_SUCCESS

    /** Uğurlu açılışdan sonra true; ləğv/xəta halında false. */
    suspend fun confirm(activity: FragmentActivity, title: String, subtitle: String): Boolean =
        suspendCancellableCoroutine { cont ->
            val executor = ContextCompat.getMainExecutor(activity)
            val prompt = BiometricPrompt(activity, executor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    if (cont.isActive) cont.resume(true)
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    if (cont.isActive) cont.resume(false)
                }
            })
            val info = BiometricPrompt.PromptInfo.Builder()
                .setTitle(title)
                .setSubtitle(subtitle)
                .setAllowedAuthenticators(AUTHENTICATORS)
                .build()
            prompt.authenticate(info)
            cont.invokeOnCancellation { prompt.cancelAuthentication() }
        }

    /** HMAC-SHA256(device_secret, challenge_id|nonce|action|subject|payload_hash|decision) — server ilə eyni format. */
    fun proof(
        deviceSecretHex: String, challengeId: String, nonce: String, action: String, subject: String,
        payloadHash: String, decision: String,
    ): String {
        val key = SecretKeySpec(hexToBytes(deviceSecretHex), "HmacSHA256")
        val mac = Mac.getInstance("HmacSHA256").apply { init(key) }
        val msg = listOf(challengeId, nonce, action, subject, payloadHash, decision).joinToString("|")
        return mac.doFinal(msg.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }

    private fun hexToBytes(hex: String): ByteArray {
        require(hex.length % 2 == 0) { "yanlış hex" }
        return ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }
}
