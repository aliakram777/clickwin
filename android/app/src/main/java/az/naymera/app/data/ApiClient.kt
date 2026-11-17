package az.naymera.app.data

import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.URI
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
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
        val uri = try { URI(raw.trim().trimEnd('/')) } catch (e: Exception) { throw IllegalArgumentException("invalid") }
        val host = uri.host ?: throw IllegalArgumentException("invalid")
        val ok = uri.scheme == "https" || (uri.scheme == "http" && host in LOCAL_HOSTS)
        require(ok) { "https only" }
        require(uri.userInfo == null && uri.query == null && uri.fragment == null) { "invalid" }
        return uri.toString()
    }
}

class ApiClient(private val store: SessionStore) {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(20, TimeUnit.SECONDS)
        .callTimeout(30, TimeUnit.SECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()
    private val media = "application/json; charset=utf-8".toMediaType()
    private val refreshMutex = Mutex()
    private val _sessionExpired = MutableStateFlow(false)

    /** true olduqda yenilənmə uğursuzdur: istifadəçi cihazı yenidən cütləşdirməlidir. */
    val sessionExpired: StateFlow<Boolean> = _sessionExpired.asStateFlow()

    /** `{id}` / `{key}` yer tutucularını doldurur (dəyər yalnız təhlükəsiz simvollardan ibarət ola bilər). */
    private fun path(template: String, vararg args: Pair<String, String>): String {
        var out = template
        for ((k, v) in args) {
            require(Regex("[A-Za-z0-9_.:\\-]{1,64}").matches(v)) { "invalid path parameter" }
            out = out.replace("{$k}", v)
        }
        return out
    }

    /** Eksponensial geri çəkilmə (backoff): 0.5s, 1s, 2s … yuxarı hədd 8s. */
    private fun backoff(attempt: Int): Long = minOf(8_000L, 500L shl attempt)

    private fun mapIo(e: IOException): ApiException {
        val failure = when (e) {
            is SocketTimeoutException -> ApiFailure.Timeout
            is UnknownHostException -> ApiFailure.Offline
            is ConnectException -> ApiFailure.Offline
            is SSLException -> ApiFailure.Unknown
            else -> ApiFailure.Offline
        }
        return ApiException(0, "network", failure.name, failure)
    }

    private suspend fun execute(method: String, path: String, body: String?, query: String, auth: Boolean): String =
        withContext(Dispatchers.IO) {
            val base = ServerUrl.validate(store.serverUrl)
            val builder = Request.Builder().url(base + path + query)
            if (auth) builder.header("Authorization", "Bearer " + store.accessToken)
            builder.method(method, body?.toRequestBody(media) ?: if (method == "POST") "{}".toRequestBody(media) else null)
            try {
                http.newCall(builder.build()).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    if (!resp.isSuccessful) {
                        val err = runCatching { json.parseToJsonElement(text).jsonObject["error"]?.jsonObject }.getOrNull()
                        throw ApiException(
                            resp.code,
                            err?.get("code")?.jsonPrimitive?.content ?: "http_${resp.code}",
                            err?.get("message")?.jsonPrimitive?.content ?: "HTTP ${resp.code}",
                        )
                    }
                    text
                }
            } catch (e: IOException) {
                throw mapIo(e)
            }
        }

    /** Eyni anda gələn 401-lər üçün tək yenilənmə (Mutex): ikinci koroutin artıq yenilənmiş tokeni görür. */
    private suspend fun refreshTokens(stale: String): Boolean = refreshMutex.withLock {
        if (store.accessToken != stale) return@withLock true
        try {
            val text = execute(
                "POST", "/v1/token/refresh",
                json.encodeToString(RefreshRequest.serializer(), RefreshRequest(store.refreshToken)), "", false,
            )
            store.saveTokens(json.decodeFromString(TokenResponse.serializer(), text))
            true
        } catch (e: ApiException) {
            if (e.failure == ApiFailure.Unauthorized) _sessionExpired.value = true
            false
        }
    }

    /** Yalnız GET (idempotent) şəbəkə xətasında backoff ilə təkrarlanır; POST avtomatik təkrarlanmır (təkrar icra riski). */
    private suspend fun <T> call(
        method: String, path: String, body: String?, ser: KSerializer<T>, query: String = "", auth: Boolean = true,
    ): T {
        var attempt = 0
        var refreshed = false
        while (true) {
            val usedToken = store.accessToken
            try {
                return json.decodeFromString(ser, execute(method, path, body, query, auth))
            } catch (e: ApiException) {
                if (e.status == 401 && auth && !refreshed && store.refreshToken.isNotEmpty() && refreshTokens(usedToken)) {
                    refreshed = true
                    continue
                }
                if (e.status == 0 && method == "GET" && attempt < 2) {
                    delay(backoff(attempt))
                    attempt++
                    continue
                }
                if (e.status == 401 && auth) _sessionExpired.value = true
                throw e
            }
        }
    }

    fun resetSessionExpired() {
        _sessionExpired.value = false
    }

    suspend fun pair(code: String, deviceName: String, publicKeyB64: String, signatureB64: String): PairResponse {
        val r = call(
            "POST", "/v1/pair",
            json.encodeToString(PairRequest.serializer(), PairRequest(code, deviceName, publicKeyB64, signatureB64)),
            PairResponse.serializer(), auth = false,
        )
        store.savePairing(r)
        _sessionExpired.value = false
        return r
    }

    suspend fun logout() {
        try { call("POST", "/v1/logout", "{}", OkOnly.serializer()) } catch (_: Exception) {}
        store.clear()
    }

    suspend fun capabilities(): Capabilities = call("GET", "/v1/capabilities", null, Capabilities.serializer())

    suspend fun status(): SystemStatus = call("GET", "/v1/system/status", null, SystemStatus.serializer())

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

    suspend fun memory(query: String): List<MemoryRecord> {
        val q = if (query.isBlank()) "?limit=50" else "?limit=50&query=" + java.net.URLEncoder.encode(query.take(200), "UTF-8")
        return call("GET", "/v1/memory", null, MemoryListResponse.serializer(), query = q).records
    }

    suspend fun addMemory(content: String): MemoryAddResponse =
        call("POST", "/v1/memory", json.encodeToString(MemoryAddRequest.serializer(), MemoryAddRequest("note", content)),
            MemoryAddResponse.serializer())

    suspend fun agents(): List<AgentDto> =
        call("GET", "/v1/company/agents", null, AgentsResponse.serializer()).agents

    suspend fun approvals(): List<ApprovalDto> =
        call("GET", "/v1/approvals", null, ApprovalsResponse.serializer()).approvals

    suspend fun approvalChallenge(id: String, decision: String): ChallengeResponse =
        call("POST", path("/v1/approvals/{id}/challenge", "id" to id),
            json.encodeToString(DecisionChallengeRequest.serializer(), DecisionChallengeRequest(decision)), ChallengeResponse.serializer())

    suspend fun approvalDecision(id: String, req: DecisionRequest): DecisionResponse =
        call("POST", path("/v1/approvals/{id}/decision", "id" to id),
            json.encodeToString(DecisionRequest.serializer(), req), DecisionResponse.serializer())

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
