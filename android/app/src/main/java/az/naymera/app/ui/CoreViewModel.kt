package az.naymera.app.ui

import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import az.naymera.app.domain.ApprovalsModel
import az.naymera.app.domain.CoreRepository
import az.naymera.app.domain.DashboardModel
import az.naymera.app.domain.GoalsModel
import az.naymera.app.domain.JournalModel
import az.naymera.app.domain.Signer
import az.naymera.app.security.SignableChallenge
import az.naymera.app.security.StepUp
import kotlinx.coroutines.launch

/**
 * MVVM: vəziyyət/məntiq `domain/` modellərindədir (Android-dan asılı olmayan, JVM-də test olunur); bu sinif yalnız həyat dövrünə bağlayır.
 * Repository → mövcud `ApiClient` (M1 kontraktları). Heç bir model avtomatik təsdiq/imza etmir.
 */
class CoreViewModel(repo: CoreRepository) : ViewModel() {
    val goals = GoalsModel(repo)
    val approvals = ApprovalsModel(repo)
    val dashboard = DashboardModel(repo)
    val journal = JournalModel(repo)

    fun run(block: suspend () -> Unit) { viewModelScope.launch { block() } }
}

/** Keystore açarı + BiometricPrompt (yalnız güclü biometrik, səs yox). İmzalanan mesaj istifadəçiyə göstərilən `SignableChallenge`-dən gəlir. */
class BiometricSigner(
    private val activity: FragmentActivity,
    private val title: String,
    private val cancel: String,
) : Signer {
    override fun available(): Boolean = StepUp.canAuthenticate(activity)

    override suspend fun sign(challenge: SignableChallenge): String? =
        StepUp.sign(activity, title, challenge.action + " · " + challenge.decision, cancel, challenge.message)
}
