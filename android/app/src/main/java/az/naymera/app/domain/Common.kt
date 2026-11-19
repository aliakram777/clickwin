package az.naymera.app.domain

import az.naymera.app.data.ApiException
import az.naymera.app.data.ApiFailure
import az.naymera.app.security.ChallengeMismatch
import az.naymera.app.security.SignableChallenge

/** ViewModel/UI vəziyyətləri Android-dan asılı deyil (JVM-də test olunur): mətn resursu seçimi ekranda (`ui/UiText.kt`) edilir. */
enum class ErrorKind { Offline, Timeout, Unauthorized, Forbidden, Conflict, RateLimited, Server, Unavailable, Unknown, Invalid, SecretRejected, StepUpUnavailable, Mismatch, Stale }

fun Throwable.toErrorKind(): ErrorKind = when (this) {
    is ChallengeMismatch -> if (field == "version" || field == "step_state") ErrorKind.Stale else ErrorKind.Mismatch
    is ApiException -> if (status == 422) ErrorKind.SecretRejected else if (status == 400) ErrorKind.Invalid else when (failure) {
        ApiFailure.Offline -> ErrorKind.Offline
        ApiFailure.Timeout -> ErrorKind.Timeout
        ApiFailure.Unauthorized -> ErrorKind.Unauthorized
        ApiFailure.Forbidden -> ErrorKind.Forbidden
        ApiFailure.Conflict -> ErrorKind.Conflict
        ApiFailure.RateLimited -> ErrorKind.RateLimited
        ApiFailure.Server -> ErrorKind.Server
        ApiFailure.Unavailable -> ErrorKind.Unavailable
        ApiFailure.Unknown -> ErrorKind.Unknown
    }
    else -> when (ApiFailure.fromThrowable(this)) {
        ApiFailure.Offline -> ErrorKind.Offline
        ApiFailure.Timeout -> ErrorKind.Timeout
        else -> ErrorKind.Unknown
    }
}

/** Yükləmə vəziyyəti. Xəta zamanı SON BİLİNƏN məzmun (`stale`) saxlanılır: oflayn rejimdə yalnız oxu; yazı/təsdiq oflayn İCRA OLUNMUR. */
sealed interface Load<out T> {
    data object Idle : Load<Nothing>
    data object Loading : Load<Nothing>
    data class Content<T>(val data: T) : Load<T>
    data class Failed<T>(val kind: ErrorKind, val stale: T? = null) : Load<T>
}

fun <T> Load<T>.dataOrNull(): T? = when (this) {
    is Load.Content -> data
    is Load.Failed -> stale
    else -> null
}

/** Biometrik imzalayıcı (Android-da `StepUp.sign`). null = ləğv/əlçatmaz. Dəyərlər imzadan əvvəl istifadəçiyə `SignableChallenge` ilə göstərilir. */
interface Signer {
    fun available(): Boolean
    suspend fun sign(challenge: SignableChallenge): String?
}

enum class ActionOutcome { Done, Cancelled, StepUpUnavailable }

sealed interface ActionResult {
    data object Done : ActionResult
    data object Cancelled : ActionResult
    data class Failed(val kind: ErrorKind) : ActionResult
}

/**
 * Gözləyən imza: istifadəçiyə AYNEN göstəriləcək dəyərlər (`challenge`: action, subject, payloadHash, decision) + təsdiqdən sonra göndəriləcək əməliyyat.
 * Heç vaxt avtomatik təsdiqlənmir: yalnız `confirm(signer)` (istifadəçi düyməsi → biometrik) imza yaradır.
 */
class PendingSign internal constructor(
    val title: String,                    // göstərilən sahə: əməliyyat təsviri (server mətni etibarsız: yalnız göstərilir)
    val challenge: SignableChallenge,
    internal val submit: suspend (proof: String) -> Unit,
)
