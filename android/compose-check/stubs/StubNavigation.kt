@file:Suppress("unused", "UNUSED_PARAMETER")
package androidx.navigation

class NavOptionsBuilder { var launchSingleTop: Boolean = false }
class NavDestination { val route: String? = null }
class Bundle { fun getString(key: String): String? = null }
class NavBackStackEntry { val destination: NavDestination = NavDestination(); val arguments: Bundle? = null }
class NavGraphBuilder
open class NavController { fun navigate(route: String, builder: NavOptionsBuilder.() -> Unit = {}) {} }
class NavHostController : NavController()
