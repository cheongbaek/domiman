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
  LOGIN, // 기본 상태 — [로그인]/[…] 버튼, 자동로그인 체크박스 표시
  EDIT, // 최근 로그인 '수정' — [수정]/[취소] 버튼, 자동로그인 체크박스 숨김
}

data class LoginUiState(
  val ip: String = "", // domiserver IP (또는 IP:포트)
  val id: String = "", // domichat ID
  val pw: String = "", // domichat PW
  val autoLoginChecked: Boolean = false,
  val mode: LoginFormMode = LoginFormMode.LOGIN,
  val editingOriginal: SavedLoginJson? = null,
  val isSubmitting: Boolean = false,
  val errorMessage: String? = null,
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
              ip = pending.ip,
              id = pending.id,
              pw = pending.pw,
              mode = LoginFormMode.EDIT,
              editingOriginal = pending,
            )
          repository.pendingEdit.value = null
        }
      }
    }
  }

  fun onIpChange(v: String) {
    _uiState.value = _uiState.value.copy(ip = v, errorMessage = null)
  }

  fun onIdChange(v: String) {
    _uiState.value = _uiState.value.copy(id = v, errorMessage = null)
  }

  fun onPwChange(v: String) {
    _uiState.value = _uiState.value.copy(pw = v, errorMessage = null)
  }

  fun onAutoLoginToggle(v: Boolean) {
    _uiState.value = _uiState.value.copy(autoLoginChecked = v)
  }

  /** '…'(최근 로그인) 진입 직전 호출 — 현재 '자동 로그인' 체크 상태를 저장소에
   * 넘겨, 최근 로그인 항목으로 로그인해도 그 체크대로 자동로그인이 무장되게 한다. */
  fun captureAutoLoginForRecent() {
    repository.pendingAutoLoginArm = _uiState.value.autoLoginChecked
  }

  private fun fieldsValid(s: LoginUiState) = s.ip.isNotBlank() && s.id.isNotBlank() && s.pw.isNotBlank()

  /** '로그인' 버튼. 실패하면 폼을 비우고 서버가 준 사유를 그대로 보여준다
   * (없는 ID/비번 틀림 → '존재하지 않는 ID입니다.', 중복 접속 → '이미 접속 중' 등). */
  fun submitLogin(onSuccess: () -> Unit) {
    val s = _uiState.value
    if (!fieldsValid(s)) {
      _uiState.value = s.copy(errorMessage = "모두 입력해 주세요.")
      return
    }
    _uiState.value = s.copy(isSubmitting = true, errorMessage = null)
    viewModelScope.launch {
      val result = repository.login(s.ip.trim(), s.id.trim(), s.pw, s.autoLoginChecked)
      if (result.ok) {
        _uiState.value = LoginUiState()
        onSuccess()
      } else {
        _uiState.value =
          LoginUiState(
            ip = s.ip, // 서버 주소는 남겨 둔다(대부분 맞는 값이고 다시 치기 번거롭다)
            autoLoginChecked = s.autoLoginChecked,
            errorMessage = result.msg ?: "로그인에 실패했습니다. 다시 입력하세요.",
          )
      }
    }
  }

  /** edit 모드 '수정' 버튼 — 목록에 반영 후 최근 로그인 화면으로. */
  fun confirmEdit(onDone: () -> Unit) {
    val s = _uiState.value
    val original = s.editingOriginal ?: return
    if (!fieldsValid(s)) {
      _uiState.value = s.copy(errorMessage = "모두 입력해 주세요.")
      return
    }
    repository.updateEntry(original, SavedLoginJson(s.ip.trim(), s.id.trim(), s.pw))
    _uiState.value = LoginUiState()
    onDone()
  }

  /** edit 모드 '취소' 버튼. */
  fun cancelEdit(onDone: () -> Unit) {
    _uiState.value = LoginUiState()
    onDone()
  }
}
