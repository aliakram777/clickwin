package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import az.naymera.app.R

/** Yaddaş: backend yaddaşındakı qeydlər (etibarsız məlumat) və sahib qeydi əlavə etmək. LLM yaddaşı dəyişə bilmir. */
@Composable
fun MemoryScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val state by vm.memory.collectAsStateWithLifecycle()
    var input by remember { mutableStateOf("") }
    LaunchedEffect(Unit) { vm.loadMemory() }

    Column(modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.memory_title), style = MaterialTheme.typography.titleLarge)
        Text(stringResource(R.string.memory_untrusted), color = MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.bodySmall)
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = input, onValueChange = { if (it.length <= 4000) input = it }, modifier = Modifier.weight(1f),
                placeholder = { Text(stringResource(R.string.memory_hint)) }, shape = RoundedCornerShape(20.dp), maxLines = 3,
            )
            Button(enabled = input.isNotBlank(), onClick = { vm.addMemory(input); input = "" }) {
                Text(stringResource(R.string.memory_add))
            }
        }
        state.notice?.let { Text(uiText(it), color = MaterialTheme.colorScheme.primary) }
        state.error?.let { Text(uiText(it), color = MaterialTheme.colorScheme.error) }
        if (state.items.isEmpty() && state.error == null) {
            Text(stringResource(R.string.memory_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            items(state.items, key = { it.id }) { r ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(14.dp)) {
                        // Düz mətn: yaddaş məzmunu heç vaxt təlimat kimi icra olunmur.
                        Text(r.content)
                        Text(r.date.take(10), style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}
