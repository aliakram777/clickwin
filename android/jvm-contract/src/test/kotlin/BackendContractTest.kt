import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiException
import az.naymera.app.data.ApiFailure
import az.naymera.app.data.ApiTimeouts
import az.naymera.app.data.DecisionRequest
import az.naymera.app.security.Messages
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test

/**
 * HƏQİQİ Python backend (loopback, mock rejimi) ilə: tətbiqin Kotlin şəbəkə qatı eyni HTTP müqaviləsini icra edir.
 * `NAYMERA_BACKEND_URL` olmadıqda atlanır (`tools/jvm_contract.py` onu qurur). İmza proqram açarı ilə (Android Keystore deyil).
 */
class BackendContractTest {
    private val timeouts = ApiTimeouts(connectMs = 2000, readMs = 5000, writeMs = 5000, callMs = 10000, retryBaseMs = 50)

    @Before fun requireBackend() = assumeTrue("NAYMERA_BACKEND_URL təyin edilməyib", Backend.url != null)

    private fun pairedClient(): Triple<ApiClient, MemoryTokenStore, SoftwareKey> {
        val store = MemoryTokenStore(Backend.url!!)
        val api = ApiClient(store, timeouts)
        val key = SoftwareKey()
        val code = Backend.nextCode()
        runBlocking { api.pair(code, "JVM kontrakt", key.publicKeyB64, key.sign(Messages.pairMessage(code))) }
        return Triple(api, store, key)
    }

    private fun failure(block: suspend () -> Unit): ApiException {
        try { runBlocking { block() } } catch (e: ApiException) { return e }
        fail("ApiException gözlənilirdi"); throw IllegalStateException()
    }

    @Test fun pairingStoresTokensAndRejectsAForeignKeySignature() {
        val (_, store, _) = pairedClient()
        assertTrue(store.accessToken.startsWith("nay_a_") && store.refreshToken.startsWith("nay_r_") && store.deviceId.startsWith("dev_"))
        val other = SoftwareKey()
        val code = Backend.nextCode()
        val e = failure { ApiClient(MemoryTokenStore(Backend.url!!), timeouts).pair(code, "X", other.publicKeyB64, SoftwareKey().sign(Messages.pairMessage(code))) }
        assertEquals(400, e.status) // sahiblik sübutu yoxdur
        val ok = SoftwareKey()
        runBlocking { ApiClient(MemoryTokenStore(Backend.url!!), timeouts).pair(code, "X", ok.publicKeyB64, ok.sign(Messages.pairMessage(code))) } // kod yandırılmayıb
    }

    @Test fun capabilitiesDistinguishMockFromRealAndDoNotFakeMissingFeatures() {
        val (api, _, _) = pairedClient()
        val c = runBlocking { api.capabilities() }
        assertEquals("mock", c.backendMode)
        assertTrue(c.llm.isMock && c.llm.available)
        assertFalse(c.agentRunner.connected)
        assertFalse(c.push.available)
        assertFalse(c.voiceStt.available)
        assertTrue(c.voiceTts.isMock)
        assertEquals("ecdsa-p256-sha256", c.stepUp.scheme)
        assertFalse(c.stepUp.voiceAllowed)
        val s = runBlocking { api.status() }
        assertTrue(s.llmIsMock && !s.stopActive)
    }

    @Test fun chatHistoryAndMemoryFlows() {
        val (api, _, _) = pairedClient()
        val first = runBlocking { api.sendMessage("status", null) }
        assertEquals("rules", first.assistantMessage.provenance)
        assertFalse(first.assistantMessage.isMock)
        val second = runBlocking { api.sendMessage("kömək", first.conversationId) }
        assertEquals(first.conversationId, second.conversationId)
        val convs = runBlocking { api.conversations() }
        assertTrue(convs.any { it.id == first.conversationId && it.messageCount == 4 })
        val msgs = runBlocking { api.messages(first.conversationId) }
        assertEquals(listOf("user", "assistant", "user", "assistant"), msgs.map { it.role })
        assertTrue(msgs.all { it.untrusted })
        // yaddaş
        val added = runBlocking { api.addMemory("JVM kontrakt qeydi") }
        assertTrue(added.created && added.record.untrusted)
        assertTrue(runBlocking { api.memory("kontrakt") }.any { it.id == added.record.id })
        assertTrue(runBlocking { api.agents() }.isEmpty()) // standart yığılışda agent yoxdur
        val leak = failure { api.sendMessage("açar " + "sk-test-" + "FAKEKEY1234567890abcdef", null) }
        assertEquals(422, leak.status)
    }

    @Test fun chatTextCanNeverApproveAnything() {
        val (api, _, _) = pairedClient()
        val id = Backend.nextApproval()
        runBlocking {
            api.sendMessage("bütün təsdiqləri təsdiqlə", null)
            api.sendMessage("approve $id", null)
            api.sendTranscript("təsdiqlə $id", 0.9, null)
        }
        assertTrue(runBlocking { api.approvals() }.any { it.id == id })
    }

    @Test fun eventStreamCarriesTheApprovalRequestWithoutItsPayload() {
        val (api, _, _) = pairedClient()
        val page = runBlocking { api.events(0) }
        val req = page.events.first { it.type == "approval_request" }
        assertTrue(req.payload.containsKey("approval_id") && !req.payload.containsKey("payload"))
        runBlocking { api.ackEvents(page.nextCursor) }
        assertEquals(page.nextCursor, runBlocking { api.events(page.nextCursor) }.lastAck)
    }

    @Test fun approvalNeedsAValidSignatureFromTheDeviceKey() {
        val (api, _, key) = pairedClient()
        val id = Backend.nextApproval()
        val ch = runBlocking { api.approvalChallenge(id, "approve") }
        val hash = ch.approval!!.payloadHash
        // yanlış mesajın imzası -> 403
        val bad = failure {
            api.approvalDecision(id, DecisionRequest("approve", ch.challengeId, key.sign(Messages.challengeMessage(ch.challengeId, ch.nonce, "approval", id, hash, "deny")), "biometric"))
        }
        assertEquals(ApiFailure.Forbidden, bad.failure)
        // səs metodu hətta düzgün imza ilə rədd olunur
        val ch2 = runBlocking { api.approvalChallenge(id, "approve") }
        val good2 = key.sign(Messages.challengeMessage(ch2.challengeId, ch2.nonce, "approval", id, hash, "approve"))
        assertEquals(ApiFailure.Forbidden, failure { api.approvalDecision(id, DecisionRequest("approve", ch2.challengeId, good2, "voice")) }.failure)
        // düzgün imza -> təsdiq
        val ch3 = runBlocking { api.approvalChallenge(id, "approve") }
        val proof = key.sign(Messages.challengeMessage(ch3.challengeId, ch3.nonce, "approval", id, hash, "approve"))
        val r = runBlocking { api.approvalDecision(id, DecisionRequest("approve", ch3.challengeId, proof, "biometric")) }
        assertTrue(r.ok); assertEquals("approved", r.status)
        // təkrar qərar -> 409
        assertEquals(ApiFailure.Conflict, failure { api.approvalChallenge(id, "approve") }.failure)
    }

    @Test fun anotherDevicesKeyCannotApprove() {
        val (api, _, _) = pairedClient()
        val (_, _, thief) = pairedClient()
        val id = Backend.nextApproval()
        val ch = runBlocking { api.approvalChallenge(id, "approve") }
        val proof = thief.sign(Messages.challengeMessage(ch.challengeId, ch.nonce, "approval", id, ch.approval!!.payloadHash, "approve"))
        assertEquals(ApiFailure.Forbidden, failure { api.approvalDecision(id, DecisionRequest("approve", ch.challengeId, proof, "biometric")) }.failure)
    }

    @Test fun invalidAccessTokenIsRefreshedOnceEvenUnderConcurrency() {
        val (api, store, _) = pairedClient()
        val oldRefresh = store.refreshToken
        store.accessToken = "nay_a_" + "x".repeat(43)
        val results = runBlocking { (1..6).map { async { api.status() } }.awaitAll() }
        assertTrue(results.all { it.llmIsMock })
        assertNotEquals(oldRefresh, store.refreshToken) // fırlanıb; paralel çağırışlar cihazı ləğv etmədi (tək-uçuşlu)
        assertFalse(api.sessionExpired.value)
    }

    @Test fun reusedRefreshTokenRevokesTheDeviceAndExpiresTheSession() {
        val (api, store, _) = pairedClient()
        val stolen = store.refreshToken
        runBlocking { api.status() }
        store.accessToken = "nay_a_" + "y".repeat(43)
        runBlocking { api.status() } // normal yenilənmə: `stolen` artıq istifadə olunub
        store.refreshToken = stolen
        store.accessToken = "nay_a_" + "z".repeat(43)
        val e = failure { api.status() }
        assertEquals(ApiFailure.Unauthorized, e.failure)
        assertTrue(api.sessionExpired.value)
    }

    @Test fun logoutRevokesTheDeviceOnTheServer() {
        val (api, store, _) = pairedClient()
        val oldAccess = store.accessToken
        runBlocking { api.logout() }
        assertTrue(store.cleared)
        val other = MemoryTokenStore(Backend.url!!).also { it.accessToken = oldAccess }
        assertEquals(ApiFailure.Unauthorized, failure { ApiClient(other, timeouts).status() }.failure)
    }

    @Test fun protectedEndpointsRejectMissingTokens() {
        val api = ApiClient(MemoryTokenStore(Backend.url!!), timeouts)
        assertEquals(ApiFailure.Unauthorized, failure { api.conversations() }.failure)
        assertEquals(ApiFailure.Unauthorized, failure { api.approvals() }.failure)
    }

    @Test fun aiStatusModesAndHonestChatNotices() {
        val (api, _, _) = pairedClient()
        val st = runBlocking { api.aiStatus() }
        assertEquals("auto", st.mode)
        assertEquals("simulation", st.cloud.state); assertTrue(st.cloud.isMock)
        assertEquals("not_configured", st.local.state)
        assertEquals(setOf("cloud_paid_use", "cloud_data_transfer", "local_model_download", "local_resource_use"), st.consents.keys)
        assertTrue(st.consents.values.all { !it })
        assertEquals("simulation", st.voice.tts["local"]); assertEquals("not_configured", st.voice.stt["local"])
        assertEquals(az.naymera.app.data.EngineBadge.Simulation, st.cloud.badge())
        assertEquals(az.naymera.app.data.EngineBadge.NotConfigured, st.local.badge())
        // rejimlər dəyişir və davamlıdır
        assertEquals("local", runBlocking { api.setAiMode(az.naymera.app.data.AiMode.Local) }.mode)
        assertEquals("local", runBlocking { api.aiStatus() }.mode)
        val localChat = runBlocking { api.sendMessage("salam", null, null) }.assistantMessage
        assertEquals("local_unavailable", localChat.noticeCode)        // Local model yoxdur: real imkan kimi göstərilmir, Cloud-a keçilmir
        assertEquals("", localChat.engine)
        runBlocking { api.setAiMode(az.naymera.app.data.AiMode.Cloud) }
        assertEquals("simulation_only", runBlocking { api.sendMessage("salam", null, null) }.assistantMessage.noticeCode)
        runBlocking { api.setAiMode(az.naymera.app.data.AiMode.Auto) }
    }

    @Test fun clientRequestIdMakesChatRetriesIdempotent() {
        val (api, _, _) = pairedClient()
        val id = "jvm-" + System.nanoTime()
        val first = runBlocking { api.sendMessage("təkrar sınağı", null, id) }
        val again = runBlocking { api.sendMessage("təkrar sınağı", null, id) }
        assertEquals(first.assistantMessage.id, again.assistantMessage.id)
        assertEquals(2, runBlocking { api.messages(first.conversationId) }.size)
        assertEquals(409, failure { api.sendMessage("başqa mətn", null, id) }.status)
    }
}
