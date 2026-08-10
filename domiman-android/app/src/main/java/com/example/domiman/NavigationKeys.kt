package com.example.domiman

import androidx.navigation3.runtime.NavKey
import kotlinx.serialization.Serializable

/** 앱UI설명.xlsx Sheet1 A1:E18 — ID/피제어PC/채널 입력 + 자동로그인. */
@Serializable data object Login : NavKey

/** 앱UI설명.xlsx Sheet1 A21:E39 — 최근 로그인 목록. */
@Serializable data object RecentLogins : NavKey

/** 앱UI설명.xlsx Sheet2 A1:E19 — 메인 제어 화면. */
@Serializable data object Main : NavKey

/** Sheet2 '알림 설정' 버튼 → 알림 항목 설정 화면. */
@Serializable data object NotificationSettings : NavKey
