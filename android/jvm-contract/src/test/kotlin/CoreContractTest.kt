import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiException
import az.naymera.app.data.ApiTimeouts
import az.naymera.app.data.GoalChallenge
import az.naymera.app.data.GoalTransitionRequest
import az.naymera.app.data.StepResolveRequest
import az.naymera.app.security.CoreChallenge
import az.naymera.app.security.Messages
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test

/**
 * CORE API HƏQİQİ Python backend (`CoreApiApp`, loopback, `tools/jvm_contract.py`) ilə: Kotlin DTO-ları, idempotent yaratma, step-up (Java ECDSA, klient tərəfi yoxlama + server tərəfi rədd),
 * köhnə versiya/təkrar/uyğunsuz imzaların rədd edilməsi, UNKNOWN addımın həlli. `NAYMERA_BACKEND_URL` olmadıqda atlanır.
 */
class CoreContractTest {
    private val timeouts = ApiTimeouts(connectMs = 2000, readMs = 5000, writeMs = 5000, callMs = 10000, retryBaseMs = 50)

    @Before fun requireBackend() = assumeTrue("NAYMERA_BACKEND_URL təyin edilməyib", Backend.url != null)

    private class Dev(val api: ApiClient, val key: SoftwareKey)

    private companion object { @Volatile var shared: Dev? = null }

    /** Cütləşmə sürət limiti (20/dəq/ünvan) server siyasətidir və dəyişmir: addım-əsaslı testlər ortaq cihazı paylaşır; imza rədd edən testlər təzə cihaz alır. */
    private fun paired(): Dev = shared ?: pairedFresh().also { shared = it }

    private fun pairedFresh(): Dev {
        val api = ApiClient(MemoryTokenStore(Backend.url!!), timeouts)
        val key = SoftwareKey()
        val code = Backend.nextCode()
        runBlocking { api.pair(code, "JVM core", key.publicKeyB64, key.sign(Messages.pairMessage(code))) }
        return Dev(api, key)
    }

    private fun status(block: suspend () -> Unit): Int {
        try { runBlocking { block() } } catch (e: ApiException) { return e.status }
        return 200
    }

    private fun newGoal(d: Dev, title: String = "JVM məqsəd") = runBlocking { d.api.createGoal(title, "təsvir", "normal", ApiClient.newClientOp()) }.goal

    private fun transition(d: Dev, id: String, to: String, version: Int) = runBlocking {
        val ch = d.api.goalChallenge(id, to)
        val sig = CoreChallenge.forGoal(ch, id, to, version)
        d.api.goalTransition(id, GoalTransitionRequest(to, sig.challengeId, d.key.sign(sig.message), "biometric")).goal
    }

    @Test fun statusIsReadOnlyAndShowsAutonomyAndNoUnapprovedExecution() {
        val c = runBlocking { paired().api.coreStatus() }
        assertEquals("L1", c.autonomy)
        assertFalse(c.stopActive)
        assertFalse(c.executesWithoutOwnerApproval)
    }

    @Test fun unauthenticatedClientIsRejected() {
        val api = ApiClient(MemoryTokenStore(Backend.url!!), timeouts)
        assertEquals(401, status { api.coreStatus() })
        assertEquals(401, status { api.goals() })
    }

    @Test fun createIsIdempotentByClientOpAndRejectsSecretsAndBadInput() {
        val d = paired()
        val op = ApiClient.newClientOp()
        val a = runBlocking { d.api.createGoal("Eyni əməliyyat", "x", "normal", op) }
        val b = runBlocking { d.api.createGoal("Eyni əməliyyat", "x", "normal", op) }
        assertEquals(a.goal.id, b.goal.id)
        assertTrue(b.duplicate)
        assertEquals("draft", a.goal.status)
        assertEquals(1, a.goal.version)
        assertEquals(409, status { d.api.createGoal("FƏRQLİ başlıq", "x", "normal", op) })                      // eyni client_op, fərqli məzmun
        assertEquals(422, status { d.api.createGoal("açar sk-" + "Jd5K".repeat(10), "", "normal", ApiClient.newClientOp()) })
        assertEquals(400, status { d.api.createGoal("t", "", "urgent!", ApiClient.newClientOp()) })
        assertEquals(400, status { d.api.createGoal("t", "", "normal", "BAD") })
        assertTrue(runBlocking { d.api.goals() }.any { it.id == a.goal.id })
        assertEquals(a.goal.id, runBlocking { d.api.goal(a.goal.id) }.goal.id)
    }

    @Test fun goalTransitionWorksWithClientVerifiedChallengeAndLifecycleRules() {
        val d = paired()
        val g = newGoal(d)
        val active = transition(d, g.id, "active", 1)
        assertEquals("active", active.status); assertEquals(2, active.version)
        val paused = transition(d, g.id, "paused", 2)
        assertEquals("paused", paused.status)
        assertEquals(409, status { transition(d, g.id, "completed", 3) })                                       // kernel qaydası: paused -> completed yoxdur
        val cancelled = transition(d, g.id, "cancelled", 3)
        assertEquals("cancelled", cancelled.status)
    }

    @Test fun serverRejectsStaleReplayedAlteredAndForeignSignatures() {
        val d = pairedFresh()
        val g = newGoal(d)
        // 1) dəyişdirilmiş qərar: "active" challenge-i üçün "cancelled" imzası
        val ch = runBlocking { d.api.goalChallenge(g.id, "active") }
        val bad = d.key.sign(Messages.challengeMessage(ch.challengeId, ch.nonce, ch.action, ch.subject, ch.payloadHash, "cancelled"))
        assertEquals(403, status { d.api.goalTransition(g.id, GoalTransitionRequest("active", ch.challengeId, bad, "biometric")) })
        // challenge yanıb: düzgün imza ilə də təkrar olmur
        val good = d.key.sign(Messages.challengeMessage(ch.challengeId, ch.nonce, ch.action, ch.subject, ch.payloadHash, ch.decision))
        assertTrue(status { d.api.goalTransition(g.id, GoalTransitionRequest("active", ch.challengeId, good, "biometric")) } in listOf(403, 409))
        // 2) uğurlu keçid və təkrar (replay)
        val ch2 = runBlocking { d.api.goalChallenge(g.id, "active") }
        val sig2 = CoreChallenge.forGoal(ch2, g.id, "active", 1)
        val req = GoalTransitionRequest("active", sig2.challengeId, d.key.sign(sig2.message), "biometric")
        assertEquals("active", runBlocking { d.api.goalTransition(g.id, req) }.goal.status)
        assertTrue(status { d.api.goalTransition(g.id, req) } in listOf(403, 409))
        // 3) köhnə versiya: ekran v1 göstərir, server artıq v2 — klient imzalamır
        val ch3 = runBlocking { d.api.goalChallenge(g.id, "paused") }
        try { CoreChallenge.forGoal(ch3, g.id, "paused", 1); fail("köhnə versiya imzalanmamalı idi") } catch (e: az.naymera.app.security.ChallengeMismatch) { assertEquals("version", e.field) }
        // 4) səs metodu və başqa cihaz
        val sig3 = CoreChallenge.forGoal(ch3, g.id, "paused", 2)
        assertEquals(403, status { d.api.goalTransition(g.id, GoalTransitionRequest("paused", sig3.challengeId, d.key.sign(sig3.message), "voice")) })
        val other = pairedFresh()
        val ch4 = runBlocking { d.api.goalChallenge(g.id, "paused") }
        val sig4 = CoreChallenge.forGoal(ch4, g.id, "paused", 2)
        assertEquals(403, status { other.api.goalTransition(g.id, GoalTransitionRequest("paused", sig4.challengeId, other.key.sign(sig4.message), "biometric")) }) // başqa cihazın tokeni+açarı
        assertEquals("active", runBlocking { d.api.goal(g.id) }.goal.status)
    }

    @Test fun unknownStepIsResolvedOnlyWithAVerifiedDeviceSignature() {
        val gid = System.getenv("NAYMERA_SEED_GOAL"); val plan = System.getenv("NAYMERA_SEED_PLAN"); val step = System.getenv("NAYMERA_SEED_STEP")
        assumeTrue("seed yoxdur", gid != null && plan != null && step != null)
        val d = pairedFresh()
        val detail = runBlocking { d.api.goal(gid!!) }
        val rec = detail.plan!!.steps.first { it.id == step }
        assertEquals("unknown", rec.state)
        assertTrue(rec.untrustedOutput)                                                                       // server mətni etibarsız kimi işarələnib
        val ch = runBlocking { d.api.stepChallenge(plan!!, step!!, "retry") }
        // klient tərəfi: ekran səhv attempt göstərərsə imzalanmır
        try { CoreChallenge.forStep(ch, plan, step, "retry", rec.attempt + 1, rec.state); fail("imzalanmamalı") } catch (e: az.naymera.app.security.ChallengeMismatch) { assertEquals("step_state", e.field) }
        val sig = CoreChallenge.forStep(ch, plan, step, "retry", rec.attempt, rec.state)
        // başqa qərarla göndərmək rədd olunur
        assertEquals(403, status { d.api.stepResolve(plan, step, StepResolveRequest("failed", sig.challengeId, d.key.sign(sig.message), "biometric")) })
        val ch2 = runBlocking { d.api.stepChallenge(plan!!, step!!, "retry") }
        val sig2 = CoreChallenge.forStep(ch2, plan, step, "retry", rec.attempt, rec.state)
        assertTrue(runBlocking { d.api.stepResolve(plan, step, StepResolveRequest("retry", sig2.challengeId, d.key.sign(sig2.message), "biometric")) }.ok)
        assertEquals("pending", runBlocking { d.api.goal(gid!!) }.plan!!.steps.first { it.id == step }.state)
        assertTrue(runBlocking { d.api.journal(gid) }.isNotEmpty())
        assertNotNull(runBlocking { d.api.awaitingSteps() })
    }
}
