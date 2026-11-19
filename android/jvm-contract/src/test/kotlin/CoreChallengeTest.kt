import az.naymera.app.data.GoalChallenge
import az.naymera.app.data.GoalDto
import az.naymera.app.data.StepChallenge
import az.naymera.app.data.StepRef
import az.naymera.app.security.ChallengeMismatch
import az.naymera.app.security.CoreChallenge
import az.naymera.app.security.Messages
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

/** Backend-siz: klient tərəfi challenge yoxlaması + Python ilə eyni vektorlar (`core_vectors.json`, `tests/test_core_signing_flow.py` onu canlı hesablamaya qarşı yoxlayır). */
class CoreChallengeTest {
    private val vectors: List<JsonObject> =
        Json.parseToJsonElement(javaClass.getResourceAsStream("/core_vectors.json")!!.bufferedReader().readText())
            .jsonObject["vectors"]!!.jsonArray.map { it.jsonObject }

    private fun JsonObject.s(k: String) = this[k]!!.jsonPrimitive.content
    private fun JsonObject.i(k: String) = this[k]!!.jsonPrimitive.int

    private fun goal(id: String, v: Int) = GoalDto(id = id, title = "t", status = "draft", version = v)

    private fun goalChallenge(v: JsonObject) = GoalChallenge(v.s("challenge_id"), v.s("nonce"), 60, v.s("action"), v.s("subject"), v.s("payload_hash"), v.s("decision"),
        goal(v.s("subject"), v.i("version")), v.s("decision"))

    private fun stepChallenge(v: JsonObject) = StepChallenge(v.s("challenge_id"), v.s("nonce"), 60, v.s("action"), v.s("subject"), v.s("payload_hash"), v.s("decision"),
        StepRef(v.s("plan_id"), v.s("step_id"), v.s("state"), v.i("attempt")))

    private fun rejects(field: String, block: () -> Unit) {
        try { block() } catch (e: ChallengeMismatch) { assertEquals(field, e.field); return }
        fail("ChallengeMismatch($field) gözlənilirdi")
    }

    @Test fun hashesAndMessagesMatchThePythonVectors() {
        for (v in vectors) {
            if (v.s("action") == "core_goal") assertEquals(v.s("payload_hash"), CoreChallenge.goalPayloadHash(v.s("decision"), v.i("version")))
            else assertEquals(v.s("payload_hash"), CoreChallenge.stepPayloadHash(v.s("decision"), v.i("attempt"), v.s("state")))
            val bytes = Messages.challengeMessage(v.s("challenge_id"), v.s("nonce"), v.s("action"), v.s("subject"), v.s("payload_hash"), v.s("decision"))
            assertEquals(v.s("message"), String(bytes, Charsets.UTF_8))
        }
    }

    @Test fun honestGoalAndStepChallengesProduceTheExactSignedMessage() {
        for (v in vectors) {
            val sig = if (v.s("action") == "core_goal") CoreChallenge.forGoal(goalChallenge(v), v.s("subject"), v.s("decision"), v.i("version"))
            else CoreChallenge.forStep(stepChallenge(v), v.s("plan_id"), v.s("step_id"), v.s("decision"), v.i("attempt"), v.s("state"))
            assertArrayEquals(v.s("message").toByteArray(Charsets.UTF_8), sig.message)
            // İstifadəçiyə göstərilməli dəyərlər imzalanan baytlarla eynidir
            assertEquals(v.s("action") + "|" + v.s("subject") + "|" + v.s("payload_hash") + "|" + v.s("decision"),
                listOf(sig.action, sig.subject, sig.payloadHash, sig.decision).joinToString("|"))
        }
    }

    @Test fun goalChallengeAlteredByTheServerOrStaleUiIsNotSigned() {
        val v = vectors.first { it.s("action") == "core_goal" && it.s("decision") == "active" }
        val gid = v.s("subject")
        val ok = goalChallenge(v)
        rejects("action") { CoreChallenge.forGoal(ok.copy(action = "approval"), gid, "active", 1) }
        rejects("subject") { CoreChallenge.forGoal(ok.copy(subject = gid + "x"), gid, "active", 1) }
        rejects("subject") { CoreChallenge.forGoal(ok, "goal_other00000000000", "active", 1) }       // başqa məqsədi imzalatmağa cəhd
        rejects("decision") { CoreChallenge.forGoal(ok.copy(decision = "cancelled"), gid, "active", 1) }
        rejects("decision") { CoreChallenge.forGoal(ok.copy(toStatus = "cancelled"), gid, "active", 1) }
        rejects("payload_hash") { CoreChallenge.forGoal(ok.copy(payloadHash = CoreChallenge.goalPayloadHash("active", 2)), gid, "active", 1) }
        rejects("version") { CoreChallenge.forGoal(ok.copy(goal = goal(gid, 2), payloadHash = CoreChallenge.goalPayloadHash("active", 2)), gid, "active", 1) } // ekran v1 göstərir, server v2
        rejects("to_status") { CoreChallenge.forGoal(ok, gid, "draft", 1) }                         // draft hədəf deyil
        rejects("expires_in") { CoreChallenge.forGoal(ok.copy(expiresIn = 0), gid, "active", 1) }
    }

    @Test fun stepChallengeAlteredByTheServerOrStaleUiIsNotSigned() {
        val v = vectors.first { it.s("action") == "core_step" && it.s("decision") == "retry" }
        val (p, s) = v.s("plan_id") to v.s("step_id")
        val ok = stepChallenge(v)
        rejects("action") { CoreChallenge.forStep(ok.copy(action = "core_goal"), p, s, "retry", 1, "unknown") }
        rejects("subject") { CoreChallenge.forStep(ok.copy(subject = "$p/other"), p, s, "retry", 1, "unknown") }
        rejects("subject") { CoreChallenge.forStep(ok, p, "other", "retry", 1, "unknown") }
        rejects("decision") { CoreChallenge.forStep(ok.copy(decision = "succeeded"), p, s, "retry", 1, "unknown") }
        rejects("payload_hash") { CoreChallenge.forStep(ok.copy(payloadHash = CoreChallenge.stepPayloadHash("retry", 2, "unknown")), p, s, "retry", 1, "unknown") }
        rejects("step_state") { CoreChallenge.forStep(ok.copy(step = ok.step.copy(attempt = 2)), p, s, "retry", 1, "unknown") }       // ekran attempt=1, server 2
        rejects("step_state") { CoreChallenge.forStep(ok.copy(step = ok.step.copy(state = "failed")), p, s, "retry", 1, "unknown") }
        rejects("resolution") { CoreChallenge.forStep(ok, p, s, "explode", 1, "unknown") }
    }

    @Test fun overlongSubjectIsTruncatedLikeTheServer() {
        val p = "plan_" + "a".repeat(100)
        val sub = CoreChallenge.stepSubject(p, "b".repeat(100))
        assertEquals(128, sub.length)
        assertEquals((p + "/" + "b".repeat(100)).take(128), sub)
    }
}
