package az.naymera.app

import androidx.appcompat.app.AppCompatDelegate
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Group
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.os.LocaleListCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
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
import az.naymera.app.ui.AgentsScreen
import az.naymera.app.ui.ApprovalScreen
import az.naymera.app.ui.ChatScreen
import az.naymera.app.ui.ChatViewModel
import az.naymera.app.ui.HistoryScreen
import az.naymera.app.ui.HomeScreen
import az.naymera.app.ui.MainViewModel
import az.naymera.app.ui.MemoryScreen
import az.naymera.app.ui.NaymeraTheme
import az.naymera.app.ui.NotificationsScreen
import az.naymera.app.ui.PairingScreen
import az.naymera.app.ui.toUiText
import az.naymera.app.ui.uiText
import az.naymera.app.voice.MockSpeech
import kotlinx.coroutines.launch

private data class Tab(val route: String, val label: Int, val icon: androidx.compose.ui.graphics.vector.ImageVector)

@Composable
fun NaymeraApp(store: SessionStore, api: ApiClient) {
    NaymeraTheme {
        var paired by remember { mutableStateOf(store.isPaired) }
        val expired by api.sessionExpired.collectAsStateWithLifecycle()
        // Sessiya yenilənməsi uğursuzdursa cihaz yenidən cütləşdirilməlidir (köhnə açar və tokenlər silinir).
        LaunchedEffect(expired) {
            if (expired) {
                store.clear()
                api.resetSessionExpired()
                paired = false
            }
        }
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
    val mainVm: MainViewModel = viewModel(factory = viewModelFactory { initializer { MainViewModel(api, sync) } })
    val tabs = listOf(
        Tab("home", R.string.tab_home, Icons.Filled.Home),
        Tab("chat", R.string.tab_chat, Icons.Filled.Chat),
        Tab("memory", R.string.tab_memory, Icons.Filled.Memory),
        Tab("notifications", R.string.tab_notifications, Icons.Filled.Notifications),
        Tab("agents", R.string.tab_agents, Icons.Filled.Group),
    )
    val current = nav.currentBackStackEntryAsState().value?.destination?.route
    Scaffold(bottomBar = {
        NavigationBar {
            tabs.forEach { t ->
                NavigationBarItem(
                    selected = current == t.route,
                    onClick = { nav.navigate(t.route) { launchSingleTop = true } },
                    icon = { Icon(t.icon, contentDescription = stringResource(t.label)) },
                    label = { Text(stringResource(t.label)) },
                )
            }
        }
    }) { pad ->
        NavHost(nav, startDestination = "home", modifier = Modifier.padding(pad)) {
            composable("home") {
                Column {
                    HomeScreen(mainVm, Modifier.weight(1f))
                    Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = { nav.navigate("history") }) { Text(stringResource(R.string.tab_history)) }
                        TextButton(onClick = { nav.navigate("settings") }) { Text(stringResource(R.string.tab_settings)) }
                    }
                }
            }
            composable("chat") { ChatScreen(chatVm) }
            composable("memory") { MemoryScreen(mainVm) }
            composable("notifications") {
                Column {
                    NotificationsScreen(mainVm, Modifier.weight(1f))
                    ApprovalScreen(api, Modifier.weight(1f))
                }
            }
            composable("agents") { AgentsScreen(mainVm) }
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
    var stopMessage by remember { mutableStateOf<az.naymera.app.ui.UiText?>(null) }
    Column(
        Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(stringResource(R.string.settings_title), style = MaterialTheme.typography.titleLarge)
        Text(stringResource(R.string.settings_server, store.serverUrl), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(stringResource(R.string.settings_language), style = MaterialTheme.typography.titleMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            // Tətbiq daxili dil seçimi: az (defolt), en, ru, tr.
            listOf("az" to R.string.lang_az, "en" to R.string.lang_en, "ru" to R.string.lang_ru, "tr" to R.string.lang_tr)
                .forEach { (tag, label) ->
                    TextButton(onClick = {
                        AppCompatDelegate.setApplicationLocales(LocaleListCompat.forLanguageTags(tag))
                    }) { Text(stringResource(label)) }
                }
        }
        Text(stringResource(R.string.settings_voice_info), color = MaterialTheme.colorScheme.onSurfaceVariant)
        OutlinedButton(onClick = {
            scope.launch {
                stopMessage = try {
                    api.stop()
                    az.naymera.app.ui.UiText(R.string.settings_stop_sent)
                } catch (e: Exception) {
                    e.toUiText()
                }
            }
        }) { Text(stringResource(R.string.settings_stop)) }
        stopMessage?.let { Text(uiText(it)) }
        Button(onClick = { scope.launch { api.logout(); onLoggedOut() } }) { Text(stringResource(R.string.settings_logout)) }
    }
}
