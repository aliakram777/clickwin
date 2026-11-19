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
import androidx.compose.material3.Card
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
import az.naymera.app.domain.Signer

/**
 * Həssas əməliyyatların təsdiqi. Hər təsdiq üçün dəqiq agent/icazə/təsvir göstərilir; «Təsdiqlə»/«Rədd et» yalnız İMZA DİALOQUNU açır,
 * imza dialoqunda dəqiq dəyərlər yenidən göstərilir və biometrik YALNIZ ikinci açıq təsdiqdən sonra başlayır. Avtomatik təsdiq yoxdur.
 * Bu ekran söhbət/səs kodundan təcrid olunub; qərarı server imzanı yoxlayaraq tətbiq edir.
 */
@Composable
fun ApprovalScreen(vm: CoreViewModel, signer: Signer, modifier: Modifier = Modifier) {
    SecureWindow()
    val items by vm.approvals.items.collectAsStateWithLifecycle()
    val pending by vm.approvals.pending.collectAsStateWithLifecycle()
    val result by vm.approvals.result.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { vm.approvals.refresh() }
    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.approvals_title), style = MaterialTheme.typography.titleLarge)
        ResultLine(result)
        LoadView(items, onRetry = { vm.run { vm.approvals.refresh() } }) { data ->
            if (data.approvals.isEmpty()) Text(stringResource(R.string.approvals_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
            data.approvals.forEach { a ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(a.agent + " / " + a.permission, style = MaterialTheme.typography.titleMedium)
                        Text(a.description, color = MaterialTheme.colorScheme.onSurfaceVariant)   // etibarsız mətn: yalnız göstərilir
                        Text(stringResource(R.string.apr_cost, a.cost.toString()), style = MaterialTheme.typography.bodySmall)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(onClick = { vm.run { vm.approvals.prepare(a, "approve") } }) { Text(stringResource(R.string.approve)) }
                            OutlinedButton(onClick = { vm.run { vm.approvals.prepare(a, "deny") } }) { Text(stringResource(R.string.deny)) }
                        }
                    }
                }
            }
            if (data.steps.isNotEmpty()) {
                Text(stringResource(R.string.approvals_steps), style = MaterialTheme.typography.titleMedium)
                Text(stringResource(R.string.approvals_steps_note), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                data.steps.forEach { s ->
                    Text(s.goalTitle + " → " + s.stepTitle + " (" + s.capabilityId + ", #" + s.attempt + ")", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }
    pending?.let { p -> SignDialog(p, onConfirm = { vm.run { vm.approvals.confirm(signer) } }, onCancel = { vm.approvals.cancelPending() }) }
}
