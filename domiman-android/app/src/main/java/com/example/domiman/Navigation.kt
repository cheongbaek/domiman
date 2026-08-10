package com.example.domiman

import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.ui.NavDisplay
import com.example.domiman.data.DomimanRepository
import com.example.domiman.ui.login.LoginScreen
import com.example.domiman.ui.main.MainScreen
import com.example.domiman.ui.notif.NotificationSettingsScreen
import com.example.domiman.ui.recent.RecentLoginsScreen

@Composable
fun MainNavigation(repository: DomimanRepository, isDark: Boolean, onToggleDark: () -> Unit) {
  // 이전에 로그인돼 있었으면(로그아웃 안 함) 로그인창을 거치지 않고 **바로 메인**에서
  // 시작한다. 실제 세션 재접속은 MainScreen의 ON_RESUME(ensureSessionAlive)이
  // 백그라운드로 처리하고, 재접속이 끝내 실패하면 그때 로그인 화면으로 돌려보낸다.
  // (과거엔 tryAutoLoginOnLaunch가 최대 15초 네트워크 로그인을 '기다린 뒤'에야
  //  메인으로 갔기에, 느리거나 간헐 실패하면 로그인창이 뜨는 문제가 있었다.)
  val startKey: NavKey = remember { if (repository.isSessionConfigured()) Main else Login }
  val backStack = rememberNavBackStack(startKey)

  NavDisplay(
    backStack = backStack,
    onBack = { backStack.removeLastOrNull() },
    entryProvider =
      entryProvider {
        entry<Login> {
          LoginScreen(
            repository = repository,
            onLoggedIn = {
              backStack.clear()
              backStack.add(Main)
            },
            onOpenRecentLogins = { backStack.add(RecentLogins) },
            onEditDone = { backStack.add(RecentLogins) },
            modifier = Modifier.safeDrawingPadding().padding(16.dp),
          )
        }
        entry<RecentLogins> {
          RecentLoginsScreen(
            repository = repository,
            onBack = { backStack.removeLastOrNull() },
            onLoggedIn = {
              backStack.clear()
              backStack.add(Main)
            },
            // '수정' 선택: repository.pendingEdit에 대상만 심어두고 RecentLogins를
            // 스택에서 빼 밑에 깔린 Login 화면을 그대로 드러낸다(ViewModel 재사용).
            onEditRequested = { backStack.removeLastOrNull() },
            modifier = Modifier.safeDrawingPadding().padding(16.dp),
          )
        }
        entry<Main> {
          MainScreen(
            repository = repository,
            isDark = isDark,
            onToggleDark = onToggleDark,
            onLoggedOut = {
              backStack.clear()
              backStack.add(Login)
            },
            onOpenNotificationSettings = { backStack.add(NotificationSettings) },
            modifier = Modifier.safeDrawingPadding().padding(16.dp),
          )
        }
        entry<NotificationSettings> {
          NotificationSettingsScreen(
            repository = repository,
            onBack = { backStack.removeLastOrNull() },
            modifier = Modifier.safeDrawingPadding().padding(16.dp),
          )
        }
      },
  )
}
