@file:Suppress("unused", "UNUSED_PARAMETER")
package androidx.lifecycle.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider

class CreationExtras
class InitializerViewModelFactoryBuilder

inline fun <reified VM : ViewModel> InitializerViewModelFactoryBuilder.initializer(noinline initializer: CreationExtras.() -> VM) {}

fun viewModelFactory(builder: InitializerViewModelFactoryBuilder.() -> Unit): ViewModelProvider.Factory = object : ViewModelProvider.Factory {}
