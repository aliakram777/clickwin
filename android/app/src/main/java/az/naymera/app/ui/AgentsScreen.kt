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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import az.naymera.app.R

/** Agentlər (yalnız oxu): səlahiyyəti yalnız sahib serverdə dəyişir. Agent icraçısı qoşulmayıbsa bu açıq göstərilir. */
@Composable
fun AgentsScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val state by vm.agents.collectAsStateWithLifecycle()
    val home by vm.home.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { vm.loadAgents(); vm.refreshHome() }
    val agentRunner = home.capabilities?.agentRunner

    Column(modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.agents_title), style = MaterialTheme.typography.titleLarge)
        Text(stringResource(R.string.agents_readonly), color = MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.bodySmall)
        if (agentRunner == null || !agentRunner.connected) {
            Text(stringResource(R.string.agent_runner_missing), color = MaterialTheme.colorScheme.secondary)
        }
        state.error?.let { Text(uiText(it), color = MaterialTheme.colorScheme.error) }
        if (state.items.isEmpty() && state.error == null) {
            Text(stringResource(R.string.agents_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            items(state.items, key = { it.name }) { a ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(a.name, style = MaterialTheme.typography.titleMedium)
                        Text(a.department + " / " + a.role, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(stringResource(R.string.agent_budget, a.budget.toString()))
                        Text(stringResource(R.string.agent_permissions, a.authority.permissions.joinToString(", ")))
                    }
                }
            }
        }
    }
}
