package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import az.naymera.app.data.AppNotification
import az.naymera.app.data.EventSync
import az.naymera.app.data.ApiClient
import androidx.compose.runtime.LaunchedEffect

/** Server hadisə axınından gələn bildirişlər. Bildirişlər yalnız təklifdir: heç bir əməliyyatı icra etmir. */
@Composable
fun NotificationsScreen(sync: EventSync, api: ApiClient, modifier: Modifier = Modifier) {
    val items = remember { mutableStateListOf<AppNotification>() }
    LaunchedEffect(Unit) {
        try {
            sync.stream().collect { events ->
                events.filter { it.type == "notification" }.forEach {
                    runCatching { api.decodeNotification(it.payload) }.getOrNull()?.let { n -> items.add(0, n) }
                }
            }
        } catch (_: Exception) {
        }
    }
    if (items.isEmpty()) {
        Column(modifier.fillMaxSize().padding(24.dp)) {
            Text("Hələ bildiriş yoxdur.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        return
    }
    LazyColumn(
        modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        items(items, key = { it.id }) { n ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(n.title, style = MaterialTheme.typography.titleMedium)
                    Text(n.body, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (n.isMock) {
                        Text("SİMULYASİYA", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.secondary)
                    }
                }
            }
        }
    }
}
