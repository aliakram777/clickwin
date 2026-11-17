package az.naymera.app

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.fragment.app.FragmentActivity
import az.naymera.app.data.ApiClient
import az.naymera.app.data.SessionStore

/** Yeganə açıq (exported) komponent. FragmentActivity: BiometricPrompt üçün. */
class MainActivity : FragmentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = SessionStore(applicationContext)
        val api = ApiClient(store)
        setContent { NaymeraApp(store, api) }
    }
}
