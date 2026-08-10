package com.example.domiman.ui.notif

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Checkbox
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.NotifyItem

@Composable
fun NotificationSettingsScreen(
  repository: DomimanRepository,
  onBack: () -> Unit,
  modifier: Modifier = Modifier,
  viewModel: NotificationSettingsViewModel =
    viewModel { NotificationSettingsViewModel(repository) },
) {
  val settings by viewModel.settings.collectAsStateWithLifecycle()

  Column(
    modifier = modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
    verticalArrangement = Arrangement.spacedBy(4.dp),
  ) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      IconButton(onClick = onBack) { Text("<", style = MaterialTheme.typography.titleLarge) }
      Text("알림 설정", style = MaterialTheme.typography.titleLarge)
    }
    HorizontalDivider()

    // 마스터 '알림 켜기' — 꺼지면 아래 항목 전부 봉인(disabled)되고 알림도 안 뜬다.
    Row(
      verticalAlignment = Alignment.CenterVertically,
      modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
    ) {
      Checkbox(checked = settings.master, onCheckedChange = viewModel::onMasterChange)
      Text("알림 켜기", style = MaterialTheme.typography.titleMedium)
    }
    HorizontalDivider()

    NotifyItem.entries.forEach { item ->
      Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp),
      ) {
        Checkbox(
          checked = settings.items[item.key] == true,
          onCheckedChange = { viewModel.onItemChange(item.key, it) },
          enabled = settings.master, // 마스터 꺼지면 봉인
        )
        Text(
          item.label,
          color =
            if (settings.master) MaterialTheme.colorScheme.onSurface
            else MaterialTheme.colorScheme.onSurfaceVariant,
        )
      }
    }
  }
}
