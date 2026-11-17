package az.naymera.app.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import az.naymera.app.R
import az.naymera.app.data.AgentDto
import az.naymera.app.data.ApiClient
import az.naymera.app.data.AppNotification
import az.naymera.app.data.Capabilities
import az.naymera.app.data.ConnectionState
import az.naymera.app.data.EventSync
import az.naymera.app.data.MemoryRecord
import az.naymera.app.data.SystemStatus
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class HomeState(
    val capabilities: Capabilities? = null,
    val status: SystemStatus? = null,
    val error: UiText? = null,
    val loading: Boolean = false,
)

data class ListState<T>(val items: List<T> = emptyList(), val error: UiText? = null, val notice: UiText? = null)

/** Backend-dən gələn məlumat: imkanlar, vəziyyət, bildirişlər, yaddaş, agentlər. Yaddaş/söhbət mətni etibarsız məlumatdır. */
class MainViewModel(private val api: ApiClient, private val sync: EventSync) : ViewModel() {
    private val _home = MutableStateFlow(HomeState())
    val home: StateFlow<HomeState> = _home.asStateFlow()
    private val _notifications = MutableStateFlow<List<AppNotification>>(emptyList())
    val notifications: StateFlow<List<AppNotification>> = _notifications.asStateFlow()
    private val _memory = MutableStateFlow(ListState<MemoryRecord>())
    val memory: StateFlow<ListState<MemoryRecord>> = _memory.asStateFlow()
    private val _agents = MutableStateFlow(ListState<AgentDto>())
    val agents: StateFlow<ListState<AgentDto>> = _agents.asStateFlow()
    val connection: StateFlow<ConnectionState> = sync.state
    private var syncJob: Job? = null

    fun refreshHome() {
        _home.value = _home.value.copy(loading = true, error = null)
        viewModelScope.launch {
            try {
                _home.value = HomeState(api.capabilities(), api.status())
            } catch (e: Exception) {
                _home.value = _home.value.copy(loading = false, error = e.toUiText())
            }
        }
    }

    fun startSync() {
        if (syncJob?.isActive == true) return
        syncJob = viewModelScope.launch {
            try {
                sync.stream().collect { events ->
                    events.filter { it.type == "notification" }.forEach {
                        runCatching { api.decodeNotification(it.payload) }.getOrNull()?.let { n ->
                            _notifications.value = listOf(n) + _notifications.value.filter { old -> old.id != n.id }
                        }
                    }
                }
            } catch (_: Exception) {
                // 401: ApiClient.sessionExpired bayrağı UI-ı cütləşməyə qaytarır.
            }
        }
    }

    fun loadMemory(query: String = "") {
        viewModelScope.launch {
            _memory.value = try { ListState(api.memory(query)) } catch (e: Exception) { _memory.value.copy(error = e.toUiText()) }
        }
    }

    fun addMemory(text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        viewModelScope.launch {
            try {
                api.addMemory(clean)
                _memory.value = ListState(api.memory(""), notice = UiText(R.string.memory_saved))
            } catch (e: Exception) {
                _memory.value = _memory.value.copy(error = e.toUiText(), notice = null)
            }
        }
    }

    fun loadAgents() {
        viewModelScope.launch {
            _agents.value = try { ListState(api.agents()) } catch (e: Exception) { _agents.value.copy(error = e.toUiText()) }
        }
    }
}
