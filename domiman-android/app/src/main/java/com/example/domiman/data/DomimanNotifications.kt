package com.example.domiman.data

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.example.domiman.MainActivity
import java.util.concurrent.atomic.AtomicInteger

/**
 * 알림 채널 생성 + 실제 알림 발송을 담당. 채널 2개:
 *  - ongoing: Foreground Service의 상시 알림(조용한 낮은 중요도)
 *  - events : 회수/교체/튕김 등 이벤트 알림(기본 중요도, 소리/헤드업)
 * POST_NOTIFICATIONS 권한이 없으면 NotificationManagerCompat.notify가 조용히
 * 무시되므로(런타임 예외 없음) 호출부에서 권한 체크를 강제하지는 않는다.
 */
class DomimanNotifications(context: Context) {
  private val appContext = context.applicationContext
  private val manager = NotificationManagerCompat.from(appContext)
  private val eventIdSeq = AtomicInteger(1000)

  /** 알림/상시알림을 탭하면 앱(MainActivity)을 연다. CLEAR_TOP으로 기존 인스턴스
   * 재사용(중복 실행 방지). PendingIntent는 Android 12+에서 FLAG_IMMUTABLE 필수. */
  private fun openAppIntent(): PendingIntent {
    val intent =
      Intent(appContext, MainActivity::class.java)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
    return PendingIntent.getActivity(
      appContext,
      0,
      intent,
      PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )
  }

  init {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
      val nm = appContext.getSystemService(NotificationManager::class.java)
      nm.createNotificationChannel(
        NotificationChannel(CHANNEL_ONGOING, "원격 연결 유지", NotificationManager.IMPORTANCE_LOW)
          .apply { description = "도미맨이 백그라운드에서 원격 상태를 수신 중임을 표시" },
      )
      nm.createNotificationChannel(
        NotificationChannel(CHANNEL_EVENTS, "낚시 이벤트 알림", NotificationManager.IMPORTANCE_DEFAULT)
          .apply { description = "살림망 회수/낚싯대·미끼 교체/게임 튕김 등 알림" },
      )
    }
  }

  /** Foreground Service가 startForeground에 쓰는 상시 알림. 탭하면 앱을 연다. */
  fun ongoingNotification(text: String): Notification =
    NotificationCompat.Builder(appContext, CHANNEL_ONGOING)
      .setContentTitle("도미맨")
      .setContentText(text)
      .setSmallIcon(android.R.drawable.ic_menu_compass)
      .setOngoing(true)
      .setPriority(NotificationCompat.PRIORITY_LOW)
      .setContentIntent(openAppIntent())
      .build()

  /** 이벤트 알림 발송. 매번 새 ID라 쌓여서 보인다. 탭하면 앱을 연다. */
  fun postEvent(text: String) {
    val n =
      NotificationCompat.Builder(appContext, CHANNEL_EVENTS)
        .setContentTitle("도미맨")
        .setContentText(text)
        .setSmallIcon(android.R.drawable.ic_menu_compass)
        .setPriority(NotificationCompat.PRIORITY_DEFAULT)
        .setAutoCancel(true)
        .setContentIntent(openAppIntent())
        .build()
    try {
      manager.notify(eventIdSeq.incrementAndGet(), n)
    } catch (_: SecurityException) {
      // POST_NOTIFICATIONS 미허용 — 무시(권한 요청은 첫 실행 플로우가 담당)
    }
  }

  companion object {
    const val CHANNEL_ONGOING = "domiman_ongoing"
    const val CHANNEL_EVENTS = "domiman_events"
    const val ONGOING_NOTIFICATION_ID = 42
  }
}
