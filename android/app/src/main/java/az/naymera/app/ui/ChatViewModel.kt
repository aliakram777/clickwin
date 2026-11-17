package az.naymera.app.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import az.naymera.app.R
import az.naymera.app.data.ApiClient
import az.naymera.app.data.ChatMessage
import az.naymera.app.voice.SpeechToText
import az.naymera.app.voice.VoiceProfile
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class ChatUiState(
    val conversationId: String? = null,
    val messages: List<ChatMessage> = emptyList(),
    val sending: Boolean = false,
    val error: UiText? = null,
)

/** Söhbət mətni (yazı və ya səs transkripti) serverə etibarsız giriş kimi göndərilir; bu sinif heç nəyi təsdiq/icra etmir. */
class ChatViewModel(private val api: ApiClient, private val stt: SpeechToText) : ViewModel() {
    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state.asStateFlow()

    val voiceAvailable: Boolean get() = stt.available
    val voiceIsMock: Boolean get() = stt.isMock

    fun send(text: String) {
        val clean = text.trim()
        if (clean.isEmpty() || _state.value.sending) return
        _state.value = _state.value.copy(sending = true, error = null)
        viewModelScope.launch {
            try {
                val r = api.sendMessage(clean, _state.value.conversationId)
                _state.value = _state.value.copy(
                    conversationId = r.conversationId,
                    messages = _state.value.messages + r.userMessage + r.assistantMessage,
                    sending = false,
                )
            } catch (e: Exception) {
                _state.value = _state.value.copy(sending = false, error = e.toUiText())
            }
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
