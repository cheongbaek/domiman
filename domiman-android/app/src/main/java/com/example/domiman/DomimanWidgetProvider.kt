package com.example.domiman

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import android.widget.Toast

/**
 * 홈 위젯(4x1): [새로고침] | 수량 | [다운로드=즉시회수] | [재생/중지].
 *  - 새로고침: '실시간 수량확인'(N). 응답이 오면 수량 칸을 갱신(수동 새로고침 시에만).
 *  - 다운로드: '즉시 회수'.
 *  - 재생/중지: 시작/중지 토글. running이면 정지(pause) 아이콘, 아니면 재생(play).
 *  - 로그인 세션이 없으면(로그아웃/미로그인) 수량 칸에 '로그인'을 표시하고, 아무
 *    칸이나 누르면 앱(로그인 화면)을 연다.
 *
 * 위젯 탭은 앱이 꺼져 있어도 이 리시버로 전달된다(그때 DomimanApplication.onCreate가
 * 먼저 돌아 Python+repository 싱글턴이 준비됨). 실제 상태 갱신은 repository가
 * 스트림 응답을 받아 prefs에 쓰고 refresh(context)를 부르는 흐름으로 이뤄진다.
 */
class DomimanWidgetProvider : AppWidgetProvider() {

  override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
    refresh(context)
  }

  override fun onReceive(context: Context, intent: Intent) {
    super.onReceive(context, intent)
    if (intent.action !in setOf(ACTION_REFRESH, ACTION_COLLECT, ACTION_TOGGLE)) return
    try {
      val repo = (context.applicationContext as? DomimanApplication)?.repository ?: return
      // 로그인 전이거나 제어할 PC를 아직 고르지 않았으면 보낼 곳이 없다
      // (그 상태의 위젯 버튼은 아래 buildViews에서 '앱 열기'로 묶여 있다).
      if (!repo.isSessionConfigured() || !repo.hasSelectedPc()) return
      // 위젯 탭은 onReceive가 반환되면 프로세스가 곧 죽을 수 있다(백그라운드).
      // 재로그인(최대 15초)이 끝나기 전에 죽으면 갱신이 안 되므로, 먼저 포그라운드
      // 서비스를 띄워 프로세스를 붙잡아 둔다(위젯 탭은 잠깐의 FGS 시작 허용창을 줌).
      runCatching { DomimanService.start(context) }
      when (intent.action) {
        ACTION_REFRESH -> {
          toast(context, "실시간 수량 확인")
          repo.widgetRefresh()
        }
        ACTION_COLLECT -> {
          toast(context, "즉시 살림망 회수")
          repo.widgetCollect()
        }
        ACTION_TOGGLE -> {
          // 현재 상태 기준으로 즉시 피드백(정지 중이면 시작, 진행 중이면 중지).
          toast(context, if (repo.widgetRunning()) "매크로가 중지되었습니다" else "매크로가 시작되었습니다")
          repo.widgetToggle()
        }
      }
    } catch (_: Exception) {
      // 어떤 경우에도 위젯 탭이 앱을 크래시시키지 않게 방어.
    }
  }

  companion object {
    private const val ACTION_REFRESH = "com.example.domiman.widget.REFRESH"
    private const val ACTION_COLLECT = "com.example.domiman.widget.COLLECT"
    private const val ACTION_TOGGLE = "com.example.domiman.widget.TOGGLE"

    private fun toast(context: Context, msg: String) {
      runCatching { Toast.makeText(context.applicationContext, msg, Toast.LENGTH_SHORT).show() }
    }

    /** 현재 저장된 상태(로그인여부·수량·running)로 모든 위젯 인스턴스를 다시 그린다.
     * 스트림 스레드 등 어디서 불려도 예외로 앱을 죽이지 않도록 방어. */
    fun refresh(context: Context) {
      try {
        val appCtx = context.applicationContext
        val repo = (appCtx as? DomimanApplication)?.repository ?: return
        val manager = AppWidgetManager.getInstance(appCtx)
        val ids =
          manager.getAppWidgetIds(ComponentName(appCtx, DomimanWidgetProvider::class.java))
        if (ids.isEmpty()) return
        val views =
          buildViews(
            appCtx,
            repo.isSessionConfigured() && repo.hasSelectedPc(),
            if (repo.isSessionConfigured()) "PC선택" else "로그인",
            repo.widgetQtyText(),
            repo.widgetRunning(),
          )
        ids.forEach { manager.updateAppWidget(it, views) }
      } catch (_: Exception) {
      }
    }

    /** ready = 로그인돼 있고 제어할 PC까지 골라진 상태. 아니면 수량 칸에
     * idleText('로그인' 또는 'PC선택')를 띄우고 어느 칸을 눌러도 앱을 연다. */
    private fun buildViews(
      context: Context,
      ready: Boolean,
      idleText: String,
      qtyText: String,
      running: Boolean,
    ): RemoteViews {
      val views = RemoteViews(context.packageName, R.layout.widget_domiman)

      // 재생/중지 아이콘(제공된 초록 물고기 세트): 진행 중이면 일시정지(우하단),
      // 대기면 재생(좌하단).
      views.setImageViewResource(
        R.id.widget_toggle,
        if (running) R.drawable.widget_ic_pause else R.drawable.widget_ic_play,
      )

      if (ready) {
        views.setTextViewText(R.id.widget_qty, qtyText)
        views.setOnClickPendingIntent(R.id.widget_refresh, broadcast(context, ACTION_REFRESH, 1))
        views.setOnClickPendingIntent(R.id.widget_collect, broadcast(context, ACTION_COLLECT, 2))
        views.setOnClickPendingIntent(R.id.widget_toggle, broadcast(context, ACTION_TOGGLE, 3))
        // 수량 칸(4/470 등)을 누르면 앱으로 들어간다(요구사항).
        views.setOnClickPendingIntent(R.id.widget_qty, openApp(context))
      } else {
        // 미로그인/제어 PC 미선택: 안내 문구를 띄우고 어느 칸을 눌러도 앱을 연다.
        views.setTextViewText(R.id.widget_qty, idleText)
        val open = openApp(context)
        views.setOnClickPendingIntent(R.id.widget_refresh, open)
        views.setOnClickPendingIntent(R.id.widget_qty, open)
        views.setOnClickPendingIntent(R.id.widget_collect, open)
        views.setOnClickPendingIntent(R.id.widget_toggle, open)
      }
      return views
    }

    private fun broadcast(context: Context, action: String, req: Int): PendingIntent {
      val intent = Intent(context, DomimanWidgetProvider::class.java).setAction(action)
      return PendingIntent.getBroadcast(
        context,
        req,
        intent,
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
      )
    }

    private fun openApp(context: Context): PendingIntent {
      val intent =
        Intent(context, MainActivity::class.java)
          .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
      return PendingIntent.getActivity(
        context,
        0,
        intent,
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
      )
    }
  }
}
