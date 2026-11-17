// Ayrıca JVM layihəsi (Android deyil): tətbiqin Android-dan asılı olmayan Kotlin qatını kompilyasiya və sınaq edir.
// Yalnız Maven Central istifadə olunur; Google Maven/Android SDK tələb olunmur.
pluginManagement {
    repositories { maven("https://repo1.maven.org/maven2/") }
}
dependencyResolutionManagement {
    repositories { maven("https://repo1.maven.org/maven2/") }
}
rootProject.name = "naymera-jvm-contract"
