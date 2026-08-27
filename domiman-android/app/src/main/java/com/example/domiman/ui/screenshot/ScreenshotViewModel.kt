package com.example.domiman.ui.screenshot

import android.util.Base64
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.domiman.data.DomimanEvent
import com.example.domiman.data.DomimanRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

// ack 15초 + 전송 시작 15초 + 정지 10초(domiman_m.SHOT_START_TIMEOUT/SHOT_STALL_TIMEOUT)를
// 다 합친 것보다 넉넉한 전체 상한 — 파이썬이 실패 이벤트를 내지 못하는 경우의 최후 방어선.
private const val OVERALL_TIMEOUT_MS = 45_000L

sealed interface ScreenshotUiState {
  data object Loading : ScreenshotUiState

  data class Success(val name: String, val bytes: ByteArray) : ScreenshotUiState

  data class Failed(val message: String) : ScreenshotUiState
}

/** 홈 위젯 '스크린샷' 칸(ScreenshotActivity)이 쓰는 단발 요청용 ViewModel.
 * repository는 앱 싱글턴이라 세션·펌프는 이미 살아있거나 여기서 되살릴 뿐이다. */
class ScreenshotViewModel(private val repository: DomimanRepository) : ViewModel() {
  private val _state = MutableStateFlow<ScreenshotUiState>(ScreenshotUiState.Loading)
  val state: StateFlow<ScreenshotUiState> = _state.asStateFlow()

  private var timeoutJob: Job? = null

  init {
    viewModelScope.launch { repository.events.collect(::handleEvent) }
    request()
  }

  private fun request() {
    viewModelScope.launch {
      if (!repository.hasSelectedPc()) {
        _state.value = ScreenshotUiState.Failed("제어할 PC가 선택돼 있지 않습니다.")
        return@launch
      }
      if (!repository.ensureClientReady()) {
        _state.value = ScreenshotUiState.Failed("서버에 연결할 수 없습니다.")
        return@launch
      }
      if (!repository.sendScreenshot()) {
        _state.value = ScreenshotUiState.Failed("이미 다른 스크린샷 요청을 기다리는 중입니다.")
        return@launch
      }
      timeoutJob?.cancel()
      timeoutJob =
        launch {
          delay(OVERALL_TIMEOUT_MS)
          if (_state.value is ScreenshotUiState.Loading) {
            _state.value = ScreenshotUiState.Failed("응답이 없습니다.")
          }
        }
    }
  }

  private fun handleEvent(event: DomimanEvent) {
    if (_state.value !is ScreenshotUiState.Loading) return
    when (event.ev) {
      "reply" ->
        if (event.echo == "I" && event.shotFail) {
          timeoutJob?.cancel()
          _state.value = ScreenshotUiState.Failed("화면을 캡처하지 못했습니다.")
        }
      "screenshot" -> {
        timeoutJob?.cancel()
        _state.value =
          if (event.ok == true && event.pngB64 != null) {
            ScreenshotUiState.Success(
              event.name ?: "screenshot.png",
              Base64.decode(event.pngB64, Base64.DEFAULT),
            )
          } else {
            ScreenshotUiState.Failed(reasonText(event.reason))
          }
      }
      "disconnected" -> {
        timeoutJob?.cancel()
        _state.value = ScreenshotUiState.Failed("서버 연결이 끊겼습니다.")
      }
      "target_gone" -> {
        timeoutJob?.cancel()
        _state.value = ScreenshotUiState.Failed("제어 중인 PC의 접속이 종료되었습니다.")
      }
    }
  }

  private fun reasonText(reason: String?): String =
    when (reason) {
      "timeout" -> "사진이 도착하지 않았습니다."
      "corrupt" -> "사진이 깨져서 도착했습니다."
      "aborted" -> "전송이 중단되었습니다."
      else -> "스크린샷을 받지 못했습니다."
    }
}
