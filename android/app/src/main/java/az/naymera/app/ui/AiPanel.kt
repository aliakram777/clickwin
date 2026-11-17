package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
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
import az.naymera.app.data.AiEngine
import az.naymera.app.data.AiMode
import az.naymera.app.data.EngineBadge
import az.naymera.app.data.LocalState
import az.naymera.app.data.OnDeviceLocal

/**
 * Hibrid AI paneli: Cloud vəziyyəti, Local vəziyyəti, aktiv rejim, səs vəziyyəti və AUTO/CLOUD/LOCAL seçimi. Hər şey serverin bildirdiyi
 * vəziyyətdir: qurulmayan imkan "NOT CONFIGURED", simulyasiya "SİMULYASİYA" kimi göstərilir və heç vaxt hazır kimi təqdim olunmur.
 * Rejim seçimi yalnız rejimi dəyişir; razılıq/ödəniş/məxfilik dəyişiklikləri bu paneldən edilmir.
 */
@Composable
fun AiPanel(vm: MainViewModel, onDevice: OnDeviceLocal, modifier: Modifier = Modifier) {
    val state by vm.ai.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { vm.loadAi() }
    val status = state.status

    Card(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(stringResource(R.string.ai_panel_title), style = MaterialTheme.typography.titleMedium)
            state.error?.let { Text(uiText(it), color = MaterialTheme.colorScheme.error) }
            if (status != null) {
                EngineRow(stringResource(R.string.ai_cloud), status.cloud)
                EngineRow(stringResource(R.string.ai_local), status.local)
                val deviceBadge = when (onDevice.state) {
                    LocalState.Ready -> EngineBadge.Ready
                    LocalState.Simulation -> EngineBadge.Simulation
                    LocalState.NotConfigured -> EngineBadge.NotConfigured
                }
                StateRow(stringResource(R.string.ai_local_device), deviceBadge)
                val activeName = when (status.active) {
                    "cloud" -> stringResource(R.string.engine_cloud)
                    "local" -> stringResource(R.string.engine_local)
                    else -> stringResource(R.string.ai_active_none)
                }
                Text(stringResource(R.string.ai_active_mode, status.mode.uppercase(), activeName), style = MaterialTheme.typography.bodyMedium)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    val current = status.aiMode()
                    FilterChip(selected = current == AiMode.Auto, onClick = { vm.setMode(AiMode.Auto) }, label = { Text(stringResource(R.string.ai_mode_auto)) })
                    FilterChip(selected = current == AiMode.Cloud, onClick = { vm.setMode(AiMode.Cloud) }, label = { Text(stringResource(R.string.ai_mode_cloud)) })
                    FilterChip(selected = current == AiMode.Local, onClick = { vm.setMode(AiMode.Local) }, label = { Text(stringResource(R.string.ai_mode_local)) })
                }
                if (status.aiMode() == AiMode.Cloud) {
                    Text(stringResource(R.string.ai_cloud_no_fallback), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
                } else {
                    Text(stringResource(R.string.ai_fallback_hint), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text(stringResource(R.string.ai_voice_title), style = MaterialTheme.typography.labelLarge)
                VoiceRow(R.string.ai_voice_tts_local, status.voice.tts["local"])
                VoiceRow(R.string.ai_voice_tts_cloud, status.voice.tts["cloud"])
                VoiceRow(R.string.ai_voice_stt_local, status.voice.stt["local"])
                VoiceRow(R.string.ai_voice_stt_cloud, status.voice.stt["cloud"])
            }
        }
    }
}

@Composable
private fun badgeText(b: EngineBadge): String = stringResource(
    when (b) {
        EngineBadge.Ready -> R.string.ai_state_ready
        EngineBadge.Simulation -> R.string.ai_state_simulation
        EngineBadge.NotConfigured -> R.string.ai_state_not_configured
        EngineBadge.Unavailable -> R.string.ai_state_unavailable
    },
)

@Composable
private fun StateRow(label: String, badge: EngineBadge) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label)
        Text(
            badgeText(badge),
            color = if (badge == EngineBadge.Ready) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.labelMedium,
        )
    }
}

@Composable
private fun EngineRow(label: String, engine: AiEngine) {
    StateRow(label, engine.badge())
    if (engine.consentNeeded.isNotEmpty()) {
        Text(
            stringResource(R.string.ai_consent_needed, engine.consentNeeded.joinToString(", ")),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.secondary,
        )
    }
}

@Composable
private fun VoiceRow(label: Int, state: String?) {
    val badge = when (state) {
        "ready" -> EngineBadge.Ready
        "simulation" -> EngineBadge.Simulation
        "not_configured", null -> EngineBadge.NotConfigured
        else -> EngineBadge.Unavailable
    }
    StateRow(stringResource(label), badge)
}
