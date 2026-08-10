package com.example.domiman

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import com.example.domiman.theme.DomimanTheme

class MainActivity : ComponentActivity() {
  // 세션을 담은 저장소는 앱 싱글턴(Activity 재생성에도 유지).
  private val repository by lazy { (application as DomimanApplication).repository }

  private val requestNotifPermission =
    registerForActivityResult(ActivityResultContracts.RequestPermission()) {
      // 허용/거부 무엇이든 다음 단계(배터리 최적화 예외 요청)로.
      requestBatteryExemption()
    }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)

    // 최초 생성 때만 요청(화면 회전 등 재생성 시 재요청 방지).
    if (savedInstanceState == null) requestFirstLaunchPermissions()

    enableEdgeToEdge()
    setContent {
      var darkOverride by remember { mutableStateOf<Boolean?>(null) }
      val systemDark = isSystemInDarkTheme()
      DomimanTheme(darkTheme = darkOverride ?: systemDark) {
        Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
          MainNavigation(
            repository = repository,
            isDark = darkOverride ?: systemDark,
            onToggleDark = { darkOverride = !(darkOverride ?: systemDark) },
          )
        }
      }
    }
  }

  /** 첫 실행 시(그리고 매 실행 미허용 상태면) 백그라운드 알림에 필요한 권한을
   * 요청한다: ① 알림 게시(POST_NOTIFICATIONS, Android 13+) ② 배터리 최적화
   * 예외(절전모드가 백그라운드 네트워크/서비스를 죽이지 않도록). 알림 권한
   * 결과 콜백이 배터리 예외 요청으로 이어진다. */
  private fun requestFirstLaunchPermissions() {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
      val granted =
        ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
          PackageManager.PERMISSION_GRANTED
      if (!granted) {
        requestNotifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        return // 콜백에서 배터리 예외로 이어감
      }
    }
    requestBatteryExemption()
  }

  /** 배터리 최적화 예외(절전모드 비활성화) 요청 — 이미 예외면 아무 것도 안 함. */
  private fun requestBatteryExemption() {
    val pm = getSystemService(PowerManager::class.java) ?: return
    if (pm.isIgnoringBatteryOptimizations(packageName)) return
    runCatching {
      startActivity(
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
          .setData(Uri.parse("package:$packageName")),
      )
    }
  }
}
