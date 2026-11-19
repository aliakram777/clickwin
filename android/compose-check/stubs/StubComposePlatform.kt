@file:Suppress("unused", "UNUSED_PARAMETER")
package androidx.compose.ui.platform

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.fragment.app.Context
import androidx.fragment.app.FragmentActivity

val LocalContext = staticCompositionLocalOf<Context> { FragmentActivity() }
