pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    // Flutter's module plugin adds its own repositories, so settings repos are preferred, not exclusive.
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
        maven("https://storage.googleapis.com/download.flutter.io")
    }
}

rootProject.name = "pulse-target-app"
include(":app")

// Flutter page (add-to-app, source integration). Requires `flutter pub get` in flutter_module first.
apply(from = File(settingsDir, "flutter_module/.android/include_flutter.groovy"))
