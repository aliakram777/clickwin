package az.naymera.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import az.naymera.app.data.ApiClient
import az.naymera.app.data.Conversation

/** Söhbət tarixçəsi (server tərəfdə saxlanılır). */
@Composable
fun HistoryScreen(api: ApiClient, onOpen: (String) -> Unit, modifier: Modifier = Modifier) {
    var items by remember { mutableStateOf<List<Conversation>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        try {
            items = api.conversations()
        } catch (e: Exception) {
            error = "Tarixçə yüklənmədi"
        }
    }
    if (items.isEmpty()) {
        Column(modifier.fillMaxSize().padding(24.dp)) {
            Text(error ?: "Tarixçə boşdur.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        return
    }
    LazyColumn(
        modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        items(items, key = { it.id }) { c ->
            Card(Modifier.fillMaxWidth().clickable { onOpen(c.id) }) {
                Column(Modifier.padding(16.dp)) {
                    Text(c.title.ifBlank { "Söhbət" }, style = MaterialTheme.typography.titleMedium)
                    Text("${c.messageCount} mesaj", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}
