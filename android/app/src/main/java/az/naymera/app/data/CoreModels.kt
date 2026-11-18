package az.naymera.app.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** CORE API DTO-ları (`api/core_api.py`). Server mətnləri (başlıq, xülasə, jurnal) ETİBARSIZDIR: yalnız göstərilir, heç vaxt icra/təsdiq üçün istifadə olunmur. */

@Serializable
data class CoreStatus(
    @SerialName("autonomy") val autonomy: String,
    @SerialName("stop_active") val stopActive: Boolean,
    @SerialName("steps") val steps: Map<String, Int> = emptyMap(),
    @SerialName("executes_without_owner_approval") val executesWithoutOwnerApproval: Boolean = false,
)

@Serializable
data class GoalDto(
    @SerialName("id") val id: String,
    @SerialName("title") val title: String,
    @SerialName("description") val description: String = "",
    @SerialName("status") val status: String,
    @SerialName("priority") val priority: String = "normal",
    @SerialName("origin") val origin: String = "",
    @SerialName("version") val version: Int,
    @SerialName("created_at") val createdAt: String = "",
    @SerialName("updated_at") val updatedAt: String = "",
    @SerialName("untrusted") val untrusted: Boolean = false,
)

@Serializable
data class PlanStepDto(
    @SerialName("id") val id: String,
    @SerialName("title") val title: String,
    @SerialName("capability_id") val capabilityId: String = "",
    @SerialName("depends_on") val dependsOn: List<String> = emptyList(),
    @SerialName("state") val state: String,
    @SerialName("attempt") val attempt: Int = 0,
    @SerialName("verified") val verified: Boolean = false,
    @SerialName("summary") val summary: String = "",
    @SerialName("error") val error: String = "",
    @SerialName("untrusted_output") val untrustedOutput: Boolean = true,
)

@Serializable
data class PlanDto(
    @SerialName("id") val id: String,
    @SerialName("version") val version: Int,
    @SerialName("status") val status: String,
    @SerialName("origin") val origin: String = "",
    @SerialName("untrusted") val untrusted: Boolean = false,
    @SerialName("steps") val steps: List<PlanStepDto> = emptyList(),
)

@Serializable data class GoalsResponse(@SerialName("goals") val goals: List<GoalDto>)

@Serializable
data class GoalDetail(@SerialName("goal") val goal: GoalDto, @SerialName("plan") val plan: PlanDto? = null)

@Serializable
data class CreateGoalRequest(
    @SerialName("title") val title: String,
    @SerialName("description") val description: String,
    @SerialName("priority") val priority: String,
    @SerialName("client_op") val clientOp: String,
)

@Serializable
data class CreateGoalResponse(@SerialName("goal") val goal: GoalDto, @SerialName("duplicate") val duplicate: Boolean = false)

@Serializable data class GoalChallengeRequest(@SerialName("to_status") val toStatus: String)

/** Serverin verdiyi challenge: imzalanacaq dəyərlər (action/subject/payload_hash/decision) açıq qaytarılır; klient onları YENİDƏN hesablayıb yoxlamadan imzalamır. */
@Serializable
data class GoalChallenge(
    @SerialName("challenge_id") val challengeId: String,
    @SerialName("nonce") val nonce: String,
    @SerialName("expires_in") val expiresIn: Int,
    @SerialName("action") val action: String,
    @SerialName("subject") val subject: String,
    @SerialName("payload_hash") val payloadHash: String,
    @SerialName("decision") val decision: String,
    @SerialName("goal") val goal: GoalDto,
    @SerialName("to_status") val toStatus: String,
)

@Serializable
data class GoalTransitionRequest(
    @SerialName("to_status") val toStatus: String,
    @SerialName("challenge_id") val challengeId: String,
    @SerialName("proof") val proof: String,
    @SerialName("method") val method: String,
)

@Serializable
data class StepRef(
    @SerialName("plan_id") val planId: String,
    @SerialName("step_id") val stepId: String,
    @SerialName("state") val state: String,
    @SerialName("attempt") val attempt: Int,
)

@Serializable data class StepChallengeRequest(@SerialName("resolution") val resolution: String)

@Serializable
data class StepChallenge(
    @SerialName("challenge_id") val challengeId: String,
    @SerialName("nonce") val nonce: String,
    @SerialName("expires_in") val expiresIn: Int,
    @SerialName("action") val action: String,
    @SerialName("subject") val subject: String,
    @SerialName("payload_hash") val payloadHash: String,
    @SerialName("decision") val decision: String,
    @SerialName("step") val step: StepRef,
)

@Serializable
data class StepResolveRequest(
    @SerialName("resolution") val resolution: String,
    @SerialName("challenge_id") val challengeId: String,
    @SerialName("proof") val proof: String,
    @SerialName("method") val method: String,
)

@Serializable
data class StepResolveResponse(@SerialName("ok") val ok: Boolean, @SerialName("resolution") val resolution: String = "")

@Serializable
data class AwaitingStep(
    @SerialName("goal_id") val goalId: String,
    @SerialName("goal_title") val goalTitle: String,
    @SerialName("plan_id") val planId: String,
    @SerialName("step_id") val stepId: String,
    @SerialName("step_title") val stepTitle: String,
    @SerialName("capability_id") val capabilityId: String,
    @SerialName("attempt") val attempt: Int,
    @SerialName("approval_description") val approvalDescription: String,
)

@Serializable data class AwaitingStepsResponse(@SerialName("steps") val steps: List<AwaitingStep>)

@Serializable
data class JournalEntry(
    @SerialName("seq") val seq: Long,
    @SerialName("ts") val ts: String = "",
    @SerialName("goal_id") val goalId: String = "",
    @SerialName("plan_id") val planId: String = "",
    @SerialName("step_id") val stepId: String = "",
    @SerialName("kind") val kind: String,
    @SerialName("detail") val detail: Map<String, String> = emptyMap(),
    @SerialName("untrusted") val untrusted: Boolean = true,
)

@Serializable data class JournalResponse(@SerialName("entries") val entries: List<JournalEntry>)
