package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import az.naymera.app.R

/** Fəaliyyət jurnalı (yalnız oxu, ən yeni əvvəl). Jurnal mətni etibarsızdır: yalnız göstərilir. */
@Composable
fun JournalScreen(vm: CoreViewModel, modifier: Modifier = Modifier) {
    val state by vm.journal.state.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { vm.journal.refresh() }
    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(stringResource(R.string.journal_title), style = MaterialTheme.typography.titleLarge)
        LoadView(state, onRetry = { vm.run { vm.journal.refresh() } }) { entries ->
            if (entries.isEmpty()) Text(stringResource(R.string.journal_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
            entries.forEach { e ->
                Text(entryLine(e.seq, e.ts.take(19) + "  " + e.kind), style = MaterialTheme.typography.bodyMedium)
                if (e.detail.isNotEmpty()) Text(e.detail.entries.joinToString(", ") { it.key + "=" + it.value }, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Text(stringResource(R.string.untrusted_note), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
