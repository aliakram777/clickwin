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
