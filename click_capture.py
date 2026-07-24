# -*- coding: utf-8 -*-
# ============================================================
# click_capture.py — 수동 클릭 좌표 검증 도구
# ------------------------------------------------------------
# 마우스를 원하는 지점(예: 보유 미끼 창의 오른쪽 세모 버튼)에 대고
# 좌클릭하면, 그 순간의 좌표를 출력한다.
#  - 화면 물리 좌표 (참고용)
#  - 게임 창 기준 FHD(1920x1080) 환산 좌표  ← 메인 코드에 쓸 값
# ESC 키로 종료.
# ============================================================
import ctypes
import time

import win32gui

# DPI 인식을 낚시.py와 동일하게 맞춤 (물리 픽셀 일관성 — 중요)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32
VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def find_game_hwnd(keyword="tales runner"):
    """게임 창 찾기 — 반드시 가시성 체크 + 클라이언트 영역 최대 창 선택.
    (같은 제목의 숨은 내부 창(1920x1080, (0,0))을 잡으면 변환이 틀어짐)"""
    cands = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and keyword in win32gui.GetWindowText(h).lower():
            l, t, r, b = win32gui.GetClientRect(h)
            cands.append((h, (r - l) * (b - t)))

    win32gui.EnumWindows(cb, None)
    if not cands:
        return None
    return max(cands, key=lambda c: c[1])[0]


def to_fhd(hwnd, sx, sy):
    """화면 물리 좌표 -> 게임 클라이언트 기준 FHD(1920x1080) 좌표.
    낚시.py to_screen의 역변환. 창이 움직일 수 있으니 매번 새로 잰다."""
    l, t, r, b = win32gui.GetClientRect(hwnd)
    cw, ch = r - l, b - t
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    if cw <= 0 or ch <= 0:
        return None
    fx = (sx - ox) / cw * 1920.0
    fy = (sy - oy) / ch * 1080.0
    inside = 0 <= sx - ox < cw and 0 <= sy - oy < ch
    return round(fx), round(fy), inside, (cw, ch), (ox, oy)


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


if __name__ == "__main__":
    hwnd = find_game_hwnd()
    if not hwnd:
        raise SystemExit("[오류] 게임 창(Tales Runner)을 찾지 못했습니다. 게임을 먼저 실행하세요.")

    l, t, r, b = win32gui.GetClientRect(hwnd)
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    print(f"[준비] 게임 창 HWND={hwnd}, 클라이언트 {r - l}x{b - t}, 원점 {(ox, oy)}")
    print("[안내] 원하는 지점에 마우스를 대고 좌클릭하세요. 클릭 좌표를 출력합니다.")
    print("[안내] ESC 키를 누르면 종료합니다.\n")

    was_down = user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000
    n = 0
    try:
        while True:
            if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                print("\n[종료] ESC 감지")
                break

            down = user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000
            if down and not was_down:  # 눌리는 순간(edge)만
                n += 1
                sx, sy = cursor_pos()
                conv = to_fhd(hwnd, sx, sy)
                if conv is None:
                    print(f"[클릭 {n}] 화면 ({sx}, {sy}) — 창 크기를 잴 수 없음")
                else:
                    fx, fy, inside, (cw, ch), origin = conv
                    mark = "" if inside else "  ※ 게임 창 밖 클릭!"
                    print(f"[클릭 {n}] 화면 ({sx}, {sy})  ->  FHD ({fx}, {fy})"
                          f"   [클라 {cw}x{ch}, 원점 {origin}]{mark}")
            was_down = down
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n[종료] Ctrl+C")
