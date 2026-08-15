package com.example.domiman

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.example.domiman.data.DomimanRepository

/**
 * 앱 프로세스 시작 시 1회 Python(Chaquopy)을 띄우고, **앱 수명 전체를 사는
 * DomimanRepository 싱글턴**을 만든다. 저장소를 Activity가 아니라 여기(앱)에
 * 두는 이유(함정): 화면 회전/백그라운드 복귀로 Activity가 재생성돼도 로그인
 * domichat 세션과 이벤트 펌프가 그대로 살아남아야 "껐다 켜야 정상" 문제가 없어진다.
 */
class DomimanApplication : Application() {
  lateinit var repository: DomimanRepository
    private set

  override fun onCreate() {
    super.onCreate()
    if (!Python.isStarted()) {
      Python.start(AndroidPlatform(this))
    }
    repository = DomimanRepository(this)
  }
}
