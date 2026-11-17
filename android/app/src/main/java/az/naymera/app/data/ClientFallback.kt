package az.naymera.app.data

/**
 * Cihazdakı Local AI (gələcək): bu mərhələdə YALNIZ interfeys var. Model yüklənmir, bundle edilmir və şəbəkədən endirilmir;
 * defolt `NotConfiguredLocal` real imkan kimi göstərilmir. Yükləmə və resurs istifadəsi yalnız istifadəçi razılığı ilə həyata keçiriləcək
 * (RAM/CPU/batareya/yaddaş limitləri serverdəki `ResourceGuard` ilə eyni qaydalara uyğun olmalıdır).
 */
enum class LocalState { Ready, Simulation, NotConfigured }

interface OnDeviceLocal {
    val state: LocalState
    suspend fun generate(prompt: String): String
}

object NotConfiguredLocal : OnDeviceLocal {
    override val state: LocalState = LocalState.NotConfigured
    override suspend fun generate(prompt: String): String = throw IllegalStateException("on-device model is not configured")
}

enum class ClientRoute { Server, OnDevice, Unavailable }

/**
 * Server cavab vermədikdə marşrut qərarı. Yalnız bağlantı itkisində (Offline/Timeout) və yalnız CLOUD seçilməyibsə və cihazda
 * model hazırdırsa cihaza keçilir. 401/403/409/429/5xx kimi xətalarda səssiz fallback YOXDUR (təhlükəsizlik/siyasət qərarlarını dolanmaq olmaz).
 */
fun decideRoute(mode: AiMode, failure: ApiFailure?, onDevice: OnDeviceLocal): ClientRoute {
    if (failure == null) return ClientRoute.Server
    if (failure != ApiFailure.Offline && failure != ApiFailure.Timeout) return ClientRoute.Unavailable
    if (mode == AiMode.Cloud) return ClientRoute.Unavailable
    return if (onDevice.state != LocalState.NotConfigured) ClientRoute.OnDevice else ClientRoute.Unavailable
}
