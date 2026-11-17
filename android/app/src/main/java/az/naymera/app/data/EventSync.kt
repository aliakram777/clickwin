package az.naymera.app.data

import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

/**
 * Hadisə axını: server kursoru ilə qısa sorğulama (push/FCM sonraki mərhələdir). Kursor yalnız hadisələr işlənəndən
 * sonra irəlilədilir və server tərəfə `ack` göndərilir.
 */
class EventSync(private val api: ApiClient, private val store: SessionStore) {
    fun stream(intervalMs: Long = 5_000): Flow<List<MobileEvent>> = flow {
        while (true) {
            try {
                val page = api.events(store.eventCursor)
                if (page.events.isNotEmpty()) {
                    emit(page.events)
                    store.eventCursor = page.nextCursor
                    api.ackEvents(page.nextCursor)
                }
            } catch (e: ApiException) {
                if (e.status == 401) throw e
            } catch (_: Exception) {
                // Şəbəkə xətası: növbəti dövrdə təkrar.
            }
            delay(intervalMs)
        }
    }
}
