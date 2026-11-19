@file:Suppress("unused", "UNUSED_PARAMETER")
package androidx.navigation.compose

import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.navigation.NavBackStackEntry
import androidx.navigation.NavGraphBuilder
import androidx.navigation.NavHostController

@Composable
fun rememberNavController(): NavHostController = NavHostController()

@Composable
fun NavHostController.currentBackStackEntryAsState(): State<NavBackStackEntry?> = mutableStateOf(null)

@Composable
fun NavHost(navController: NavHostController, startDestination: String, modifier: Modifier = Modifier, builder: NavGraphBuilder.() -> Unit) {}

fun NavGraphBuilder.composable(route: String, content: @Composable (NavBackStackEntry) -> Unit) {}
