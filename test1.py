import win32gui
hwnd = 10617916   # 실전 로그의 GAME_HWND
print("제목:", repr(win32gui.GetWindowText(hwnd)))
print("가시성:", win32gui.IsWindowVisible(hwnd))
print("Rect:", win32gui.GetWindowRect(hwnd))
print("Client:", win32gui.GetClientRect(hwnd))
print("ClientToScreen(0,0):", win32gui.ClientToScreen(hwnd, (0,0)))