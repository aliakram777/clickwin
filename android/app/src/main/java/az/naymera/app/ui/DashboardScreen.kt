package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import az.naymera.app.R
import az.naymera.app.data.ConnectionState
import az.naymera.app.domain.ActionResult
import az.naymera.app.domain.DashboardData

/** Əsas panel: brend, əlaqə və AI statusu (mock/real açıq), məqsədlər, gözləyən təsdiqlər, son fəaliyyət. STOP həmişə əlçatandır. */
@Composable
fun DashboardScreen(
    vm: CoreViewModel, main: MainViewModel, onGoals: () -> Unit, onApprovals: () -> Unit, onJournal: () -> Unit, onCapabilities: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val state by vm.dashboard.state.collectAsStateWithLifecycle()
    val connection by main.connection.collectAsStateWithLifecycle()
    val home by main.home.collectAsStateWithLifecycle()
    var stopResult by remember { mutableStateOf<ActionResult?>(null) }
    LaunchedEffect(Unit) { vm.dashboard.refresh(); main.refreshHome() }

    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text(stringResource(R.string.app_name), style = MaterialTheme.typography.headlineMedium, color = MaterialTheme.colorScheme.primary)
        Text(stringResource(R.string.app_tagline), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            ConnectionChip(connection)
            val mock = home.capabilities?.llm?.isMock ?: true
            Text(stringResource(if (mock) R.string.dash_ai_mock else R.string.dash_ai_real), style = MaterialTheme.typography.labelMedium)
        }
        LoadView(state, onRetry = { vm.run { vm.dashboard.refresh() } }) { d -> DashboardCards(d, onGoals, onApprovals, onJournal) }
        Button(
            onClick = { vm.run { stopResult = vm.dashboard.stop() } },
            colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error),
            modifier = Modifier.fillMaxWidth(),
        ) { Text(stringResource(R.string.dash_stop)) }
        ResultLine(stopResult)
        TextButton(onClick = onCapabilities) { Text(stringResource(R.string.nav_capabilities)) }
    }
}

@Composable
private fun DashboardCards(d: DashboardData, onGoals: () -> Unit, onApprovals: () -> Unit, onJournal: () -> Unit) {
    if (d.core?.stopActive == true || d.system?.stopActive == true) {
        Text(stringResource(R.string.dash_stop_active), color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.titleMedium)
    }
    d.core?.let { Text(stringResource(R.string.dash_autonomy, it.autonomy), color = MaterialTheme.colorScheme.onSurfaceVariant) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(stringResource(R.string.dash_goals), style = MaterialTheme.typography.titleMedium)
            Text(stringResource(R.string.dash_goals_summary, d.goals.size, d.goals.count { it.status == "active" }))
            TextButton(onClick = onGoals) { Text(stringResource(R.string.goals_title)) }
        }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(stringResource(R.string.home_pending, d.pendingApprovals), style = MaterialTheme.typography.titleMedium)
            TextButton(onClick = onApprovals) { Text(stringResource(R.string.nav_approvals)) }
        }
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(stringResource(R.string.dash_recent), style = MaterialTheme.typography.titleMedium)
            if (d.recent.isEmpty()) Text(stringResource(R.string.dash_no_activity), color = MaterialTheme.colorScheme.onSurfaceVariant)
            d.recent.forEach { Text(entryLine(it.seq, it.kind), style = MaterialTheme.typography.bodySmall) }
            TextButton(onClick = onJournal) { Text(stringResource(R.string.nav_journal)) }
        }
    }
}
