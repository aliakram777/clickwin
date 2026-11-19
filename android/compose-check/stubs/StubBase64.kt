@file:Suppress("unused", "UNUSED_PARAMETER")
package android.util

object Base64 {
    const val NO_WRAP = 2
    fun encodeToString(input: ByteArray, flags: Int): String = java.util.Base64.getEncoder().encodeToString(input)
}
