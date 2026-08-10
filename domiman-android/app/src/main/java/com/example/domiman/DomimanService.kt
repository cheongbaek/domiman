package com.example.domiman

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.ServiceCompat
import com.example.domiman.data.DomimanNotifications

/**
 * 포그라운드 서비스 — ntfy 스트림 자체를 들고 있지는 않고(스트림은 앱 싱글턴
 * DomimanRepository.appScope에서 돌아 프로세스 수명과 함께 간다), 이 서비스가
 * 상시 알림과 함께 떠 있어 **OS가 백그라운드에서 프로세스를 죽이지 못하게** 한다.
 * 그 결과 앱이 백그라운드로 가도 스트림이 살아 이벤트 알림이 계속 오고, 다시
 * 앱에 들어와도 세션이 그대로라 "껐다 켜야 정상" 문제가 사라진다.
 *
 * 로그인 성공 시 start(), 로그아웃 시 stop(). START_STICKY라 OS가 예외적으로
 * 죽여도 재시작되며, 재시작 시 세션이 없으면(프로세스 재생성) 저장된 마지막
 * 로그인으로 재접속을 시도한다(DomimanRepository.reviveIfNeeded).
 */
class DomimanService : Service() {
  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    val notifications = DomimanNotifications(this)
    // 34+ = specialUse(시간제한 없음), 29~33 = dataSync, 그 미만 = 0.
    val type =
      when {
        Build.VERSION.SDK_INT >= 34 -> ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q ->
          ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        else -> 0
      }
    runCatching {
      ServiceCompat.startForeground(
        this,
        DomimanNotifications.ONGOING_NOTIFICATION_ID,
        notifications.ongoingNotification("원격 상태 수신 중"),
        type,
      )
    }
    // intent == null → 시스템에 의한 sticky 재시작. 프로세스가 새로 떴을 수
    // 있으니 세션 소실 시 저장된 로그인으로 되살린다.
    (application as? DomimanApplication)?.repository?.reviveIfNeeded()
    return START_STICKY
  }

  companion object {
    fun start(context: Context) {
      val intent = Intent(context, DomimanService::class.java)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent)
      } else {
        context.startService(intent)
      }
    }

    fun stop(context: Context) {
      context.stopService(Intent(context, DomimanService::class.java))
    }
  }
}
