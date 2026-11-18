package az.naymera.app.security

import az.naymera.app.data.GoalChallenge
import az.naymera.app.data.StepChallenge
import java.security.MessageDigest

/** Klient tərəfi challenge yoxlaması: cihaz YALNIZ istifadəçiyə göstərilən əməliyyatı imzalayır. Uyğunsuzluqda imzalanmır. */
class ChallengeMismatch(val field: String) : Exception("challenge uyğunsuzluğu: $field")

/** İmzalanmazdan əvvəl istifadəçiyə AYNEN göstərilməli dəyərlər + imzalanacaq bayt-lar. */
class SignableChallenge(
    val action: String, val subject: String, val payloadHash: String, val decision: String,
    val challengeId: String, val message: ByteArray,
)

object CoreChallenge {
    const val ACTION_GOAL = "core_goal"
    const val ACTION_STEP = "core_step"
    val GOAL_TARGETS = setOf("active", "paused", "cancelled", "completed")
    val STEP_RESOLUTIONS = setOf("retry", "failed", "succeeded")

    fun sha256Hex(text: String): String =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

    fun goalPayloadHash(toStatus: String, version: Int): String = sha256Hex("$toStatus:$version")

    fun stepPayloadHash(resolution: String, attempt: Int, state: String): String = sha256Hex("$resolution:$attempt:$state")

    fun stepSubject(planId: String, stepId: String): String = "$planId/$stepId".take(128)

    private fun need(ok: Boolean, field: String) { if (!ok) throw ChallengeMismatch(field) }

    /**
     * @param expectedGoalId  istifadəçinin seçdiyi məqsəd
     * @param expectedTo      istifadəçinin seçdiyi hədəf status (ekranda göstərilir)
     * @param shownVersion    ekranda göstərilən məqsəd versiyası (köhnə ekran ⇒ server fərqli hash qaytarır ⇒ imzalanmır)
     */
    fun forGoal(ch: GoalChallenge, expectedGoalId: String, expectedTo: String, shownVersion: Int): SignableChallenge {
        need(expectedTo in GOAL_TARGETS, "to_status")
        need(ch.expiresIn > 0, "expires_in")
        need(ch.action == ACTION_GOAL, "action")
        need(ch.subject == expectedGoalId && ch.goal.id == expectedGoalId, "subject")
        need(ch.decision == expectedTo && ch.toStatus == expectedTo, "decision")
        need(ch.goal.version == shownVersion, "version")
        need(ch.payloadHash == goalPayloadHash(expectedTo, shownVersion), "payload_hash")
        return SignableChallenge(ch.action, ch.subject, ch.payloadHash, ch.decision, ch.challengeId,
            Messages.challengeMessage(ch.challengeId, ch.nonce, ch.action, ch.subject, ch.payloadHash, ch.decision))
    }

    fun forStep(ch: StepChallenge, planId: String, stepId: String, expectedResolution: String, shownAttempt: Int, shownState: String): SignableChallenge {
        need(expectedResolution in STEP_RESOLUTIONS, "resolution")
        need(ch.expiresIn > 0, "expires_in")
        need(ch.action == ACTION_STEP, "action")
        need(ch.subject == stepSubject(planId, stepId) && ch.step.planId == planId && ch.step.stepId == stepId, "subject")
        need(ch.decision == expectedResolution, "decision")
        need(ch.step.attempt == shownAttempt && ch.step.state == shownState, "step_state")
        need(ch.payloadHash == stepPayloadHash(expectedResolution, shownAttempt, shownState), "payload_hash")
        return SignableChallenge(ch.action, ch.subject, ch.payloadHash, ch.decision, ch.challengeId,
            Messages.challengeMessage(ch.challengeId, ch.nonce, ch.action, ch.subject, ch.payloadHash, ch.decision))
    }
}
