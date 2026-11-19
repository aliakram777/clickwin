package az.naymera.app.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import az.naymera.app.R
import az.naymera.app.data.AiMode
import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiException
import az.naymera.app.data.ApiFailure
import az.naymera.app.data.ClientRoute
import az.naymera.app.data.LocalState
import az.naymera.app.data.OnDeviceLocal
import az.naymera.app.data.decideRoute
import az.naymera.app.data.ChatMessage
import az.naymera.app.voice.SpeechToText
import az.naymera.app.voice.VoiceProfile
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.UUID

data class ChatUiState(
    val conversationId: String? = null,
    val messages: List<ChatMessage> = emptyList(),
    val sending: Boolean = false,
    val error: UiText? = null,
)

/** Söhbət mətni (yazı və ya səs transkripti) serverə etibarsız giriş kimi göndərilir; bu sinif heç nəyi təsdiq/icra etmir. */
class ChatViewModel(
    private val api: ApiClient,
    private val stt: SpeechToText,
    private val onDevice: OnDeviceLocal,
    private val mode: () -> AiMode,
) : ViewModel() {
    private var pending: Pair<String, String>? = null // (mətn, clientRequestId): təkrar cəhd eyni identifikatorla (ikiqat icra yoxdur)
    private var localSeq = 0L

    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    val voiceAvailable: Boolean get() = stt.available
    val voiceIsMock: Boolean get() = stt.isMock

    fun send(text: String) {
        val clean = text.trim()
        if (clean.isEmpty() || _state.value.sending) return
        val clientRequestId = pending?.takeIf { it.first == clean }?.second ?: UUID.randomUUID().toString().also { pending = clean to it }
        _state.value = _state.value.copy(sending = true, error = null)
        viewModelScope.launch {
            try {
                val r = api.sendMessage(clean, _state.value.conversationId, clientRequestId)
                pending = null
                _state.value = _state.value.copy(
                    conversationId = r.conversationId,
                    messages = _state.value.messages + r.userMessage + r.assistantMessage,
                    sending = false,
                )
            } catch (e: ApiException) {
                handleFailure(clean, e)
            } catch (e: Exception) {
                _state.value = _state.value.copy(sending = false, error = e.toUiText())
            }
        }
    }

    /**
     * Server cavab vermədi. Yalnız bağlantı itkisində (Offline/Timeout) və CLOUD seçilməyibsə cihazdakı Local AI-yə keçilir;
     * cihazda model qurulmayıbsa bu AÇIQ bildirilir (real imkan kimi göstərilmir). Cavab serverə yazılmır və heç nəyi təsdiq/icra etmir.
     */
    private suspend fun handleFailure(text: String, e: ApiException) {
        when (decideRoute(mode(), e.failure, onDevice)) {
            ClientRoute.OnDevice -> {
                val answer = try { onDevice.generate(text) } catch (_: Exception) { null }
                if (answer == null) {
                    _state.value = _state.value.copy(sending = false, error = UiText(R.string.ai_offline_no_local))
                    return
                }
                val cid = _state.value.conversationId ?: "device"
                val now = System.currentTimeMillis() / 1000.0
                val user = ChatMessage(--localSeq, cid, "user", text, "text", now, true)
                val bot = ChatMessage(--localSeq, cid, "assistant", answer, "text", now, true, "", "llm_local_device",
                    onDevice.state == LocalState.Simulation, "device", "", "device_fallback")
                pending = null
                _state.value = _state.value.copy(messages = _state.value.messages + user + bot, sending = false)
            }
            ClientRoute.Unavailable -> {
                val offline = e.failure == ApiFailure.Offline || e.failure == ApiFailure.Timeout
                val err = if (offline && mode() != AiMode.Cloud) UiText(R.string.ai_offline_no_local) else e.toUiText()
                _state.value = _state.value.copy(sending = false, error = err)
            }
            ClientRoute.Server -> _state.value = _state.value.copy(sending = false, error = e.toUiText())
        }
    }

    /** Səsli giriş: real STT qoşulmayıbsa (simulyasiya) heç nə göndərilmir. */
    fun listen() {
        if (!stt.available) {
            _state.value = _state.value.copy(error = UiText(R.string.chat_voice_unavailable))
            return
        }
        viewModelScope.launch {
            val t = stt.listen(VoiceProfile()) ?: return@launch
            try {
                val r = api.sendTranscript(t.text, t.confidence, _state.value.conversationId)
                _state.value = _state.value.copy(
                    conversationId = r.conversationId,
                    messages = _state.value.messages + r.userMessage + r.assistantMessage,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.toUiText())
            }
        }
    }

    fun open(conversationId: String) {
        viewModelScope.launch {
            try {
                _state.value = ChatUiState(conversationId, api.messages(conversationId))
            } catch (e: Exception) {
                _state.value = _state.value.copy(error = e.toUiText())
            }
        }
    }

    fun newConversation() {
        _state.value = ChatUiState()
    }
}
