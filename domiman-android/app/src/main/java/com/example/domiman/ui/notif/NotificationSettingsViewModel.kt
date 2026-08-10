package com.example.domiman.ui.notif

import androidx.lifecycle.ViewModel
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.NotificationSettings
import kotlinx.coroutines.flow.StateFlow

/** 알림 설정 화면 VM — 실제 저장/영속화는 repository.notificationPrefs가 담당하고
 * 여기선 그 StateFlow를 노출하고 토글을 전달만 한다. */
class NotificationSettingsViewModel(private val repository: DomimanRepository) : ViewModel() {
  val settings: StateFlow<NotificationSettings> = repository.notificationPrefs.settings

  fun onMasterChange(on: Boolean) = repository.notificationPrefs.setMaster(on)

  fun onItemChange(key: String, on: Boolean) = repository.notificationPrefs.setItem(key, on)
}
