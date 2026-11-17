package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import az.naymera.app.R
import az.naymera.app.data.ConnectionState

/** Əsas ekran: backend rejimi (mock/real), sistem vəziyyəti və imkan cədvəli. Backend-də olmayan imkan "yoxdur" göstərilir. */
@Composable
fun HomeScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val state by vm.home.collectAsStateWithLifecycle()
    val connection by vm.connection.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { vm.refreshHome() }
    val capabilities = state.capabilities

    Column(
        modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text(stringResource(R.string.home_title), style = MaterialTheme.typography.headlineSmall)
        ConnectionChip(connection)
        state.error?.let {
            Text(uiText(it), color = MaterialTheme.colorScheme.error)
            OutlinedButton(onClick = { vm.refreshHome() }) { Text(stringResource(R.string.retry)) }
        }
        if (capabilities != null) {
            val mock = capabilities.backendMode != "real"
            Card(
                Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = if (mock) MaterialTheme.colorScheme.tertiaryContainer else MaterialTheme.colorScheme.primaryContainer,
                ),
            ) {
                Column(Modifier.padding(16.dp)) {
                    Text(stringResource(if (mock) R.string.mock_badge else R.string.state_real),
                        style = MaterialTheme.typography.labelLarge)
                    Text(stringResource(if (mock) R.string.mode_mock else R.string.mode_real))
                }
            }
        }
        state.status?.let { s ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(stringResource(R.string.home_status), style = MaterialTheme.typography.titleMedium)
                    Text(stringResource(if (s.stopActive) R.string.home_stop_active else R.string.home_running))
                    Text(stringResource(R.string.home_pending, s.pendingApprovals))
                }
            }
        }
        if (capabilities != null) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(stringResource(R.string.home_capabilities), style = MaterialTheme.typography.titleMedium)
                    CapabilityRow(R.string.cap_chat, capabilities.chat.available, false)
                    CapabilityRow(R.string.cap_llm, capabilities.llm.available, capabilities.llm.isMock)
                    CapabilityRow(R.string.cap_agent_runner, capabilities.agentRunner.connected, false)
                    CapabilityRow(R.string.cap_voice_tts, capabilities.voiceTts.available, capabilities.voiceTts.isMock)
                    CapabilityRow(R.string.cap_voice_stt, capabilities.voiceStt.available, capabilities.voiceStt.isMock)
                    CapabilityRow(R.string.cap_push, capabilities.push.available, false)
                    CapabilityRow(R.string.cap_events, capabilities.events.available, false)
                    if (!capabilities.agentRunner.connected) {
                        Text(stringResource(R.string.feature_unavailable), color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

@Composable
private fun CapabilityRow(label: Int, available: Boolean, isMock: Boolean) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(stringResource(label))
        val state = when {
            !available -> R.string.state_missing
            isMock -> R.string.state_mock
            else -> R.string.state_available
        }
        Text(
            stringResource(state),
            color = if (available && !isMock) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
fun ConnectionChip(state: ConnectionState) {
    val text = when (state) {
        ConnectionState.Connected -> R.string.conn_connected
        ConnectionState.Reconnecting -> R.string.conn_reconnecting
        ConnectionState.Offline -> R.string.conn_offline
    }
    Text(
        stringResource(text),
        style = MaterialTheme.typography.labelMedium,
        color = if (state == ConnectionState.Connected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error,
    )
}
