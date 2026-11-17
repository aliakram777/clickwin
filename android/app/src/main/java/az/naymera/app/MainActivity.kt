package az.naymera.app

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.appcompat.app.AppCompatActivity
import az.naymera.app.data.ApiClient
import az.naymera.app.data.SessionStore

/** Yeganə açıq (exported) komponent. AppCompatActivity (FragmentActivity): BiometricPrompt və tətbiq daxili dil seçimi üçün. */
class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = SessionStore(applicationContext)
        val api = ApiClient(store)
        setContent { NaymeraApp(store, api) }
    }
}
