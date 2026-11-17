package az.naymera.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

private val Ink = Color(0xFF0B1020)
private val Teal = Color(0xFF3DD6C6)
private val Violet = Color(0xFF8B7CFF)

private val Dark = darkColorScheme(
    primary = Teal, onPrimary = Ink, secondary = Violet, background = Ink, onBackground = Color(0xFFE8ECF8),
    surface = Color(0xFF131A2E), onSurface = Color(0xFFE8ECF8), surfaceVariant = Color(0xFF1B2440),
    onSurfaceVariant = Color(0xFFA9B3D1), error = Color(0xFFFF6B7A),
)

private val Light = lightColorScheme(
    primary = Color(0xFF0E8F83), onPrimary = Color.White, secondary = Color(0xFF5B4BDB),
    background = Color(0xFFF6F8FD), surface = Color.White, surfaceVariant = Color(0xFFE9EEFA),
)

private val AppShapes = Shapes(
    small = RoundedCornerShape(12.dp), medium = RoundedCornerShape(18.dp), large = RoundedCornerShape(28.dp),
)

@Composable
fun NaymeraTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (dark) Dark else Light, shapes = AppShapes, content = content)
}
