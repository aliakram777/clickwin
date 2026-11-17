# kotlinx.serialization: model sinifləri və serializer-lər saxlanılır.
-keepattributes *Annotation*, InnerClasses
-keepclassmembers class az.naymera.app.data.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class az.naymera.app.data.**$$serializer { *; }
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
