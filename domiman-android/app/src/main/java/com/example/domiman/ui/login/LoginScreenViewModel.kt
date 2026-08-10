package com.example.domiman.ui.login

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.SavedLoginJson
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

enum class LoginFormMode {
  LOGIN, // Sheet1 A1:E18 기본 상태 — [로그인]/[…] 버튼, 자동로그인 체크박스 표시
  EDIT, // Sheet1 G9 — [수정]/[취소] 버튼, 자동로그인 체크박스 숨김
}

data class LoginUiState(
  val id: String = "",
  val targetPc: String = "",
  val channel: String = "",
  val autoLoginChecked: Boolean = false,
  val mode: LoginFormMode = LoginFormMode.LOGIN,
  val editingOriginal: SavedLoginJson? = null,
  val isSubmitting: Boolean = false,
  val errorMessage: String? = null, // Sheet1 G5: 자동로그인/로그인 실패 안내
)

class LoginScreenViewModel(private val repository: DomimanRepository) : ViewModel() {
  private val _uiState = MutableStateFlow(LoginUiState())
  val uiState: StateFlow<LoginUiState> = _uiState.asStateFlow()

  init {
    // Login 화면은 RecentLogins 진입 시 스택에서 안 사라지고 밑에 남아있다가
    // '수정' 선택 시 다시 드러나는 것이라(ViewModel 재생성 없음), init에서
    // 한 번만 값을 읽지 않고 계속 구독해야 그 시점의 편집 요청을 받을 수 있다.
    viewModelScope.launch {
      repository.pendingEdit.collect { pending ->
        if (pending != null) {
          _uiState.value =
            LoginUiState(
              id = pending.id,
              targetPc = pending.targetPc,
              channel = pending.channel,
              mode = LoginFormMode.EDIT,
              editingOriginal = pending,
            )
          repository.pendingEdit.value = null
        }
      }
    }
  }

  fun onIdChange(v: String) {
    _uiState.value = _uiState.value.copy(id = v, errorMessage = null)
  }

  fun onTargetPcChange(v: String) {
    _uiState.value = _uiState.value.copy(targetPc = v, errorMessage = null)
  }

  fun onChannelChange(v: String) {
    _uiState.value = _uiState.value.copy(channel = v, errorMessage = null)
  }

  fun onAutoLoginToggle(v: Boolean) {
    _uiState.value = _uiState.value.copy(autoLoginChecked = v)
  }

  /** '…'(최근 로그인) 진입 직전 호출 — 현재 '자동 로그인' 체크 상태를 저장소에
   * 넘겨, 최근 로그인 항목으로 로그인해도 그 체크대로 자동로그인이 무장되게 한다. */
  fun captureAutoLoginForRecent() {
    repository.pendingAutoLoginArm = _uiState.value.autoLoginChecked
  }

  private fun fieldsValid(s: LoginUiState) = s.id.isNotBlank() && s.targetPc.isNotBlank() && s.channel.isNotBlank()

  /** '로그인' 버튼(Sheet1 B14). 실패하면 G5대로 폼을 비우고 재입력 안내. */
  fun submitLogin(onSuccess: () -> Unit) {
    val s = _uiState.value
    if (!fieldsValid(s)) {
      _uiState.value = s.copy(errorMessage = "모두 입력해 주세요.")
      return
    }
    _uiState.value = s.copy(isSubmitting = true, errorMessage = null)
    viewModelScope.launch {
      val status = repository.login(s.id, s.targetPc, s.channel, s.autoLoginChecked)
      if (status != null) {
        onSuccess()
      } else {
        _uiState.value = LoginUiState(errorMessage = "입력값을 초기화하고 다시 입력하세요.")
      }
    }
  }

  /** edit 모드 '수정' 버튼(Sheet1 G10) — 목록에 반영 후 최근 로그인 화면으로. */
  fun confirmEdit(onDone: () -> Unit) {
    val s = _uiState.value
    val original = s.editingOriginal ?: return
    if (!fieldsValid(s)) {
      _uiState.value = s.copy(errorMessage = "모두 입력해 주세요.")
      return
    }
    repository.updateEntry(original, SavedLoginJson(s.id, s.targetPc, s.channel))
    _uiState.value = LoginUiState()
    onDone()
  }

  /** edit 모드 '취소' 버튼(Sheet1 G9). */
  fun cancelEdit(onDone: () -> Unit) {
    _uiState.value = LoginUiState()
    onDone()
  }
}
