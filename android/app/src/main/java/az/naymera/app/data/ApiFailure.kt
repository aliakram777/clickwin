package az.naymera.app.data

import java.io.IOException
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/** Şəbəkə və server xətalarının istifadəçiyə göstərilən növləri (mətn `UiText.kt`-də tərcümə olunur). */
enum class ApiFailure {
    Offline, Timeout, Unauthorized, Forbidden, Conflict, RateLimited, Server, Unavailable, Unknown;

    companion object {
        fun fromStatus(status: Int): ApiFailure = when {
            status == 401 -> Unauthorized
            status == 403 -> Forbidden
            status == 409 -> Conflict
            status == 429 -> RateLimited
            status == 404 || status == 405 || status == 503 -> Unavailable
            status in 500..599 -> Server
            else -> Unknown
        }

        fun fromThrowable(t: Throwable): ApiFailure = when (t) {
            is SocketTimeoutException -> Timeout
            is UnknownHostException -> Offline
            is ConnectException -> Offline
            is SSLException -> Unknown
            is IOException -> Offline
            else -> Unknown
        }
    }
}

/** `status == 0`: şəbəkə səviyyəsi xətası (cavab alınmayıb). */
class ApiException(
    val status: Int,
    val code: String,
    message: String,
    val failure: ApiFailure = ApiFailure.fromStatus(status),
) : Exception(message)
