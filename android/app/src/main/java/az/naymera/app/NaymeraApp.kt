package az.naymera.app

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import az.naymera.app.data.ApiClient
import az.naymera.app.data.EventSync
import az.naymera.app.data.SessionStore
import az.naymera.app.ui.ApprovalScreen
import az.naymera.app.ui.ChatScreen
import az.naymera.app.ui.ChatViewModel
import az.naymera.app.ui.HistoryScreen
import az.naymera.app.ui.NaymeraTheme
import az.naymera.app.ui.NotificationsScreen
import az.naymera.app.ui.PairingScreen
import az.naymera.app.voice.MockSpeech
import kotlinx.coroutines.launch

private data class Tab(val route: String, val label: String, val icon: androidx.compose.ui.graphics.vector.ImageVector)

@Composable
fun NaymeraApp(store: SessionStore, api: ApiClient) {
    NaymeraTheme {
        var paired by remember { mutableStateOf(store.isPaired) }
        if (!paired) {
            PairingScreen(store, api) { paired = true }
        } else {
            Main(store, api) { paired = false }
        }
    }
}

@Composable
private fun Main(store: SessionStore, api: ApiClient, onLoggedOut: () -> Unit) {
    val nav = rememberNavController()
    val sync = remember { EventSync(api, store) }
    val speech = remember { MockSpeech() } // real səs qoşulana qədər SİMULYASİYA
    val chatVm: ChatViewModel = viewModel(factory = viewModelFactory { initializer { ChatViewModel(api, speech) } })
    val tabs = listOf(
        Tab("chat", "Söhbət", Icons.Filled.Chat),
        Tab("notifications", "Bildirişlər", Icons.Filled.Notifications),
        Tab("history", "Tarixçə", Icons.Filled.History),
        Tab("settings", "Ayarlar", Icons.Filled.Settings),
    )
    val current = nav.currentBackStackEntryAsState().value?.destination?.route
    Scaffold(bottomBar = {
        NavigationBar {
            tabs.forEach { t ->
                NavigationBarItem(
                    selected = current == t.route,
                    onClick = { nav.navigate(t.route) { launchSingleTop = true } },
                    icon = { Icon(t.icon, contentDescription = t.label) },
                    label = { Text(t.label) },
                )
            }
        }
    }) { pad ->
        NavHost(nav, startDestination = "chat", modifier = Modifier.padding(pad)) {
            composable("chat") { ChatScreen(chatVm) }
            composable("notifications") {
                Column {
                    NotificationsScreen(sync, api, Modifier.weight(1f))
                    ApprovalScreen(api, store, Modifier.weight(1f))
                }
            }
            composable("history") {
                HistoryScreen(api, onOpen = { id -> chatVm.open(id); nav.navigate("chat") })
            }
            composable("settings") { SettingsScreen(store, api, onLoggedOut) }
        }
    }
}

@Composable
private fun SettingsScreen(store: SessionStore, api: ApiClient, onLoggedOut: () -> Unit) {
    val scope = rememberCoroutineScope()
    var stopMessage by remember { mutableStateOf<String?>(null) }
    Column(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("Ayarlar", style = MaterialTheme.typography.titleLarge)
        Text("Server: ${store.serverUrl}", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(
            "Səs: az-AZ, təbii qadın səsi (hədəf). Hazırda SİMULYASİYA: real səs tanıma/sintez yoxdur və səslə təsdiq mümkün deyil.",
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(onClick = {
            scope.launch { stopMessage = try { api.stop(); "Dayandırma sorğusu göndərildi" } catch (e: Exception) { "Dayandırıla bilmədi" } }
        }) { Text("Sistemi dayandır") }
        stopMessage?.let { Text(it) }
        Button(onClick = { scope.launch { api.logout(); onLoggedOut() } }) { Text("Çıxış (cihazı ayır)") }
    }
}
