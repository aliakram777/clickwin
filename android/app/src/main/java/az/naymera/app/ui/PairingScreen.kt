package az.naymera.app.ui

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
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiException
import az.naymera.app.data.ServerUrl
import az.naymera.app.data.SessionStore
import kotlinx.coroutines.launch

/** Cütləşmə: sahib serverdə birdəfəlik kod yaradır, istifadəçi onu burada daxil edir. APK-da heç bir açar yoxdur. */
@Composable
fun PairingScreen(store: SessionStore, api: ApiClient, onPaired: () -> Unit) {
    var url by remember { mutableStateOf(store.serverUrl) }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, androidx.compose.ui.Alignment.CenterVertically),
    ) {
        Text("NAYMERA", style = MaterialTheme.typography.headlineLarge, color = MaterialTheme.colorScheme.primary)
        Text("Cihazı cütləşdirin", style = MaterialTheme.typography.titleMedium)
        OutlinedTextField(
            value = url, onValueChange = { url = it }, label = { Text("Server ünvanı (https://…)") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = code, onValueChange = { code = it.uppercase().take(8) }, label = { Text("Cütləşmə kodu") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(
            enabled = !busy && code.length == 8 && url.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
            onClick = {
                busy = true
                error = null
                scope.launch {
                    try {
                        store.serverUrl = ServerUrl.validate(url)
                        api.pair(code, android.os.Build.MODEL ?: "Android")
                        onPaired()
                    } catch (e: IllegalArgumentException) {
                        error = e.message
                    } catch (e: ApiException) {
                        error = e.message
                    } catch (e: Exception) {
                        error = "Əlaqə xətası"
                    } finally {
                        busy = false
                    }
                }
            },
        ) { Text("Cütləşdir") }
    }
}
