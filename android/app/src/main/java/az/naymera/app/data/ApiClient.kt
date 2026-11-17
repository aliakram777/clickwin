package az.naymera.app.data

import java.net.URI
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** Yalnız server ünvanına qoşulur. HTTPS məcburidir; düz HTTP yalnız yerli debug hostları üçündür (şəbəkə təhlükəsizlik konfiqurasiyası da bunu tətbiq edir). */
object ServerUrl {
    private val LOCAL_HOSTS = setOf("10.0.2.2", "localhost", "127.0.0.1")

    fun validate(raw: String): String {
        val uri = try { URI(raw.trim().trimEnd('/')) } catch (e: Exception) { throw IllegalArgumentException("Ünvan yanlışdır") }
        val host = uri.host ?: throw IllegalArgumentException("Ünvan yanlışdır")
        val ok = uri.scheme == "https" || (uri.scheme == "http" && host in LOCAL_HOSTS)
        require(ok) { "Yalnız HTTPS ünvanı qəbul olunur" }
        require(uri.userInfo == null && uri.query == null && uri.fragment == null) { "Ünvan yanlışdır" }
        return uri.toString()
    }
}

class ApiClient(private val store: SessionStore) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .followRedirects(false)
        .build()
    private val media = "application/json; charset=utf-8".toMediaType()

    /** `{id}` / `{key}` yer tutucularını doldurur (dəyər yalnız təhlükəsiz simvollardan ibarət ola bilər). */
    private fun path(template: String, vararg args: Pair<String, String>): String {
        var out = template
        for ((k, v) in args) {
            require(Regex("[A-Za-z0-9_.:\\-]{1,64}").matches(v)) { "yanlış yol parametri" }
            out = out.replace("{$k}", v)
        }
        return out
    }

    private suspend fun <T> call(
        method: String, path: String, body: String?, ser: KSerializer<T>, query: String = "", auth: Boolean = true,
        retry: Boolean = true,
    ): T = withContext(Dispatchers.IO) {
        val base = ServerUrl.validate(store.serverUrl)
        val builder = Request.Builder().url(base + path + query)
        if (auth) builder.header("Authorization", "Bearer " + store.accessToken)
        builder.method(method, body?.toRequestBody(media) ?: if (method == "POST") "{}".toRequestBody(media) else null)
        http.newCall(builder.build()).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (resp.code == 401 && auth && retry && store.refreshToken.isNotEmpty() && refreshTokens()) {
                return@withContext call(method, path, body, ser, query, auth, retry = false)
            }
            if (!resp.isSuccessful) {
                val err = runCatching { json.parseToJsonElement(text).jsonObject["error"]?.jsonObject }.getOrNull()
                throw ApiException(
                    resp.code,
                    err?.get("code")?.jsonPrimitive?.content ?: "http_${resp.code}",
                    err?.get("message")?.jsonPrimitive?.content ?: "Sorğu uğursuz oldu",
                )
            }
            json.decodeFromString(ser, text)
        }
    }

    private suspend fun refreshTokens(): Boolean = try {
        val t = call("POST", "/v1/token/refresh", json.encodeToString(RefreshRequest.serializer(), RefreshRequest(store.refreshToken)),
            TokenResponse.serializer(), auth = false, retry = false)
        store.saveTokens(t)
        true
    } catch (e: Exception) {
        false
    }

    suspend fun pair(code: String, deviceName: String): PairResponse {
        val r = call("POST", "/v1/pair", json.encodeToString(PairRequest.serializer(), PairRequest(code, deviceName)),
            PairResponse.serializer(), auth = false)
        store.savePairing(r)
        return r
    }

    suspend fun logout() {
        try { call("POST", "/v1/logout", "{}", OkOnly.serializer()) } catch (_: Exception) {}
        store.clear()
    }

    suspend fun sendMessage(text: String, conversationId: String?): ChatResponse =
        call("POST", "/v1/chat/messages", json.encodeToString(ChatRequest.serializer(), ChatRequest(text, conversationId)), ChatResponse.serializer())

    suspend fun sendTranscript(text: String, confidence: Double, conversationId: String?): ChatResponse =
        call("POST", "/v1/voice/transcripts",
            json.encodeToString(TranscriptRequest.serializer(), TranscriptRequest(text, confidence, "az-AZ", conversationId)),
            ChatResponse.serializer())

    suspend fun conversations(): List<Conversation> =
        call("GET", "/v1/chat/conversations", null, ConversationsResponse.serializer()).conversations

    suspend fun messages(id: String): List<ChatMessage> =
        call("GET", path("/v1/chat/conversations/{id}/messages", "id" to id), null, MessagesResponse.serializer()).messages

    suspend fun events(cursor: Long): EventsResponse =
        call("GET", "/v1/events", null, EventsResponse.serializer(), query = "?cursor=$cursor&limit=100")

    suspend fun ackEvents(seq: Long) {
        call("POST", "/v1/events/ack", json.encodeToString(AckRequest.serializer(), AckRequest(seq)), OkOnly.serializer())
    }

    suspend fun approvals(): List<ApprovalDto> =
        call("GET", "/v1/approvals", null, ApprovalsResponse.serializer()).approvals

    suspend fun approvalChallenge(id: String, decision: String): ChallengeResponse =
        call("POST", path("/v1/approvals/{id}/challenge", "id" to id),
            json.encodeToString(DecisionChallengeRequest.serializer(), DecisionChallengeRequest(decision)), ChallengeResponse.serializer())

    suspend fun approvalDecision(id: String, req: DecisionRequest): DecisionResponse =
        call("POST", path("/v1/approvals/{id}/decision", "id" to id),
            json.encodeToString(DecisionRequest.serializer(), req), DecisionResponse.serializer())

    suspend fun status(): SystemStatus = call("GET", "/v1/system/status", null, SystemStatus.serializer())

    suspend fun stop() {
        call("POST", "/v1/system/stop", "{}", OkOnly.serializer())
    }

    suspend fun ackNotification(key: String) {
        call("POST", path("/v1/notifications/{key}/ack", "key" to key), "{}", OkOnly.serializer())
    }

    fun decodeNotification(payload: JsonObject): AppNotification =
        json.decodeFromJsonElement(AppNotification.serializer(), payload)
}

@kotlinx.serialization.Serializable
data class OkOnly(@kotlinx.serialization.SerialName("ok") val ok: Boolean = true)
