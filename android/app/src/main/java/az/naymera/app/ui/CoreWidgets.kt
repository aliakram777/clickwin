package az.naymera.app.ui

import android.view.WindowManager
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import az.naymera.app.R
import az.naymera.app.domain.ActionResult
import az.naymera.app.domain.ErrorKind
import az.naymera.app.domain.Load
import az.naymera.app.domain.PendingSign
import az.naymera.app.domain.SecureFlagCounter

fun ErrorKind.res(): Int = when (this) {
    ErrorKind.Offline -> R.string.err_offline
    ErrorKind.Timeout -> R.string.err_timeout
    ErrorKind.Unauthorized -> R.string.err_unauthorized
    ErrorKind.Forbidden -> R.string.err_forbidden
    ErrorKind.Conflict -> R.string.err_conflict
    ErrorKind.RateLimited -> R.string.err_rate_limited
    ErrorKind.Server -> R.string.err_server
    ErrorKind.Unavailable -> R.string.err_unavailable
    ErrorKind.Unknown -> R.string.err_unknown
    ErrorKind.Invalid -> R.string.err_invalid
    ErrorKind.SecretRejected -> R.string.err_secret
    ErrorKind.StepUpUnavailable -> R.string.stepup_unavailable
    ErrorKind.Mismatch -> R.string.err_mismatch
    ErrorKind.Stale -> R.string.err_stale
}

/** Jurnal sətri (jurnal mətni etibarsızdır: yalnız göstərilir). */
fun entryLine(seq: Long, text: String): String = "#$seq  $text"

fun statusRes(status: String): Int = when (status) {
    "draft" -> R.string.status_draft
    "active" -> R.string.status_active
    "paused" -> R.string.status_paused
    "blocked" -> R.string.status_blocked
    "completed" -> R.string.status_completed
    "failed" -> R.string.status_failed
    "cancelled" -> R.string.status_cancelled
    else -> R.string.err_unknown
}

/** Təsdiq/imza ekranlarında ekran görüntüsü və son-tətbiq önizləməsi söndürülür (FLAG_SECURE). */
@Composable
fun SecureWindow() {
    val activity = LocalContext.current as FragmentActivity
    DisposableEffect(Unit) {
        // Sayğac: iki təhlükəsiz ekran arasında keçiddə köhnə ekranın onDispose-u yeni ekranın bayrağını silməsin.
        if (SecureFlagCounter.acquire()) activity.window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        onDispose { if (SecureFlagCounter.release()) activity.window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE) }
    }
}

/** Yükləmə/xəta/oflayn: xətada son bilinən məzmun varsa `content` ilə birgə göstərilir (yalnız oxu). */
@Composable
fun <T> LoadView(state: Load<T>, onRetry: () -> Unit, content: @Composable (T) -> Unit) {
    when (state) {
        Load.Idle, Load.Loading -> {
            CircularProgressIndicator()
            Text(stringResource(R.string.loading), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        is Load.Content -> content(state.data)
        is Load.Failed -> {
            Text(stringResource(state.kind.res()), color = MaterialTheme.colorScheme.error)
            if (state.stale != null) {
                Text(stringResource(R.string.offline_stale), color = MaterialTheme.colorScheme.onSurfaceVariant)
                content(state.stale)
            }
            OutlinedButton(onClick = onRetry) { Text(stringResource(R.string.retry)) }
        }
    }
}

@Composable
fun ResultLine(result: ActionResult?) {
    when (result) {
        ActionResult.Done -> Text(stringResource(R.string.sign_done), color = MaterialTheme.colorScheme.primary)
        ActionResult.Cancelled -> Text(stringResource(R.string.approval_cancelled), color = MaterialTheme.colorScheme.onSurfaceVariant)
        is ActionResult.Failed -> Text(stringResource(result.kind.res()), color = MaterialTheme.colorScheme.error)
        null -> {}
    }
}

/**
 * İmzadan əvvəl DƏQİQ dəyərlər (action, subject, payload_hash, decision) göstərilir; yalnız istifadəçi «imzala» basarsa biometrik başlayır.
 * Server mətni (`pending.title`) etibarsızdır: yalnız göstərilir.
 */
@Composable
fun SignDialog(pending: PendingSign, onConfirm: () -> Unit, onCancel: () -> Unit) {
    val c = pending.challenge
    AlertDialog(
        onDismissRequest = onCancel,
        title = { Text(stringResource(R.string.sign_title)) },
        text = {
            Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(stringResource(R.string.sign_intro), color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (pending.title.isNotBlank()) Text(pending.title, style = MaterialTheme.typography.titleMedium)
                Field(R.string.sign_action, c.action)
                Field(R.string.sign_subject, c.subject)
                Field(R.string.sign_decision, c.decision)
                Field(R.string.sign_hash, c.payloadHash)
                Text(stringResource(R.string.untrusted_note), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        },
        confirmButton = { Button(onClick = onConfirm) { Text(stringResource(R.string.sign_confirm)) } },
        dismissButton = { TextButton(onClick = onCancel) { Text(stringResource(R.string.stepup_cancel)) } },
    )
}

@Composable
private fun Field(label: Int, value: String) {
    Column {
        Text(stringResource(label), style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.secondary)
        Text(value, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 2.dp))
    }
}
