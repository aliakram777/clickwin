package az.naymera.app.security

/**
 * İmzalanan mesajların formatı (Android-dan asılı deyil). Server tərəfdəki `AuthService.pair_message` və `step_up_message` ilə
 * EYNİ olmalıdır; dillərarası vektorlar `tests/test_api_auth.py` və `jvm-contract/MessagesTest.kt`-dədir.
 */
object Messages {
    fun pairMessage(code: String): ByteArray = "naymera-pair|$code".toByteArray(Charsets.UTF_8)

    fun challengeMessage(
        challengeId: String, nonce: String, action: String, subject: String, payloadHash: String, decision: String,
    ): ByteArray =
        listOf("naymera-step-up", challengeId, nonce, action, subject, payloadHash, decision)
            .joinToString("|").toByteArray(Charsets.UTF_8)
}
