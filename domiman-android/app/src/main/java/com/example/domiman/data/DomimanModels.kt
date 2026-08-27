package com.example.domiman.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** domiman.py 프로토콜의 상태 응답(S/V/T/C) — domiman_m.DomimanSession.parse_status
 * 와 같은 필드. '실행중' 이름은 파이썬 dict 키 그대로(running). */
@Serializable
data class DomimanStatus(
  val timer: String,
  val resolution: String,
  @SerialName("res_auto") val resAuto: Boolean,
  val logsave: Boolean,
  val running: Boolean,
  val rod: Boolean? = null,
  val bait: Boolean? = null,
)

/**
 * domiman_m.DomimanSession.poll_event_json()이 올려보내는 이벤트 한 건.
 * `ev`가 종류이고, 종류에 따라 관련 필드만 채워진다.
 *
 *  - `login_ok` / `reconnected` : 로그인 성공 / 끊겼다 다시 붙음
 *  - `login_fail` / `cert_changed` : 로그인 실패(코드는 code) / 인증서 지문 변경
 *  - `cert_pinned` : 첫 접속에 지문을 고정(TOFU) — server/fp 저장
 *  - `disconnected` : 연결 끊김(자동 재연결 진행 중)
 *  - `target_joined` / `target_denied` / `target_gone` : 제어 PC 방 입장·거절·이탈
 *  - `reply` : 내 명령/질의에 대한 응답(status·tank·echo 중 하나)
 *  - `report` : 대상 PC의 상황 보고(,Z,F,*) — 알림 발송 판단에 report_notify_key
 */
@Serializable
data class DomimanEvent(
  val ev: String? = null,
  val msg: String? = null,
  val code: String? = null,
  val id: String? = null,
  val pc: String? = null,
  val reason: String? = null,
  val server: String? = null,
  val fp: String? = null,
  val status: DomimanStatus? = null,
  val tank: List<Int>? = null,
  @SerialName("tank_fail") val tankFail: Boolean = false,
  // 상태 필드 없는 명령 에코("G"/"P"/"W"/"Q"/"Y"/"I"). G/P로 시작/중지 상태를 갱신한다.
  val echo: String? = null,
  @SerialName("sched_minutes") val schedMinutes: String? = null,
  @SerialName("report_text") val reportText: String? = null,
  @SerialName("report_status_key") val reportStatusKey: String? = null,
  @SerialName("report_notify_key") val reportNotifyKey: String? = null,
  // echo=="I"인데 캡처 자체가 실패("`,Z,I,fail`")했을 때만 true.
  @SerialName("shot_fail") val shotFail: Boolean = false,
  // ev=="screenshot"(파일 전송 결과) 전용 필드.
  val ok: Boolean? = null,
  val name: String? = null,
  @SerialName("png_b64") val pngB64: String? = null,
)

/** domiman_m.DomimanSession.wait_login_json()의 반환 스키마. */
@Serializable
data class LoginResult(
  val ok: Boolean = false,
  val id: String? = null,
  val msg: String? = null,
  val code: String? = null,
  val fp: String? = null,
  val server: String? = null,
)

/** domiman_m.DomimanSession.wait_target_json()의 반환 스키마.
 * reason: bad_pw_room / blocked / room_missing(그 PC가 접속한 적 없음) /
 *         no_response(방은 있으나 PC가 꺼져 있음) / timeout / owner_gone. */
@Serializable
data class TargetResult(
  val ok: Boolean = false,
  val pc: String? = null,
  val reason: String? = null,
  val status: DomimanStatus? = null,
)

/** '최근 로그인' 한 행. domichat 이식으로 (ID·피제어PC·채널) →
 * **(서버 IP, domichat ID, PW)** 로 바뀌었다 — 제어할 PC는 로그인이 아니라
 * 메인 화면 상단에서 고르기 때문. */
@Serializable
data class SavedLoginJson(
  val ip: String,
  val id: String,
  val pw: String,
)

/** domiman_m.LoginStore.to_json()의 반환 스키마.
 * fingerprints: "IP:포트" → 서버 인증서 SHA-256 지문(TOFU 고정값). */
@Serializable
data class LoginStoreJson(
  val recent: List<SavedLoginJson> = emptyList(),
  @SerialName("auto_login_enabled") val autoLoginEnabled: Boolean = false,
  val fingerprints: Map<String, String> = emptyMap(),
)
