@file:Suppress("unused", "UNUSED_PARAMETER")
package androidx.lifecycle

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

abstract class ViewModel
val ViewModel.viewModelScope: CoroutineScope get() = CoroutineScope(SupervisorJob() + Dispatchers.Default)
class ViewModelProvider { interface Factory }
