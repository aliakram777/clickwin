package az.naymera.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import az.naymera.app.data.GoalDetail
import az.naymera.app.data.GoalDto
import az.naymera.app.domain.ActionResult
import az.naymera.app.domain.GoalAction
import az.naymera.app.domain.RESOLVABLE_STEP_STATES
import az.naymera.app.domain.STEP_RESOLUTIONS
import az.naymera.app.domain.Signer
import az.naymera.app.domain.allowedActions

private fun GoalAction.label(): Int = when (this) {
    GoalAction.Start -> R.string.action_start
    GoalAction.Pause -> R.string.action_pause
    GoalAction.Resume -> R.string.action_resume
    GoalAction.Complete -> R.string.action_complete
    GoalAction.Cancel -> R.string.action_cancel
}

@Composable
fun GoalsScreen(vm: CoreViewModel, onOpen: (String) -> Unit, modifier: Modifier = Modifier) {
    val list by vm.goals.list.collectAsStateWithLifecycle()
    var creating by remember { mutableStateOf(false) }
    var createResult by remember { mutableStateOf<ActionResult?>(null) }
    LaunchedEffect(Unit) { vm.goals.refresh() }
    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(stringResource(R.string.goals_title), style = MaterialTheme.typography.titleLarge)
            Button(onClick = { creating = true; createResult = null }) { Text(stringResource(R.string.goals_new)) }
        }
        LoadView(list, onRetry = { vm.run { vm.goals.refresh() } }) { goals ->
            if (goals.isEmpty()) Text(stringResource(R.string.goals_empty), color = MaterialTheme.colorScheme.onSurfaceVariant)
            goals.forEach { GoalCard(it) { onOpen(it.id) } }
        }
    }
    if (creating) CreateGoalDialog(createResult, onDismiss = { creating = false }) { title, desc ->
        vm.run { createResult = vm.goals.create(title, desc); if (createResult == ActionResult.Done) creating = false }
    }
}

@Composable
private fun GoalCard(g: GoalDto, onClick: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(g.title, style = MaterialTheme.typography.titleMedium)   // server/istifadəçi mətni: yalnız göstərilir
            Text(stringResource(R.string.goal_status_line, stringResource(statusRes(g.status)), g.version), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun CreateGoalDialog(result: ActionResult?, onDismiss: () -> Unit, onCreate: (String, String) -> Unit) {
    var title by remember { mutableStateOf("") }
    var desc by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.goals_new)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(title, { title = it.take(200) }, label = { Text(stringResource(R.string.goal_title_hint)) }, singleLine = true)
                OutlinedTextField(desc, { desc = it.take(2000) }, label = { Text(stringResource(R.string.goal_desc_hint)) })
                Text(stringResource(R.string.goal_create_note), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                ResultLine(result)
            }
        },
        confirmButton = { Button(onClick = { onCreate(title, desc) }, enabled = title.isNotBlank()) { Text(stringResource(R.string.goal_create)) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text(stringResource(R.string.stepup_cancel)) } },
    )
}

/** Məqsəd detalı: status, plan/addımlar (server mətni etibarsız), dəstəklənən keçidlər və UNKNOWN/BLOCKED/FAILED addım həlli — hamısı imza dialoqu ilə. */
@Composable
fun GoalDetailScreen(vm: CoreViewModel, goalId: String, signer: Signer, modifier: Modifier = Modifier) {
    SecureWindow()
    val detail by vm.goals.detail.collectAsStateWithLifecycle()
    val pending by vm.goals.pending.collectAsStateWithLifecycle()
    val result by vm.goals.result.collectAsStateWithLifecycle()
    LaunchedEffect(goalId) { vm.goals.open(goalId) }
    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        LoadView(detail, onRetry = { vm.run { vm.goals.open(goalId) } }) { d ->
            DetailBody(d, vm)
        }
        ResultLine(result)
        Text(stringResource(R.string.untrusted_note), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
    pending?.let { p -> SignDialog(p, onConfirm = { vm.run { vm.goals.confirm(signer) } }, onCancel = { vm.goals.cancelPending() }) }
}

@Composable
private fun DetailBody(d: GoalDetail, vm: CoreViewModel) {
    val g = d.goal
    Text(g.title, style = MaterialTheme.typography.titleLarge)
    if (g.description.isNotBlank()) Text(g.description, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Text(stringResource(R.string.goal_status_line, stringResource(statusRes(g.status)), g.version), style = MaterialTheme.typography.titleMedium)
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        allowedActions(g.status).forEach { a ->
            OutlinedButton(onClick = { vm.run { vm.goals.prepareTransition(g.id, a, g.version, g.status) } }) { Text(stringResource(a.label())) }
        }
    }
    Text(stringResource(R.string.goal_plan), style = MaterialTheme.typography.titleMedium)
    val plan = d.plan
    if (plan == null) Text(stringResource(R.string.goal_no_plan), color = MaterialTheme.colorScheme.onSurfaceVariant)
    plan?.let { pl -> pl.steps.forEach { s ->
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(s.title, style = MaterialTheme.typography.titleSmall)
                Text(stringResource(R.string.step_line, s.state, s.attempt), style = MaterialTheme.typography.bodySmall)
                if (s.summary.isNotBlank()) Text(s.summary, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (s.error.isNotBlank()) Text(s.error, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
                if (s.state in RESOLVABLE_STEP_STATES) {
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        STEP_RESOLUTIONS.forEach { r ->
                            TextButton(onClick = { vm.run { vm.goals.prepareStepResolve(pl.id, s.id, r, s.attempt, s.state) } }) {
                                Text(stringResource(when (r) { "retry" -> R.string.step_resolve_retry; "failed" -> R.string.step_resolve_failed; else -> R.string.step_resolve_succeeded }))
                            }
                        }
                    }
                }
            }
        }
    } }
}
