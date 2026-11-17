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
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import az.naymera.app.R
import az.naymera.app.data.ApiClient
import az.naymera.app.data.Conversation

/** Söhbət tarixçəsi (server tərəfdə saxlanılır). */
@Composable
fun HistoryScreen(api: ApiClient, onOpen: (String) -> Unit, modifier: Modifier = Modifier) {
    var items by remember { mutableStateOf<List<Conversation>>(emptyList()) }
    var error by remember { mutableStateOf<UiText?>(null) }
    var reload by remember { mutableIntStateOf(0) }
    LaunchedEffect(reload) {
        error = null
        try {
            items = api.conversations()
        } catch (e: Exception) {
            error = e.toUiText()
        }
    }
    Column(modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.history_title), style = MaterialTheme.typography.titleLarge)
        error?.let {
            Text(uiText(it), color = MaterialTheme.colorScheme.error)
            OutlinedButton(onClick = { reload++ }) { Text(stringResource(R.string.retry)) }
        }
        if (items.isEmpty() && error == null) {
            Text(stringResource(R.string.history_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        LazyColumn(contentPadding = PaddingValues(bottom = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            items(items, key = { it.id }) { c ->
                Card(Modifier.fillMaxWidth().clickable { onOpen(c.id) }) {
                    Column(Modifier.padding(16.dp)) {
                        Text(c.title.ifBlank { stringResource(R.string.history_untitled) }, style = MaterialTheme.typography.titleMedium)
                        Text(stringResource(R.string.history_messages, c.messageCount), color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}
