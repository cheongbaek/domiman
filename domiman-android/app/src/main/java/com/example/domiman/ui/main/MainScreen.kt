package com.example.domiman.ui.main

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.domiman.data.DomimanRepository

@Composable
fun MainScreen(
  repository: DomimanRepository,
  isDark: Boolean,
  onToggleDark: () -> Unit,
  onLoggedOut: () -> Unit,
  onOpenNotificationSettings: () -> Unit,
  modifier: Modifier = Modifier,
  viewModel: MainScreenViewModel = viewModel { MainScreenViewModel(repository) },
) {
  val state by viewModel.uiState.collectAsStateWithLifecycle()
  var backPressCount by remember { mutableStateOf(0) }
  var showResolutionDialog by remember { mutableStateOf(false) }
  var showLogoutHint by remember { mutableStateOf(false) }
  var showSchedDialog by remember { mutableStateOf(false) }

  // 포그라운드 복귀 시 스트림 재접속 + 상태 재동기화(백그라운드에서 끊겼을 수
  // 있음). 세션을 되살리지 못하면 로그인 화면으로.
  LifecycleEventEffect(Lifecycle.Event.ON_RESUME) {
    viewModel.onResume(onSessionLost = onLoggedOut)
  }

  // Sheet2 G20: 뒤로가기 1번=안내 토스트 성격의 메시지, 2번=로그아웃.
  // (BackHandler는 Activity 레벨 연동이 필요해 화면 내 버튼으로 동일 동작을 노출)
  Column(
    modifier = modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
    verticalArrangement = Arrangement.spacedBy(10.dp),
  ) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      Text("해상도", modifier = Modifier.weight(1f))
      Text(state.resolutionLabel, style = MaterialTheme.typography.bodyMedium)
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(onClick = { showResolutionDialog = true }, enabled = !state.isPending) { Text("직접 설정") }
      OutlinedButton(onClick = viewModel::onResolutionAuto, enabled = !state.isPending) { Text("자동 감지") }
    }

    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      Text("타이머")
      OutlinedTextField(
        // timerText는 ViewModel이 소유(입력 즉시 반영·발신은 디바운스). isPending으로
        // 막지 않는다 — 입력 도중 필드가 잠기면 타이핑이 끊기기 때문.
        value = state.timerText,
        onValueChange = { new ->
          if (new.matches(Regex("""\d*\.?\d*"""))) viewModel.onTimerInput(new)
        },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        singleLine = true,
        modifier = Modifier.weight(1f),
      )
      Text("(분)")
    }
    Text("0을 입력하면 살림망 감시 모드로 작동합니다.", style = MaterialTheme.typography.bodySmall)

    Row(verticalAlignment = Alignment.CenterVertically) {
      Checkbox(
        checked = state.rod,
        onCheckedChange = { viewModel.onFlagsToggled(state.logSave, it, state.bait) },
        enabled = !state.isPending,
      )
      Text("낚싯대 자동교체")
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
      Checkbox(
        checked = state.bait,
        onCheckedChange = { viewModel.onFlagsToggled(state.logSave, state.rod, it) },
        enabled = !state.isPending,
      )
      Text("미끼 자동교체")
    }
    Text(
      "낚싯대, 미끼 자동교체는 살림망 감시 모드에서만 사용 가능합니다.",
      style = MaterialTheme.typography.bodySmall,
    )

    Button(
      onClick = viewModel::onStartStop,
      enabled = !state.isPending,
      modifier = Modifier.fillMaxWidth(),
    ) {
      Text(if (state.running) "중지" else "시작")
    }
    Text(state.statusMessage, style = MaterialTheme.typography.bodyMedium)

    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(
        onClick = { showSchedDialog = true },
        enabled = !state.isPending,
        modifier = Modifier.weight(1f),
      ) { Text("예약 종료") }
      OutlinedButton(
        onClick = viewModel::onCollectNow,
        enabled = !state.isPending,
        modifier = Modifier.weight(1f),
      ) { Text("즉시 회수") }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      // Sheet2 G13: Android는 GitHub 자동 업데이트가 불가해 이 자리는 항상
      // '실시간 수량확인'으로 확정(풀투리프레시 제스처는 구현하지 않음).
      OutlinedButton(onClick = viewModel::onTankQuery, modifier = Modifier.weight(1f)) { Text("실시간 수량확인") }
      OutlinedButton(onClick = onToggleDark, modifier = Modifier.weight(1f)) {
        Text(if (isDark) "화이트모드" else "다크모드")
      }
    }

    Row(verticalAlignment = Alignment.CenterVertically) {
      Text("로그", modifier = Modifier.weight(1f))
      TextButton(onClick = viewModel::onLogClear) { Text("x") }
    }
    Column(
      modifier =
        Modifier.fillMaxWidth()
          .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(8.dp))
          .padding(8.dp),
    ) {
      if (state.logLines.isEmpty()) {
        Text("(로그 없음)", style = MaterialTheme.typography.bodySmall)
      }
      state.logLines.forEach { line -> Text(line, style = MaterialTheme.typography.bodySmall) }
    }

    // 하단: 로그아웃 + 알림 설정(앱UI설명 — 로그아웃 옆 같은 모양 버튼).
    // 뒤로가기 2번=로그아웃 규칙을 임시로 화면 내 버튼으로도 노출
    // (시스템 뒤로가기 인터셉트는 Activity의 OnBackPressedCallback 연동 필요 — 향후 작업).
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      TextButton(
        onClick = {
          if (backPressCount == 0) {
            backPressCount = 1
            showLogoutHint = true
          } else {
            viewModel.onLogout()
            onLoggedOut()
          }
        },
      ) {
        Text("로그아웃")
      }
      TextButton(onClick = onOpenNotificationSettings) { Text("알림 설정") }
    }
  }

  if (showLogoutHint) {
    AlertDialog(
      onDismissRequest = {
        showLogoutHint = false
        backPressCount = 0
      },
      confirmButton = {
        TextButton(
          onClick = {
            showLogoutHint = false
            viewModel.onLogout()
            onLoggedOut()
          },
        ) { Text("로그아웃") }
      },
      dismissButton = { TextButton(onClick = { showLogoutHint = false; backPressCount = 0 }) { Text("취소") } },
      text = { Text("한 번 더 누르면 로그아웃됩니다.") },
    )
  }

  if (showResolutionDialog) {
    AlertDialog(
      onDismissRequest = { showResolutionDialog = false },
      confirmButton = {},
      text = {
        Column {
          TextButton(
            onClick = {
              viewModel.onResolutionManual("1080")
              showResolutionDialog = false
            },
          ) { Text("1920 x 1080") }
          TextButton(
            onClick = {
              viewModel.onResolutionManual("1440")
              showResolutionDialog = false
            },
          ) { Text("2560 x 1440") }
        }
      },
    )
  }

  if (showSchedDialog) {
    var minutesField by remember { mutableStateOf("") }
    AlertDialog(
      onDismissRequest = { showSchedDialog = false },
      title = { Text("예약 종료") },
      text = {
        Column {
          Text("몇 분 뒤에 종료할까요? (0 = 예약 해제)")
          OutlinedTextField(
            value = minutesField,
            onValueChange = { new -> if (new.matches(Regex("""\d*"""))) minutesField = new },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            singleLine = true,
          )
        }
      },
      confirmButton = {
        TextButton(
          onClick = {
            minutesField.toDoubleOrNull()?.let(viewModel::onSchedExitSet)
            showSchedDialog = false
          },
        ) { Text("확인") }
      },
      dismissButton = { TextButton(onClick = { showSchedDialog = false }) { Text("취소") } },
    )
  }
}
