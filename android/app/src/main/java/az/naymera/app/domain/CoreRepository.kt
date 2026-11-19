package az.naymera.app.domain

import az.naymera.app.data.ApiClient
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
import az.naymera.app.data.StepChallenge
import az.naymera.app.data.StepResolveRequest
import az.naymera.app.data.StepResolveResponse
import az.naymera.app.data.SystemStatus

/** Repository sərhədi: ViewModel-lər yalnız bunu görür (testdə saxta, istehsalda `ApiCoreRepository` → M1 `ApiClient`). */
interface CoreRepository {
    suspend fun coreStatus(): CoreStatus
    suspend fun systemStatus(): SystemStatus
    suspend fun goals(): List<GoalDto>
    suspend fun createGoal(title: String, description: String, priority: String, clientOp: String): CreateGoalResponse
    suspend fun goal(id: String): GoalDetail
    suspend fun goalChallenge(id: String, toStatus: String): GoalChallenge
    suspend fun goalTransition(id: String, req: GoalTransitionRequest): CreateGoalResponse
    suspend fun awaitingSteps(): List<AwaitingStep>
    suspend fun approvals(): List<ApprovalDto>
    suspend fun approvalChallenge(id: String, decision: String): ChallengeResponse
    suspend fun approvalDecision(id: String, req: DecisionRequest): DecisionResponse
    suspend fun stepChallenge(planId: String, stepId: String, resolution: String): StepChallenge
    suspend fun stepResolve(planId: String, stepId: String, req: StepResolveRequest): StepResolveResponse
    suspend fun journal(goalId: String? = null, afterSeq: Long = 0): List<JournalEntry>
    suspend fun stop()
}

class ApiCoreRepository(private val api: ApiClient) : CoreRepository {
    override suspend fun coreStatus() = api.coreStatus()
    override suspend fun systemStatus() = api.status()
    override suspend fun goals() = api.goals()
    override suspend fun createGoal(title: String, description: String, priority: String, clientOp: String) = api.createGoal(title, description, priority, clientOp)
    override suspend fun goal(id: String) = api.goal(id)
    override suspend fun goalChallenge(id: String, toStatus: String) = api.goalChallenge(id, toStatus)
    override suspend fun goalTransition(id: String, req: GoalTransitionRequest) = api.goalTransition(id, req)
    override suspend fun awaitingSteps() = api.awaitingSteps()
    override suspend fun approvals() = api.approvals()
    override suspend fun approvalChallenge(id: String, decision: String) = api.approvalChallenge(id, decision)
    override suspend fun approvalDecision(id: String, req: DecisionRequest) = api.approvalDecision(id, req)
    override suspend fun stepChallenge(planId: String, stepId: String, resolution: String) = api.stepChallenge(planId, stepId, resolution)
    override suspend fun stepResolve(planId: String, stepId: String, req: StepResolveRequest) = api.stepResolve(planId, stepId, req)
    override suspend fun journal(goalId: String?, afterSeq: Long) = api.journal(goalId, afterSeq)
    override suspend fun stop() = api.stop()
}
