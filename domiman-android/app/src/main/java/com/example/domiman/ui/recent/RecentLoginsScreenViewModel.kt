package com.example.domiman.ui.recent

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.LoginStoreJson
import com.example.domiman.data.SavedLoginJson
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

class RecentLoginsScreenViewModel(private val repository: DomimanRepository) : ViewModel() {
  val loginState: StateFlow<LoginStoreJson> =
    repository.loginState.stateIn(
      viewModelScope,
      SharingStarted.WhileSubscribed(5000),
      repository.loginState.value,
    )

  private val _message = MutableStateFlow<String?>(null)
  val message: StateFlow<String?> = _message.asStateFlow()

  private val _busy = MutableStateFlow(false)
  val busy: StateFlow<Boolean> = _busy.asStateFlow()

  /** 짧게 탭 — 그 정보(IP·ID·PW)로 즉시 로그인. 자동로그인 무장 여부는 로그인
   * 화면에서 '자동 로그인'을 체크하고 '…'로 들어왔는지(pendingAutoLoginArm)로
   * 결정한다 — 체크했으면 이 최근 로그인도 자동로그인으로 무장. */
  fun tapEntry(entry: SavedLoginJson, onSuccess: () -> Unit) {
    if (_busy.value) return
    _busy.value = true
    _message.value = null
    viewModelScope.launch {
      val arm = repository.pendingAutoLoginArm
      val result = repository.login(entry.ip, entry.id, entry.pw, arm)
      _busy.value = false
      if (result.ok) onSuccess() else _message.value = result.msg ?: "로그인에 실패했습니다."
    }
  }

  /** 길게 눌러 '수정' 선택 — Login 화면에 넘길 대상만 심어둔다. */
  fun requestEdit(entry: SavedLoginJson) {
    repository.pendingEdit.value = entry
  }

  /** 길게 눌러 '삭제' 선택. */
  fun deleteEntry(entry: SavedLoginJson) {
    repository.deleteEntry(entry)
  }
}
