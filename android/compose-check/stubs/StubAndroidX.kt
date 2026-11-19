@file:Suppress("unused", "UNUSED_PARAMETER")
package androidx.fragment.app

import android.view.Window

open class Context
open class FragmentActivity : Context() { val window: Window = Window() }
