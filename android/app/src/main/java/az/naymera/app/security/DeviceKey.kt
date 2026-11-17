package az.naymera.app.security

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import androidx.biometric.BiometricPrompt
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * Cihaz açarı: Android Keystore-da yaradılan P-256 (secp256r1) açar cütü. Şəxsi açar çıxarıla bilməz; hər imza üçün
 * biometrik təsdiq tələb olunur (`setUserAuthenticationRequired(true)`, hər istifadədə) və yeni barmaq izi əlavə olunduqda
 * açar etibarsız olur. Serverə YALNIZ açıq açar (X.509 SubjectPublicKeyInfo) göndərilir. İmza: SHA256withECDSA (DER).
 */
object DeviceKey {
    private const val ALIAS = "naymera_device_key"
    private const val PROVIDER = "AndroidKeyStore"

    private fun keyStore(): KeyStore = KeyStore.getInstance(PROVIDER).apply { load(null) }

    fun exists(): Boolean = try { keyStore().containsAlias(ALIAS) } catch (e: Exception) { false }

    fun delete() {
        try { keyStore().deleteEntry(ALIAS) } catch (_: Exception) {}
    }

    private fun spec(strongBox: Boolean): KeyGenParameterSpec {
        val b = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)
            .setUserAuthenticationRequired(true)
            .setInvalidatedByBiometricEnrollment(true)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            // Hər imza üçün güclü biometrik təsdiq (müddət 0 = hər istifadədə).
            b.setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
        } else {
            @Suppress("DEPRECATION")
            b.setUserAuthenticationValidityDurationSeconds(-1)
        }
        if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) b.setIsStrongBoxBacked(true)
        return b.build()
    }

    /** Köhnə açarı silib yenisini yaradır; açıq açarı (SPKI) qaytarır. Şəxsi açar heç vaxt oxunmur. */
    fun generate(): ByteArray {
        delete()
        val gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, PROVIDER)
        try {
            gen.initialize(spec(strongBox = true))
            gen.generateKeyPair()
        } catch (e: StrongBoxUnavailableException) {
            gen.initialize(spec(strongBox = false))
            gen.generateKeyPair()
        }
        return publicKeySpki()
    }

    fun publicKeySpki(): ByteArray = keyStore().getCertificate(ALIAS).publicKey.encoded

    /** İmza obyekti (hələ biometrik təsdiqlənməyib): `BiometricPrompt.CryptoObject` ilə açılır. */
    fun newSignature(): Signature {
        val key = keyStore().getKey(ALIAS, null) as PrivateKey
        return Signature.getInstance("SHA256withECDSA").apply { initSign(key) }
    }

    fun cryptoObject(): BiometricPrompt.CryptoObject = BiometricPrompt.CryptoObject(newSignature())

    /** Server tərəfdə `AuthService.pair_message` ilə eyni mesaj. */
    fun pairMessage(code: String): ByteArray = "naymera-pair|$code".toByteArray(Charsets.UTF_8)

    /** Biometrik təsdiqdən sonra açılmış `Signature` ilə imzalayır. */
    fun signWith(crypto: BiometricPrompt.CryptoObject, message: ByteArray): ByteArray {
        val sig = requireNotNull(crypto.signature) { "no signature" }
        sig.update(message)
        return sig.sign()
    }
}
