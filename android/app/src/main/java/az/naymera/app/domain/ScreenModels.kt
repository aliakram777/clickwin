package az.naymera.app.domain

import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApprovalDto
import az.naymera.app.data.AwaitingStep
import az.naymera.app.data.CoreStatus
import az.naymera.app.data.DecisionRequest
import az.naymera.app.data.GoalDetail
import az.naymera.app.data.GoalDto
import az.naymera.app.data.GoalTransitionRequest
import az.naymera.app.data.JournalEntry
import az.naymera.app.data.StepResolveRequest
import az.naymera.app.data.SystemStatus
import az.naymera.app.security.CoreChallenge
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

const val STEP_UP_METHOD = "biometric"

private suspend fun <T> load(state: MutableStateFlow<Load<T>>, block: suspend () -> T) {
    val before = state.value.dataOrNull()
    state.value = Load.Loading
    state.value = try { Load.Content(block()) } catch (e: CancellationException) { throw e } catch (e: Exception) { Load.Failed(e.toErrorKind(), before) }
}

/** Biometrik imza + göndərmə. Avtomatik təsdiq YOXDUR: yalnız istifadəçinin açıq «təsdiqlə» əməli bunu çağırır. */
private suspend fun signAndSubmit(p: PendingSign, signer: Signer): ActionResult {
    if (!signer.available()) return ActionResult.Failed(ErrorKind.StepUpUnavailable)
    val proof = try { signer.sign(p.challenge) } catch (e: CancellationException) { throw e } catch (e: Exception) { return ActionResult.Failed(ErrorKind.Unknown) }
        ?: return ActionResult.Cancelled
    return try { p.submit(proof); ActionResult.Done } catch (e: CancellationException) { throw e } catch (e: Exception) { ActionResult.Failed(e.toErrorKind()) }
}

enum class GoalAction(val to: String) { Start("active"), Pause("paused"), Resume("active"), Complete("completed"), Cancel("cancelled") }

/** Serverdəki kernel keçid cədvəlinin API-yə açılmış alt çoxluğu: yalnız dəstəklənən əməliyyatlar təklif olunur. */
fun allowedActions(status: String): List<GoalAction> = when (status) {
    "draft" -> listOf(GoalAction.Start, GoalAction.Cancel)
    "active" -> listOf(GoalAction.Pause, GoalAction.Complete, GoalAction.Cancel)
    "paused", "blocked" -> listOf(GoalAction.Resume, GoalAction.Cancel)
    else -> emptyList()                                       // completed/failed/cancelled: terminal
}

val RESOLVABLE_STEP_STATES = setOf("unknown", "blocked", "failed")
val STEP_RESOLUTIONS = listOf("retry", "failed", "succeeded")

class GoalsModel(private val repo: CoreRepository, private val newOp: () -> String = ApiClient::newClientOp) {
    private val _list = MutableStateFlow<Load<List<GoalDto>>>(Load.Idle)
    val list: StateFlow<Load<List<GoalDto>>> = _list.asStateFlow()
    private val _detail = MutableStateFlow<Load<GoalDetail>>(Load.Idle)
    val detail: StateFlow<Load<GoalDetail>> = _detail.asStateFlow()
    private val _pending = MutableStateFlow<PendingSign?>(null)
    val pending: StateFlow<PendingSign?> = _pending.asStateFlow()
    private val _result = MutableStateFlow<ActionResult?>(null)
    val result: StateFlow<ActionResult?> = _result.asStateFlow()
    private var opKey: String? = null
    private var op: String? = null

    suspend fun refresh() = load(_list) { repo.goals() }

    suspend fun open(id: String) { _pending.value = null; _result.value = null; load(_detail) { repo.goal(id) } }

    /** Yaratma yalnız qaralama (draft) yaradır. Şəbəkə xətasından sonra EYNİ clientOp təkrarlanır (server idempotent: ikiqat məqsəd yoxdur). */
    suspend fun create(title: String, description: String, priority: String = "normal"): ActionResult {
        val t = title.trim(); val d = description.trim()
        if (t.isEmpty() || t.length > 200 || d.length > 2000) return ActionResult.Failed(ErrorKind.Invalid)
        val key = "$t|$d|$priority"
        if (opKey != key) { opKey = key; op = newOp() }
        return try {
            repo.createGoal(t, d, priority, op!!)
            opKey = null; op = null
            refresh()
            ActionResult.Done
        } catch (e: CancellationException) { throw e } catch (e: Exception) {
            val k = e.toErrorKind()
            if (k == ErrorKind.SecretRejected || k == ErrorKind.Invalid || k == ErrorKind.Conflict) { opKey = null; op = null }
            ActionResult.Failed(k)
        }
    }

    /** İstifadəçinin gördüyü versiya `shownVersion`-dır: server fərqli versiya/hash qaytarırsa imza hazırlanmır (detal yenilənir). */
    suspend fun prepareTransition(goalId: String, action: GoalAction, shownVersion: Int, shownStatus: String): ActionResult {
        _result.value = null
        if (action !in allowedActions(shownStatus)) return fail(ErrorKind.Invalid)
        return try {
            val ch = repo.goalChallenge(goalId, action.to)
            val sig = CoreChallenge.forGoal(ch, goalId, action.to, shownVersion)
            _pending.value = PendingSign(ch.goal.title, sig) { proof ->
                repo.goalTransition(goalId, GoalTransitionRequest(action.to, sig.challengeId, proof, STEP_UP_METHOD))
            }
            ActionResult.Done
        } catch (e: CancellationException) { throw e } catch (e: Exception) {
            if (e.toErrorKind() == ErrorKind.Stale) runCatching { open(goalId) }
            fail(e.toErrorKind())
        }
    }

    suspend fun prepareStepResolve(planId: String, stepId: String, resolution: String, shownAttempt: Int, shownState: String): ActionResult {
        _result.value = null
        if (shownState !in RESOLVABLE_STEP_STATES || resolution !in STEP_RESOLUTIONS) return fail(ErrorKind.Invalid)
        return try {
            val ch = repo.stepChallenge(planId, stepId, resolution)
            val sig = CoreChallenge.forStep(ch, planId, stepId, resolution, shownAttempt, shownState)
            _pending.value = PendingSign(stepId, sig) { proof -> repo.stepResolve(planId, stepId, StepResolveRequest(resolution, sig.challengeId, proof, STEP_UP_METHOD)) }
            ActionResult.Done
        } catch (e: CancellationException) { throw e } catch (e: Exception) { fail(e.toErrorKind()) }
    }

    /** İstifadəçi ekrandakı dəyərləri gördükdən və «təsdiqlə» basdıqdan sonra. Gözləyən yoxdursa heç nə olmur. */
    suspend fun confirm(signer: Signer): ActionResult {
        val p = _pending.value ?: return ActionResult.Failed(ErrorKind.Invalid)
        _pending.value = null
        val r = signAndSubmit(p, signer)
        _result.value = r
        if (r is ActionResult.Done) {
            val id = (_detail.value.dataOrNull())?.goal?.id
            runCatching { refresh() }
            if (id != null) runCatching { open(id) }
            _result.value = r
        }
        return r
    }

    fun cancelPending() { _pending.value = null }

    private fun fail(k: ErrorKind): ActionResult { val r = ActionResult.Failed(k); _result.value = r; return r }
}

data class PendingApprovals(val approvals: List<ApprovalDto>, val steps: List<AwaitingStep>)

class ApprovalsModel(private val repo: CoreRepository) {
    private val _items = MutableStateFlow<Load<PendingApprovals>>(Load.Idle)
    val items: StateFlow<Load<PendingApprovals>> = _items.asStateFlow()
    private val _pending = MutableStateFlow<PendingSign?>(null)
    val pending: StateFlow<PendingSign?> = _pending.asStateFlow()
    private val _result = MutableStateFlow<ActionResult?>(null)
    val result: StateFlow<ActionResult?> = _result.asStateFlow()

    suspend fun refresh() = load(_items) { PendingApprovals(repo.approvals(), runCatching { repo.awaitingSteps() }.getOrDefault(emptyList())) }

    /** Yalnız İMZA HAZIRLANIR (təsdiq deyil): istifadəçi dəqiq əməliyyatı görür və ayrıca «təsdiqlə/rədd et» + biometrik edir. */
    suspend fun prepare(a: ApprovalDto, decision: String): ActionResult {
        _result.value = null
        return try {
            val ch = repo.approvalChallenge(a.id, decision)
            val sig = CoreChallenge.forApproval(ch, a.id, a.payloadHash, decision)
            _pending.value = PendingSign(a.description, sig) { proof -> check(repo.approvalDecision(a.id, DecisionRequest(decision, sig.challengeId, proof, STEP_UP_METHOD)).ok) }
            ActionResult.Done
        } catch (e: CancellationException) { throw e } catch (e: Exception) { val r = ActionResult.Failed(e.toErrorKind()); _result.value = r; r }
    }

    suspend fun confirm(signer: Signer): ActionResult {
        val p = _pending.value ?: return ActionResult.Failed(ErrorKind.Invalid)
        _pending.value = null
        val r = signAndSubmit(p, signer)
        _result.value = r
        if (r is ActionResult.Done) { runCatching { refresh() }; _result.value = r }
        return r
    }

    fun cancelPending() { _pending.value = null }
}

data class DashboardData(val core: CoreStatus?, val system: SystemStatus?, val goals: List<GoalDto>, val pendingApprovals: Int, val recent: List<JournalEntry>)

class DashboardModel(private val repo: CoreRepository) {
    private val _state = MutableStateFlow<Load<DashboardData>>(Load.Idle)
    val state: StateFlow<Load<DashboardData>> = _state.asStateFlow()

    /** Hissələr müstəqil: biri uğursuz olsa digərləri göstərilir; hamısı uğursuzdursa `Failed` (köhnə məzmun saxlanılır). */
    suspend fun refresh() {
        val before = _state.value.dataOrNull()
        _state.value = Load.Loading
        var firstError: Exception? = null
        suspend fun <T> part(block: suspend () -> T): T? = try { block() } catch (e: CancellationException) { throw e } catch (e: Exception) { if (firstError == null) firstError = e; null }
        val core = part { repo.coreStatus() }
        val system = part { repo.systemStatus() }
        val goals = part { repo.goals() }
        val approvals = part { repo.approvals() }
        val recent = part { repo.journal(null, 0) }
        _state.value = if (core == null && system == null && goals == null && approvals == null && recent == null) Load.Failed(firstError!!.toErrorKind(), before)
        else Load.Content(DashboardData(core, system, goals ?: before?.goals.orEmpty(), approvals?.size ?: before?.pendingApprovals ?: 0, (recent ?: before?.recent.orEmpty()).takeLast(5).reversed()))
    }

    /** STOP həmişə əlçatandır: imza tələb etmir (yalnız dayandırır), mövcud server siyasəti. */
    suspend fun stop(): ActionResult = try { repo.stop(); refresh(); ActionResult.Done } catch (e: CancellationException) { throw e } catch (e: Exception) { ActionResult.Failed(e.toErrorKind()) }
}

class JournalModel(private val repo: CoreRepository) {
    private val _state = MutableStateFlow<Load<List<JournalEntry>>>(Load.Idle)
    val state: StateFlow<Load<List<JournalEntry>>> = _state.asStateFlow()

    suspend fun refresh(goalId: String? = null) = load(_state) { repo.journal(goalId, 0).sortedByDescending { it.seq } }
}
