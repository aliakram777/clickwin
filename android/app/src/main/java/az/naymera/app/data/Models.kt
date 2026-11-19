package az.naymera.app.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/** Server JSON müqaviləsi (api/app.py). Mətn sahələri (söhbət, səs transkripti, yaddaş) ETİBARSIZ məlumatdır. */

/** Cütləşmə: yalnız Keystore açarının AÇIQ açarı (base64 X.509) və kodun imzası göndərilir; şəxsi açar cihazdan çıxmır. */
@Serializable
data class PairRequest(
    @SerialName("code") val code: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("public_key") val publicKey: String,
    @SerialName("signature") val signature: String,
)

@Serializable
data class PairResponse(
    @SerialName("device_id") val deviceId: String,
    @SerialName("access_token") val accessToken: String,
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("access_expires_in") val accessExpiresIn: Int,
)

@Serializable
data class RefreshRequest(@SerialName("refresh_token") val refreshToken: String)

@Serializable
data class TokenResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("access_expires_in") val accessExpiresIn: Int,
)

@Serializable
data class ChatRequest(
    @SerialName("text") val text: String,
    @SerialName("conversation_id") val conversationId: String? = null,
    @SerialName("client_request_id") val clientRequestId: String? = null,
)

@Serializable
data class ChatMessage(
    @SerialName("id") val id: Long,
    @SerialName("conversation_id") val conversationId: String,
    @SerialName("role") val role: String,
    @SerialName("text") val text: String,
    @SerialName("input_mode") val inputMode: String = "text",
    @SerialName("created_at") val createdAt: Double = 0.0,
    @SerialName("untrusted") val untrusted: Boolean = true,
    @SerialName("speech_text") val speechText: String = "",
    @SerialName("provenance") val provenance: String = "",
    @SerialName("is_mock") val isMock: Boolean = false,
    @SerialName("engine") val engine: String = "",
    @SerialName("notice") val notice: String = "",
    @SerialName("notice_code") val noticeCode: String = "",
)

@Serializable
data class ChatResponse(
    @SerialName("conversation_id") val conversationId: String,
    @SerialName("user_message") val userMessage: ChatMessage,
    @SerialName("assistant_message") val assistantMessage: ChatMessage,
)

@Serializable
data class Conversation(
    @SerialName("id") val id: String,
    @SerialName("title") val title: String = "",
    @SerialName("created_at") val createdAt: Double = 0.0,
    @SerialName("updated_at") val updatedAt: Double = 0.0,
    @SerialName("message_count") val messageCount: Int = 0,
)

@Serializable
data class ConversationsResponse(@SerialName("conversations") val conversations: List<Conversation>)

@Serializable
data class MessagesResponse(@SerialName("messages") val messages: List<ChatMessage>)

@Serializable
data class MobileEvent(
    @SerialName("seq") val seq: Long,
    @SerialName("type") val type: String,
    @SerialName("id") val id: String,
    @SerialName("ts") val ts: Double,
    @SerialName("payload") val payload: JsonObject,
    @SerialName("schema_version") val schemaVersion: Int = 1,
)

@Serializable
data class EventsResponse(
    @SerialName("events") val events: List<MobileEvent>,
    @SerialName("next_cursor") val nextCursor: Long,
    @SerialName("last_ack") val lastAck: Long = 0,
)

@Serializable
data class AckRequest(@SerialName("seq") val seq: Long)

@Serializable
data class NotificationAction(
    @SerialName("action_id") val actionId: String,
    @SerialName("label") val label: String,
    @SerialName("requires_approval") val requiresApproval: Boolean = false,
    @SerialName("kind") val kind: String = "open",
)

@Serializable
data class AppNotification(
    @SerialName("id") val id: String,
    @SerialName("title") val title: String,
    @SerialName("body") val body: String,
    @SerialName("speech_text") val speechText: String = "",
    @SerialName("importance") val importance: String,
    @SerialName("priority") val priority: Int,
    @SerialName("category") val category: String = "",
    @SerialName("dedupe_key") val dedupeKey: String,
    @SerialName("schema_version") val schemaVersion: Int = 1,
    @SerialName("is_mock") val isMock: Boolean = false,
    @SerialName("actions") val actions: List<NotificationAction> = emptyList(),
)

@Serializable
data class ApprovalDto(
    @SerialName("id") val id: String,
    @SerialName("agent") val agent: String,
    @SerialName("permission") val permission: String,
    @SerialName("cost") val cost: Double = 0.0,
    @SerialName("description") val description: String = "",
    @SerialName("payload_hash") val payloadHash: String,
    @SerialName("expires_at") val expiresAt: Double = 0.0,
)

@Serializable
data class ApprovalsResponse(@SerialName("approvals") val approvals: List<ApprovalDto>)

@Serializable
data class DecisionChallengeRequest(@SerialName("decision") val decision: String)

@Serializable
data class ChallengeResponse(
    @SerialName("challenge_id") val challengeId: String,
    @SerialName("nonce") val nonce: String,
    @SerialName("expires_in") val expiresIn: Int,
    @SerialName("approval") val approval: ApprovalDto? = null,
)

@Serializable
data class DecisionRequest(
    @SerialName("decision") val decision: String,
    @SerialName("challenge_id") val challengeId: String,
    @SerialName("proof") val proof: String,
    @SerialName("method") val method: String,
)

@Serializable
data class DecisionResponse(
    @SerialName("ok") val ok: Boolean,
    @SerialName("status") val status: String = "",
)

@Serializable
data class SystemStatus(
    @SerialName("stop_active") val stopActive: Boolean,
    @SerialName("pending_approvals") val pendingApprovals: Int,
    @SerialName("api_version") val apiVersion: Int,
    @SerialName("llm_is_mock") val llmIsMock: Boolean,
)

@Serializable
data class TranscriptRequest(
    @SerialName("text") val text: String,
    @SerialName("confidence") val confidence: Double,
    @SerialName("language") val language: String,
    @SerialName("conversation_id") val conversationId: String? = null,
)

@Serializable
data class CapabilityFlag(
    @SerialName("available") val available: Boolean = false,
    @SerialName("is_mock") val isMock: Boolean = true,
    @SerialName("connected") val connected: Boolean = false,
    @SerialName("engine") val engine: String = "",
    @SerialName("transport") val transport: String = "",
)

@Serializable
data class StepUpCapability(
    @SerialName("scheme") val scheme: String = "",
    @SerialName("voice_allowed") val voiceAllowed: Boolean = false,
)

/** Backend-in açıq imkan cədvəli: yalnız `available`/`connected` olan imkan işləkmiş kimi göstərilir. */
@Serializable
data class Capabilities(
    @SerialName("api_version") val apiVersion: Int = 1,
    @SerialName("backend_mode") val backendMode: String = "mock",
    @SerialName("chat") val chat: CapabilityFlag = CapabilityFlag(),
    @SerialName("llm") val llm: CapabilityFlag = CapabilityFlag(),
    @SerialName("agent_runner") val agentRunner: CapabilityFlag = CapabilityFlag(),
    @SerialName("voice_tts") val voiceTts: CapabilityFlag = CapabilityFlag(),
    @SerialName("voice_stt") val voiceStt: CapabilityFlag = CapabilityFlag(),
    @SerialName("push") val push: CapabilityFlag = CapabilityFlag(),
    @SerialName("events") val events: CapabilityFlag = CapabilityFlag(),
    @SerialName("step_up") val stepUp: StepUpCapability = StepUpCapability(),
)

@Serializable
data class MemoryRecord(
    @SerialName("id") val id: String,
    @SerialName("category") val category: String,
    @SerialName("project") val project: String = "default",
    @SerialName("content") val content: String,
    @SerialName("provenance") val provenance: String = "",
    @SerialName("date") val date: String = "",
    @SerialName("untrusted") val untrusted: Boolean = true,
)

@Serializable
data class MemoryListResponse(@SerialName("records") val records: List<MemoryRecord>)

@Serializable
data class MemoryAddRequest(
    @SerialName("category") val category: String,
    @SerialName("content") val content: String,
    @SerialName("project") val project: String = "default",
)

@Serializable
data class MemoryAddResponse(
    @SerialName("created") val created: Boolean,
    @SerialName("record") val record: MemoryRecord,
)

@Serializable
data class AuthorityDto(
    @SerialName("permissions") val permissions: List<String> = emptyList(),
    @SerialName("sensitive_ok") val sensitiveOk: Boolean = false,
    @SerialName("max_task_cost") val maxTaskCost: Double = 0.0,
)

@Serializable
data class AgentDto(
    @SerialName("name") val name: String,
    @SerialName("department") val department: String,
    @SerialName("role") val role: String = "",
    @SerialName("status") val status: String = "active",
    @SerialName("budget") val budget: Double = 0.0,
    @SerialName("authority") val authority: AuthorityDto = AuthorityDto(),
)

@Serializable
data class AgentsResponse(@SerialName("agents") val agents: List<AgentDto>)
