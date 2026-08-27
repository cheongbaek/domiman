plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
  alias(libs.plugins.chaquopy)
}

android {
    namespace = "com.example.domiman"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.example.domiman"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
        ndk {
            // Chaquopy가 Python 인터프리터를 내장할 ABI(에뮬레이터=x86_64, 실기기=arm64-v8a)
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
      compose = true
      aidl = false
      buildConfig = false
      shaders = false
    }

    packaging {
      resources {
        excludes += "/META-INF/{AL2.0,LGPL2.1}"
      }
    }
}

kotlin {
    jvmToolchain(17)
}

chaquopy {
    defaultConfig {
        version = "3.13"
        // buildPython 자동 탐지는 이 PC의 py 런처 슬롯이 Microsoft Store 스텁일
        // 때 실패한다(반복된 함정) — python.org 정식 설치 경로를 명시적으로 지정.
        // ⚠️ 다른 PC에서 빌드하려면 이 경로를 그 PC의 python.org 인터프리터로
        //    바꿔야 하며, version 값과 major.minor가 같아야 한다.
        // 260828a: 3.12로 적혀 있었으나 이 PC에는 3.12가 설치돼 있지 않다
        // (`py -0` = 3.14·3.13). 3.13은 실제로 APK를 만들어낸 조합이다.
        buildPython("C:/Users/windo/AppData/Local/Programs/Python/Python313/python.exe")
        // pip 의존성 없음: domichat 이식 후 domiman_m.py는 표준 라이브러리
        // (socket/ssl/struct/hashlib/json/threading/queue)만 쓴다. ntfy 시절의
        // requests는 더 이상 필요 없어 걷어냈다(빌드도 그만큼 빨라진다).
    }
    sourceSets {
        getByName("main") {
            // domiman_m.py(및 향후 파생 모듈)가 여기에 위치
            srcDir("src/main/python")
        }
    }
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  // Navigation
  implementation(libs.androidx.navigation3.ui)
  implementation(libs.androidx.navigation3.runtime)
  implementation(libs.androidx.lifecycle.viewmodel.navigation3)

  // JSON (Chaquopy 경계에서 PyObject 대신 JSON 문자열로 주고받기 위함)
  implementation(libs.kotlinx.serialization.json)
}

// APK 배포 위치: 빌드는 기본 경로(app/build/outputs/apk/debug/app-debug.apk)에 하고,
// 완료 후 C:\Users\windo\OneDrive - 한국교통대학교\domiman.apk 로 '복사'해 배포한다.
// (자동 copy 태스크는 두지 않음 — 매 빌드 후 수동/스크립트 복사.)
