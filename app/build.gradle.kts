import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.ksp)
}

val localProperties = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.exists()) {
        file.inputStream().use { load(it) }
    }
}

val defaultServerUrl =
    (localProperties.getProperty("OPENCLAW_SERVER_URL") ?: "")
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")

val mobileGatewayApiKey =
    localProperties.getProperty("mobile_gateway_api_key") ?: ""

android {
    namespace = "com.user"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.user"
        minSdk = 29
        targetSdk = 35
        versionCode = 3
        versionName = "1.2.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "DEFAULT_SERVER_URL", "\"$defaultServerUrl\"")
        if (mobileGatewayApiKey.isNotEmpty()) {
            buildConfigField("String", "MOBILE_GATEWAY_API_KEY", "\"${mobileGatewayApiKey}\"")
        } else {
            buildConfigField("String", "MOBILE_GATEWAY_API_KEY", "\"\"")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources {
            excludes += "META-INF/versions/**"
            pickFirsts.add("org/bouncycastle/x509/CertPathReviewerMessages_de.properties")
            pickFirsts.add("org/bouncycastle/x509/CertPathReviewerMessages.properties")
        }
    }

    kotlinOptions { jvmTarget = "17" }
    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
}

dependencies {
    implementation(libs.core.ktx)
    implementation(libs.appcompat)
    implementation(libs.material)
    implementation(libs.constraintlayout)
    implementation(libs.coordinatorlayout)

    implementation(libs.room.runtime)
    implementation(libs.room.ktx)
    implementation(libs.androidx.remote.creation.compose)
    ksp(libs.room.compiler)

    implementation(libs.retrofit)
    implementation(libs.retrofit.gson)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)

    implementation(libs.coroutines)
    implementation(libs.lifecycle.runtime)
    implementation(libs.lifecycle.viewmodel)

    implementation("com.tom-roush:pdfbox-android:2.0.27.0") {
        exclude(group = "org.bouncycastle", module = "bcprov-jdk15to18")
    }

    // MVVM
    implementation(libs.lifecycle.livedata)
    implementation(libs.activity.ktx)

    implementation(libs.bouncycastle)

    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.espresso)
}

