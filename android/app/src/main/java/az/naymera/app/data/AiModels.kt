package az.naymera.app.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Hibrid AI (server) müqaviləsi: `/v1/ai/status`, `/v1/ai/mode`. Android yalnız serverin bildirdiyi vəziyyəti göstərir. */

enum class AiMode(val wire: String) {
    Auto("auto"), Cloud("cloud"), Local("local");

    companion object {
        fun fromWire(value: String): AiMode? = values().firstOrNull { it.wire == value }
    }
}

/** Mühərrikin istifadəçiyə göstərilən vəziyyəti: simulyasiya real kimi göstərilmir, qurulmayan "NOT CONFIGURED" olur. */
enum class EngineBadge { Ready, Simulation, NotConfigured, Unavailable }

@Serializable
data class AiEngine(
    @SerialName("name") val name: String = "",
    @SerialName("state") val state: String,
    @SerialName("is_mock") val isMock: Boolean = true,
    @SerialName("reason") val reason: String = "",
    @SerialName("model") val model: String = "",
    @SerialName("supported_kinds") val supportedKinds: List<String> = emptyList(),
    @SerialName("consent_needed") val consentNeeded: List<String> = emptyList(),
) {
    fun badge(): EngineBadge = when (state) {
        "ready" -> EngineBadge.Ready
        "simulation" -> EngineBadge.Simulation
        "not_configured" -> EngineBadge.NotConfigured
        else -> EngineBadge.Unavailable
    }
}

@Serializable
data class AiVoiceSummary(
    @SerialName("tts") val tts: Map<String, String> = emptyMap(),
    @SerialName("stt") val stt: Map<String, String> = emptyMap(),
)

@Serializable
data class AiPrivacy(
    @SerialName("cloud_may_receive_memory") val cloudMayReceiveMemory: Boolean = false,
    @SerialName("cloud_categories") val cloudCategories: List<String> = emptyList(),
)

@Serializable
data class AiStatus(
    @SerialName("mode") val mode: String,
    @SerialName("active") val active: String,
    @SerialName("cloud") val cloud: AiEngine,
    @SerialName("local") val local: AiEngine,
    @SerialName("consents") val consents: Map<String, Boolean> = emptyMap(),
    @SerialName("privacy") val privacy: AiPrivacy = AiPrivacy(),
    @SerialName("voice") val voice: AiVoiceSummary = AiVoiceSummary(),
) {
    fun aiMode(): AiMode = AiMode.fromWire(mode) ?: AiMode.Auto
}

@Serializable
data class AiModeRequest(@SerialName("mode") val mode: String)

@Serializable
data class AiModeResponse(
    @SerialName("mode") val mode: String,
    @SerialName("active") val active: String = "none",
)
