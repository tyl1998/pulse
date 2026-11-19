plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.pulse.target"
    compileSdk = 36
    // Same NDK as the Flutter module; needed to strip libflutter.so (otherwise ~165 MB per ABI).
    ndkVersion = "28.2.13676358"

    defaultConfig {
        applicationId = "com.pulse.target"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0.0"
        // Real devices (arm64) + emulators (x86_64); Flutter release AOT has no 32-bit x86.
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    packaging {
        // Flutter debug engine ships a ~250 MB Vulkan validation layer; not needed for the fixture.
        jniLibs { excludes += "**/libVkLayer_khronos_validation.so" }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Test fixture only: release is signed with the debug key on purpose.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        abortOnError = true
        // Fixture app: hard-coded Chinese/English UI strings are intentional test data.
        disable += setOf("HardcodedText", "SetTextI18n")
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(project(":flutter"))
    testImplementation("junit:junit:4.13.2")
}
