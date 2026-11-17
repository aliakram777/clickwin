package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApiException
import az.naymera.app.data.ApprovalDto
import az.naymera.app.data.DecisionRequest
import az.naymera.app.data.SessionStore
import az.naymera.app.security.StepUp
import kotlinx.coroutines.launch

/**
 * Həssas əməliyyatların təsdiqi: yalnız biometrik/cihaz kilidi açılışı və cihaz sirri sübutu ilə. Bu ekran söhbət və səs
 * kodundan təcrid olunub; qərar yalnız serverin Approval Service-i tərəfindən tətbiq olunur.
 */
@Composable
fun ApprovalScreen(api: ApiClient, store: SessionStore, modifier: Modifier = Modifier) {
    val activity = LocalContext.current as FragmentActivity
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<ApprovalDto>>(emptyList()) }
    var message by remember { mutableStateOf<String?>(null) }

    suspend fun reload() {
        items = try { api.approvals() } catch (e: Exception) { message = "Siyahı yüklənmədi"; items }
    }

    fun decide(a: ApprovalDto, decision: String) {
        scope.launch {
            try {
                val ch = api.approvalChallenge(a.id, decision)
                val ok = StepUp.confirm(activity, "Təsdiq", "${a.agent}: ${a.permission}")
                if (!ok) {
                    message = "Təsdiq ləğv edildi"
                    return@launch
                }
                val proof = StepUp.proof(
                    store.deviceSecret, ch.challengeId, ch.nonce, "approval", a.id,
                    ch.approval?.payloadHash ?: a.payloadHash, decision,
                )
                val r = api.approvalDecision(a.id, DecisionRequest(decision, ch.challengeId, proof, StepUp.METHOD))
                message = if (r.ok) "Qərar qeydə alındı" else "Qərar qəbul olunmadı"
                reload()
            } catch (e: ApiException) {
                message = e.message
            } catch (e: Exception) {
                message = "Əlaqə xətası"
            }
        }
    }

    LaunchedEffect(Unit) { reload() }

    Column(modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Gözləyən təsdiqlər", style = MaterialTheme.typography.titleLarge)
        message?.let { Text(it, color = MaterialTheme.colorScheme.secondary) }
        if (items.isEmpty()) Text("Gözləyən təsdiq yoxdur.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            items(items, key = { it.id }) { a ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("${a.agent} · ${a.permission}", style = MaterialTheme.typography.titleMedium)
                        Text(a.description, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(onClick = { decide(a, "approve") }) { Text("Təsdiqlə") }
                            OutlinedButton(onClick = { decide(a, "deny") }) { Text("Rədd et") }
                        }
                    }
                }
            }
        }
    }
}
