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
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

private const val PREFS_NAME = "domiman_prefs"
private const val KEY_LOGIN_STORE = "login_store_json"
private const val KEY_SESSION_ACTIVE = "session_active"
private const val KEY_ACTIVE_IP = "active_ip" // 현재 세션 자격 — 로그아웃 시 삭제
private const val KEY_ACTIVE_ID = "active_id"
private const val KEY_ACTIVE_PW = "active_pw"
private const val KEY_PC_LIST = "pc_list_json" // 제어 PC 후보 목록
private const val KEY_SELECTED_PC = "selected_pc"
private const val KEY_WIDGET_QTY = "widget_qty" // 위젯 수량 표시(마지막 새로고침 값)
private const val KEY_WIDGET_RUNNING = "widget_running" // 위젯 재생/중지 아이콘 상태

/** 수량 방송(",Z,N,cur,mx")을 위젯에 반영하는 최소 간격.
 * 피제어 PC는 감시 사이클마다(최소 획득 시간 = 보통 5~20초) 보내오지만,
 * 살림망은 몇 시간에 걸쳐 차는 값이라 초 단위로 다시 그릴 이유가 없다.
 * 값은 항상 메모리에 담아 두고 재그림만 이 간격으로 **묶는다**(버리지 않는다).
 * 백그라운드가 하는 일을 '살림망 변화 감지' 수준으로 묶어두려는 값이다. */
private const val TANK_WIDGET_MIN_INTERVAL_MS = 30_000L

private const val LOGIN_TIMEOUT_SEC = 20.0
private const val TARGET_TIMEOUT_SEC = 20.0
private const val TEARDOWN_TIMEOUT_MS = 4_000L

/**
 * domiman_m.py(Chaquopy Python)를 감싸는 **앱 싱글턴** 저장소. domichat 프로토콜
 * (DomimanSession) + '최근 로그인'(LoginStore) + 제어 PC 선택 + 알림 발송을 담당.
 *
 * 세션은 Activity/ViewModel이 아니라 이 싱글턴과 **앱 스코프(appScope)** 에 매여
 * 있어, 화면 재생성이나 백그라운드 복귀에도 살아남는다. 백그라운드에서 프로세스가
 * 죽지 않도록 로그인 시 포그라운드 서비스(DomimanService)를 함께 띄운다.
 *
 * ⚠️ **파이썬은 메인 스레드에서 절대 호출하지 않는다.** 소켓을 들고 있는 세션
 * 스레드와 GIL을 다투면 UI가 통째로 멈춘다(로그아웃 시 '응답 없음'의 원인이었다).
 * 모든 파이썬 접근은 suspend + Dispatchers.IO이며, 되돌릴 수 없는 UI 동작
 * (로그아웃)은 **먼저 화면을 넘기고 정리는 뒤에서** 한다.
 */
class DomimanRepository(context: Context) {
  private val appContext = context.applicationContext
  private val prefs = appContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
  private val json = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
    explicitNulls = false
  }

  // 스트림/재접속이 도는 앱 수명 스코프(뷰모델/액티비티와 독립).
  private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

  private val py: Python get() = Python.getInstance()
  private val module: PyObject get() = py.getModule("domiman_m")

  /** 세션 생성/종료가 겹치지 않게(ON_RESUME 중복 호출·위젯 탭 동시 발생 대비). */
  private val sessionMutex = Mutex()

  /** LoginStore는 prefs JSON이 단일 진실이고, 변경할 때만 파이썬 객체를 새로
   * 만들어 규칙을 적용한다(메모리 사본이 낡을 여지를 없앤다). */
  private val storeMutex = Mutex()

  val notificationPrefs = NotificationPrefs(appContext)
  private val notifications = DomimanNotifications(appContext)

  // 첫 값은 파이썬 없이 Kotlin이 그대로 디코드한다(앱 시작을 막지 않기 위해).
  private val _loginState = MutableStateFlow(readStoreFromPrefs())
  val loginState: StateFlow<LoginStoreJson> = _loginState.asStateFlow()

  private val _pcList = MutableStateFlow(readPcList())
  val pcList: StateFlow<List<String>> = _pcList.asStateFlow()

  private val _selectedPc = MutableStateFlow(prefs.getString(KEY_SELECTED_PC, null))
  val selectedPc: StateFlow<String?> = _selectedPc.asStateFlow()

  private val _connected = MutableStateFlow(false)
  val connected: StateFlow<Boolean> = _connected.asStateFlow()

  /** RecentLogins '수정' → Login 화면 전환 사이 편집 대상 전달용(즉시 소비 후 null). */
  val pendingEdit = MutableStateFlow<SavedLoginJson?>(null)

  /** 로그인 화면 '자동 로그인' 체크 상태를 '…'(최근 로그인) 진입 시 넘겨받아,
   * 최근 로그인 항목 탭으로 로그인해도 그 체크대로 자동로그인이 무장되게 한다. */
  @Volatile var pendingAutoLoginArm: Boolean = false

  /** 현재 로그인된 domichat 세션(로그인 성공 후에만 존재). */
  @Volatile private var session: PyObject? = null

  /** 마지막으로 받은 대상 PC 상태. MainScreen 진입 즉시 UI 시드용. */
  @Volatile var lastStatus: DomimanStatus? = null
    private set

  private val _events = MutableSharedFlow<DomimanEvent>(extraBufferCapacity = 64)
  val events: SharedFlow<DomimanEvent> = _events.asSharedFlow()

  private var pumpJob: Job? = null

  // 수량 방송 코얼레싱 상태(아래 onTankBroadcast). **락이 필요하다** —
  // appScope는 Dispatchers.IO(다중 스레드)라 이벤트 펌프 코루틴과 30초 뒤
  // 깨어나는 지연 코루틴이 동시에 여기를 만질 수 있다.
  private val tankLock = Any()
  private var pendingTankText: String? = null // 아직 위젯에 못 그린 최신 값
  private var lastTankFlushMs = 0L
  private var tankFlushJob: Job? = null

  // ---------- 영속 상태(파이썬 없이 읽고 쓰는 값들) ----------
  private fun readStoreFromPrefs(): LoginStoreJson =
    runCatching { json.decodeFromString<LoginStoreJson>(prefs.getString(KEY_LOGIN_STORE, "") ?: "") }
      .getOrElse { LoginStoreJson() }

  private fun readPcList(): List<String> =
    runCatching { json.decodeFromString<List<String>>(prefs.getString(KEY_PC_LIST, "") ?: "") }
      .getOrNull()
      ?: DEFAULT_PC_LIST

  private fun setSessionActive(active: Boolean) =
    prefs.edit().putBoolean(KEY_SESSION_ACTIVE, active).apply()

  private fun sessionWasActive(): Boolean = prefs.getBoolean(KEY_SESSION_ACTIVE, false)

  /** 현재 세션 자격 — 프로세스가 죽었다 살아났을 때 재접속용. **로그아웃하면
   * 지운다**(자동 로그인을 체크하지 않았다면 로그아웃 후 아무것도 남지 않는다). */
  private fun saveActiveCredential(ip: String, id: String, pw: String) =
    prefs.edit().putString(KEY_ACTIVE_IP, ip).putString(KEY_ACTIVE_ID, id)
      .putString(KEY_ACTIVE_PW, pw).apply()

  private fun clearActiveCredential() =
    prefs.edit().remove(KEY_ACTIVE_IP).remove(KEY_ACTIVE_ID).remove(KEY_ACTIVE_PW).apply()

  private fun activeCredential(): SavedLoginJson? {
    val ip = prefs.getString(KEY_ACTIVE_IP, null) ?: return null
    val id = prefs.getString(KEY_ACTIVE_ID, null) ?: return null
    val pw = prefs.getString(KEY_ACTIVE_PW, null) ?: return null
    return SavedLoginJson(ip, id, pw)
  }

  /** 파이썬 split_server와 같은 규칙으로 지문 저장 키를 만든다. */
  private fun serverKey(ip: String): String {
    val s = ip.trim()
    val idx = s.lastIndexOf(':')
    if (idx > 0 && s.substring(idx + 1).toIntOrNull() != null) return s
    return "$s:$CHAT_PORT"
  }

  // ---------- LoginStore 변경(파이썬 규칙 사용, 항상 IO에서) ----------
  private suspend fun mutateStore(block: (PyObject) -> Unit) =
    withContext(Dispatchers.IO) {
      storeMutex.withLock {
        runCatching {
          val obj =
            module.get("LoginStore")!!.callAttr("from_json", prefs.getString(KEY_LOGIN_STORE, "") ?: "")
          block(obj)
          val out = obj.callAttr("to_json").toString()
          prefs.edit().putString(KEY_LOGIN_STORE, out).apply()
          _loginState.value = json.decodeFromString(out)
        }
        Unit
      }
    }

  private fun toPySavedLogin(entry: SavedLoginJson): PyObject =
    module.callAttr("SavedLogin", entry.ip, entry.id, entry.pw)

  fun updateEntry(old: SavedLoginJson, updated: SavedLoginJson) {
    appScope.launch { mutateStore { it.callAttr("update", toPySavedLogin(old), toPySavedLogin(updated)) } }
  }

  fun deleteEntry(entry: SavedLoginJson) {
    appScope.launch { mutateStore { it.callAttr("remove", toPySavedLogin(entry)) } }
  }

  // ---------- 제어 PC 목록 ----------
  private fun persistPcList(list: List<String>) {
    _pcList.value = list
    prefs.edit().putString(KEY_PC_LIST, json.encodeToString(list)).apply()
  }

  /** 리스트에 PC 이름 추가(중복·형식 위반은 무시). 성공하면 true. */
  fun addPc(name: String): Boolean {
    val v = name.trim()
    if (v.isEmpty() || !PC_NAME_RE.matches(v)) return false
    if (_pcList.value.any { it.equals(v, ignoreCase = true) }) return false
    persistPcList(_pcList.value + v)
    return true
  }

  /** 리스트에서 제거. 지금 제어 중인 PC였다면 선택도 해제한다. */
  fun removePc(name: String) {
    persistPcList(_pcList.value.filterNot { it == name })
    if (_selectedPc.value == name) clearSelectedPc()
  }

  private fun clearSelectedPc() {
    _selectedPc.value = null
    lastStatus = null
    prefs.edit().remove(KEY_SELECTED_PC).apply()
    appScope.launch { withContext(Dispatchers.IO) { runCatching { session?.callAttr("clear_target") } } }
  }

  /**
   * 메인 화면 최상단에서 제어할 PC를 고른다 — 그 PC의 방(domi_fishing_{PC})에
   * 입장·구독하고 S 질의로 상태를 받아온다. 실패해도(꺼져 있는 PC 등) 선택은
   * 유지한다 — 다시 켜지면 ON_RESUME의 resync가 다시 붙는다.
   */
  suspend fun selectPc(pc: String): TargetResult =
    withContext(Dispatchers.IO) {
      _selectedPc.value = pc
      prefs.edit().putString(KEY_SELECTED_PC, pc).apply()
      lastStatus = null
      val s = session ?: return@withContext TargetResult(ok = false, pc = pc, reason = "no_session")
      val result =
        runCatching {
            s.callAttr("select_target", pc)
            json.decodeFromString<TargetResult>(s.callAttr("wait_target_json", TARGET_TIMEOUT_SEC).toString())
          }
          .getOrElse { TargetResult(ok = false, pc = pc, reason = "error") }
      result.status?.let { lastStatus = it }
      result
    }

  // ---------- 로그인 / 로그아웃 ----------
  /** 로그인 화면 '로그인' 버튼 및 최근 로그인 짧게 탭 공용.
   * autoLoginChecked가 **true일 때만** 최근 로그인 목록에 남는다(사용자 확정 규칙). */
  suspend fun login(ip: String, id: String, pw: String, autoLoginChecked: Boolean): LoginResult =
    sessionMutex.withLock { loginInternal(ip, id, pw, autoLoginChecked, record = true) }

  private suspend fun loginInternal(
    ip: String,
    id: String,
    pw: String,
    autoLoginChecked: Boolean,
    record: Boolean,
  ): LoginResult =
    withContext(Dispatchers.IO) {
      teardownSessionLocked()
      val pinned = _loginState.value.fingerprints[serverKey(ip)]
      val s =
        runCatching { module.callAttr("DomimanSession") }
          .getOrElse { return@withContext LoginResult(ok = false, msg = "내부 오류: ${it.message}") }
      val result =
        runCatching {
            s.callAttr("start", ip, id, pw, pinned)
            json.decodeFromString<LoginResult>(s.callAttr("wait_login_json", LOGIN_TIMEOUT_SEC).toString())
          }
          .getOrElse { LoginResult(ok = false, msg = "접속에 실패했습니다. (${it.message})") }

      if (!result.ok) {
        runCatching { s.callAttr("stop") }
        return@withContext result
      }

      session = s
      _connected.value = true
      startPump(s)
      saveActiveCredential(ip, id, pw)
      setSessionActive(true)
      if (record) {
        mutateStore { store ->
          store.put("auto_login_enabled", autoLoginChecked)
          // 자동 로그인을 체크했을 때만 목록에 남긴다.
          if (autoLoginChecked) store.callAttr("add_or_bump", module.callAttr("SavedLogin", ip, id, pw))
        }
      }
      result.fp?.let { fp -> mutateStore { it.callAttr("pin", result.server ?: serverKey(ip), fp) } }
      runCatching { DomimanService.start(appContext) }

      // 이전에 고르던 제어 PC가 있으면 그대로 다시 붙는다.
      _selectedPc.value?.let { pc ->
        runCatching {
          s.callAttr("select_target", pc)
          val t = json.decodeFromString<TargetResult>(s.callAttr("wait_target_json", TARGET_TIMEOUT_SEC).toString())
          t.status?.let { lastStatus = it }
        }
      }
      DomimanWidgetProvider.refresh(appContext)
      result
    }

  /** 앱 시작 시 1회. 무장 안 됐거나 이력 없으면 시도하지 않는다(null). */
  suspend fun tryAutoLoginOnLaunch(): LoginResult? {
    val state = _loginState.value
    if (!state.autoLoginEnabled) return null
    val last = state.recent.firstOrNull() ?: return null
    return login(last.ip, last.id, last.pw, autoLoginChecked = true)
  }

  /**
   * 로그아웃 — **메인 스레드에서 불려도 즉시 반환한다.** 화면은 곧바로 로그인으로
   * 넘어가고, 소켓·서비스·파이썬 정리는 앱 스코프에서 뒤따라 끝낸다(최대
   * TEARDOWN_TIMEOUT_MS). 최근 로그인 목록은 유지하지만 **현재 세션 자격은
   * 지운다** — 자동 로그인을 체크하지 않았다면 로그아웃 후 아무것도 남지 않는다.
   */
  fun logout() {
    // 1) 메인 스레드에서는 값싼 prefs/StateFlow만 건드린다(파이썬 호출 금지).
    setSessionActive(false)
    clearActiveCredential()
    _connected.value = false
    lastStatus = null
    val dead = session
    session = null
    pumpJob?.cancel()
    pumpJob = null
    cancelTankFlush() // 세션이 없으니 늦게 깨어나 그릴 이유도 없다
    DomimanWidgetProvider.refresh(appContext) // 수량 → '로그인' 표시로 전환

    // 2) 실제 정리는 뒤에서. 소켓이 물려 있어도 UI는 이미 넘어가 있다.
    appScope.launch {
      runCatching { DomimanService.stop(appContext) }
      withTimeoutOrNull(TEARDOWN_TIMEOUT_MS) {
        withContext(Dispatchers.IO) { runCatching { dead?.callAttr("stop") } }
      }
      mutateStore { it.put("auto_login_enabled", false) }
    }
  }

  /** 세션만 조용히 끊는다(재로그인 직전 등). 반드시 IO에서 호출. */
  private suspend fun teardownSessionLocked() {
    val dead = session ?: return
    session = null
    _connected.value = false
    pumpJob?.cancel()
    pumpJob = null
    cancelTankFlush()
    withTimeoutOrNull(TEARDOWN_TIMEOUT_MS) {
      withContext(Dispatchers.IO) { runCatching { dead.callAttr("stop") } }
    }
  }

  private fun cancelTankFlush() {
    synchronized(tankLock) {
      tankFlushJob?.cancel()
      tankFlushJob = null
      pendingTankText = null
    }
  }

  // ---------- 세션 생존 ----------
  /**
   * 앱이 포그라운드로 복귀했거나(ON_RESUME) 서비스가 sticky 재시작됐을 때 호출.
   *  - 세션이 살아있으면: resync(방 재입장 또는 S 재질의)로 상태를 맞춘다.
   *  - 세션이 없는데 직전까지 로그인 상태였으면: 저장된 세션 자격으로 재접속.
   * 반환: 세션이 살아있게 되면 true(재접속도 실패하면 false → 로그인 화면으로).
   */
  suspend fun ensureSessionAlive(): Boolean =
    sessionMutex.withLock {
      val s = session
      if (s != null) {
        withContext(Dispatchers.IO) { runCatching { s.callAttr("resync") } }
        return@withLock true
      }
      if (!sessionWasActive()) return@withLock false // 정상 로그아웃 상태 — 되살리지 않음
      val cred = activeCredential() ?: return@withLock false
      loginInternal(cred.ip, cred.id, cred.pw, autoLoginChecked = false, record = false).ok
    }

  /** 서비스 sticky 재시작 시(프로세스 재생성 가능) 세션 복구를 시도. */
  fun reviveIfNeeded() {
    if (session != null || !sessionWasActive()) return
    appScope.launch { ensureSessionAlive() }
  }

  // ---------- 제어 명령(전부 suspend + Dispatchers.IO) ----------
  private suspend fun cmd(name: String, vararg args: Any?) =
    withContext(Dispatchers.IO) { runCatching { session?.callAttr(name, *args) } }

  suspend fun sendStart() = cmd("cmd_start")

  suspend fun sendStop() = cmd("cmd_stop")

  suspend fun sendSchedExitSet(minutes: Double) = cmd("cmd_sched_exit_set", minutes)

  suspend fun sendCollectNow() = cmd("cmd_collect_now")

  suspend fun sendSetResolution(mode: String) = cmd("cmd_set_resolution", mode)

  suspend fun sendSetTimer(minutes: Double) = cmd("cmd_set_timer", minutes)

  suspend fun sendSetFlags(logSave: Boolean, rod: Boolean?, bait: Boolean?) =
    cmd("cmd_set_flags", logSave, rod, bait)

  suspend fun sendTankQuery() = cmd("cmd_tank_query")

  /** '스크린샷'(I) 요청. 이미 사진을 기다리는 중이면 파이썬이 보내지 않고
   * false를 돌려준다(cmd_screenshot의 중복 요청 방지 그대로). */
  suspend fun sendScreenshot(): Boolean =
    withContext(Dispatchers.IO) {
      runCatching { session?.callAttr("cmd_screenshot")?.toBoolean() ?: false }.getOrElse { false }
    }

  // ---------- 이벤트 펌프 ----------
  /** 파이썬 세션 큐를 IO 코루틴에서 계속 비워 Kotlin 이벤트로 흘린다.
   * 파이썬으로 콜백(람다)을 넘기지 않는 이유: 로그아웃 때 죽은 스코프를 붙잡아
   * 블로킹되는 사고를 구조적으로 없애기 위해서다. */
  private fun startPump(target: PyObject) {
    pumpJob?.cancel()
    pumpJob =
      appScope.launch {
        while (isActive) {
          val raw =
            runCatching { target.callAttr("poll_event_json", 0.5).toString() }
              .getOrElse {
                delay(500)
                continue
              }
          if (raw == "null") continue
          val event = runCatching { json.decodeFromString<DomimanEvent>(raw) }.getOrNull() ?: continue
          runCatching { handleEvent(event) }
        }
      }
  }

  private suspend fun handleEvent(event: DomimanEvent) {
    // 수량 방송은 여기서 끝낸다(260828a). 아래의 알림·상태·이벤트 흐름에
    // 태우지 않는 이유: 사이클마다 오는 신호라 알림창과 8줄짜리 화면 로그가
    // 그것만으로 가득 차고, UI 재구성도 헛돌게 된다. 이 신호가 바꾸는 것은
    // **위젯 표시 하나뿐**이다.
    if (event.ev == "tank") {
      onTankBroadcast(event)
      return
    }
    when (event.ev) {
      "login_ok", "reconnected" -> _connected.value = true
      "disconnected", "login_fail", "cert_changed" -> _connected.value = false
      "cert_pinned" ->
        if (event.server != null && event.fp != null) {
          mutateStore { it.callAttr("pin", event.server, event.fp) }
        }
      "target_gone" -> lastStatus = null
    }

    event.status?.let { lastStatus = it }
    _events.tryEmit(event) // 화면이 떠 있으면 UI가 반영

    // 보고 이벤트는 알림 설정에 맞춰 (앱이 백그라운드여도) 알림을 띄운다.
    if (event.ev == "report" && event.reportText != null) {
      if (notificationPrefs.current().isEnabled(event.reportNotifyKey)) {
        notifications.postEvent(event.reportText)
      }
    }

    // ---- 위젯 상태 갱신 ----
    var widgetChanged = false
    // running: 상태 응답 또는 G/P 에코로 갱신 → 재생/중지 아이콘 반영(항상).
    event.status?.let { if (setWidgetRunning(it.running)) widgetChanged = true }
    when (event.echo) {
      "G" -> if (setWidgetRunning(true)) widgetChanged = true
      "P" -> if (setWidgetRunning(false)) widgetChanged = true
    }
    // 수량: N **응답**(사용자가 새로고침을 누른 결과)은 기다리는 사람이 있으니
    // 지체 없이 그린다. 사이클마다 오는 방송은 위 onTankBroadcast가 30초로
    // 묶어서 처리한다(둘의 차이는 '사람이 기다리는가'다).
    if (event.tank != null && event.tank.size >= 2) {
      setWidgetQty("${event.tank[0]}/${event.tank[1]}")
      widgetChanged = true
    } else if (event.tankFail && event.ev == "reply") {
      setWidgetQty("실패")
      widgetChanged = true
    }
    if (widgetChanged) DomimanWidgetProvider.refresh(appContext)
  }

  // ---------- 수량 상시 방송(위젯 전용) ----------
  /** 피제어 PC가 감시 사이클마다 보내오는 살림망 수량을 위젯에 반영한다.
   *
   * **백그라운드가 하는 일을 최소로 묶는 것이 이 함수의 요지다:**
   *  - 값이 이미 같으면 즉시 반환(prefs·RemoteViews 둘 다 건드리지 않는다).
   *  - 홈에 위젯이 하나도 없으면 아무 것도 하지 않는다(그릴 대상이 없다).
   *  - 값이 바뀌었어도 재그림은 30초에 한 번(TANK_WIDGET_MIN_INTERVAL_MS).
   *    묶는 동안 들어온 값은 pendingTankText에 덮어써 두고, 창이 열리면
   *    **마지막 값 하나만** 그린다 — 지연될 뿐 버려지지 않는다. */
  private fun onTankBroadcast(event: DomimanEvent) {
    val text =
      if (event.tank != null && event.tank.size >= 2) "${event.tank[0]}/${event.tank[1]}"
      else if (event.tankFail) "실패"
      else return
    var flushNow = false
    synchronized(tankLock) {
      if (text == pendingTankText) return // 이미 대기 중인 값과 같다
      if (pendingTankText == null && text == widgetQtyText()) return // 이미 그려진 값
      // 홈에 위젯이 없으면 그릴 대상이 없다 — prefs도 건드리지 않고 끝낸다.
      if (!DomimanWidgetProvider.hasInstances(appContext)) return
      pendingTankText = text
      val waited = System.currentTimeMillis() - lastTankFlushMs
      if (waited >= TANK_WIDGET_MIN_INTERVAL_MS) {
        flushNow = true
      } else if (tankFlushJob?.isActive != true) {
        tankFlushJob = appScope.launch { delay(TANK_WIDGET_MIN_INTERVAL_MS - waited); flushTank() }
      }
    }
    if (flushNow) flushTank() // 그리는 일은 락 밖에서(바인더 호출을 물고 있지 않게)
  }

  private fun flushTank() {
    var taken: String? = null
    synchronized(tankLock) {
      taken = pendingTankText ?: return
      pendingTankText = null
      lastTankFlushMs = System.currentTimeMillis()
    }
    val text = taken ?: return
    setWidgetQty(text)
    DomimanWidgetProvider.refresh(appContext)
  }

  /** 위젯 수량 표시 저장. 같은 값이면 쓰지 않는다(디스크 I/O 절약). */
  private fun setWidgetQty(text: String) {
    if (widgetQtyText() == text) return
    prefs.edit().putString(KEY_WIDGET_QTY, text).apply()
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

  /** 위젯 렌더링용 — 제어할 PC가 골라져 있는가(고르기 전엔 보낼 곳이 없다). */
  fun hasSelectedPc(): Boolean = _selectedPc.value != null

  fun widgetQtyText(): String = prefs.getString(KEY_WIDGET_QTY, "0/0") ?: "0/0"

  fun widgetRunning(): Boolean = prefs.getBoolean(KEY_WIDGET_RUNNING, false)

  /** 위젯·스크린샷 화면 등 단발 명령 전에 세션 + 제어 PC가 준비돼 있는지 보장.
   * 제어 PC를 아직 고르지 않았으면 보낼 곳이 없으므로 false. */
  suspend fun ensureClientReady(): Boolean {
    if (_selectedPc.value == null) return false
    if (session != null) return true
    return ensureSessionAlive()
  }

  fun widgetRefresh() = appScope.launch { if (ensureClientReady()) sendTankQuery() }

  fun widgetToggle() =
    appScope.launch {
      if (ensureClientReady()) {
        if (prefs.getBoolean(KEY_WIDGET_RUNNING, false)) sendStop() else sendStart()
      }
    }

  companion object {
    const val CHAT_PORT = 47821

    /** 제어 PC 후보 기본 목록(domiman_m.DEFAULT_PC_LIST와 같은 값). */
    val DEFAULT_PC_LIST = listOf("seoul", "chungju", "galaxy")

    /** domichat 계정 ID 규칙(domiserver.ID_RE) — 제어 PC 이름도 그 PC의 로그인 ID다. */
    val PC_NAME_RE = Regex("[A-Za-z0-9_-]{1,20}")
  }
}
