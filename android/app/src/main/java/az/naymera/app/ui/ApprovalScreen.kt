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
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import az.naymera.app.R
import az.naymera.app.data.ApiClient
import az.naymera.app.data.ApprovalDto
import az.naymera.app.data.DecisionRequest
import az.naymera.app.security.StepUp
import kotlinx.coroutines.launch

/**
 * Həssas əməliyyatların təsdiqi: yalnız güclü biometrik təsdiqlə Keystore açarı ilə imza. Bu ekran söhbət və səs kodundan
 * təcrid olunub; qərar yalnız serverin Approval Service-i tərəfindən tətbiq olunur (server imzanı yoxlayır).
 */
@Composable
fun ApprovalScreen(api: ApiClient, modifier: Modifier = Modifier) {
    val activity = LocalContext.current as FragmentActivity
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<ApprovalDto>>(emptyList()) }
    var message by remember { mutableStateOf<UiText?>(null) }
    val title = stringResource(R.string.stepup_title)
    val cancel = stringResource(R.string.stepup_cancel)

    suspend fun reload() {
        try {
            items = api.approvals()
        } catch (e: Exception) {
            message = e.toUiText()
        }
    }

    fun decide(a: ApprovalDto, decision: String, subtitle: String) {
        scope.launch {
            try {
                if (!StepUp.canAuthenticate(activity)) {
                    message = UiText(R.string.stepup_unavailable)
                    return@launch
                }
                val ch = api.approvalChallenge(a.id, decision)
                val payloadHash = ch.approval?.payloadHash ?: a.payloadHash
                val message0 = StepUp.challengeMessage(ch.challengeId, ch.nonce, "approval", a.id, payloadHash, decision)
                val proof = StepUp.sign(activity, title, subtitle, cancel, message0)
                if (proof == null) {
                    message = UiText(R.string.approval_cancelled)
                    return@launch
                }
                val r = api.approvalDecision(a.id, DecisionRequest(decision, ch.challengeId, proof, StepUp.METHOD))
                message = UiText(if (r.ok) R.string.approval_recorded else R.string.approval_rejected)
                reload()
            } catch (e: Exception) {
                message = e.toUiText()
            }
        }
    }

    LaunchedEffect(Unit) { reload() }

    Column(modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.approvals_title), style = MaterialTheme.typography.titleLarge)
        message?.let { Text(uiText(it), color = MaterialTheme.colorScheme.secondary) }
        if (items.isEmpty()) Text(stringResource(R.string.approvals_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
        LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            items(items, key = { it.id }) { a ->
                val subtitle = stringResource(R.string.stepup_subtitle, a.agent, a.permission)
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(a.agent + " / " + a.permission, style = MaterialTheme.typography.titleMedium)
                        Text(a.description, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(onClick = { decide(a, "approve", subtitle) }) { Text(stringResource(R.string.approve)) }
                            OutlinedButton(onClick = { decide(a, "deny", subtitle) }) { Text(stringResource(R.string.deny)) }
                        }
                    }
                }
            }
        }
    }
}
