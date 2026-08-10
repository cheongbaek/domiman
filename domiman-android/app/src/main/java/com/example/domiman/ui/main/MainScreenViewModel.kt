package com.example.domiman.ui.main

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.domiman.data.DispatchResult
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.DomimanStatus
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** Sheet2 A11 '[상태 메시지]' — domiman.py STATUS_TEXT와 동일한 문구를 여기선
 * 그대로 report_text/기본값으로만 다룬다(전체 표는 PC와 동일해 재복사하지 않음). */
private const val DEFAULT_STATUS = "강태공이 낚시를 준비합니다."
private const val LOG_MAX_LINES = 8 // Sheet2 G15 — 항상 펼쳐진 채 마지막 8줄만
private const val PENDING_TIMEOUT_MS = 15_000L // domiman.py _check_pending_timeout과 동일
private const val TIMER_DEBOUNCE_MS = 1_500L // PC의 타이머 3초 디바운스에 대응(과발신 방지)

data class MainUiState(
  val resolutionLabel: String = "감지 중",
  val resAuto: Boolean = true,
  val timerText: String = "0",
  val logSave: Boolean = false,
  val rod: Boolean = true,
  val bait: Boolean = true,
  val running: Boolean = false,
  val statusMessage: String = DEFAULT_STATUS,
  val logLines: List<String> = emptyList(),
  val isPending: Boolean = false,
)

class MainScreenViewModel(private val repository: DomimanRepository) : ViewModel() {
  private val _uiState = MutableStateFlow(seedState())
  val uiState: StateFlow<MainUiState> = _uiState.asStateFlow()

  private var pendingTimeoutJob: Job? = null
  private var timerDebounceJob: Job? = null

  init {
    // 세션(서비스+스트림)은 로그인 시 이미 시작됐고 앱 스코프에서 산다. 여기선
    // 화면이 떠 있는 동안 이벤트를 구독해 UI에 반영만 한다.
    viewModelScope.launch { repository.events.collect(::handleEvent) }
  }

  /** 화면이 포그라운드로 돌아올 때(ON_RESUME) 호출 — 백그라운드에서 끊겼을 수
   * 있는 스트림을 새로 붙이고 상태를 재동기화. 세션 자체가 소실됐고 되살리지도
   * 못하면 onSessionLost()로 로그인 화면으로 보낸다. */
  fun onResume(onSessionLost: () -> Unit) {
    viewModelScope.launch {
      if (repository.ensureSessionAlive()) {
        // 재접속 성공(콜드스타트/프로세스 사망 후 포함) — 방금 받아둔 상태로 UI 갱신
        // (안 그러면 seedState가 null→기본값 "감지 중"인 채로 남아있을 수 있음).
        repository.lastStatus?.let(::applyStatus)
      } else {
        onSessionLost()
      }
    }
  }

  /** 로그인 때 받아둔 상태로 초기 UI를 채운다(없으면 기본값). 진입 직후
   * "감지 중"/running=false가 잠깐 보이던 문제를 없앤다. */
  private fun seedState(): MainUiState {
    val s = repository.lastStatus ?: return MainUiState()
    return MainUiState(
      resolutionLabel = resolutionLabelOf(s.resolution, s.resAuto),
      resAuto = s.resAuto,
      timerText = s.timer,
      logSave = s.logsave,
      rod = s.rod ?: true,
      bait = s.bait ?: true,
      running = s.running,
    )
  }

  private fun handleEvent(event: DispatchResult) {
    when (event.kind) {
      "reply" -> {
        // 어떤 형태든 '응답'이 왔다 = 우리가 보낸 명령이 처리됐다는 뜻이므로
        // 대기를 푼다(보고 ',Z,F,*'는 unsolicited라 reply가 아니고 여기 안 옴).
        event.status?.let(::applyStatus)
        when (event.echo) {
          // G/P 에코 = 시작/중지가 실제로 적용된 결과 상태(도미맨 PC가 결과에
          // 맞춰 G 또는 P를 되돌려줌). 이걸로 running을 갱신한다.
          "G" -> _uiState.value = _uiState.value.copy(running = true)
          "P" -> _uiState.value = _uiState.value.copy(running = false)
          "W" -> addLog("즉시 회수 명령이 접수되었습니다.")
          "Y" -> {
            val m = event.schedMinutes
            if (m != null) {
              addLog(if (m == "0") "예약 종료가 해제되었습니다." else "${m}분 뒤 종료가 예약되었습니다.")
            }
          }
          "Q" -> addLog("원격 프로그램이 종료되었습니다.")
        }
        if (event.tank != null) {
          addLog("살림망 ${event.tank[0]}/${event.tank[1]}")
        } else if (event.tankFail) {
          addLog("수량 파싱 실패")
        }
        clearPending()
      }
      "report" -> event.reportText?.let(::addLog)
    }
  }

  private fun applyStatus(s: DomimanStatus) {
    _uiState.value =
      _uiState.value.copy(
        resolutionLabel = resolutionLabelOf(s.resolution, s.resAuto),
        resAuto = s.resAuto,
        timerText = s.timer,
        logSave = s.logsave,
        rod = s.rod ?: _uiState.value.rod,
        bait = s.bait ?: _uiState.value.bait,
        running = s.running,
      )
  }

  private fun resolutionLabelOf(resolution: String, auto: Boolean): String {
    val base =
      when (resolution) {
        "1080" -> "1920 x 1080"
        "1440" -> "2560 x 1440"
        else -> "감지 실패"
      }
    return if (auto && resolution in setOf("1080", "1440")) "$base (자동 감지됨)" else base
  }

  private fun addLog(line: String) {
    val updated = (_uiState.value.logLines + line).takeLast(LOG_MAX_LINES)
    _uiState.value = _uiState.value.copy(logLines = updated)
  }

  /** 명령 발신 시 UI를 잠그고, 15초 안에 응답(상태/에코)이 없으면 자동 해제 +
   * '응답 없음' 로그(대상이 꺼져 있어도 UI가 영구히 잠기지 않게). */
  private fun markPending() {
    _uiState.value = _uiState.value.copy(isPending = true)
    pendingTimeoutJob?.cancel()
    pendingTimeoutJob =
      viewModelScope.launch {
        delay(PENDING_TIMEOUT_MS)
        if (_uiState.value.isPending) {
          _uiState.value = _uiState.value.copy(isPending = false)
          addLog("응답이 없습니다.")
        }
      }
  }

  private fun clearPending() {
    pendingTimeoutJob?.cancel()
    pendingTimeoutJob = null
    if (_uiState.value.isPending) _uiState.value = _uiState.value.copy(isPending = false)
  }

  /** '시작/중지'(Sheet2 A9) — 현재 상태에 따라 반대 명령을 보낸다.
   * repository.send*는 suspend(Dispatchers.IO)이므로 반드시 코루틴 안에서
   * 호출할 것 — 메인 스레드에서 직접 부르면 블로킹 네트워크 호출이 UI를
   * 막는다(실기기에서 ntfy 발신이 버벅이던 원인이었음). */
  fun onStartStop() {
    markPending()
    viewModelScope.launch {
      if (_uiState.value.running) repository.sendStop() else repository.sendStart()
    }
  }

  fun onResolutionManual(mode: String) {
    markPending()
    viewModelScope.launch { repository.sendSetResolution(mode) } // "1080" | "1440"
  }

  fun onResolutionAuto() {
    markPending()
    viewModelScope.launch { repository.sendSetResolution("a") }
  }

  /** 타이머 입력 — 키 입력마다 발신하면 ntfy 한도를 태우므로, 화면엔 즉시
   * 반영하되 실제 T 발신은 1.5초 디바운스(PC의 3초 디바운스에 대응). */
  fun onTimerInput(text: String) {
    _uiState.value = _uiState.value.copy(timerText = text)
    val minutes = text.toDoubleOrNull() ?: return
    timerDebounceJob?.cancel()
    timerDebounceJob =
      viewModelScope.launch {
        delay(TIMER_DEBOUNCE_MS)
        markPending()
        repository.sendSetTimer(minutes)
      }
  }

  fun onFlagsToggled(logSave: Boolean, rod: Boolean, bait: Boolean) {
    markPending()
    viewModelScope.launch { repository.sendSetFlags(logSave, rod, bait) }
  }

  /** 예약 종료(Sheet2 A12) — minutes분 뒤 종료(0=해제). PC의 2단계(Y ack→창→Y,n)
   * 대신 모바일은 앱 안에서 분을 입력받아 바로 Y,n을 보낸다(응답은 Y 에코로
   * 확인). */
  fun onSchedExitSet(minutes: Double) {
    markPending()
    viewModelScope.launch { repository.sendSchedExitSet(minutes) }
  }

  fun onCollectNow() {
    markPending()
    viewModelScope.launch { repository.sendCollectNow() }
  }

  /** Sheet2 G13 — '업데이트' 대신 확정된 '실시간 수량확인' 버튼. */
  fun onTankQuery() {
    markPending()
    viewModelScope.launch { repository.sendTankQuery() }
  }

  fun onLogClear() {
    _uiState.value = _uiState.value.copy(logLines = emptyList())
  }

  /** 뒤로가기 2번(Sheet2 G20)에서 호출 — 무장 해제 + 세션 종료. */
  fun onLogout() {
    repository.logout()
  }

  // onCleared에서 세션을 끄지 않는다(중요): 세션은 로그인~로그아웃 수명이며 앱
  // 싱글턴에 매여 있다. 설정 화면 이동/화면 재생성으로 이 VM이 정리돼도 세션은
  // 유지돼야 백그라운드 알림이 계속 온다.
}
