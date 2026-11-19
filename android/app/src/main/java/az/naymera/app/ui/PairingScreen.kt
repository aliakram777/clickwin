package az.naymera.app.ui

import android.util.Base64
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import az.naymera.app.R
import az.naymera.app.data.ApiClient
import az.naymera.app.data.ServerUrl
import az.naymera.app.data.SessionStore
import az.naymera.app.security.DeviceKey
import az.naymera.app.security.StepUp
import kotlinx.coroutines.launch

/**
 * Cütləşmə: sahib serverdə birdəfəlik kod yaradır, istifadəçi onu burada daxil edir. Cihaz açarı Keystore-da yaradılır və kod
 * biometrik təsdiqlə həmin açarla imzalanır; serverə yalnız açıq açar və imza gedir. APK-da heç bir açar yoxdur.
 */
@Composable
fun PairingScreen(store: SessionStore, api: ApiClient, onPaired: () -> Unit) {
    val activity = LocalContext.current as FragmentActivity
    var url by remember { mutableStateOf(store.serverUrl) }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<UiText?>(null) }
    val scope = rememberCoroutineScope()
    val title = stringResource(R.string.stepup_title)
    val subtitle = stringResource(R.string.pair_title)
    val cancel = stringResource(R.string.stepup_cancel)

    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
    ) {
        Text(stringResource(R.string.app_name), style = MaterialTheme.typography.headlineLarge, color = MaterialTheme.colorScheme.primary)
        Text(stringResource(R.string.pair_title), style = MaterialTheme.typography.titleMedium)
        Text(stringResource(R.string.pair_biometric_hint), color = MaterialTheme.colorScheme.onSurfaceVariant)
        OutlinedTextField(
            value = url, onValueChange = { url = it }, label = { Text(stringResource(R.string.pair_server)) },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = code, onValueChange = { code = it.uppercase().take(8) }, label = { Text(stringResource(R.string.pair_code)) },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(uiText(it), color = MaterialTheme.colorScheme.error) }
        Button(
            enabled = !busy && code.length == 8 && url.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
            onClick = {
                busy = true
                error = null
                scope.launch {
                    try {
                        store.serverUrl = ServerUrl.validate(url)
                        if (!StepUp.canAuthenticate(activity)) {
                            error = UiText(R.string.stepup_unavailable)
                            return@launch
                        }
                        val spki = DeviceKey.generate()
                        val signature = StepUp.sign(activity, title, subtitle, cancel, DeviceKey.pairMessage(code))
                        if (signature == null) {
                            DeviceKey.delete()
                            error = UiText(R.string.approval_cancelled)
                            return@launch
                        }
                        api.pair(code, android.os.Build.MODEL ?: "Android", Base64.encodeToString(spki, Base64.NO_WRAP), signature)
                        onPaired()
                    } catch (e: Exception) {
                        DeviceKey.delete()
                        error = e.toUiText()
                    } finally {
                        busy = false
                    }
                }
            },
        ) { Text(stringResource(R.string.pair_button)) }
    }
}
