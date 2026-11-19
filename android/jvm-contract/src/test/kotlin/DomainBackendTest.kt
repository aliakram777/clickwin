import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiTimeouts
import az.naymera.app.domain.ActionResult
import az.naymera.app.domain.ApiCoreRepository
import az.naymera.app.domain.ApprovalsModel
import az.naymera.app.domain.DashboardModel
import az.naymera.app.domain.GoalAction
import az.naymera.app.domain.GoalsModel
import az.naymera.app.domain.JournalModel
import az.naymera.app.domain.Load
import az.naymera.app.domain.Signer
import az.naymera.app.security.Messages
import az.naymera.app.security.SignableChallenge
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test

/** M2 ViewModel qatı HƏQİQİ Python backend ilə (`ApiCoreRepository` → M1 `ApiClient`): imza proqram açarı ilə (cihazda Keystore/BiometricPrompt). */
class DomainBackendTest {
    @Before fun requireBackend() = assumeTrue("NAYMERA_BACKEND_URL təyin edilməyib", Backend.url != null)

    private class KeySigner(val key: SoftwareKey) : Signer {
        val shown = mutableListOf<SignableChallenge>()
        override fun available() = true
        override suspend fun sign(challenge: SignableChallenge): String { shown += challenge; return key.sign(challenge.message) }
    }

    @Test fun goalLifecycleApprovalsDashboardAndJournalWorkEndToEnd() = runBlocking {
        val store = MemoryTokenStore(Backend.url!!)
        val api = ApiClient(store, ApiTimeouts(2000, 5000, 5000, 10000, 50))
        val key = SoftwareKey(); val code = Backend.nextCode()
        api.pair(code, "M2 domain", key.publicKeyB64, key.sign(Messages.pairMessage(code)))
        val repo = ApiCoreRepository(api); val signer = KeySigner(key)

        val goals = GoalsModel(repo)
        assertEquals(ActionResult.Done, goals.create("M2 məqsəd", "təsvir"))
        val g = (goals.list.value as Load.Content).data.first { it.title == "M2 məqsəd" }
        assertEquals("draft", g.status)
        goals.open(g.id)
        var d = (goals.detail.value as Load.Content).data
        assertEquals(ActionResult.Done, goals.prepareTransition(g.id, GoalAction.Start, d.goal.version, d.goal.status))
        assertTrue(signer.shown.isEmpty())                                                                 // hazırlıq imza yaratmır
        assertEquals(ActionResult.Done, goals.confirm(signer))
        d = (goals.detail.value as Load.Content).data
        assertEquals("active", d.goal.status)
        assertEquals(ActionResult.Done, goals.prepareTransition(g.id, GoalAction.Pause, d.goal.version, d.goal.status))
        assertEquals(ActionResult.Done, goals.confirm(signer))
        assertEquals("paused", (goals.detail.value as Load.Content).data.goal.status)
        assertEquals(listOf("core_goal", "core_goal"), signer.shown.map { it.action })

        val approvals = ApprovalsModel(repo)
        val aid = Backend.nextApproval()
        approvals.refresh()
        val a = (approvals.items.value as Load.Content).data.approvals.first { it.id == aid }
        assertEquals(ActionResult.Done, approvals.prepare(a, "deny"))
        assertEquals("approval", approvals.pending.value!!.challenge.action)
        assertEquals(ActionResult.Done, approvals.confirm(signer))
        assertTrue((approvals.items.value as Load.Content).data.approvals.none { it.id == a.id })

        val dash = DashboardModel(repo); dash.refresh()
        val data = (dash.state.value as Load.Content).data
        assertEquals("L1", data.core!!.autonomy); assertTrue(data.goals.any { it.id == g.id })
        val j = JournalModel(repo); j.refresh(g.id)
        assertTrue((j.state.value as Load.Content).data.isNotEmpty())
    }
}
