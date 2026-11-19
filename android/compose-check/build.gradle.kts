plugins {
    kotlin("jvm") version "2.0.20"
    kotlin("plugin.serialization") version "2.0.20"
    kotlin("plugin.compose") version "2.0.20"
}

// TİP YOXLAMASI (APK DEYİL): tətbiqin HƏQİQİ Compose/Material3 mənbələri JVM-də Compose Multiplatform 1.7.0 (androidx.compose.* ilə eyni paket adları,
// BOM 2024.09.00 ≈ Compose 1.7.0 / Material3 1.3.0) qarşısında kompilyasiya olunur. Yalnız Android-a məxsus simvollar (R, navigation, lifecycle, FragmentActivity,
// Biometric/Keystore, SessionStore) `stubs/`-dadır. Bu AGP/aapt2/manifest/resurs/R8 yoxlaması DEYİL; real build üçün Android SDK + Google Maven lazımdır.
val app = "../app/src/main/java/az/naymera/app"
val generatedR = layout.buildDirectory.dir("generated/r")

val genR by tasks.registering {
    val xml = file("../app/src/main/res/values/strings.xml")
    val out = generatedR
    inputs.file(xml); outputs.dir(out)
    doLast {
        val names = Regex("""<string name="([a-z0-9_]+)"""").findAll(xml.readText()).map { it.groupValues[1] }.toList()
        val body = names.mapIndexed { i, n -> "        const val $n = ${i + 1}" }.joinToString("\n")
        out.get().asFile.resolve("R.kt").apply { parentFile.mkdirs() }.writeText(
            "package az.naymera.app\n\nobject R {\n    object string {\n$body\n    }\n    object drawable { const val ic_launcher = 1 }\n}\n",
        )
    }
}

val syncSources by tasks.registering(Sync::class) {
    from("$app/data") { include("*.kt"); exclude("SessionStore.kt") }
    from("$app/domain") { include("*.kt") }
    from("$app/security") { include("Messages.kt", "CoreChallenge.kt") }
    from("$app/ui") { include("*.kt") }
    from("$app/voice") { include("*.kt") }
    from(app) { include("NaymeraApp.kt") }
    from("stubs") { include("*.kt") }
    into(layout.buildDirectory.dir("generated/app-sources"))
}

// JDK 17 (CI) VƏ yeni JDK-lar ilə işləsin deyə toolchain sabitlənmir: bayt-kod hədəfi 17.
java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
    sourceSets["main"].kotlin.srcDir(syncSources).srcDir(genR)
}

dependencies {
    implementation("org.jetbrains.compose.material3:material3:1.7.0")
    implementation("org.jetbrains.compose.material:material-icons-extended:1.7.0")
    implementation("org.jetbrains.compose.foundation:foundation:1.7.0")
    implementation("org.jetbrains.compose.runtime:runtime:1.7.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
}
