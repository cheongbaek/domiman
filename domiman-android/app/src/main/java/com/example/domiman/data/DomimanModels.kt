package com.example.domiman.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** domiman.py ntfy 프로토콜의 상태 응답(S/V/T/C) — domiman_m.DomimanClient.parse_status
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

/** domiman_m.dispatch_json()의 반환 스키마. kind에 따라 관련 필드만 채워진다. */
@Serializable
data class DispatchResult(
  val kind: String? = null,
  val status: DomimanStatus? = null,
  val tank: List<Int>? = null,
  @SerialName("tank_fail") val tankFail: Boolean = false,
  // 상태 필드 없는 명령 에코("G"/"P"/"W"/"Q"/"Y"). G/P로 시작/중지 상태를 갱신한다.
  val echo: String? = null,
  @SerialName("sched_minutes") val schedMinutes: String? = null,
  @SerialName("report_text") val reportText: String? = null,
  @SerialName("report_status_key") val reportStatusKey: String? = null,
  @SerialName("report_notify_key") val reportNotifyKey: String? = null,
)

/** '최근 로그인' 한 행 (앱UI설명.xlsx Sheet1 A24:C25). */
@Serializable
data class SavedLoginJson(
  val id: String,
  @SerialName("target_pc") val targetPc: String,
  val channel: String,
)

/** domiman_m.LoginStore.to_json()의 반환 스키마(Sheet1 전체 상태). */
@Serializable
data class LoginStoreJson(
  val recent: List<SavedLoginJson> = emptyList(),
  @SerialName("auto_login_enabled") val autoLoginEnabled: Boolean = false,
)
