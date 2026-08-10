package com.example.domiman.ui.login

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.domiman.data.DomimanRepository

@Composable
fun LoginScreen(
  repository: DomimanRepository,
  onLoggedIn: () -> Unit,
  onOpenRecentLogins: () -> Unit,
  onEditDone: () -> Unit,
  modifier: Modifier = Modifier,
  viewModel: LoginScreenViewModel = viewModel { LoginScreenViewModel(repository) },
) {
  val state by viewModel.uiState.collectAsStateWithLifecycle()

  Column(
    modifier = modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
    verticalArrangement = Arrangement.spacedBy(12.dp),
  ) {
    Text("당신도 강태공이 되어보세요!", style = MaterialTheme.typography.headlineSmall)

    OutlinedTextField(
      value = state.id,
      onValueChange = viewModel::onIdChange,
      label = { Text("ntfy에 표시될 ID 입력") },
      singleLine = true,
      keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
      modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
      value = state.targetPc,
      onValueChange = viewModel::onTargetPcChange,
      label = { Text("피제어 PC 이름 입력") },
      singleLine = true,
      keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
      modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
      value = state.channel,
      onValueChange = viewModel::onChannelChange,
      label = { Text("ntfy 채널명 입력") },
      singleLine = true,
      keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
      modifier = Modifier.fillMaxWidth(),
    )

    if (state.mode == LoginFormMode.LOGIN) {
      Row(verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = state.autoLoginChecked, onCheckedChange = viewModel::onAutoLoginToggle)
        Text("자동 로그인")
      }
    }

    if (state.errorMessage != null) {
      Text(state.errorMessage!!, color = MaterialTheme.colorScheme.error)
    }

    if (state.isSubmitting) {
      CircularProgressIndicator()
    } else if (state.mode == LoginFormMode.LOGIN) {
      Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = { viewModel.submitLogin(onLoggedIn) }, modifier = Modifier.weight(1f)) {
          Text("로그인")
        }
        OutlinedButton(
          onClick = {
            viewModel.captureAutoLoginForRecent() // '자동 로그인' 체크를 최근로그인에 전달
            onOpenRecentLogins()
          },
        ) {
          Text("…")
        }
      }
    } else {
      Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Button(onClick = { viewModel.confirmEdit(onEditDone) }, modifier = Modifier.weight(1f)) {
          Text("수정")
        }
        OutlinedButton(onClick = { viewModel.cancelEdit(onEditDone) }) { Text("취소") }
      }
    }
  }
}
