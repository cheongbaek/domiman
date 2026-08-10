package com.example.domiman.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** 알림 설정 항목. key는 domiman_m.NOTIFY_KEYS(파이썬)와 정확히 일치해야 한다
 * — dispatch_json의 report_notify_key가 이 key로 돌아오기 때문. 순서 = 설정
 * 화면 체크박스 순서. */
enum class NotifyItem(val key: String, val label: String) {
  ROUTINE_START("routine_start", "살림망 회수 시작 시 알림"),
  ROUTINE_SUCCESS("routine_success", "살림망 회수 성공 시 알림"),
  ROUTINE_FAIL("routine_fail", "살림망 회수 실패 시 알림"),
  ROD_START("rod_start", "낚싯대 교체 시작 시 알림"),
  ROD_SUCCESS("rod_success", "낚싯대 교체 성공 시 알림"),
  ROD_FAIL("rod_fail", "낚싯대 교체 실패 시 알림"),
  BAIT_START("bait_start", "미끼 교체 시작 시 알림"),
  BAIT_SUCCESS("bait_success", "미끼 교체 성공 시 알림"),
  BAIT_FAIL("bait_fail", "미끼 교체 실패 시 알림"),
  CRASH("crash", "게임이 튕겼을 때 알림"),
}

/** 알림 설정 스냅샷. master=맨 위 '알림 켜기'(꺼지면 하위 전체 봉인·미발송),
 * items=10개 이벤트별 on/off. 기본값 전부 true(앱UI설명 요구). */
data class NotificationSettings(
  val master: Boolean = true,
  val items: Map<String, Boolean> = NotifyItem.entries.associate { it.key to true },
) {
  /** 해당 알림키(NOTIFY_KEYS의 값)가 실제로 발송되어야 하는가.
   * 마스터가 켜져 있고 개별 항목도 켜져 있어야 true. */
  fun isEnabled(notifyKey: String?): Boolean {
    if (!master || notifyKey == null) return false
    return items[notifyKey] == true
  }
}

/**
 * 알림 설정 영속화(SharedPreferences). 앱을 껐다 켜도 유지되고, 앱 데이터/캐시를
 * 지우면 함께 사라진다(앱UI설명 Sheet2 알림 요구 — 최근 로그인과 동일 저장소 성격).
 */
class NotificationPrefs(context: Context) {
  private val prefs =
    context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

  private val _settings = MutableStateFlow(load())
  val settings: StateFlow<NotificationSettings> = _settings.asStateFlow()

  private fun load(): NotificationSettings =
    NotificationSettings(
      master = prefs.getBoolean(KEY_MASTER, true),
      items = NotifyItem.entries.associate { it.key to prefs.getBoolean(it.key, true) },
    )

  fun setMaster(on: Boolean) {
    prefs.edit().putBoolean(KEY_MASTER, on).apply()
    _settings.value = _settings.value.copy(master = on)
  }

  fun setItem(key: String, on: Boolean) {
    prefs.edit().putBoolean(key, on).apply()
    _settings.value = _settings.value.copy(items = _settings.value.items + (key to on))
  }

  /** 현재 스냅샷(백그라운드 알림 발송 판단용 — Flow 구독 없이 즉시 읽기). */
  fun current(): NotificationSettings = _settings.value

  companion object {
    private const val PREFS_NAME = "domiman_notif_prefs"
    private const val KEY_MASTER = "notif_master"
  }
}
