package az.naymera.app.data

import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flow

enum class ConnectionState { Connected, Reconnecting, Offline }

/**
 * Hadisə axını: server kursoru ilə qısa sorğulama (push/FCM yoxdur). Kursor yalnız hadisələr işlənəndən sonra irəlilədilir
 * və server tərəfə `ack` göndərilir. Əlaqə kəsilərsə eksponensial backoff ilə yenidən cəhd edilir; 401 yuxarı atılır.
 */
class EventSync(private val api: ApiClient, private val store: SessionStore) {
    private val _state = MutableStateFlow(ConnectionState.Connected)
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private fun backoff(failures: Int): Long = minOf(60_000L, 5_000L shl minOf(failures, 4))

    fun stream(intervalMs: Long = 5_000): Flow<List<MobileEvent>> = flow {
        var failures = 0
        while (true) {
            try {
                val page = api.events(store.eventCursor)
                failures = 0
                _state.value = ConnectionState.Connected
                if (page.events.isNotEmpty()) {
                    emit(page.events)
                    store.eventCursor = page.nextCursor
                    api.ackEvents(page.nextCursor)
                }
                delay(intervalMs)
            } catch (e: ApiException) {
                if (e.status == 401) throw e
                failures++
                _state.value = if (failures >= 3 || e.failure == ApiFailure.Offline) ConnectionState.Offline else ConnectionState.Reconnecting
                delay(backoff(failures))
            }
        }
    }
}
