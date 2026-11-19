import az.naymera.app.data.ApiException
import az.naymera.app.data.ApprovalDto
import az.naymera.app.data.AwaitingStep
import az.naymera.app.data.ChallengeResponse
import az.naymera.app.data.CoreStatus
import az.naymera.app.data.CreateGoalResponse
import az.naymera.app.data.DecisionRequest
import az.naymera.app.data.DecisionResponse
import az.naymera.app.data.GoalChallenge
import az.naymera.app.data.GoalDetail
import az.naymera.app.data.GoalDto
import az.naymera.app.data.GoalTransitionRequest
import az.naymera.app.data.JournalEntry
import az.naymera.app.data.PlanDto
import az.naymera.app.data.PlanStepDto
import az.naymera.app.data.StepChallenge
import az.naymera.app.data.StepRef
import az.naymera.app.data.StepResolveRequest
import az.naymera.app.data.StepResolveResponse
import az.naymera.app.data.SystemStatus
import az.naymera.app.domain.ActionResult
import az.naymera.app.domain.ApprovalsModel
import az.naymera.app.domain.CoreRepository
import az.naymera.app.domain.DashboardModel
import az.naymera.app.domain.ErrorKind
import az.naymera.app.domain.GoalAction
import az.naymera.app.domain.GoalsModel
import az.naymera.app.domain.JournalModel
import az.naymera.app.domain.Load
import az.naymera.app.domain.Signer
import az.naymera.app.domain.allowedActions
import az.naymera.app.security.CoreChallenge
import az.naymera.app.security.SignableChallenge
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Backend-siz ViewModel/UI-vəziyyət testləri (saxta repository): yükləmə/xəta/oflayn-köhnə məzmun, yaratma idempotentliyi, imza axınının təsdiq tələbi. */
class FakeRepo : CoreRepository {
    var goalsResult: () -> List<GoalDto> = { emptyList() }
    var offline = false
    var version = 1
    var status = "draft"
    var approvalHash = "h1"
    val created = mutableListOf<String>()
    var transitioned: GoalTransitionRequest? = null
    var decided: DecisionRequest? = null
    var resolved: StepResolveRequest? = null
    var createFail: Exception? = null
    var stepState = "unknown"; var stepAttempt = 1
    var stopped = false

    private fun net() { if (offline) throw ApiException(0, "network", "Offline", az.naymera.app.data.ApiFailure.Offline) }
    private fun goal() = GoalDto("goal_a", "Başlıq", status = status, version = version)
    override suspend fun coreStatus(): CoreStatus { net(); return CoreStatus("L1", stopped) }
    override suspend fun systemStatus(): SystemStatus { net(); return SystemStatus(stopped, 1, 1, true) }
    override suspend fun goals(): List<GoalDto> { net(); return goalsResult() }
    override suspend fun createGoal(title: String, description: String, priority: String, clientOp: String): CreateGoalResponse {
        net(); createFail?.let { throw it }; created += clientOp; return CreateGoalResponse(goal())
    }
    override suspend fun goal(id: String): GoalDetail { net(); return GoalDetail(goal(), PlanDto("plan_a", 1, "active", steps = listOf(PlanStepDto("s1", "Addım", state = stepState, attempt = stepAttempt)))) }
    override suspend fun goalChallenge(id: String, toStatus: String): GoalChallenge { net(); return GoalChallenge("c1", "n1", 60, "core_goal", id, CoreChallenge.goalPayloadHash(toStatus, version), toStatus, goal(), toStatus) }
    override suspend fun goalTransition(id: String, req: GoalTransitionRequest): CreateGoalResponse { net(); transitioned = req; status = req.toStatus; version++; return CreateGoalResponse(goal()) }
    override suspend fun awaitingSteps(): List<AwaitingStep> = emptyList()
    override suspend fun approvals(): List<ApprovalDto> { net(); return listOf(ApprovalDto("apr_1", "fin_agent", "spend_money", 0.5, "Ödəniş", approvalHash)) }
    override suspend fun approvalChallenge(id: String, decision: String): ChallengeResponse =
        ChallengeResponse("c2", "n2", 60, ApprovalDto(id, "fin_agent", "spend_money", 0.5, "Ödəniş", approvalHash))
    override suspend fun approvalDecision(id: String, req: DecisionRequest): DecisionResponse { decided = req; return DecisionResponse(true, "approved") }
    override suspend fun stepChallenge(planId: String, stepId: String, resolution: String): StepChallenge =
        StepChallenge("c3", "n3", 60, "core_step", "$planId/$stepId", CoreChallenge.stepPayloadHash(resolution, stepAttempt, stepState), resolution, StepRef(planId, stepId, stepState, stepAttempt))
    override suspend fun stepResolve(planId: String, stepId: String, req: StepResolveRequest): StepResolveResponse { resolved = req; return StepResolveResponse(true, req.resolution) }
    override suspend fun journal(goalId: String?, afterSeq: Long): List<JournalEntry> { net(); return (1L..7L).map { JournalEntry(it, kind = "tick") } }
    override suspend fun stop() { net(); stopped = true }
}

class RecordingSigner(var ok: Boolean = true, var proof: String? = "PROOF", var avail: Boolean = true) : Signer {
    val shown = mutableListOf<SignableChallenge>()
    override fun available() = avail
    override suspend fun sign(challenge: SignableChallenge): String? { shown += challenge; return if (ok) proof else null }
}

class DomainModelsTest {
    @Test fun goalsListShowsLoadingContentAndKeepsStaleContentWhenOffline() = runBlocking {
        val repo = FakeRepo(); val m = GoalsModel(repo)
        assertTrue(m.list.value is Load.Idle)
        repo.goalsResult = { listOf(GoalDto("goal_a", "A", status = "draft", version = 1)) }
        m.refresh()
        assertEquals(1, (m.list.value as Load.Content).data.size)
        repo.offline = true
        m.refresh()
        val f = m.list.value as Load.Failed
        assertEquals(ErrorKind.Offline, f.kind); assertEquals(1, f.stale!!.size)                     // oflayn: son məzmun oxuna bilir
    }

    @Test fun createValidatesAndReusesTheSameClientOpAfterANetworkFailure() = runBlocking {
        val repo = FakeRepo(); var n = 0; val m = GoalsModel(repo) { "op-${++n}-xxxxxxxx" }
        assertEquals(ActionResult.Failed(ErrorKind.Invalid), m.create("   ", ""))
        assertEquals(ActionResult.Failed(ErrorKind.Invalid), m.create("x".repeat(201), ""))
        repo.createFail = ApiException(0, "network", "x", az.naymera.app.data.ApiFailure.Offline)
        assertEquals(ActionResult.Failed(ErrorKind.Offline), m.create("Məqsəd", "təsvir"))
        repo.createFail = null
        assertEquals(ActionResult.Done, m.create("Məqsəd", "təsvir"))                                  // təkrar: EYNİ op (ikiqat məqsəd yoxdur)
        assertEquals(listOf("op-1-xxxxxxxx"), repo.created)
        m.create("Başqa", ""); assertEquals("op-2-xxxxxxxx", repo.created.last())                      // yeni məzmun: yeni op
        repo.createFail = ApiException(422, "secret_detected", "x")
        assertEquals(ActionResult.Failed(ErrorKind.SecretRejected), m.create("açar", "s"))
    }

    @Test fun supportedActionsFollowTheStatus() {
        assertEquals(listOf(GoalAction.Start, GoalAction.Cancel), allowedActions("draft"))
        assertEquals(listOf(GoalAction.Pause, GoalAction.Complete, GoalAction.Cancel), allowedActions("active"))
        assertEquals(listOf(GoalAction.Resume, GoalAction.Cancel), allowedActions("paused"))
        for (t in listOf("completed", "failed", "cancelled")) assertTrue(allowedActions(t).isEmpty())
        assertEquals("active", GoalAction.Resume.to); assertEquals("paused", GoalAction.Pause.to)
    }

    @Test fun transitionNeverSignsOrSendsUntilTheUserConfirmsAndShowsTheExactValues() = runBlocking {
        val repo = FakeRepo(); val m = GoalsModel(repo); val signer = RecordingSigner()
        m.open("goal_a")
        assertEquals(ActionResult.Done, m.prepareTransition("goal_a", GoalAction.Start, 1, "draft"))
        val p = m.pending.value!!
        assertEquals(listOf("core_goal", "goal_a", CoreChallenge.goalPayloadHash("active", 1), "active"),
            listOf(p.challenge.action, p.challenge.subject, p.challenge.payloadHash, p.challenge.decision))   // göstərilən dəyərlər = imzalanan dəyərlər
        assertTrue(signer.shown.isEmpty()); assertNull(repo.transitioned)                              // hazırlıq imza/göndərmə etmir
        assertEquals(ActionResult.Done, m.confirm(signer))
        assertEquals("biometric", repo.transitioned!!.method); assertEquals("PROOF", repo.transitioned!!.proof)
        assertEquals(1, signer.shown.size); assertNull(m.pending.value)
        assertEquals("active", (m.detail.value as Load.Content).data.goal.status)                      // detal yeniləndi
    }

    @Test fun cancelledBiometricUnavailableSignerAndMismatchSendNothing() = runBlocking {
        val repo = FakeRepo(); val m = GoalsModel(repo)
        m.prepareTransition("goal_a", GoalAction.Start, 1, "draft")
        assertEquals(ActionResult.Cancelled, m.confirm(RecordingSigner(ok = false))); assertNull(repo.transitioned)
        m.prepareTransition("goal_a", GoalAction.Start, 1, "draft")
        assertEquals(ActionResult.Failed(ErrorKind.StepUpUnavailable), m.confirm(RecordingSigner(avail = false))); assertNull(repo.transitioned)
        assertEquals(ActionResult.Failed(ErrorKind.Invalid), m.confirm(RecordingSigner()))             // gözləyən yoxdur: heç nə olmur
        repo.version = 2                                                                                // ekran v1 göstərir, server v2
        assertEquals(ActionResult.Failed(ErrorKind.Stale), m.prepareTransition("goal_a", GoalAction.Start, 1, "draft"))
        assertNull(m.pending.value)
        assertEquals(ActionResult.Failed(ErrorKind.Invalid), m.prepareTransition("goal_a", GoalAction.Resume, 2, "draft")) // dəstəklənməyən keçid
    }

    @Test fun stepResolveRequiresResolvableStateAndConfirmation() = runBlocking {
        val repo = FakeRepo(); val m = GoalsModel(repo)
        assertEquals(ActionResult.Failed(ErrorKind.Invalid), m.prepareStepResolve("plan_a", "s1", "retry", 1, "succeeded"))
        assertEquals(ActionResult.Failed(ErrorKind.Invalid), m.prepareStepResolve("plan_a", "s1", "explode", 1, "unknown"))
        assertEquals(ActionResult.Done, m.prepareStepResolve("plan_a", "s1", "retry", 1, "unknown")); assertNull(repo.resolved)
        assertEquals(ActionResult.Done, m.confirm(RecordingSigner())); assertEquals("retry", repo.resolved!!.resolution)
        repo.stepAttempt = 2                                                                            // addım arada dəyişdi
        assertEquals(ActionResult.Failed(ErrorKind.Stale), m.prepareStepResolve("plan_a", "s1", "retry", 1, "unknown"))
    }

    @Test fun approvalsLoadPrepareThenExplicitConfirmOnly() = runBlocking {
        val repo = FakeRepo(); val m = ApprovalsModel(repo); val signer = RecordingSigner()
        m.refresh()
        val a = (m.items.value as Load.Content).data.approvals.single()
        assertEquals(ActionResult.Done, m.prepare(a, "approve"))
        val s = m.pending.value!!.challenge
        assertEquals(listOf("approval", "apr_1", "h1", "approve"), listOf(s.action, s.subject, s.payloadHash, s.decision))
        assertNull(repo.decided); assertTrue(signer.shown.isEmpty())                                    // heç vaxt avtomatik təsdiq yoxdur
        assertEquals(ActionResult.Done, m.confirm(signer)); assertEquals("approve", repo.decided!!.decision)
        // server fərqli hash qaytararsa (dəyişdirilmiş təsdiq) imza hazırlanmır
        repo.decided = null; repo.approvalHash = "h-changed"
        val shown = a
        assertEquals(ActionResult.Failed(ErrorKind.Mismatch), m.prepare(shown, "deny")); assertNull(m.pending.value)
        assertEquals(ActionResult.Failed(ErrorKind.Mismatch), m.prepare(shown, "approve"))
        assertNull(repo.decided)
        repo.approvalHash = "h1"
        assertEquals(ActionResult.Failed(ErrorKind.Mismatch), m.prepare(shown, "maybe"))                // yanlış qərar dəyəri imzalanmır
        assertNull(m.pending.value)
    }

    @Test fun approvalsOfflineKeepsStaleListAndDoesNotPrepare() = runBlocking {
        val repo = FakeRepo(); val m = ApprovalsModel(repo)
        m.refresh(); repo.offline = true; m.refresh()
        val f = m.items.value as Load.Failed
        assertEquals(ErrorKind.Offline, f.kind); assertEquals(1, f.stale!!.approvals.size)
    }

    @Test fun dashboardShowsPartialDataAndFailsOnlyWhenEverythingFails() = runBlocking {
        val repo = FakeRepo(); val m = DashboardModel(repo)
        repo.goalsResult = { listOf(GoalDto("goal_a", "A", status = "active", version = 2)) }
        m.refresh()
        val d = (m.state.value as Load.Content).data
        assertEquals(1, d.goals.size); assertEquals(1, d.pendingApprovals); assertEquals(5, d.recent.size); assertEquals(7L, d.recent.first().seq) // ən yeni əvvəl
        assertEquals("L1", d.core!!.autonomy)
        repo.offline = true
        m.refresh()
        val f = m.state.value as Load.Failed
        assertEquals(ErrorKind.Offline, f.kind); assertNotNull(f.stale)                                 // köhnə dashboard saxlanılır
        repo.offline = false
        assertEquals(ActionResult.Done, m.stop()); assertTrue(repo.stopped)
        assertTrue((m.state.value as Load.Content).data.core!!.stopActive)
    }

    @Test fun journalIsNewestFirstAndFailureKinds() = runBlocking {
        val repo = FakeRepo(); val m = JournalModel(repo)
        m.refresh(); assertEquals(7L, (m.state.value as Load.Content).data.first().seq)
        repo.offline = true; m.refresh(); assertEquals(7, (m.state.value as Load.Failed).stale!!.size)
        assertFalse(m.state.value is Load.Content)
    }
}
