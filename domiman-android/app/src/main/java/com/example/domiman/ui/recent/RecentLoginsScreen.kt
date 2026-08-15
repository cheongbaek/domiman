package com.example.domiman.ui.recent

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.domiman.data.DomimanRepository
import com.example.domiman.data.SavedLoginJson

/**
 * 최근 로그인 목록. **자동 로그인을 체크하고 로그인했을 때만** 여기에 남는다
 * (체크 없이 로그인한 자격은 로그아웃과 함께 사라진다). 한 행은 domichat ID와
 * 그 아래 domiserver 주소이며, 비밀번호는 저장은 하되 화면에 보이지 않는다.
 */
@Composable
fun RecentLoginsScreen(
  repository: DomimanRepository,
  onBack: () -> Unit,
  onLoggedIn: () -> Unit,
  onEditRequested: () -> Unit,
  modifier: Modifier = Modifier,
  viewModel: RecentLoginsScreenViewModel = viewModel { RecentLoginsScreenViewModel(repository) },
) {
  val state by viewModel.loginState.collectAsStateWithLifecycle()
  val message by viewModel.message.collectAsStateWithLifecycle()
  val busy by viewModel.busy.collectAsStateWithLifecycle()

  Column(modifier = modifier.fillMaxWidth()) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      IconButton(onClick = onBack) {
        Text("<", style = MaterialTheme.typography.titleLarge)
      }
      Text("최근 로그인", style = MaterialTheme.typography.titleLarge)
    }

    HorizontalDivider()

    if (busy) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
    message?.let {
      Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(vertical = 8.dp))
    }

    if (state.recent.isEmpty()) {
      Text(
        "저장된 로그인이 없습니다. '자동 로그인'을 체크하고 로그인하면 여기에 남습니다.",
        style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(vertical = 16.dp),
      )
    }

    LazyColumn {
      items(state.recent, key = { it.ip + "|" + it.id }) { entry ->
        RecentLoginRow(
          entry = entry,
          onTap = { viewModel.tapEntry(entry, onSuccess = onLoggedIn) },
          onEdit = {
            viewModel.requestEdit(entry)
            onEditRequested()
          },
          onDelete = { viewModel.deleteEntry(entry) },
        )
        HorizontalDivider()
      }
    }
  }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun RecentLoginRow(
  entry: SavedLoginJson,
  onTap: () -> Unit,
  onEdit: () -> Unit,
  onDelete: () -> Unit,
) {
  var menuExpanded by remember { mutableStateOf(false) }

  Row(
    modifier =
      Modifier.fillMaxWidth()
        .combinedClickable(onClick = onTap, onLongClick = { menuExpanded = true })
        .padding(vertical = 12.dp, horizontal = 4.dp),
  ) {
    Column(modifier = Modifier.fillMaxWidth()) {
      Text(entry.id, style = MaterialTheme.typography.bodyLarge)
      Text(
        entry.ip,
        style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
      )
    }
    DropdownMenu(expanded = menuExpanded, onDismissRequest = { menuExpanded = false }) {
      DropdownMenuItem(
        text = { Text("수정") },
        onClick = {
          menuExpanded = false
          onEdit()
        },
      )
      DropdownMenuItem(
        text = { Text("삭제") },
        onClick = {
          menuExpanded = false
          onDelete()
        },
      )
    }
  }
}
