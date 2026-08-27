package com.example.domiman.ui.screenshot

import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.graphics.BitmapFactory
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.widget.Toast
import androidx.compose.foundation.Image
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import java.io.File
import java.io.FileOutputStream

/**
 * 받은 스크린샷을 보여주는 화면(표시 + 1:1/맞춤 토글 + 저장 + 클립보드 복사).
 * domiman.py ScreenshotWindow의 버튼 4개(1:1 보기/저장/클립보드 복사/닫기)와
 * 1:1 대응. MainScreen(앱 안 버튼)과 ScreenshotActivity(위젯) 양쪽에서 공용으로 쓴다.
 */
@Composable
fun ScreenshotViewer(name: String, bytes: ByteArray, onClose: () -> Unit) {
  val context = LocalContext.current
  var fit by remember { mutableStateOf(true) }
  val bitmap = remember(bytes) { BitmapFactory.decodeByteArray(bytes, 0, bytes.size) }

  Column(modifier = Modifier.fillMaxSize()) {
    Row(
      modifier = Modifier.fillMaxWidth().padding(8.dp),
      horizontalArrangement = Arrangement.spacedBy(4.dp),
      verticalAlignment = Alignment.CenterVertically,
    ) {
      TextButton(onClick = { fit = !fit }) { Text(if (fit) "1:1 보기" else "창에 맞춤") }
      TextButton(onClick = { saveScreenshot(context, name, bytes) }) { Text("저장") }
      TextButton(onClick = { copyScreenshotToClipboard(context, name, bytes) }) { Text("클립보드 복사") }
      Spacer(modifier = Modifier.weight(1f))
      TextButton(onClick = onClose) { Text("닫기") }
    }
    if (bitmap == null) {
      Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Text("스크린샷을 표시하지 못했습니다(파일이 손상됐습니다).")
      }
    } else {
      val image = remember(bitmap) { bitmap.asImageBitmap() }
      if (fit) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
          Image(
            bitmap = image,
            contentDescription = name,
            contentScale = ContentScale.Fit,
            modifier = Modifier.fillMaxSize(),
          )
        }
      } else {
        Box(
          modifier =
            Modifier.fillMaxSize()
              .horizontalScroll(rememberScrollState())
              .verticalScroll(rememberScrollState()),
        ) {
          Image(bitmap = image, contentDescription = name, contentScale = ContentScale.None)
        }
      }
    }
  }
}

/** 원본 PNG 그대로 MediaStore(Pictures/domiman)에 저장 — scoped storage(API 29+)라
 * 별도 저장 권한이 필요 없다(domiman.py save()의 모바일 대응, 대화상자 없이 즉시 저장). */
private fun saveScreenshot(context: Context, name: String, bytes: ByteArray) {
  runCatching {
    val values =
      ContentValues().apply {
        put(MediaStore.Images.Media.DISPLAY_NAME, name)
        put(MediaStore.Images.Media.MIME_TYPE, "image/png")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
          put(MediaStore.Images.Media.RELATIVE_PATH, "${Environment.DIRECTORY_PICTURES}/domiman")
        }
      }
    val uri =
      context.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
        ?: error("insert 실패")
    context.contentResolver.openOutputStream(uri)?.use { it.write(bytes) } ?: error("openOutputStream 실패")
    Toast.makeText(context, "스크린샷을 저장했습니다.", Toast.LENGTH_SHORT).show()
  }.onFailure {
    Toast.makeText(context, "저장에 실패했습니다.", Toast.LENGTH_SHORT).show()
  }
}

/** 안드로이드는 raw 이미지 클립보드(Windows CF_DIB 같은)가 없어 content:// URI만
 * 복사할 수 있다 — 캐시에 PNG를 쓰고 FileProvider로 URI를 발급해 붙여넣기 대상
 * 앱에 읽기 권한이 자동으로 넘어가게 한다(domiman.py copy_clipboard()의 모바일 대응). */
private fun copyScreenshotToClipboard(context: Context, name: String, bytes: ByteArray) {
  runCatching {
    val dir = File(context.cacheDir, "screenshots").apply { mkdirs() }
    val file = File(dir, name)
    FileOutputStream(file).use { it.write(bytes) }
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val clip = ClipData.newUri(context.contentResolver, name, uri)
    val cm = context.getSystemService(ClipboardManager::class.java)
    cm.setPrimaryClip(clip)
    Toast.makeText(context, "클립보드에 복사했습니다.", Toast.LENGTH_SHORT).show()
  }.onFailure {
    Toast.makeText(context, "클립보드 복사에 실패했습니다.", Toast.LENGTH_SHORT).show()
  }
}
