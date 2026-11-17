package az.naymera.app.voice

/**
 * Səs qatı (gələcək): Speech-to-Text və Text-to-Speech ayrı interfeyslərdir. Hədəf profil: az-AZ, təbii qadın səsi.
 * Bu mərhələdə YALNIZ SİMULYASİYA var (`MockSpeech`): real səs tanıma və sintez qoşulmayıb və `isMock=true` ilə
 * UI-da açıq göstərilir. Səs transkripti etibarsız mətn girişidir; səs HEÇ VAXT təsdiq mənbəyi deyil
 * (təsdiq kodu bu paketi import etmir).
 */
enum class Gender { FEMALE, MALE, NEUTRAL }

data class VoiceProfile(
    val language: String = "az-AZ",
    val gender: Gender = Gender.FEMALE,
    val style: String = "natural",
    val rate: Float = 1.0f,
    val pitch: Float = 0.0f,
)

data class Transcript(val text: String, val confidence: Double, val isMock: Boolean)

data class SpeechOutput(val text: String, val isMock: Boolean)

interface SpeechToText {
    val isMock: Boolean
    val available: Boolean
    suspend fun listen(profile: VoiceProfile): Transcript?
}

interface TextToSpeech {
    val isMock: Boolean
    val available: Boolean
    suspend fun speak(text: String, profile: VoiceProfile): SpeechOutput
}

/** Real səs yaratmır. Nəticələr `isMock=true` ilə qaytarılır və real kimi təqdim olunmamalıdır. */
class MockSpeech : SpeechToText, TextToSpeech {
    override val isMock: Boolean = true
    override val available: Boolean = false

    override suspend fun listen(profile: VoiceProfile): Transcript? = null

    override suspend fun speak(text: String, profile: VoiceProfile): SpeechOutput = SpeechOutput(text, isMock = true)
}
