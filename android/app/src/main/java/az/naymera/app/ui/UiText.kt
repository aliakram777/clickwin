package az.naymera.app.ui

import androidx.annotation.StringRes
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import az.naymera.app.R
import az.naymera.app.data.ApiFailure

/** ViewModel-lər Context bilmir: mətn resurs identifikatoru kimi saxlanılır və ekranda tərcümə olunur (az/en/ru/tr). */
data class UiText(@StringRes val id: Int, val args: List<Any> = emptyList())

@Composable
fun uiText(t: UiText): String = stringResource(t.id, *t.args.toTypedArray())

fun ApiFailure.text(): UiText = UiText(
    when (this) {
        ApiFailure.Offline -> R.string.err_offline
        ApiFailure.Timeout -> R.string.err_timeout
        ApiFailure.Unauthorized -> R.string.err_unauthorized
        ApiFailure.Forbidden -> R.string.err_forbidden
        ApiFailure.Conflict -> R.string.err_conflict
        ApiFailure.RateLimited -> R.string.err_rate_limited
        ApiFailure.Server -> R.string.err_server
        ApiFailure.Unavailable -> R.string.err_unavailable
        ApiFailure.Unknown -> R.string.err_unknown
    },
)

fun Throwable.toUiText(): UiText = when (this) {
    is az.naymera.app.data.ApiException -> failure.text()
    is IllegalArgumentException -> UiText(R.string.err_invalid_url)
    else -> az.naymera.app.data.ApiFailure.fromThrowable(this).text()
}

/** Serverin `notice_code` dəyərini istifadəçi dilindəki mətn resursuna çevirir (naməlum kod: null). */
fun noticeRes(code: String): Int? = when (code) {
    "fallback" -> R.string.notice_fallback
    "consent_required" -> R.string.notice_consent_required
    "simulation_only" -> R.string.notice_simulation_only
    "all_unavailable" -> R.string.notice_all_unavailable
    "cloud_unavailable" -> R.string.notice_cloud_unavailable
    "local_unavailable" -> R.string.notice_local_unavailable
    "unsupported_locally" -> R.string.notice_unsupported_locally
    "blocked" -> R.string.notice_blocked
    "blocked_egress" -> R.string.notice_blocked_egress
    "in_progress" -> R.string.notice_in_progress
    "bad_request" -> R.string.notice_bad_request
    "device_fallback" -> R.string.notice_device_fallback
    else -> null
}

fun engineRes(engine: String, provenance: String): Int = when {
    engine == "cloud" -> R.string.engine_cloud
    engine == "local" -> R.string.engine_local
    engine == "device" -> R.string.engine_device
    else -> R.string.engine_rules
}
