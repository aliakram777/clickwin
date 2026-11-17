package az.naymera.app.security

import android.util.Base64
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine

/**
 * Həssas əməliyyat təsdiqi (step-up) və cütləşmə sahibliyi sübutu. İmza YALNIZ uğurlu güclü biometrik təsdiqdən sonra, Keystore
 * açarı ilə (`DeviceKey`, `BiometricPrompt.CryptoObject`) yaranır; açar cihazdan çıxmır. Server imzanı açıq açarla yoxlayır
 * (api/auth.py): imza action, subject, payload_hash və qərara bağlıdır. Metod həmişə "biometric"dir; söhbət mətni və ya başqa
 * mənbə bu yola çıxmır.
 */
object StepUp {
    const val METHOD = "biometric"

    fun canAuthenticate(activity: FragmentActivity): Boolean =
        BiometricManager.from(activity).canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) ==
            BiometricManager.BIOMETRIC_SUCCESS

    /** Server tərəfdə `AuthService.step_up_message` ilə eyni mesaj (`Messages`). */
    fun challengeMessage(
        challengeId: String, nonce: String, action: String, subject: String, payloadHash: String, decision: String,
    ): ByteArray = Messages.challengeMessage(challengeId, nonce, action, subject, payloadHash, decision)

    /** Biometrik təsdiq + imza. Ləğv/xəta/açar etibarsızlığı halında null; base64 (NO_WRAP) DER imza qaytarır. */
    suspend fun sign(
        activity: FragmentActivity, title: String, subtitle: String, cancel: String, message: ByteArray,
    ): String? = suspendCancellableCoroutine { cont ->
        val crypto = try { DeviceKey.cryptoObject() } catch (e: Exception) {
            cont.resume(null)
            return@suspendCancellableCoroutine
        }
        val executor = ContextCompat.getMainExecutor(activity)
        val prompt = BiometricPrompt(activity, executor, object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                val out = try {
                    Base64.encodeToString(DeviceKey.signWith(result.cryptoObject ?: crypto, message), Base64.NO_WRAP)
                } catch (e: Exception) { null }
                if (cont.isActive) cont.resume(out)
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                if (cont.isActive) cont.resume(null)
            }
        })
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(title)
            .setSubtitle(subtitle)
            .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
            .setNegativeButtonText(cancel)
            .build()
        prompt.authenticate(info, crypto)
        cont.invokeOnCancellation { prompt.cancelAuthentication() }
    }
}
