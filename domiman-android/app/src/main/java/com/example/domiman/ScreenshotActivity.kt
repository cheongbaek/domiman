package com.example.domiman

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.domiman.theme.DomimanTheme
import com.example.domiman.ui.screenshot.ScreenshotUiState
import com.example.domiman.ui.screenshot.ScreenshotViewModel
import com.example.domiman.ui.screenshot.ScreenshotViewer

/**
 * 홈 위젯 '스크린샷' 칸에서 여는 단독 화면. 매니페스트에서 taskAffinity=""로
 * 비워 앱의 다른 화면과 별개 태스크로 뜬다 — 그래서 뒤로 가기를 누르면
 * MainActivity를 거치지 않고 곧바로 홈 화면으로 돌아간다.
 */
class ScreenshotActivity : ComponentActivity() {
  private val repository by lazy { (application as DomimanApplication).repository }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    // 세션이 죽어 있었다면(백그라운드에서 프로세스가 죽었다 위젯 탭으로 되살아난
    // 경우) ViewModel의 재접속이 끝나기 전에 프로세스가 죽지 않도록 붙잡아 둔다.
    runCatching { DomimanService.start(this) }

    enableEdgeToEdge()
    setContent {
      DomimanTheme(darkTheme = isSystemInDarkTheme()) {
        Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
          val viewModel: ScreenshotViewModel = viewModel { ScreenshotViewModel(repository) }
          val state by viewModel.state.collectAsStateWithLifecycle()
          when (val s = state) {
            is ScreenshotUiState.Loading ->
              Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Column(
                  horizontalAlignment = Alignment.CenterHorizontally,
                  verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                  CircularProgressIndicator()
                  Text("화면을 요청하는 중입니다...")
                }
              }
            is ScreenshotUiState.Success ->
              ScreenshotViewer(name = s.name, bytes = s.bytes, onClose = { finish() })
            is ScreenshotUiState.Failed ->
              Box(modifier = Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
                Column(
                  horizontalAlignment = Alignment.CenterHorizontally,
                  verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                  Text(s.message)
                  TextButton(onClick = { finish() }) { Text("닫기") }
                }
              }
          }
        }
      }
    }
  }
}
