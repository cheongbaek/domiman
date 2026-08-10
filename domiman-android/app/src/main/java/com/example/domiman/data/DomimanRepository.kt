package com.example.domiman.data

import android.content.Context
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.example.domiman.DomimanService
import com.example.domiman.DomimanWidgetProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json

private const val PREFS_NAME = "domiman_prefs"
private const val KEY_LOGIN_STORE = "login_store_json"
private const val KEY_SESSION_ACTIVE = "session_active"
private const val KEY_WIDGET_QTY = "widget_qty"       // 위젯 수량 표시(마지막 새로고침 값)
private const val KEY_WIDGET_RUNNING = "widget_running" // 위젯 재생/중지 아이콘 상태
private const val PENDING_TIMEOUT = 15.0

/**
 * domiman_m.py(Chaquopy Python)를 감싸는 **앱 싱글턴** 저장소. ntfy 프로토콜
 * (DomimanClient) + '최근 로그인'(LoginStore) + 스트림 세션 + 알림 발송을 담당.
 *
 * 세션(activeClient·스트림)은 Activity/ViewModel이 아니라 이 싱글턴과
 * **앱 스코프(appScope)** 에 매여 있어, 화면 재생성이나 백그라운드 복귀에도
 * 살아남는다. 백그라운드에서 프로세스가 죽지 않도록 로그인 시 포그라운드
 * 서비스(DomimanService)를 함께 띄운다.
 */
class DomimanRepository(context: Context) {
  private val appContext = context.applicationContext
  private val prefs = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
  private val json = Json { ignoreUnknownKeys = true }

  // 스트림/재접속이 도는 앱 수명 스코프(뷰모델/액티비티와 독립).
  private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

  private val py: Python get() = Python.getInstance()
  private val module: PyObject get() = py.getModule("domiman_m")

  private var storeObj: PyObject =
    module.get("LoginStore")!!.callAttr("from_json", prefs.getString(KEY_LOGIN_STORE, null) ?: "")

  val notificationPrefs = NotificationPrefs(appContext)
  private val notifications = DomimanNotifications(appContext)

  private val _loginState = MutableStateFlow(readStoreState())
  val loginState: StateFlow<LoginStoreJson> = _loginState.asStateFlow()

  /** RecentLogins '수정' → Login 화면 전환 사이 편집 대상 전달용(즉시 소비 후 null). */
  val pendingEdit = MutableStateFlow<SavedLoginJson?>(null)

  /** 로그인 화면 '자동 로그인' 체크 상태를 '…'(최근 로그인) 진입 시 넘겨받아,
   * 최근 로그인 항목 탭으로 로그인해도 그 체크대로 자동로그인을 무장하게 한다. */
  @Volatile var pendingAutoLoginArm: Boolean = false

  /** 현재 로그인된 세션의 DomimanClient(로그인 성공 후에만 존재). */
  @Volatile var activeClient: PyObject? = null
    private set

  /** 로그인(S 질의) 성공 시 받은 상태. MainScreen 진입 즉시 UI 시드용. */
  @Volatile var lastStatus: DomimanStatus? = null
    private set

  private val _events = MutableSharedFlow<DispatchResult>(extraBufferCapacity = 64)
  val events: SharedFlow<DispatchResult> = _events.asSharedFlow()

  private var streamJob: Job? = null

  private fun readStoreState(): LoginStoreJson =
    json.decodeFromString(storeObj.callAttr("to_json").toString())

  private fun persist() {
    prefs.edit().putString(KEY_LOGIN_STORE, storeObj.callAttr("to_json").toString()).apply()
    _loginState.value = readStoreState()
  }

  private fun setSessionActive(active: Boolean) =
    prefs.edit().putBoolean(KEY_SESSION_ACTIVE, active).apply()

  private fun sessionWasActive(): Boolean = prefs.getBoolean(KEY_SESSION_ACTIVE, false)

  private fun makeClient(id: String, targetPc: String, channel: String): PyObject =
    module.callAttr("DomimanClient", id, targetPc, channel)

  private fun toPySavedLogin(entry: SavedLoginJson): PyObject =
    module.callAttr("SavedLogin", entry.id, entry.targetPc, entry.channel)

  private suspend fun attemptLogin(client: PyObject): DomimanStatus? =
    withContext(Dispatchers.IO) {
      val statusJson = module.callAttr("attempt_login_json", client, PENDING_TIMEOUT).toString()
      json.decodeFromString<DomimanStatus?>(statusJson)
    }

  /** Sheet1 '로그인' 버튼 및 최근 로그인 짧게 탭 공용. 성공 시 최근 로그인 반영
   * (중복이면 갱신+맨 위로) + 자동로그인 무장 + 세션 시작(서비스+스트림). */
  suspend fun login(id: String, targetPc: String, channel: String, autoLoginChecked: Boolean): DomimanStatus? {
    val client = makeClient(id, targetPc, channel)
    val status = attemptLogin(client)
    if (status != null) {
      storeObj.callAttr("add_or_bump", makeSaved(id, targetPc, channel))
      storeObj.put("auto_login_enabled", autoLoginChecked)
      persist()
      activeClient = client
      lastStatus = status
      beginSession()
    }
    return status
  }

  private fun makeSaved(id: String, targetPc: String, channel: String): PyObject =
    module.callAttr("SavedLogin", id, targetPc, channel)

  /** 앱 시작 시 1회(Sheet1 G4/G6). 무장 안 됐거나 이력 없으면 시도 안 함(null). */
  suspend fun tryAutoLoginOnLaunch(): DomimanStatus? {
    val state = readStoreState()
    val last = state.recent.firstOrNull() ?: return null
    if (!state.autoLoginEnabled) return null
    val client = makeClient(last.id, last.targetPc, last.channel)
    val status = attemptLogin(client)
    if (status != null) {
      activeClient = client
      lastStatus = status
      beginSession()
    }
    return status
  }

  fun updateEntry(old: SavedLoginJson, updated: SavedLoginJson) {
    storeObj.callAttr("update", toPySavedLogin(old), toPySavedLogin(updated))
    persist()
  }

  fun deleteEntry(entry: SavedLoginJson) {
    storeObj.callAttr("remove", toPySavedLogin(entry))
    persist()
  }

  /** Sheet2 뒤로가기 2번(G20) — 자동로그인 무장 해제 + 세션 완전 종료. 최근 로그인
   * 목록과 마지막 로그인 값은 유지(G21, 로그아웃 ≠ 데이터 삭제). */
  fun logout() {
    stopStream()
    DomimanService.stop(appContext)
    setSessionActive(false)
    storeObj.put("auto_login_enabled", false)
    persist()
    activeClient = null
    lastStatus = null
    DomimanWidgetProvider.refresh(appContext) // 수량 → '로그인' 표시로 전환
  }

  // ---------- 세션(서비스 + 스트림) ----------
  private fun beginSession() {
    setSessionActive(true)
    // 위젯 초기값: 수량은 최초 1회 "0/0"(요구 기본값), running은 현재 상태 반영.
    if (!prefs.contains(KEY_WIDGET_QTY)) prefs.edit().putString(KEY_WIDGET_QTY, "0/0").apply()
    prefs.edit().putBoolean(KEY_WIDGET_RUNNING, lastStatus?.running == true).apply()
    // 포그라운드 서비스 시작. 백그라운드(위젯 탭 등)에서의 시작 제한으로 예외가
    // 날 수 있으나(배터리 최적화 예외 허용 시 완화) 스트림/발신 자체는 계속 되므로
    // 삼켜서 앱이 죽지 않게 한다.
    runCatching { DomimanService.start(appContext) }
    startStream()
    DomimanWidgetProvider.refresh(appContext) // 로그인 → 수량 표시로 전환
  }

  private fun startStream() {
    val client = activeClient ?: return
    streamJob?.cancel()
    streamJob =
      appScope.launch {
        // domiman.py _ntfy_stream_loop과 같은 재연결 패턴(끊기면 1초 쉬고 재시도).
        while (isActive) {
          try {
            client.callAttr(
              "stream",
              { title: String, body: String -> onStreamMessage(client, title, body) },
              { !isActive },
            )
          } catch (_: Exception) {
            // 네트워크 블립 — 아래에서 재연결
          }
          if (isActive) delay(1000)
        }
      }
  }

  private fun stopStream() {
    streamJob?.cancel()
    streamJob = null
    // 블로킹 iter_lines를 즉시 깨워 스레드가 살아남지 않게(domiman.py와 동일).
    runCatching { activeClient?.callAttr("stream_disconnect") }
  }

  /** 앱이 포그라운드로 복귀했거나(ON_RESUME) 서비스가 sticky 재시작됐을 때 호출.
   *  - 세션이 살아있으면: 스트림을 새로 붙이고(오래된 연결 정리) 상태를 재동기화.
   *  - 세션이 없는데 직전까지 로그인 상태였으면: 저장된 마지막 로그인으로 재접속.
   * 반환: 세션이 살아있게 되면 true(재접속도 실패하면 false → 호출부가 로그인
   * 화면으로 보냄). */
  suspend fun ensureSessionAlive(): Boolean {
    if (activeClient == null) {
      if (!sessionWasActive()) return false // 정상 로그아웃 상태 — 되살리지 않음
      val last = readStoreState().recent.firstOrNull() ?: return false
      val client = makeClient(last.id, last.targetPc, last.channel)
      val status = attemptLogin(client) ?: return false
      activeClient = client
      lastStatus = status
      beginSession()
      return true
    }
    // 세션 있음 — 스트림 재접속 + 상태 재동기화(S 질의).
    startStream()
    runCatching { withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_login") } }
    return true
  }

  /** 서비스 sticky 재시작 시(프로세스 재생성 가능) 세션 복구를 시도. */
  fun reviveIfNeeded() {
    if (activeClient != null || !sessionWasActive()) return
    appScope.launch { ensureSessionAlive() }
  }

  // ---------- Sheet2 제어 명령(전부 suspend + Dispatchers.IO) ----------
  suspend fun sendStart() = withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_start") }

  suspend fun sendStop() = withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_stop") }

  suspend fun sendSchedExitSet(minutes: Double) =
    withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_sched_exit_set", minutes) }

  suspend fun sendCollectNow() =
    withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_collect_now") }

  suspend fun sendSetResolution(mode: String) =
    withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_set_resolution", mode) }

  suspend fun sendSetTimer(minutes: Double) =
    withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_set_timer", minutes) }

  suspend fun sendSetFlags(logSave: Boolean, rod: Boolean?, bait: Boolean?) =
    withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_set_flags", logSave, rod, bait) }

  suspend fun sendTankQuery() =
    withContext(Dispatchers.IO) { activeClient?.callAttr("cmd_tank_query") }

  // ---------- 스트림 수신 처리 ----------
  private fun onStreamMessage(client: PyObject, title: String, body: String) {
    // 스트림 스레드에서 예외가 새어 나가면 앱이 죽을 수 있으니 통째로 방어.
    try {
      handleStreamMessage(client, title, body)
    } catch (_: Exception) {
    }
  }

  private fun handleStreamMessage(client: PyObject, title: String, body: String) {
    val resultJson = module.callAttr("dispatch_json", client, title, body).toString()
    val result = json.decodeFromString<DispatchResult>(resultJson)
    if (result.kind == null) return
    _events.tryEmit(result) // 화면이 떠 있으면 UI가 반영

    // 보고 이벤트는 알림 설정에 맞춰 (앱이 백그라운드여도) 알림을 띄운다.
    if (result.kind == "report" && result.reportText != null) {
      if (notificationPrefs.current().isEnabled(result.reportNotifyKey)) {
        notifications.postEvent(result.reportText)
      }
    }

    // ---- 위젯 상태 갱신 ----
    var widgetChanged = false
    // running: 상태 응답 또는 G/P 에코로 갱신 → 재생/중지 아이콘 반영(항상).
    result.status?.let {
      if (setWidgetRunning(it.running)) widgetChanged = true
    }
    when (result.echo) {
      "G" -> if (setWidgetRunning(true)) widgetChanged = true
      "P" -> if (setWidgetRunning(false)) widgetChanged = true
    }
    // 수량: N 응답(tank)일 때만 갱신(= 수동 새로고침 시에만, 요구사항).
    if (result.tank != null) {
      prefs.edit().putString(KEY_WIDGET_QTY, "${result.tank[0]}/${result.tank[1]}").apply()
      widgetChanged = true
    } else if (result.tankFail && result.kind == "reply") {
      prefs.edit().putString(KEY_WIDGET_QTY, "실패").apply()
      widgetChanged = true
    }
    if (widgetChanged) DomimanWidgetProvider.refresh(appContext)
  }

  /** running 상태를 위젯용으로 저장. 값이 바뀌었으면 true(그때만 위젯 재그림). */
  private fun setWidgetRunning(running: Boolean): Boolean {
    if (prefs.getBoolean(KEY_WIDGET_RUNNING, false) == running) return false
    prefs.edit().putBoolean(KEY_WIDGET_RUNNING, running).apply()
    return true
  }

  // ---------- 위젯 연동 ----------
  /** 위젯 렌더링용 로그인 여부(세션이 구성돼 있었는가 — 프로세스 사망과 무관). */
  fun isSessionConfigured(): Boolean = sessionWasActive()

  fun widgetQtyText(): String = prefs.getString(KEY_WIDGET_QTY, "0/0") ?: "0/0"

  fun widgetRunning(): Boolean = prefs.getBoolean(KEY_WIDGET_RUNNING, false)

  /** 세션이 살아있는지 보장(없으면 저장된 마지막 로그인으로 재접속). ensureSessionAlive와
   * 달리 이미 연결돼 있으면 추가 S 재동기화를 하지 않아 가볍다(위젯 단발 명령용). */
  private suspend fun ensureClientReady(): Boolean {
    if (activeClient != null) return true
    if (!sessionWasActive()) return false
    val last = readStoreState().recent.firstOrNull() ?: return false
    val client = makeClient(last.id, last.targetPc, last.channel)
    val status = attemptLogin(client) ?: return false
    activeClient = client
    lastStatus = status
    beginSession()
    return true
  }

  fun widgetRefresh() = appScope.launch { if (ensureClientReady()) sendTankQuery() }

  fun widgetCollect() = appScope.launch { if (ensureClientReady()) sendCollectNow() }

  fun widgetToggle() =
    appScope.launch {
      if (ensureClientReady()) {
        if (prefs.getBoolean(KEY_WIDGET_RUNNING, false)) sendStop() else sendStart()
      }
    }
}
