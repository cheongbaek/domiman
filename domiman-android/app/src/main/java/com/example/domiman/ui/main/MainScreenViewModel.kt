package com.example.domiman.ui.main

import android.util.Base64
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.domiman.data.DomimanEvent
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.DomimanStatus
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** 상태 메시지 — domiman.py STATUS_TEXT와 동일한 문구를 여기선 report_text/
 * 기본값으로만 다룬다(전체 표는 PC와 동일해 재복사하지 않음). */
private const val DEFAULT_STATUS = "강태공이 낚시를 준비합니다."
private const val NO_TARGET_STATUS = "제어할 PC를 먼저 선택하세요."
private const val LOG_MAX_LINES = 8 // 항상 펼쳐진 채 마지막 8줄만
private const val PENDING_TIMEOUT_MS = 15_000L // domiman.py _check_pending_timeout과 동일
private const val TIMER_DEBOUNCE_MS = 1_500L // PC의 타이머 3초 디바운스에 대응(과발신 방지)
// 스크린샷은 ack(15초) 뒤에도 사진 전송을 더 기다려야 하므로 일반 명령보다 길게 잡는다
// (domiman_m.SHOT_START_TIMEOUT 15초 + SHOT_STALL_TIMEOUT 10초보다 넉넉한 전체 상한).
private const val SHOT_TIMEOUT_MS = 45_000L

data class MainUiState(
  val resolutionLabel: String = "감지 중",
  val resAuto: Boolean = true,
  val timerText: String = "0",
  val logSave: Boolean = false,
  val rod: Boolean = true,
  val bait: Boolean = true,
  val running: Boolean = false,
  val statusMessage: String = NO_TARGET_STATUS,
  val logLines: List<String> = emptyList(),
  val isPending: Boolean = false,
  /** 제어 PC 방에 입장해 상태를 받아오는 중(선택 직후). */
  val isConnectingTarget: Boolean = false,
)

/** 도착한 스크린샷 — 원본 PNG 바이트는 저장·클립보드 복사에 그대로 쓰인다
 * (domiman.py ScreenshotWindow.png와 같은 역할). ByteArray를 담으므로 MainUiState
 * 안에 두지 않고 별도 StateFlow로 뺐다(data class equals가 매번 배열을 훑지 않게). */
class ScreenshotData(val name: String, val bytes: ByteArray)

class MainScreenViewModel(private val repository: DomimanRepository) : ViewModel() {
  private val _uiState = MutableStateFlow(seedState())
  val uiState: StateFlow<MainUiState> = _uiState.asStateFlow()

  private val _screenshotResult = MutableStateFlow<ScreenshotData?>(null)
  val screenshotResult: StateFlow<ScreenshotData?> = _screenshotResult.asStateFlow()

  private var shotTimeoutJob: Job? = null

  /** 최상단 박스에 표시할 제어 PC(없으면 '제어 PC 선택하기'). */
  val selectedPc: StateFlow<String?> = repository.selectedPc

  /** 선택 다이얼로그에 뜨는 PC 후보 목록(추가·삭제 가능). */
  val pcList: StateFlow<List<String>> = repository.pcList

  /** domiserver 연결 상태(끊기면 상단에 표시). */
  val connected: StateFlow<Boolean> = repository.connected

  private var pendingTimeoutJob: Job? = null
  private var timerDebounceJob: Job? = null

  init {
    // 세션은 로그인 시 이미 시작됐고 앱 스코프에서 산다. 여기선 화면이 떠 있는
    // 동안 이벤트를 구독해 UI에 반영만 한다.
    viewModelScope.launch { repository.events.collect(::handleEvent) }
  }

  /** 화면이 포그라운드로 돌아올 때(ON_RESUME) 호출 — 백그라운드에서 끊겼을 수
   * 있는 세션을 되살리고 방 재입장/상태 재동기화를 시킨다. 세션 자체가 소실됐고
   * 되살리지도 못하면 onSessionLost()로 로그인 화면으로 보낸다. */
  fun onResume(onSessionLost: () -> Unit) {
    viewModelScope.launch {
      if (repository.ensureSessionAlive()) {
        repository.lastStatus?.let(::applyStatus)
      } else {
        onSessionLost()
      }
    }
  }

  /** 로그인 때 받아둔 상태로 초기 UI를 채운다(없으면 기본값). */
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
      statusMessage = DEFAULT_STATUS,
    )
  }

  // ---------- 제어 PC 선택 ----------
  /**
   * 최상단 박스에서 PC를 고르면 그 PC의 채팅방(domi_fishing_{PC})에 입장·구독하고
   * S 질의로 상태를 받아온다. 꺼져 있는 PC여도 선택은 남는다(켜지면 다시 붙는다).
   *
   * **로그아웃 없이 제어 대상만 갈아탄다** — domichat 로그인 세션은 그대로 두고
   * 이전 PC의 방에서만 나오고(unsub+leave) 새 방에 들어간다. 그래서 화면에 남아
   * 있던 이전 PC의 타이머/해상도/실행중 값은 여기서 지워야 한다(안 그러면 새 PC의
   * 상태가 도착하기 전까지 남의 값이 보인다).
   */
  fun onSelectPc(pc: String) {
    _uiState.value =
      _uiState.value.copy(
        isConnectingTarget = true,
        isPending = false,
        running = false,
        resolutionLabel = "감지 중",
        statusMessage = "'$pc'에 연결하는 중...",
      )
    pendingTimeoutJob?.cancel()
    timerDebounceJob?.cancel()
    addLog("'$pc' 제어를 시작합니다...")
    viewModelScope.launch {
      val result = repository.selectPc(pc)
      _uiState.value = _uiState.value.copy(isConnectingTarget = false)
      val status = result.status
      if (result.ok && status != null) {
        applyStatus(status)
        _uiState.value = _uiState.value.copy(statusMessage = DEFAULT_STATUS)
        addLog("'$pc'에 연결되었습니다.")
      } else {
        _uiState.value = _uiState.value.copy(statusMessage = targetFailText(pc, result.reason))
        addLog(targetFailText(pc, result.reason))
      }
    }
  }

  fun onAddPc(name: String): Boolean = repository.addPc(name)

  fun onRemovePc(name: String) = repository.removePc(name)

  private fun targetFailText(pc: String, reason: String?): String =
    when (reason) {
      "room_missing" -> "'$pc'가 아직 domichat에 접속한 적이 없습니다."
      "bad_pw_room" -> "'$pc' 채팅방 비밀번호가 맞지 않습니다."
      "blocked" -> "'$pc' 채팅방에서 강제 퇴장된 계정입니다."
      "no_response" -> "'$pc'가 응답하지 않습니다. PC가 켜져 있는지 확인하세요."
      "no_session" -> "서버에 로그인되어 있지 않습니다."
      else -> "'$pc'에 연결하지 못했습니다."
    }

  // ---------- 이벤트 ----------
  private fun handleEvent(event: DomimanEvent) {
    when (event.ev) {
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
          "I" -> {
            if (event.shotFail) {
              shotTimeoutJob?.cancel()
              addLog("화면을 캡처하지 못했습니다.")
            } else {
              // ack만 왔다 — 사진은 이어서 "screenshot" 이벤트로 온다. shotTimeoutJob이
              // 이미 돌고 있으므로 isPending을 그대로 두고(잠금 유지) 여기서 끝낸다.
              addLog("화면을 찍는 중입니다. 사진을 기다립니다...")
              return
            }
          }
        }
        if (event.tank != null && event.tank.size >= 2) {
          addLog("살림망 ${event.tank[0]}/${event.tank[1]}")
        } else if (event.tankFail) {
          addLog("수량 파싱 실패")
        }
        clearPending()
      }
      "screenshot" -> {
        shotTimeoutJob?.cancel()
        _uiState.value = _uiState.value.copy(isPending = false)
        if (event.ok == true && event.pngB64 != null) {
          _screenshotResult.value =
            ScreenshotData(event.name ?: "screenshot.png", Base64.decode(event.pngB64, Base64.DEFAULT))
        } else {
          addLog(screenshotFailText(event.reason))
        }
      }
      "report" -> event.reportText?.let(::addLog)
      "target_joined" -> addLog("'${event.pc}' 채팅방에 입장했습니다.")
      "target_denied" -> {
        addLog(targetFailText(event.pc ?: "", event.reason))
        clearPending()
      }
      "target_gone" -> {
        addLog("'${event.pc}'의 접속이 종료되었습니다.")
        _uiState.value =
          _uiState.value.copy(running = false, statusMessage = "'${event.pc}'가 접속을 종료했습니다.")
        clearPending()
      }
      "disconnected" -> addLog("서버 연결이 끊겼습니다. 다시 접속하는 중...")
      "reconnected" -> addLog("서버에 다시 접속했습니다.")
      "cert_changed", "login_fail" -> event.msg?.let(::addLog)
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

  /** '시작/중지' — 현재 상태에 따라 반대 명령을 보낸다.
   * repository.send*는 suspend(Dispatchers.IO)이므로 반드시 코루틴 안에서
   * 호출할 것 — 메인 스레드에서 직접 부르면 블로킹 소켓 발신이 UI를 막는다. */
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

  /** 타이머 입력 — 키 입력마다 발신하지 않도록 화면엔 즉시 반영하되 실제 T
   * 발신은 1.5초 디바운스(PC의 3초 디바운스에 대응). */
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

  /** 예약 종료 — minutes분 뒤 종료(0=해제). PC의 2단계(Y ack→창→Y,n) 대신
   * 모바일은 앱 안에서 분을 입력받아 바로 Y,n을 보낸다(응답은 Y 에코로 확인). */
  fun onSchedExitSet(minutes: Double) {
    markPending()
    viewModelScope.launch { repository.sendSchedExitSet(minutes) }
  }

  fun onCollectNow() {
    markPending()
    viewModelScope.launch { repository.sendCollectNow() }
  }

  /** '실시간 수량확인'(N). */
  fun onTankQuery() {
    markPending()
    viewModelScope.launch { repository.sendTankQuery() }
  }

  /** '스크린샷' — ack(15초)뿐 아니라 사진 전송까지 기다려야 해서 일반
   * markPending()의 15초보다 긴 자체 타임아웃(SHOT_TIMEOUT_MS)을 쓴다. */
  fun onScreenshot() {
    _uiState.value = _uiState.value.copy(isPending = true)
    shotTimeoutJob?.cancel()
    shotTimeoutJob =
      viewModelScope.launch {
        delay(SHOT_TIMEOUT_MS)
        if (_uiState.value.isPending) {
          _uiState.value = _uiState.value.copy(isPending = false)
          addLog("스크린샷 요청이 시간 초과되었습니다.")
        }
      }
    viewModelScope.launch { repository.sendScreenshot() }
  }

  fun onScreenshotDialogDismiss() {
    _screenshotResult.value = null
  }

  private fun screenshotFailText(reason: String?): String =
    when (reason) {
      "timeout" -> "사진이 도착하지 않았습니다."
      "corrupt" -> "사진이 깨져서 도착했습니다. 다시 요청하세요."
      "aborted" -> "전송이 중단되었습니다."
      else -> "스크린샷을 받지 못했습니다."
    }

  fun onLogClear() {
    _uiState.value = _uiState.value.copy(logLines = emptyList())
  }

  /** '로그아웃' — repository.logout()은 **즉시 반환**하고 소켓/서비스 정리는
   * 앱 스코프에서 뒤따른다. 예전엔 여기서 파이썬 정리를 메인 스레드로 하다가
   * 앱이 '응답 없음'이 되곤 했다. */
  fun onLogout() {
    repository.logout()
  }

  // onCleared에서 세션을 끄지 않는다(중요): 세션은 로그인~로그아웃 수명이며 앱
  // 싱글턴에 매여 있다. 설정 화면 이동/화면 재생성으로 이 VM이 정리돼도 세션은
  // 유지돼야 백그라운드 알림이 계속 온다.
}
