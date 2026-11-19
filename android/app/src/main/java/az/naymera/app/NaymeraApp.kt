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
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material.icons.filled.Flag
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.VerifiedUser
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.IconButton
import androidx.compose.ui.Alignment
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.fragment.app.FragmentActivity
import az.naymera.app.domain.ApiCoreRepository
import az.naymera.app.domain.Destination
import az.naymera.app.ui.BiometricSigner
import az.naymera.app.ui.CoreViewModel
import az.naymera.app.ui.DashboardScreen
import az.naymera.app.ui.GoalDetailScreen
import az.naymera.app.ui.GoalsScreen
import az.naymera.app.ui.JournalScreen
import androidx.compose.material.icons.automirrored.filled.Chat
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
import az.naymera.app.data.NotConfiguredLocal
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

private fun Destination.icon(): ImageVector = when (this) {
    Destination.Home -> Icons.Filled.Home
    Destination.Assistant -> Icons.AutoMirrored.Filled.Chat
    Destination.Goals -> Icons.Filled.Flag
    Destination.Approvals -> Icons.Filled.VerifiedUser
    Destination.Memory -> Icons.Filled.Memory
    Destination.Journal -> Icons.Filled.History
    Destination.Settings -> Icons.Filled.Settings
}

private fun Destination.label(): Int = when (this) {
    Destination.Home -> R.string.tab_home
    Destination.Assistant -> R.string.nav_assistant
    Destination.Goals -> R.string.nav_goals
    Destination.Approvals -> R.string.nav_approvals
    Destination.Memory -> R.string.tab_memory
    Destination.Journal -> R.string.nav_journal
    Destination.Settings -> R.string.tab_settings
}

@Composable
private fun Main(store: SessionStore, api: ApiClient, onLoggedOut: () -> Unit) {
    val nav = rememberNavController()
    val sync = remember { EventSync(api, store) }
    val speech = remember { MockSpeech() } // real səs qoşulana qədər SİMULYASİYA
    val onDevice = remember { NotConfiguredLocal } // cihazdakı Local AI: model qurulmayıb (NOT CONFIGURED)
    val mainVm: MainViewModel = viewModel(factory = viewModelFactory { initializer { MainViewModel(api, sync) } })
    val chatVm: ChatViewModel = viewModel(factory = viewModelFactory { initializer { ChatViewModel(api, speech, onDevice) { mainVm.currentMode } } })
    val coreVm: CoreViewModel = viewModel(factory = viewModelFactory { initializer { CoreViewModel(ApiCoreRepository(api)) } })
    val activity = LocalContext.current as FragmentActivity
    val stepUpTitle = stringResource(R.string.stepup_title)
    val stepUpCancel = stringResource(R.string.stepup_cancel)
    val signer = remember(activity, stepUpTitle, stepUpCancel) { BiometricSigner(activity, stepUpTitle, stepUpCancel) }
    LaunchedEffect(Unit) { mainVm.startSync() }
    val route = nav.currentBackStackEntryAsState().value?.destination?.route
    val selected = Destination.selectedTab(route)
    var menu by remember { mutableStateOf(false) }

    fun go(r: String) = nav.navigate(r) { launchSingleTop = true }

    Scaffold(
        topBar = {
            Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Text(stringResource(R.string.app_name), style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
                Box {
                    IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, contentDescription = stringResource(R.string.nav_more)) }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        (Destination.overflow.map { it.route to it.label() } + listOf(
                            "notifications" to R.string.tab_notifications, "agents" to R.string.tab_agents, "history" to R.string.tab_history, "capabilities" to R.string.nav_capabilities,
                        )).forEach { (r, l) -> DropdownMenuItem(text = { Text(stringResource(l)) }, onClick = { menu = false; go(r) }) }
                    }
                }
            }
        },
        bottomBar = {
            NavigationBar {
                Destination.bottomBar.forEach { d ->
                    NavigationBarItem(
                        selected = selected == d,
                        onClick = { go(d.route) },
                        icon = { Icon(d.icon(), contentDescription = stringResource(d.label())) },
                        label = { Text(stringResource(d.label()), maxLines = 1) },
                    )
                }
            }
        },
    ) { pad ->
        NavHost(nav, startDestination = Destination.START, modifier = Modifier.padding(pad)) {
            composable(Destination.Home.route) {
                DashboardScreen(coreVm, mainVm, onGoals = { go(Destination.Goals.route) }, onApprovals = { go(Destination.Approvals.route) },
                    onJournal = { go(Destination.Journal.route) }, onCapabilities = { go("capabilities") })
            }
            composable(Destination.Assistant.route) {
                Column {
                    TextButton(onClick = { go("history") }, modifier = Modifier.padding(horizontal = 8.dp)) { Text(stringResource(R.string.tab_history)) }
                    ChatScreen(chatVm, Modifier.weight(1f))
                }
            }
            composable(Destination.Goals.route) { GoalsScreen(coreVm, onOpen = { id -> go(Destination.goalDetail(id)) }) }
            composable(Destination.GOAL_DETAIL) { entry ->
                val id = entry.arguments?.getString("id").orEmpty()
                if (Regex("[A-Za-z0-9_.:\\-]{1,64}").matches(id)) GoalDetailScreen(coreVm, id, signer)
            }
            composable(Destination.Approvals.route) { ApprovalScreen(coreVm, signer) }
            composable(Destination.Memory.route) { MemoryScreen(mainVm) }
            composable(Destination.Journal.route) { JournalScreen(coreVm) }
            composable(Destination.Settings.route) { SettingsScreen(store, api, onLoggedOut) }
            composable("notifications") { NotificationsScreen(mainVm) }
            composable("agents") { AgentsScreen(mainVm) }
            composable("capabilities") { HomeScreen(mainVm, onDevice) }
            composable("history") { HistoryScreen(api, onOpen = { id -> chatVm.open(id); go(Destination.Assistant.route) }) }
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
