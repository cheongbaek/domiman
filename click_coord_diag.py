"""
click_coord_diag.py — QHD 클릭 좌표 변환 진단
"""
import ctypes, win32gui, time

# DPI 인식을 본 코드와 동일하게 맞춤 (중요)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

def find_hwnd(keyword="tales runner"):
    found = [None]
    def cb(h, _):
        if win32gui.IsWindowVisible(h) and keyword.lower() in win32gui.GetWindowText(h).lower():
            found[0] = h
    win32gui.EnumWindows(cb, None)
    return found[0]

hwnd = find_hwnd()
print("HWND:", hwnd)
if not hwnd:
    raise SystemExit("게임 창을 못 찾음")

print("제목:", repr(win32gui.GetWindowText(hwnd)))

l, t, r, b = win32gui.GetWindowRect(hwnd)
print(f"GetWindowRect : left={l}, top={t}, right={r}, bottom={b}")
print(f"  -> 창 크기   : {r-l} x {b-t}")

cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
print(f"GetClientRect : {cr-cl} x {cb-ct}  (클라이언트 영역)")

# 클라이언트 (0,0)이 화면상 어디인지
pt = win32gui.ClientToScreen(hwnd, (0, 0))
print(f"ClientToScreen(0,0): {pt}")

# 현재 마우스 위치 (게임 특정 지점에 올려두고 실행해보면 대조 가능)
class P(ctypes.Structure):
    _fields_=[("x",ctypes.c_long),("y",ctypes.c_long)]
    time.sleep(5)
p=P(); ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
print(f"현재 커서 위치 : ({p.x}, {p.y})")

# FHD 좌표 하나를 변환해보기 (살림망 버튼)
fx, fy = 1007, 1006
sx = l + fx/1920.0*(r-l)
sy = t + fy/1080.0*(b-t)
print(f"\nFHD({fx},{fy}) -> 화면 변환 결과: ({int(sx)}, {int(sy)})")