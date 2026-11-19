@file:Suppress("unused", "UNUSED_PARAMETER")
package az.naymera.app.security

import androidx.fragment.app.FragmentActivity

/** Həqiqi `StepUp`/`DeviceKey` Android Keystore/BiometricPrompt-a bağlıdır (stub: yalnız imza uyğunluğu). */
object StepUp {
    const val METHOD = "biometric"
    fun canAuthenticate(activity: FragmentActivity): Boolean = false
    fun challengeMessage(challengeId: String, nonce: String, action: String, subject: String, payloadHash: String, decision: String): ByteArray = ByteArray(0)
    suspend fun sign(activity: FragmentActivity, title: String, subtitle: String, cancel: String, message: ByteArray): String? = null
}

object DeviceKey {
    fun exists(): Boolean = false
    fun delete() {}
    fun generate(): ByteArray = ByteArray(0)
    fun publicKeySpki(): ByteArray = ByteArray(0)
    fun pairMessage(code: String): ByteArray = ByteArray(0)
}
