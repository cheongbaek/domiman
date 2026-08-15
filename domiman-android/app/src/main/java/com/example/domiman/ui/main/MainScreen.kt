package com.example.domiman.ui.main

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
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
import androidx.compose.material3.HorizontalDivider
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
import androidx.compose.ui.text.font.FontWeight
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
  val selectedPc by viewModel.selectedPc.collectAsStateWithLifecycle()
  val pcList by viewModel.pcList.collectAsStateWithLifecycle()
  val connected by viewModel.connected.collectAsStateWithLifecycle()

  var backPressCount by remember { mutableStateOf(0) }
  var showResolutionDialog by remember { mutableStateOf(false) }
  var showLogoutHint by remember { mutableStateOf(false) }
  var showSchedDialog by remember { mutableStateOf(false) }
  var showPcDialog by remember { mutableStateOf(false) }

  // 제어할 PC를 고르기 전에는 아무 명령도 보낼 곳이 없다 → 컨트롤을 잠근다.
  val hasTarget = selectedPc != null
  val controlsEnabled = hasTarget && !state.isPending && !state.isConnectingTarget

  // 포그라운드 복귀 시 세션 되살리기 + 방 재입장/상태 재동기화.
  LifecycleEventEffect(Lifecycle.Event.ON_RESUME) {
    viewModel.onResume(onSessionLost = onLoggedOut)
  }

  Column(
    modifier = modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
    verticalArrangement = Arrangement.spacedBy(10.dp),
  ) {
    // ── 최상단: 제어할 PC 선택 박스 (기본은 미선택) ──────────────────────────
    Row(
      modifier =
        Modifier.fillMaxWidth()
          .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(10.dp))
          .border(BorderStroke(1.dp, MaterialTheme.colorScheme.outline), RoundedCornerShape(10.dp))
          .clickable { showPcDialog = true }
          .padding(horizontal = 14.dp, vertical = 14.dp),
      verticalAlignment = Alignment.CenterVertically,
    ) {
      Text(
        text = selectedPc ?: "제어 PC 선택하기",
        style = MaterialTheme.typography.titleMedium,
        fontWeight = if (hasTarget) FontWeight.Bold else FontWeight.Normal,
        color =
          if (hasTarget) MaterialTheme.colorScheme.onSurface
          else MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.weight(1f),
      )
      Text("▾", style = MaterialTheme.typography.titleMedium)
    }
    if (!connected) {
      Text(
        "서버와 연결이 끊어졌습니다. 다시 접속하는 중...",
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.error,
      )
    }

    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      Text("해상도", modifier = Modifier.weight(1f))
      Text(state.resolutionLabel, style = MaterialTheme.typography.bodyMedium)
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(onClick = { showResolutionDialog = true }, enabled = controlsEnabled) { Text("직접 설정") }
      OutlinedButton(onClick = viewModel::onResolutionAuto, enabled = controlsEnabled) { Text("자동 감지") }
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
        enabled = hasTarget,
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
        enabled = controlsEnabled,
      )
      Text("낚싯대 자동교체")
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
      Checkbox(
        checked = state.bait,
        onCheckedChange = { viewModel.onFlagsToggled(state.logSave, state.rod, it) },
        enabled = controlsEnabled,
      )
      Text("미끼 자동교체")
    }
    Text(
      "낚싯대, 미끼 자동교체는 살림망 감시 모드에서만 사용 가능합니다.",
      style = MaterialTheme.typography.bodySmall,
    )

    Button(
      onClick = viewModel::onStartStop,
      enabled = controlsEnabled,
      modifier = Modifier.fillMaxWidth(),
    ) {
      Text(if (state.running) "중지" else "시작")
    }
    Text(state.statusMessage, style = MaterialTheme.typography.bodyMedium)

    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(
        onClick = { showSchedDialog = true },
        enabled = controlsEnabled,
        modifier = Modifier.weight(1f),
      ) { Text("예약 종료") }
      OutlinedButton(
        onClick = viewModel::onCollectNow,
        enabled = controlsEnabled,
        modifier = Modifier.weight(1f),
      ) { Text("즉시 회수") }
    }
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(
        onClick = viewModel::onTankQuery,
        enabled = hasTarget,
        modifier = Modifier.weight(1f),
      ) { Text("실시간 수량확인") }
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

    // 하단: 로그아웃 + 알림 설정.
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

  if (showPcDialog) {
    PcSelectDialog(
      pcList = pcList,
      selected = selectedPc,
      onDismiss = { showPcDialog = false },
      onSelect = { pc ->
        showPcDialog = false
        viewModel.onSelectPc(pc)
      },
      onAdd = viewModel::onAddPc,
      onDelete = viewModel::onRemovePc,
    )
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
            // 화면을 먼저 넘기고, 소켓·서비스 정리는 저장소가 뒤에서 끝낸다
            // (메인 스레드에서 정리하다 '응답 없음'이 되던 문제 대응).
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

/**
 * 제어 PC 선택 다이얼로그. 목록은 PC 이름(= 그 PC의 domichat 로그인 ID)만 담으며
 * 사용자가 추가·삭제할 수 있다. 기본값은 seoul / chungju / galaxy.
 * 고르면 `domi_fishing_{이름}` 채팅방을 구독해 상태를 받기 시작한다.
 */
@Composable
private fun PcSelectDialog(
  pcList: List<String>,
  selected: String?,
  onDismiss: () -> Unit,
  onSelect: (String) -> Unit,
  onAdd: (String) -> Boolean,
  onDelete: (String) -> Unit,
) {
  var newName by remember { mutableStateOf("") }
  var addError by remember { mutableStateOf<String?>(null) }

  AlertDialog(
    onDismissRequest = onDismiss,
    title = { Text("제어 PC 선택") },
    text = {
      Column(
        modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(4.dp),
      ) {
        if (pcList.isEmpty()) {
          Text("등록된 PC가 없습니다. 아래에서 추가하세요.", style = MaterialTheme.typography.bodySmall)
        }
        pcList.forEach { pc ->
          Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            TextButton(onClick = { onSelect(pc) }, modifier = Modifier.weight(1f)) {
              Text(
                text = if (pc == selected) "$pc  ✓" else pc,
                modifier = Modifier.fillMaxWidth(),
                fontWeight = if (pc == selected) FontWeight.Bold else FontWeight.Normal,
              )
            }
            TextButton(onClick = { onDelete(pc) }) { Text("삭제") }
          }
          HorizontalDivider()
        }

        Row(
          verticalAlignment = Alignment.CenterVertically,
          horizontalArrangement = Arrangement.spacedBy(6.dp),
          modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        ) {
          OutlinedTextField(
            value = newName,
            onValueChange = {
              newName = it
              addError = null
            },
            label = { Text("PC 이름") },
            singleLine = true,
            modifier = Modifier.weight(1f),
          )
          TextButton(
            onClick = {
              if (onAdd(newName)) {
                newName = ""
                addError = null
              } else {
                addError = "이미 있거나 쓸 수 없는 이름입니다."
              }
            },
          ) { Text("추가") }
        }
        addError?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
      }
    },
    confirmButton = { TextButton(onClick = onDismiss) { Text("닫기") } },
  )
}
