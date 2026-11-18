plugins {
    kotlin("jvm") version "2.0.20"
    kotlin("plugin.serialization") version "2.0.20"
}

// Tətbiqin həqiqi mənbə faylları köçürülür (Android-dan asılı SessionStore.kt xaric); kod təkrarlanmır.
val syncSources by tasks.registering(Copy::class) {
    from("../app/src/main/java/az/naymera/app/data") {
        include("*.kt")
        exclude("SessionStore.kt")
    }
    from("../app/src/main/java/az/naymera/app/security/Messages.kt")
    from("../app/src/main/java/az/naymera/app/security/CoreChallenge.kt")
    into(layout.buildDirectory.dir("generated/app-sources"))
}

kotlin {
    jvmToolchain(21)
    sourceSets["main"].kotlin.srcDir(syncSources)
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
    testImplementation("junit:junit:4.13.2")
}

tasks.test {
    useJUnit()
    testLogging {
        events("passed", "skipped", "failed")
        showStandardStreams = false
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
