package az.naymera.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import az.naymera.app.R

/**
 * Hadisə axınından gələn proaktiv bildirişlər. Bildirişlər yalnız təklifdir: heç bir əməliyyatı icra etmir.
 * Push (FCM) yoxdur: hadisələr yalnız tətbiq açıq olanda yenilənir və bu açıq göstərilir.
 */
@Composable
fun NotificationsScreen(vm: MainViewModel, modifier: Modifier = Modifier) {
    val items by vm.notifications.collectAsStateWithLifecycle()
    val connection by vm.connection.collectAsStateWithLifecycle()
    LaunchedEffect(Unit) { vm.startSync() }

    Column(modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp)) {
            Text(stringResource(R.string.notif_title), style = MaterialTheme.typography.titleLarge)
            ConnectionChip(connection)
            Text(
                stringResource(R.string.push_unavailable),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (items.isEmpty()) {
            Text(
                stringResource(R.string.notif_empty),
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(16.dp),
            )
        }
        LazyColumn(
            Modifier.weight(1f),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            items(items, key = { it.id }) { n ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(n.title, style = MaterialTheme.typography.titleMedium)
                        Text(n.body, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        if (n.isMock) {
                            Text(
                                stringResource(R.string.mock_badge),
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.secondary,
                            )
                        }
                    }
                }
            }
        }
    }
}
