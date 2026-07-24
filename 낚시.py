"""
낚시.py — 낚시 자동화 (FHD/QHD 통합: 윤곽선 매칭)
====================================================================
FHD(1080p) → pyautogui 화면 캡처 + cv2 윤곽선(실루엣 마스크) 비교
QHD(1440p) → WGC(Windows Graphics Capture)로 게임 창을 원본으로 잡아
             1920x1080으로 축소 후, FHD와 동일한 윤곽선 매칭 사용
             (CNN 제거 — 두 해상도 모두 동일 로직으로 통일)
+ ntfy.sh 푸시 알림 (선택, 양방향)
"""
import ctypes
import time
import sys
import os
import re
import random
import json
import atexit
import traceback
import warnings
import msvcrt
import socket
import threading
import queue

import cv2
import numpy as np
import pyautogui
import win32gui
import win32con
import win32api
import win32process
import easyocr
import requests

# WGC는 QHD 모드에서만 필요. FHD 전용 사용자는 설치 없이도 동작하도록 지연 처리.
try:
    from windows_capture import WindowsCapture
    _WGC_AVAILABLE = True
except Exception:
    WindowsCapture = None
    _WGC_AVAILABLE = False


# ============================================================
# [0-A] 경고 억제
# ============================================================
warnings.filterwarnings("ignore", message=".*pin_memory.*")

# ============================================================
# [0-B] 콘솔 출력 전체를 파일에 동시 기록하는 Tee 클래스
# ============================================================
pyautogui.FAILSAFE = False
LOG_FILENAME = f"fishing_full_{time.strftime('%Y%m%d_%H%M%S')}.log"

_log_enabled = False
_log_saved = False


class Tee:
    def __init__(self, filename):
        self._console = sys.__stdout__
        self._filename = filename
        self._file = None

    def activate(self):
        if self._file is None:
            try:
                self._file = open(self._filename, "w", encoding="utf-8", buffering=1)
            except Exception as e:
                print(f"[경고] 로그 파일을 열 수 없습니다: {e}", file=self._console)

    def write(self, data):
        self._console.write(data)
        if self._file:
            self._file.write(data)

    def flush(self):
        self._console.flush()
        if self._file:
            self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None

    def fileno(self):
        return self._console.fileno()

    @property
    def is_active(self):
        return self._file is not None


_tee = Tee(LOG_FILENAME)
sys.stdout = _tee

# ============================================================
# [0-C] 잡히지 않은 예외를 로그에 남기는 핸들러
# ============================================================


def _unhandled_exception_hook(exc_type, exc_value, exc_tb):
    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    print(f"\n{'='*60}")
    print(f"[치명적 오류] {timestamp}")
    print(error_msg)
    print(f"{'='*60}\n")
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _unhandled_exception_hook


def _close_tee():
    global _log_saved
    if _log_enabled and not _log_saved:
        _log_saved = True
        print(f"\n[시스템] 전체 로그가 '{LOG_FILENAME}' 에 저장되었습니다.")
    sys.stdout = sys.__stdout__
    _tee.close()
    if not _log_enabled and os.path.exists(LOG_FILENAME):
        try:
            os.remove(LOG_FILENAME)
        except Exception:
            pass


atexit.register(_close_tee)

# ============================================================
# [0-D] 기본 경로 설정
# ============================================================
if getattr(sys, 'frozen', False):
    # PyInstaller onedir/onefile: 번들 데이터(ocr_model)는 _MEIPASS 아래에 있음.
    # py2exe 등 _MEIPASS가 없는 경우엔 실행 파일 폴더로 폴백.
    SCRIPT_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# [0-E] ntfy.sh 푸시 알림 (양방향)
# ============================================================
NTFY_TOPIC = "domi_fishing_9714"
NTFY_SERVER = "https://ntfy.sh"
NTFY_URL = f"{NTFY_SERVER}/{NTFY_TOPIC}"
_ntfy_enabled = False

PC_NAME = socket.gethostname()

_ntfy_last_poll_time = 0


def notify(message, title=None, priority=3, tags=None):
    """ntfy.sh로 푸시 알림 전송. 비활성화 상태면 무시."""
    if not _ntfy_enabled:
        return
    if title is None:
        title = PC_NAME
    try:
        headers = {"Priority": str(priority)}
        if title:
            headers["Title"] = title
        if tags:
            headers["Tags"] = ",".join(tags)
        resp = requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            print(f" -> [ntfy] 알림 전송 완료")
        elif resp.status_code == 429:
            print(f" -> [ntfy 경고] rate limit, 5초 후 재시도...")
            time.sleep(5)
            resp2 = requests.post(
                NTFY_URL,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
            if resp2.status_code == 200:
                print(f" -> [ntfy] 재시도 전송 완료")
            else:
                print(f" -> [ntfy 경고] 재시도 실패 HTTP {resp2.status_code}")
        else:
            print(f" -> [ntfy 경고] HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f" -> [ntfy 에러] {e}")


def notify_settings(context="설정 완료"):
    """현재 해상도·타이머 설정을 알림으로 전송."""
    cap = "WGC->1080p" if CURRENT_RESOLUTION == "1440p" else "screen"
    timer_min = LOOP_INTERVAL // 60
    msg = (f"[{context}]\n"
           f"- resolution: {CURRENT_RESOLUTION} (contour, {cap})\n"
           f"- timer: {timer_min}min")
    notify(msg, priority=3, tags=["gear"])


def poll_ntfy():
    """ntfy 토픽에서 새 메시지를 폴링. 가장 최근 메시지의 본문을 반환, 없으면 None."""
    global _ntfy_last_poll_time
    if not _ntfy_enabled:
        return None
    try:
        since = str(_ntfy_last_poll_time) if _ntfy_last_poll_time > 0 else "10s"
        resp = requests.get(
            f"{NTFY_URL}/json?poll=1&since={since}",
            timeout=3,
        )
        if resp.status_code != 200:
            return None

        latest_msg = None
        latest_time = _ntfy_last_poll_time

        for line in resp.text.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("event") != "message":
                continue

            if data.get("title", "") == PC_NAME:
                continue

            msg_time = data.get("time", 0)
            if msg_time > latest_time:
                latest_time = msg_time
                latest_msg = data.get("message", "").strip()

        if latest_time > _ntfy_last_poll_time:
            _ntfy_last_poll_time = latest_time

        return latest_msg if latest_msg else None

    except Exception:
        return None


# --- ntfy 백그라운드 폴러 ---------------------------------------------------
# 폴링(블로킹 HTTP)을 전용 스레드로 분리해, 입력 감시 루프가 네트워크에 막혀
# 키 입력이 씹히는 문제를 없앤다. 수신 메시지는 큐로 전달하고, 소비 측
# (dual_input / wait_with_keycheck)은 큐만 논블로킹으로 확인한다.
_ntfy_queue = queue.Queue()
_ntfy_poller_started = False


def _ntfy_poller_loop():
    while True:
        try:
            if _ntfy_enabled:
                msg = poll_ntfy()
                if msg is not None:
                    _ntfy_queue.put(msg)
                # 활성: 5초 간격(중간에 비활성화되면 조기 탈출)
                for _ in range(10):
                    if not _ntfy_enabled:
                        break
                    time.sleep(0.5)
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(1.0)


def start_ntfy_poller():
    """ntfy 폴러 스레드를 1회 기동(데몬). _ntfy_enabled는 스레드가 매번 확인."""
    global _ntfy_poller_started
    if _ntfy_poller_started:
        return
    _ntfy_poller_started = True
    threading.Thread(target=_ntfy_poller_loop, daemon=True).start()


def get_ntfy_message():
    """큐에 쌓인 ntfy 메시지 중 가장 최근 것을 반환(나머지는 버림). 없으면 None."""
    msg = None
    try:
        while True:
            msg = _ntfy_queue.get_nowait()
    except queue.Empty:
        pass
    return msg


def dual_input(prompt, ntfy_prompt=None):
    """
    터미널 input()과 ntfy 폴링을 동시에 대기.
    ntfy 비활성화 시 일반 input()만 사용.
    ntfy에서 '0'은 빈 문자열(Enter)로 변환.
    """
    if not _ntfy_enabled:
        return input(prompt)

    if ntfy_prompt:
        notify(ntfy_prompt, priority=3, tags=["question"])

    # 이전에 쌓인 메시지는 이번 프롬프트와 무관하므로 비운다.
    get_ntfy_message()

    result = [None]
    source = [None]

    def keyboard_thread():
        try:
            val = input(prompt)
            if result[0] is None:
                result[0] = val
                source[0] = "keyboard"
        except EOFError:
            pass

    t = threading.Thread(target=keyboard_thread, daemon=True)
    t.start()

    while result[0] is None:
        msg = get_ntfy_message()
        if msg is not None and result[0] is None:
            if msg == "0":
                result[0] = ""
            else:
                result[0] = msg
            source[0] = "ntfy"
            break
        time.sleep(0.2)

    src = source[0] or "unknown"
    if src == "ntfy":
        print(f"{prompt}{result[0]}  [ntfy]")

    return result[0]


# ============================================================

print("=== 낚시 매크로 설정 ===")

print("[시스템] OCR 모델을 불러오는 중입니다. 잠시만 기다려주세요...")
OCR_MODEL_DIR = os.path.join(SCRIPT_DIR, "ocr_model")
reader = easyocr.Reader(['ko', 'en'], gpu=False,
                        model_storage_directory=OCR_MODEL_DIR,
                        download_enabled=False)


# === [1. 윈도우 API 설정 및 구조체 정의] ===
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_ESCAPE = 0x1B
KEYEVENTF_KEYUP = 0x0002


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# === [2. 좌표 및 설정] ===
# 좌표는 항상 FHD(1920x1080) 기준. QHD 모드에서는 인식은 축소본에서,
# 클릭은 to_screen()이 실제 창 크기로 역변환한다.
COORD_FISHING_BTN = None
COORD_TANK_BTN = None
COORD_MYROOM_BTN = None
COORD_CONFIRM_BTN = None

REGION_Q_LEFT = None
REGION_Q_RIGHT = None
REGION_ANSWERS = None
REGION_VERIFY_TEXT = None

# 감시 모드용 OCR 영역 (FHD 기준). CLAUDE.md의 중심점 기준으로 박스화.
REGION_TANK_QTY = None    # 살림망 수량 'current/max' (중심 890,1007)
REGION_MIN_TIME = None    # 최소 획득 시간 '20초'     (중심 975,933)

# --- 미끼 자동 교체 (감시 모드 전용) — FHD 기준, 스크린샷 OCR + 실측 검증 ---
REGION_NO_BAIT = (890, 499, 142, 36)      # '미끼가 부족합니다' 팝업 메시지
COORD_OWN_BAIT_BTN = (1004, 578)          # 팝업의 '보유 미끼' 버튼
COORD_BAIT_NEXT_BTN = (1291, 565)         # 보유 미끼 창 오른쪽(다음 페이지) 화살표
REGION_BAIT_NAMES = (640, 483, 640, 202)  # 카드 이름 바 8칸(2행x4열)을 덮는 영역
BAIT_USE_BTNS = [                         # '사용하기' 버튼 좌표 (2행x4열, 실측)
    [(708, 544), (874, 543), (1038, 540), (1209, 541)],
    [(714, 702), (877, 699), (1042, 702), (1201, 701)],
]
BAIT_COL_X = (715, 878, 1041, 1204)       # 이름 매칭 위치 -> 열 판정용 중심 x
BAIT_NAME_ROW_Y = (503, 660)              # 이름 매칭 위치 -> 행 판정용 중심 y
# '갯지렁이'가 OCR에서 '개지렇미' 등으로 깨져 읽히므로 유연한 패턴 매칭
BAIT_TARGET_PATTERN = r"[갯개]지[렁렇령]"
BAIT_MAX_PAGE_MOVES = 4                   # 오른쪽 화살표 최대 4번 = 총 5페이지

# --- 낚싯대 자동 교체 (감시 모드 전용) — FHD 기준, 스크린샷 OCR 검증 ---
REGION_ROD_EXPIRE = (843, 444, 148, 40)   # '! 아이템 기간 만료' 팝업 타이틀
REGION_ROD_ITEM_NAME = (898, 575, 100, 34)  # 팝업 속 만료 아이템 이름('테런 낚싯대')
# 이름 오인식이 심해('테런 낚싯대'→'그런 낚싼다') 낚싯대류 공통 글자 '낚'만 확인
ROD_EXPIRE_NAME_PATTERN = r"낚"
COORD_ROD_LIST_BTN = (815, 916)           # 보유 낚싯대 리스트 열기 버튼 (실측)
# '매직 스타 낚싯대' / '푸른 장미검 낚싯대' — 이름 오인식이 잦아 핵심 단어만
ROD_TARGET_PATTERN = r"스타|장미검"
# 보유 낚싯대 창은 보유 미끼 창과 그리드가 동일 → BAIT_* 좌표 재사용

# --- 접속 끊김 감지 (감시 모드 전용) — 스크린샷 OCR 검증 ---
REGION_DISCONNECT = (769, 386, 258, 40)   # '서버와 접속이 끊어졌습니다.' 타이틀

LOOP_INTERVAL = 3600

CURRENT_RESOLUTION = None

GAME_KEYWORD = "tales runner"   # 창 검색용(부분·소문자 매칭)
WGC_WINDOW_NAME = "Tales Runner"  # WGC용(정확 일치 필요)
GAME_HWND = None                # bring_game_to_front에서 갱신


# === [3. QHD 캡처: WGC 백그라운드 캡처] ===

class GameCapture:
    """
    Windows Graphics Capture로 게임 창을 백그라운드에서 연속 캡처하고
    최신 프레임(원본 해상도 BGR)을 보관한다. get_frame_1080()으로
    1920x1080 축소본을 요청 시점에 생성.
    """

    def __init__(self, window_name):
        self.window_name = window_name
        self._latest = None            # 최신 원본 BGR 프레임
        self._lock = threading.Lock()
        self._control = None
        self._running = False

    def start(self):
        if self._running:
            return
        if not _WGC_AVAILABLE:
            raise RuntimeError("windows-capture 패키지가 없습니다. 'pip install windows-capture'")

        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            window_name=self.window_name,
        )

        @cap.event
        def on_frame_arrived(frame, capture_control):
            # frame.frame_buffer: (H, W, 4) BGRA. cvtColor가 새 배열을 만들어 안전.
            bgr = cv2.cvtColor(frame.frame_buffer, cv2.COLOR_BGRA2BGR)
            with self._lock:
                self._latest = bgr

        @cap.event
        def on_closed():
            pass

        # ⚠ 버전에 따라 메서드명이 다를 수 있음. 논블로킹 백그라운드 실행.
        self._control = cap.start_free_threaded()
        self._running = True

    def stop(self):
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:
                pass
        self._control = None
        self._running = False
        with self._lock:
            self._latest = None

    @property
    def is_running(self):
        return self._running

    def wait_ready(self, timeout=5.0):
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if self._latest is not None:
                    return True
            time.sleep(0.1)
        return False

    def get_frame_1080(self):
        with self._lock:
            raw = self._latest
        if raw is None:
            return None
        return cv2.resize(raw, (1920, 1080), interpolation=cv2.INTER_AREA)


game_capture = None   # QHD 모드에서 setup_resolution이 생성


# === [4. 화면 소스 추상화 (FHD/QHD 공통)] ===

def _find_game_hwnd(keyword=GAME_KEYWORD):
    found = [None]

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                if keyword.lower() in win32gui.GetWindowText(hwnd).lower():
                    found[0] = hwnd
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return found[0]


def _pick_game_hwnd(keyword=GAME_KEYWORD):
    """
    실제 렌더링 창(hwnd)을 선택. 반환 없으면 None.

    게임은 같은 제목 창을 2개 만든다: 진짜 렌더링 창과 숨은 내부 창
    (가시성 0, 1920x1080, (0,0)). IsWindowVisible로 숨은 창을 거른다.
    최소화된 진짜 창도 visible=1로 잡히므로: 최소화 안 된 창을 우선하되,
    전부 최소화면 그중 클라이언트 최대 창을 쓴다(모니터 판정은 최소화여도 가능).
    """
    wins = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if keyword.lower() not in win32gui.GetWindowText(hwnd).lower():
                return
            ico = bool(win32gui.IsIconic(hwnd))
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            wins.append((hwnd, ico, cr - cl, cb - ct))
        except Exception:
            pass

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return None

    if not wins:
        return None

    shown = [w for w in wins if not w[1]]
    pool = shown if shown else wins
    best = max(pool, key=lambda x: x[2] * x[3])
    return best[0]


def _monitor_scale_is_100(hmon):
    """모니터 배율이 100%(96 DPI)인지. 조회 실패 시 True로 간주(관대)."""
    try:
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        # GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI=0, &x, &y)
        ctypes.windll.shcore.GetDpiForMonitor(
            int(hmon), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return dpi_x.value == 96
    except Exception:
        return True


def detect_resolution(keyword=GAME_KEYWORD):
    """
    게임 창이 '어느 모니터'에 있는지로 해상도 모드를 판별.
    (게임이 DPI-unaware라 클라이언트 크기는 배율과 무관하게 항상 ~1920x1080이
     나오므로 크기로는 구분 불가. 모니터 물리 크기가 유일하게 신뢰할 신호다.)

    - 주 모니터 · 1920x1080 · 100% 배율 → "1080p" (pyautogui 직접 캡처 가능)
    - 그 외(서브모니터/고배율/비1080p) → "1440p" (WGC로 창 직접 캡처)

    창을 못 찾으면 None. 반환: (mode, mon_w, mon_h, is_primary)
    """
    hwnd = _pick_game_hwnd(keyword)
    if hwnd is None:
        return None

    try:
        hmon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        mi = win32api.GetMonitorInfo(hmon)
        ml, mt, mr, mb = mi['Monitor']
        mw, mh = mr - ml, mb - mt
        is_primary = bool(mi['Flags'] & win32con.MONITORINFOF_PRIMARY)
    except Exception:
        return None

    if is_primary and mw == 1920 and mh == 1080 and _monitor_scale_is_100(hmon):
        return "1080p", mw, mh, is_primary
    return "1440p", mw, mh, is_primary


def grab_region_rgb(region):
    """
    FHD 좌표(region=(x,y,w,h))에 해당하는 화면 조각을 RGB numpy로 반환.
    - FHD: pyautogui로 화면 직접 캡처 (게임이 화면 (0,0) 기준이라고 가정)
    - QHD: WGC 최신 프레임을 1920x1080으로 축소 후 해당 영역을 crop
    실패 시 None.
    """
    x, y, w, h = region
    if CURRENT_RESOLUTION == "1080p":
        shot = pyautogui.screenshot(region=region)
        return np.array(shot)  # RGB

    # QHD
    frame = game_capture.get_frame_1080() if game_capture else None
    if frame is None:
        return None
    crop = frame[y:y + h, x:x + w]
    if crop.size == 0:
        return None
    return cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)

def to_screen(coords):
    fx, fy = coords
    if CURRENT_RESOLUTION == "1080p":
        return int(fx), int(fy)

    hwnd = GAME_HWND or _find_game_hwnd()
    print(f"[to_screen] GAME_HWND={GAME_HWND}, 사용 hwnd={hwnd}")   # ← 진단
    if not hwnd:
        print("[경고] 게임 창을 찾지 못해 좌표 보정을 건너뜁니다.")
        return int(fx), int(fy)

    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    cw, ch = cr - cl, cb - ct
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    sx = ox + fx / 1920.0 * cw
    sy = oy + fy / 1080.0 * ch
    print(f"[to_screen] FHD({fx},{fy}) -> SCR({int(sx)},{int(sy)})")  # ← 진단
    return int(sx), int(sy)


# === [5. FHD/QHD 공통 윤곽선 매칭 함수] ===

def get_normalized_fish_mask(region):
    rgb = grab_region_rgb(region)
    if rgb is None:
        return None
    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return None
    margin_x = int(w * 0.15)
    margin_y = int(h * 0.15)
    img = img[margin_y:h - margin_y, margin_x:w - margin_x]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    mask = np.zeros_like(binary)
    cv2.drawContours(mask, [largest], -1, 255, cv2.FILLED)

    x, y, bw, bh = cv2.boundingRect(largest)
    cropped = mask[y:y + bh, x:x + bw]
    resized = cv2.resize(cropped, (64, 64), interpolation=cv2.INTER_AREA)

    return resized


def solve_quiz_step(region_q, answer_slots, side_label=""):
    """질문 물고기 실루엣을 보기 슬롯들과 윤곽선 매칭하여 최선의 칸을 클릭."""
    print(f"  [{side_label}] 실루엣 분석 중... (윤곽선 매칭)")

    try:
        q_mask = get_normalized_fish_mask(region_q)
        if q_mask is None:
            print(f"    [실패] 실루엣 마스크 추출 실패")
            return False

        best_center = None
        best_index = -1
        best_diff = float('inf')

        for slot in answer_slots:
            ans_mask = get_normalized_fish_mask(slot["region"])
            if ans_mask is None:
                continue

            diff = np.mean(cv2.absdiff(q_mask, ans_mask))

            if diff < best_diff:
                best_diff = diff
                best_center = slot["center"]
                best_index = slot["index"]

        if best_center and best_diff < 80.0:
            print(f"    매칭 답안: {best_index}번 칸 (유사도 오차: {best_diff:.1f})")
            click_real(best_center, delay=0.5)
            return True
        elif best_center:
            print(f"    [경고] 오차가 높음 ({best_diff:.1f}), 그래도 최선: {best_index}번 칸")
            click_real(best_center, delay=0.5)
            return True
        else:
            print(f"    [실패] 매칭 답안을 찾지 못함")
            return False

    except Exception:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        print(f"    [에러] {timestamp}")
        traceback.print_exc()
        return False


# === [6. 헬퍼 함수들] ===

def _force_foreground(hwnd, timeout=2.0):
    """
    창을 확실히 포그라운드로. Windows 포그라운드 잠금을 AttachThreadInput +
    ALT 키 트릭으로 우회한다. 이미 최상단이면 아무것도 건드리지 않는다
    (불필요한 창 조작이 게임 튕김을 유발할 수 있으므로).
    성공(포그라운드 확인) 시 True.
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
    except Exception:
        pass

    # 이미 앞에 있으면 그대로 둔다 (게임에 손대지 않음)
    try:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass

    cur = win32api.GetCurrentThreadId()
    fg_thread = tgt_thread = 0
    try:
        fg = win32gui.GetForegroundWindow()
        if fg:
            fg_thread = win32process.GetWindowThreadProcessId(fg)[0]
    except Exception:
        pass
    try:
        tgt_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    except Exception:
        pass

    attached = []
    for t in {fg_thread, tgt_thread}:
        if t and t != cur:
            try:
                win32process.AttachThreadInput(cur, t, True)
                attached.append(t)
            except Exception:
                pass

    try:
        # ALT 살짝 눌러 포그라운드 잠금 해제(표준 트릭)
        try:
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)          # ALT down
            ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)     # ALT up
        except Exception:
            pass
        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    finally:
        for t in attached:
            try:
                win32process.AttachThreadInput(cur, t, False)
            except Exception:
                pass

    end = time.time() + timeout
    while time.time() < end:
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def bring_game_to_front(keyword=GAME_KEYWORD):
    global GAME_HWND
    candidates = []

    def callback(hwnd, extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if keyword.lower() not in win32gui.GetWindowText(hwnd).lower():
                return
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            candidates.append((hwnd, (cr - cl) * (cb - ct)))
        except Exception:
            pass

    try:
        win32gui.EnumWindows(callback, None)
    except Exception as e:
        print(f"[경고] 창 목록 열거 실패: {e}")
        return False

    if not candidates:
        print(f"\n[경고] '{keyword}'(이)가 포함된 (보이는) 창을 찾을 수 없습니다.")
        return False

    # 클라이언트 영역이 가장 큰 창 = 실제 게임 화면
    candidates.sort(key=lambda c: c[1], reverse=True)
    target_hwnd = candidates[0][0]
    GAME_HWND = target_hwnd

    # 이하 기존과 동일 (제목 출력, 복구, SetForegroundWindow ...)
    try:
        real_title = win32gui.GetWindowText(target_hwnd)
        print(f"\n[시스템] '{real_title}' 창을 찾았습니다! 화면 앞으로 불러옵니다.")
    except Exception:
        print("\n[시스템] 대상 창을 찾았습니다. 화면 앞으로 불러옵니다.")

    # 이미 최상단이면 손대지 않고, 아니면 최대 3회 부드럽게 전환 시도.
    for _ in range(3):
        if _force_foreground(target_hwnd):
            time.sleep(0.5)
            return True
        time.sleep(0.4)

    print("[경고] 포커스 전환 실패 → 창 전환 없이 루틴 계속")
    return True

def click_real(coords, delay=1.0):
    """coords는 항상 FHD 좌표. to_screen이 실제 화면 좌표로 변환."""
    x, y = to_screen(coords)
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.1)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print(f"[Click] FHD({int(coords[0])},{int(coords[1])}) -> SCR({x},{y})")
    time.sleep(delay)


def press_esc(delay=1.0):
    """ESC 키를 게임에 전송(클릭과 동일한 ctypes 채널) 후 delay초 대기."""
    ctypes.windll.user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
    print("[Key] ESC")
    time.sleep(delay)


def get_answer_slot_regions(grid_region, rows=4, cols=3):
    gx, gy, gw, gh = grid_region
    cell_w = gw / cols
    cell_h = gh / rows

    slots = []
    for r in range(rows):
        for c in range(cols):
            index = r * cols + c + 1
            if index > 10:
                break
            sx = int(gx + c * cell_w)
            sy = int(gy + r * cell_h)
            sw = int(cell_w)
            sh = int(cell_h)
            cx = int(sx + cell_w / 2)
            cy = int(sy + cell_h / 2)
            slots.append({
                "region": (sx, sy, sw, sh),
                "center": (cx, cy),
                "index": index
            })

    return slots


def get_next_run_time_str(seconds_from_now):
    next_time = time.localtime(time.time() + seconds_from_now)
    return time.strftime('%Y-%m-%d %H:%M:%S', next_time)


# === [7. 좌표 설정 함수] ===

def _apply_resolution_coords():
    """좌표/영역은 두 모드 모두 FHD(1920x1080) 기준으로 동일하게 사용한다."""
    global COORD_FISHING_BTN, COORD_TANK_BTN, COORD_MYROOM_BTN, COORD_CONFIRM_BTN
    global REGION_Q_LEFT, REGION_Q_RIGHT, REGION_ANSWERS, REGION_VERIFY_TEXT
    global REGION_TANK_QTY, REGION_MIN_TIME

    COORD_FISHING_BTN = (1086, 988)
    COORD_TANK_BTN = (1007, 1006)
    COORD_MYROOM_BTN = (1138, 245)
    COORD_CONFIRM_BTN = (958, 575)

    REGION_Q_LEFT = (742, 406, 78, 69)
    REGION_Q_RIGHT = (830, 407, 78, 69)
    REGION_ANSWERS = (972, 433, 226, 296)
    REGION_VERIFY_TEXT = (832, 503, 255, 38)

    # 중심점 기준 ±폭. 인식 안 되면 이 박스 크기부터 조정할 것.
    # 수량은 자릿수(1~3자리)에 따라 중앙정렬로 폭이 변하므로 좌우로 넉넉히.
    REGION_TANK_QTY = (825, 989, 130, 36)  # '1/920'~'920/920' (중심 890,1007)
    REGION_MIN_TIME = (940, 916, 72, 34)   # '20초' 등


def _apply_resolution_mode(mode):
    """선택/감지된 모드('1080p'|'1440p')를 실제 상태에 반영."""
    global CURRENT_RESOLUTION, game_capture

    if mode == "1080p":
        print(" -> [설정] 1080p 모드 (화면 직접 캡처 + 윤곽선 매칭)")
        CURRENT_RESOLUTION = "1080p"
        game_capture = None
    else:
        print(" -> [설정] 1440p 모드 (WGC 캡처 -> 1080p 축소 + 윤곽선 매칭)")
        CURRENT_RESOLUTION = "1440p"

        if not _WGC_AVAILABLE:
            print("[오류] windows-capture 패키지가 필요합니다.")
            print("       'pip install windows-capture' 후 다시 실행하세요.")
            sys.exit(1)

        if not _find_game_hwnd():
            print(f"[경고] '{GAME_KEYWORD}' 창을 아직 찾지 못했습니다.")
            print("       게임을 실행한 뒤 진행하세요. (루틴 시작 시 재시도)")

        game_capture = GameCapture(WGC_WINDOW_NAME)
        print("[시스템] QHD WGC 캡처 준비 완료 (루틴 시작 시 캡처 개시)")


def setup_resolution():
    print("\n=== 해상도 설정 ===")

    _apply_resolution_coords()

    # 1) 자동 감지 시도: 게임 창이 있는 모니터로 판별
    detected = detect_resolution()
    if detected is not None:
        mode, mw, mh, is_primary = detected
        loc = "주 모니터" if is_primary else "보조 모니터"
        print(f"[자동 감지] 게임 위치: {loc} {mw}x{mh} -> {mode}")
        _apply_resolution_mode(mode)
        return

    # 2) 감지 실패(창 못 찾음) → 수동 선택 폴백
    print(f"[자동 감지 실패] '{GAME_KEYWORD}' 창을 찾지 못했습니다. 수동으로 선택하세요.")
    res_input = dual_input(
        "Enter) 1080p (FHD)  /  Any) 1440p (QHD) : ",
        "Resolution?\n0) 1080p (FHD)\nAny) 1440p (QHD)"
    )
    _apply_resolution_mode("1080p" if res_input.strip() == "" else "1440p")


# === [8. 성공 판정 함수 (OCR)] ===

def verify_fishing_success():
    start = time.time()
    while time.time() - start < 3.0:
        try:
            img_rgb = grab_region_rgb(REGION_VERIFY_TEXT)
            if img_rgb is None:
                time.sleep(0.3)
                continue

            results = reader.readtext(img_rgb, detail=0)
            detected_text = " ".join(results)
            print(f" -> [OCR 판독 결과] : '{detected_text}'")

            # OCR이 '살림망'을 '산림망'으로 읽는 경우가 있어 '림망' 부분매칭 사용
            cleaned = detected_text.replace(" ", "")
            if '림망' in cleaned:
                return True
        except Exception:
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            print(f" -> [OCR 에러] {timestamp}")
            traceback.print_exc()
        time.sleep(0.3)
    return False


# === [8.5. 살림망 감시 모드 (타이머 0)] ===

WATCH_DEBUG = False   # True로 두면 OCR 실패 시 마지막 원문을 출력(영역 보정용)


def _ensure_watch_capture():
    """
    감시 모드 OCR용 WGC 캡처 보장. FHD/QHD **둘 다 WGC로 게임 창을 직접** 읽어
    가림·포커스·창 위치에 강하게 만든다(검증: 메인/서브 모두 정확히 읽힘).
    FHD 모드라 game_capture가 없으면 여기서 생성한다.
    WGC 불가(패키지 없음/실패) 시 False → 호출측이 pyautogui로 폴백.
    """
    global game_capture
    if not _WGC_AVAILABLE:
        return False
    if game_capture is None:
        game_capture = GameCapture(WGC_WINDOW_NAME)
    if not game_capture.is_running:
        try:
            game_capture.start()
            game_capture.wait_ready(timeout=5.0)
        except Exception:
            print("[경고] 감시용 WGC 캡처 시작 실패")
            traceback.print_exc()
            return False
    return True


def _watch_grab_region(region):
    """
    감시 모드용 영역 캡처(RGB). WGC 프레임(게임 창 직접)을 우선 사용하고,
    불가 시 grab_region_rgb(FHD=화면 직접)로 폴백. 실패 시 None.
    """
    x, y, w, h = region
    if game_capture is not None and game_capture.is_running:
        frame = game_capture.get_frame_1080()
        if frame is not None:
            crop = frame[y:y + h, x:x + w]
            if crop.size:
                return cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    return grab_region_rgb(region)


def _ocr_region(region):
    """감시 영역을 최신 WGC 프레임에서 읽어 OCR 원문(공백제거) 반환. 실패 시 ''."""
    img = _watch_grab_region(region)
    if img is None:
        return ""
    return " ".join(reader.readtext(img, detail=0)).replace(" ", "")


def read_tank_quantity(retries=4, delay=0.3):
    """
    살림망 수량 'current/max'를 OCR. (current, max) 또는 None.
    숫자 갱신 애니메이션/깜빡임으로 특정 프레임에서 놓칠 수 있어, 새 프레임을
    받아가며 여러 번 재시도한다(WGC는 프레임을 계속 갱신).
    """
    last = ""
    for i in range(retries):
        txt = _ocr_region(REGION_TANK_QTY)
        if txt:
            last = txt
            # 슬래시가 공백·붙음·세로줄로 깨질 수 있어 유연하게 두 숫자 추출
            m = re.search(r'(\d+)\D+(\d+)', txt)
            if m:
                cur, mx = int(m.group(1)), int(m.group(2))
                if mx > 0 and 0 <= cur <= mx:   # OCR 글리치 방어
                    return cur, mx
        if i < retries - 1:
            time.sleep(delay)
    if WATCH_DEBUG:
        print(f"    [debug] tank OCR 마지막='{last}'")
    return None


def read_min_gain_time(retries=3, delay=0.2):
    """최소 획득 시간(초)을 OCR. int 또는 None. (재시도 포함)"""
    last = ""
    for i in range(retries):
        txt = _ocr_region(REGION_MIN_TIME)
        if txt:
            last = txt
            m = re.search(r'(\d+)', txt)
            if m:
                sec = int(m.group(1))
                if 0 < sec <= 600:           # 비정상값 방어
                    return sec
        if i < retries - 1:
            time.sleep(delay)
    if WATCH_DEBUG:
        print(f"    [debug] min OCR 마지막='{last}'")
    return None


def _detect_no_bait_popup():
    """
    '미끼가 부족합니다' 팝업 감지. 팝업 글자는 OCR 신뢰도가 낮아(0.3대)
    완전일치 대신 '미끼가'/'부족' 부분매칭만 본다. 감지되면 True.
    """
    txt = _ocr_region(REGION_NO_BAIT)
    return ("미끼가" in txt) or ("부족" in txt)


def _detect_disconnect():
    """서버 접속 끊김 대화상자 감지. 타이틀 '서버와 접속이 끊어졌습니다.'에서
    '서버와' 부분매칭(타이틀 conf 0.64, 본문은 0.01이라 타이틀만 신뢰)."""
    return "서버와" in _ocr_region(REGION_DISCONNECT)


def _detect_rod_expire_popup():
    """
    '! 아이템 기간 만료' 팝업(낚싯대 만료) 감지 — 2중 확인.
    타이틀에서 '만료'가 보이고, 팝업 속 아이템 이름에서 '낚'까지 판독돼야
    True (낚싯대가 아닌 다른 아이템 만료 팝업 오탐 방지).
    """
    if "만료" not in _ocr_region(REGION_ROD_EXPIRE):
        return False
    name_txt = _ocr_region(REGION_ROD_ITEM_NAME)
    return re.search(ROD_EXPIRE_NAME_PATTERN, name_txt) is not None


def _find_cards_by_pattern(pattern):
    """
    보유 미끼/보유 낚싯대 창(그리드 동일)의 현재 페이지에서 이름이 pattern에
    매칭되는 카드를 모두 찾는다. 이름 바 8칸을 덮는 영역을 통째로 OCR하고,
    매칭 글자의 중심을 가장 가까운 (행, 열)로 환산.
    반환: [(row, col, 인식이름), ...] 좌상단 순 정렬. 없으면 [].
    (OCR이 '개지렇미' 등으로 깨져 읽는 것까지 pattern이 흡수)
    """
    x0, y0 = REGION_BAIT_NAMES[0], REGION_BAIT_NAMES[1]
    for _ in range(2):   # 첫 프레임이 창 애니메이션 중일 수 있어 1회 재시도
        img = _watch_grab_region(REGION_BAIT_NAMES)
        results = reader.readtext(img) if img is not None else []
        found = []
        for box, text, _conf in results:
            ntext = text.replace(" ", "")
            if not re.search(pattern, ntext):
                continue
            cx = x0 + sum(p[0] for p in box) / 4.0
            cy = y0 + sum(p[1] for p in box) / 4.0
            row = min(range(len(BAIT_NAME_ROW_Y)), key=lambda r: abs(cy - BAIT_NAME_ROW_Y[r]))
            col = min(range(len(BAIT_COL_X)), key=lambda c: abs(cx - BAIT_COL_X[c]))
            if abs(cy - BAIT_NAME_ROW_Y[row]) > 30:   # 이름 바 행이 아닌 매칭 방어
                continue
            found.append((row, col, ntext))
        if found:
            return sorted(found)
        if results:                         # 글자는 읽혔는데 대상이 없음 = 이 페이지엔 없음
            return []
        time.sleep(0.5)                     # 아무 글자도 못 읽음 = 프레임 문제 → 재시도
    return []


def _use_card_and_restart(row, col):
    """카드 '사용하기' 클릭 -> ESC 2번(리스트 창 + 밑에 깔린 팝업까지 닫기)
    -> '낚시 시작' 클릭. 팝업이 남아 있으면 모달이 클릭을 삼키므로 2번 필수."""
    click_real(BAIT_USE_BTNS[row][col], delay=1.0)   # 사용하기
    press_esc(delay=0.5)                             # ESC 1: 리스트 창 닫기
    press_esc(delay=1.0)                             # ESC 2: 팝업 닫기
    click_real(COORD_FISHING_BTN, delay=1.0)         # 낚시 시작


def run_bait_swap_routine():
    """
    미끼 자동 교체 루틴 (감시 모드 전용).
    '보유 미끼' 클릭 -> 페이지를 넘기며(최대 4번) 대상 미끼 탐색 ->
    해당 칸 '사용하기' 클릭 -> ESC로 창 닫기 -> '낚시 시작' 클릭.
    끝까지 못 찾으면 현재 페이지 좌상단 미끼를 대신 사용하고 재개한다.
    클릭은 전부 click_real(FHD -> to_screen 변환)이라 QHD/창 위치 무관.
    """
    print("\n=== [미끼 자동 교체] '미끼가 부족합니다' 감지 ===")
    notify("Out of bait! Swapping bait...", priority=4, tags=["worm"])

    if not bring_game_to_front(GAME_KEYWORD):
        print("[경고] 게임 창을 찾지 못했습니다. 교체를 건너뜁니다.")
        return

    click_real(COORD_OWN_BAIT_BTN, delay=1.5)   # 팝업의 '보유 미끼'

    found = None
    for page in range(BAIT_MAX_PAGE_MOVES + 1):
        cards = _find_cards_by_pattern(BAIT_TARGET_PATTERN)
        if cards:
            row, col, ntext = cards[0]          # 좌상단 우선
            print(f" -> [미끼 감지] '{ntext}' -> {page + 1}페이지 {row + 1}행 {col + 1}열")
            found = (row, col)
            break
        if page < BAIT_MAX_PAGE_MOVES:
            print(f" -> {page + 1}페이지 미감지 — 다음 페이지로 넘깁니다.")
            click_real(COORD_BAIT_NEXT_BTN, delay=1.2)

    if found is None:
        # 5페이지까지 실패 → 마지막 페이지 좌상단 '사용하기'를 눌러 강행
        print(" -> [감지 실패] 좌상단 미끼를 대신 사용합니다.")
        notify("Target bait not found. Using top-left bait.",
               priority=4, tags=["warning"])
        found = (0, 0)

    _use_card_and_restart(*found)   # 사용하기 -> 1초 후 ESC -> 낚시 시작

    print("=== [미끼 교체 완료] 낚시를 재개합니다 ===")
    notify("Bait swapped. Fishing resumed.", priority=3, tags=["white_check_mark"])


def run_rod_swap_routine():
    """
    낚싯대 자동 교체 루틴 (감시 모드 전용).
    '! 아이템 기간 만료' 팝업 감지 시: ESC(팝업 닫기) -> 낚싯대 리스트 열기
    (815, 916) -> '스타'/'장미검' 매칭 낚싯대 탐색(둘 다 있으면 랜덤) ->
    사용하기 -> 1초 후 ESC -> 낚시 시작. (미끼와 달리 페이지 넘김 없음)
    """
    print("\n=== [낚싯대 자동 교체] '아이템 기간 만료' 감지 ===")
    notify("Rod expired! Swapping rod...", priority=4, tags=["fishing_pole_and_fish"])

    if not bring_game_to_front(GAME_KEYWORD):
        print("[경고] 게임 창을 찾지 못했습니다. 교체를 건너뜁니다.")
        return

    press_esc(delay=1.0)                        # 만료 팝업 닫기
    click_real(COORD_ROD_LIST_BTN, delay=1.5)   # 보유 낚싯대 리스트 열기

    cards = _find_cards_by_pattern(ROD_TARGET_PATTERN)
    if cards:
        row, col, ntext = random.choice(cards)  # 둘 다 찾으면 랜덤 선택
        print(f" -> [낚싯대 감지] {len(cards)}개 매칭, '{ntext}' 선택 "
              f"({row + 1}행 {col + 1}열)")
        _use_card_and_restart(row, col)
        print("=== [낚싯대 교체 완료] 낚시를 재개합니다 ===")
        notify("Rod swapped. Fishing resumed.", priority=3, tags=["white_check_mark"])
    else:
        # 대상 낚싯대를 못 찾음 → 창만 닫고 재시작 시도(수동 확인 필요할 수 있음)
        print(" -> [감지 실패] 대상 낚싯대('스타'/'장미검')를 찾지 못했습니다.")
        notify("Target rod not found! Check the game.", priority=5, tags=["sos"])
        press_esc(delay=1.0)
        click_real(COORD_FISHING_BTN, delay=1.0)


def _watch_apply_choice(choice):
    """
    감시 모드 메뉴 선택을 처리(키보드/ntfy 공통). q는 즉시 종료,
    t는 감시를 끝내고 일반 타이머 모드로 전환(복귀하지 않음),
    나머지는 설정만 바꾸고 반환한다(반환 후 호출측이 감시 재개).
    """
    global _log_enabled, _ntfy_enabled

    choice = (choice or "").strip().lower()

    if choice == 'q':
        print("\n매크로를 종료합니다.")
        sys.exit(0)
    elif choice == 't':
        # 타이머 재설정 = 감시 모드 종료 후 일반 타이머 모드로 전환.
        # (감시용 WGC 캡처는 정리. ntfy 경로에선 아직 안 멈췄을 수 있으므로 여기서.)
        if game_capture is not None and game_capture.is_running:
            game_capture.stop()
        print("\n[감시 종료] 타이머 재설정 — 일반 타이머 모드로 전환합니다.")
        handle_timer_change(0)   # 새 타이머 입력(0/o면 감시 재진입) + 즉시실행 여부
        run_timer_loop(0)        # 타이머 대기 루프(정상 종료 시 프로그램 종료)
        sys.exit(0)              # run_timer_loop 정상 반환 시 종료(감시로 복귀 안 함)
    elif choice == 'r':
        if _log_enabled:
            print(" -> [안내] 로그 저장이 이미 활성화되어 있습니다.")
        else:
            _log_enabled = True
            _tee.activate()
            print(f" -> [설정] 로그 저장 활성화! 종료 시 '{LOG_FILENAME}'에 저장됩니다.")
    elif choice == 'n':
        _ntfy_enabled = not _ntfy_enabled
        state = "활성화" if _ntfy_enabled else "비활성화"
        print(f" -> [설정] ntfy 푸시 알림이 {state}되었습니다.")
        if _ntfy_enabled:
            notify("ntfy connected!", priority=3, tags=["bell"])
    elif choice == 'd':
        setup_resolution()      # 게임 창이 있는 모니터로 모드 재감지
        notify_settings("resolution changed")
    # Enter/기타 → 아무것도 바꾸지 않고 감시 재개

    print(" -> 감시를 재개합니다.")


def _watch_menu_keyboard():
    """키 입력으로 진입하는 감시 모드 메뉴(blocking). 대기 중엔 감시하지 않음."""
    print("\n>>> [감시 일시정지] 무엇을 하시겠습니까?")
    print("    t) 타이머 재설정(감시 종료)  d) 해상도 재감지")
    print("    r) 로그 저장 활성화  n) ntfy 알림 토글  q) 종료  Enter) 감시 재개")
    choice = input("    선택: ").strip().lower()
    _watch_apply_choice(choice)


def _watch_handle_ntfy(msg):
    """ntfy 원격 명령으로 감시 모드 메뉴 조작. 알 수 없는 명령은 도움말 안내."""
    cmd = (msg or "").strip().lower()
    print(f"\n -> [ntfy 명령] '{cmd}'")
    if cmd in ('q', 't', 'r', 'n', 'd'):
        _watch_apply_choice(cmd)
    else:
        notify(
            "Watch menu:\nt) timer(exit watch)  d) resolution\n"
            "r) log  n) ntfy toggle  q) quit",
            priority=2, tags=["info"]
        )
        print(" -> 감시를 계속합니다.")


def watch_tank_mode():
    """
    타이머 0 = 살림망 감시 모드. 무한 루프(Ctrl+C로만 종료).
    살림망 수량 current/max를 주기적으로 읽어 `current >= max - 5`가 되면
    회수 루틴을 실행한다. 폴링 간격 = 최소 획득 시간(초)으로 자동 추종.
    (max-5 버퍼가 있어 한 사이클에 최대 1마리 → 그대로 폴링해도 절대 못 넘김)
    """
    print("\n=== [살림망 감시 모드] ===")
    print("살림망 수량이 (최대-5)에 도달하면 자동 회수합니다.")
    print(" * 아무 키나 누르면 메뉴 진입(감시 일시정지), Enter로 감시 재개.")
    print(" * 종료: 메뉴에서 q 또는 Ctrl+C.")
    notify("Tank watch mode started.", priority=3, tags=["eyes"])

    last_interval = 20.0   # 직전 정상 폴링 간격(초). 파싱 실패 시 이 값 유지.
    fail_streak = 0

    while True:
        _ensure_watch_capture()

        qty = read_tank_quantity()
        minsec = read_min_gain_time()

        # 폴링 간격 결정: 최소 획득 시간(초) 그대로 사용, 못 읽으면 직전 값 유지
        if minsec is not None:
            last_interval = float(minsec)
        interval = max(3.0, last_interval)   # 최소 3초 하한(CPU 보호)

        now = time.strftime('%H:%M:%S', time.localtime())
        if qty is not None:
            fail_streak = 0
            cur, mx = qty
            mstr = f"{minsec}초" if minsec is not None else "?"
            print(f"[{now}] 살림망 {cur}/{mx} (회수 기준 {mx - 5}), "
                  f"최소획득 {mstr}, 다음 확인 {interval:.0f}초 후")

            if cur >= mx - 5:
                print(" -> [회수 조건 충족] 회수 루틴을 실행합니다.")
                notify(f"Tank {cur}/{mx} -> collecting.",
                       priority=4, tags=["rotating_light"])
                # 회수 루틴은 캡처를 자체 관리(시작/종료)하므로 감시용 캡처는 정리
                if game_capture is not None and game_capture.is_running:
                    game_capture.stop()
                run_fishing_routine()
                # 회수 후 수량 리셋 → 곧바로 감시 계속. 잠깐 텀 후 재측정.
                time.sleep(3.0)
                continue

            # 매 사이클 두 팝업을 확인해 분기(수량 정체 여부와 무관).
            #  - '미끼가'(미끼 부족)  -> 미끼 자동 교체
            #  - '만료'(낚싯대 만료) -> 낚싯대 자동 교체
            # 둘 다 안 보이면 아무 표시 없이 넘어간다.
            if _detect_no_bait_popup():
                run_bait_swap_routine()
                time.sleep(2.0)
                continue
            if _detect_rod_expire_popup():
                run_rod_swap_routine()
                time.sleep(2.0)
                continue
        else:
            fail_streak += 1
            print(f"[{now}] 수량 파싱 실패({fail_streak}) — "
                  f"직전 간격 {interval:.0f}초 유지")
            # 파싱 실패 = 게임 화면이 아닐 수 있음 → 접속 끊김 대화상자 확인.
            # '서버와' 미판독이면 평소처럼 다음 사이클에 재시도.
            if _detect_disconnect():
                print("\n[긴급] 게임이 튕겼습니다")
                notify("게임이 튕겼습니다", priority=5, tags=["rotating_light"])
                if game_capture is not None and game_capture.is_running:
                    game_capture.stop()
                print("매크로를 종료합니다.")
                sys.exit(1)

        # 폴링 간격만큼 대기하되, 키/ntfy가 오면 메뉴 진입(감시 일시정지).
        # 대기 중 아무 일 없으면 다음 사이클로 넘어가 다시 측정한다.
        waited, ntfy_msg = wait_with_keycheck(interval)
        if waited == 0 and ntfy_msg is None:
            continue   # 정상 만료 → 다음 감시 사이클

        if ntfy_msg is not None:
            # 원격(ntfy) 명령: 즉시 처리(입력 블로킹 없음)
            _watch_handle_ntfy(ntfy_msg)
        else:
            # 키 입력: 감시를 멈추고(캡처 정지, CPU 절약) 메뉴 진입.
            # 메뉴를 빠져나오면 루프 top의 _ensure_watch_capture가 감시 재개.
            if game_capture is not None and game_capture.is_running:
                game_capture.stop()
            print("\n[감시 일시정지] 메뉴 진입 — 대기 중에는 살림망을 확인하지 않습니다.")
            _watch_menu_keyboard()
        # 메뉴/명령 처리 후 곧바로 다음 사이클(즉시 재측정)


# === [9. 메인 루틴] ===

def run_fishing_routine():
    print("\n=== 낚시 수거 루틴 시작 ===")

    notify(
        "Quiz routine started.",
        priority=3,
        tags=["fishing_pole_and_fish"],
    )

    if not bring_game_to_front(GAME_KEYWORD):
        print("[경고] 게임 창을 찾지 못했습니다. 루틴을 건너뜁니다.")
        return

    # QHD: WGC 캡처 시작 후 첫 프레임 대기
    capture_started = False
    if CURRENT_RESOLUTION == "1440p":
        try:
            game_capture.start()
            capture_started = True
            if not game_capture.wait_ready(timeout=5.0):
                print("[경고] WGC 첫 프레임 수신 실패 — 인식이 실패할 수 있습니다.")
        except Exception:
            print("[오류] WGC 캡처 시작 실패")
            traceback.print_exc()

    try:
        print("1. 낚시 취소")
        for _ in range(4):
            pyautogui.press('enter')
            time.sleep(0.5)
            pyautogui.press('esc')
            time.sleep(0.5)

        click_real(COORD_FISHING_BTN, delay=2)

        end_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f" -> [종료 시각] {end_time}")

        print("2. 살림망 확인")
        click_real(COORD_TANK_BTN, delay=1.5)

        answer_slots = get_answer_slot_regions(REGION_ANSWERS)

        max_retries = 10
        attempt = 0
        verify_success = False

        while attempt < max_retries:
            attempt += 1
            print(f"\n[시도 {attempt}/{max_retries}] 퀴즈 풀이 프로세스 시작")

            click_real(COORD_MYROOM_BTN, delay=2.5)

            solve_quiz_step(REGION_Q_LEFT, answer_slots, "왼쪽")
            time.sleep(0.5)
            solve_quiz_step(REGION_Q_RIGHT, answer_slots, "오른쪽")

            print("3. 완료 확인 (OCR 판독)")
            if verify_fishing_success():
                print(" -> [확인 성공] '살림망' 텍스트를 감지했습니다!")
                verify_success = True
                break

            if attempt < max_retries:
                print(" -> [확인 실패] 다시 시도합니다...")
                time.sleep(2.0)
            else:
                print(" -> [경고] 최대 재시도(10회) 초과. 강제 진행합니다.")

        print("4. 완료 확인")
        click_real(COORD_CONFIRM_BTN, delay=1)

        print("5. 낚시 다시 시작")
        start_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f" -> [시작 시각] {start_time}")

        click_real(COORD_FISHING_BTN, delay=1)

        result_str = "SUCCESS" if verify_success else f"FAIL (tried {attempt})"
        print(f"=== 루틴 완료 [{result_str}] ===")

        notify(
            f"Routine complete. Result: {result_str}",
            priority=3,
            tags=["checkered_flag"],
        )

    finally:
        if capture_started:
            game_capture.stop()


# === [10. 대기 루프 (키보드 + ntfy 동시 감시)] ===

def wait_with_keycheck(seconds):
    """
    지정된 초 동안 대기하면서 키보드 입력 또는 ntfy 메시지를 감시.
    반환값:
      (0, None)       → 타이머 정상 만료
      (남은초, None)  → 키보드 입력 감지
      (남은초, str)   → ntfy 메시지 감지 (메시지 내용 포함)
    """
    end_time = time.time() + seconds

    while True:
        remaining = end_time - time.time()
        if remaining <= 0:
            return (0, None)

        if msvcrt.kbhit():
            msvcrt.getch()
            return (int(remaining), None)

        # 네트워크 폴링은 백그라운드 스레드가 담당. 여기선 큐만 즉시 확인하므로
        # 키 입력 감시가 HTTP 요청에 막히지 않는다.
        if _ntfy_enabled:
            msg = get_ntfy_message()
            if msg is not None:
                return (int(remaining), msg)

        time.sleep(0.1)


def handle_timer_change(consecutive_failures):
    """타이머 재설정 처리."""
    global LOOP_INTERVAL

    while True:
        new_min = dual_input(
            "\n타이머를 설정해주세요(분, 0 또는 o=살림망 감시 모드): ",
            "Enter new timer (min, o=watch mode):"
        ).strip()
        # 키보드 '0'/'o' 또는 ntfy 'o'로 감시 모드 진입.
        # (ntfy '0'은 dual_input에서 ''로 변환되어 '지금 실행' 관례와 안 겹침)
        if new_min.lower() in ("0", "o"):
            watch_tank_mode()   # 무한 루프. Ctrl+C로만 탈출.
            sys.exit(0)
        if new_min.isdigit() and int(new_min) > 0:
            LOOP_INTERVAL = int(new_min) * 60
            print(f" -> 주기: {LOOP_INTERVAL}초 ({new_min}분) 재설정 완료")
            break
        else:
            print("0/o(감시) 또는 1 이상의 숫자를 입력해주세요.")

    notify_settings("timer changed")

    print("\n지금 실행하시겠습니까?")
    run_now = dual_input(
        "Enter)지금  Any)나중에 : ",
        "Run now?\n0) Now\nAny) Later"
    )
    if run_now.strip() == "":
        print("\n[즉시 실행 모드] 5초 뒤에 작업을 시작합니다.")
        time.sleep(5)
        try:
            run_fishing_routine()
            consecutive_failures = 0
        except KeyboardInterrupt:
            print("\n매크로를 종료합니다.")
            sys.exit(0)
        except Exception:
            consecutive_failures += 1
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            print(f"\n[오류] 루틴 실행 중 예외 발생 — {timestamp}")
            traceback.print_exc()

    return LOOP_INTERVAL, consecutive_failures


def handle_resolution_change(consecutive_failures):
    """해상도 재설정 처리."""
    setup_resolution()

    notify_settings("resolution changed")

    print("\n지금 실행하시겠습니까?")
    run_now = dual_input(
        "Enter)지금  Any)나중에 : ",
        "Run now?\n0) Now\nAny) Later"
    )
    if run_now.strip() == "":
        print("\n[즉시 실행 모드] 5초 뒤에 작업을 시작합니다.")
        time.sleep(5)
        try:
            run_fishing_routine()
            consecutive_failures = 0
        except KeyboardInterrupt:
            print("\n매크로를 종료합니다.")
            sys.exit(0)
        except Exception:
            consecutive_failures += 1
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            print(f"\n[오류] 루틴 실행 중 예외 발생 — {timestamp}")
            traceback.print_exc()

    return LOOP_INTERVAL, consecutive_failures


def process_menu_choice(choice, remaining, consecutive_failures):
    """
    메뉴 선택을 처리.
    반환: (remaining, consecutive_failures, should_reset_timer)
    """
    global _log_enabled, _ntfy_enabled

    if choice == 'q':
        print("\n매크로를 종료합니다.")
        sys.exit(0)

    elif choice == 'r':
        if _log_enabled:
            print(" -> [안내] 로그 저장이 이미 활성화되어 있습니다.")
        else:
            _log_enabled = True
            _tee.activate()
            print(f" -> [설정] 로그 저장 활성화! 종료 시 '{LOG_FILENAME}'에 저장됩니다.")
        print(f" -> 남은 {remaining}초 이어서 대기합니다.")
        return remaining, consecutive_failures, False

    elif choice == 'n':
        _ntfy_enabled = not _ntfy_enabled
        state = "활성화" if _ntfy_enabled else "비활성화"
        print(f" -> [설정] ntfy 푸시 알림이 {state}되었습니다.")
        if _ntfy_enabled:
            notify("ntfy connected!", priority=3, tags=["bell"])
        print(f" -> 남은 {remaining}초 이어서 대기합니다.")
        return remaining, consecutive_failures, False

    elif choice == 't':
        new_remaining, consecutive_failures = handle_timer_change(consecutive_failures)
        return new_remaining, consecutive_failures, True

    elif choice == 'd':
        new_remaining, consecutive_failures = handle_resolution_change(consecutive_failures)
        return new_remaining, consecutive_failures, True

    else:
        print(f" -> 남은 {remaining}초 이어서 대기합니다.")
        return remaining, consecutive_failures, False


def run_timer_loop(consecutive_failures=0):
    """
    LOOP_INTERVAL 주기로 회수 루틴을 반복하는 메인 타이머 대기 루프.
    대기 중 키/ntfy로 메뉴 진입 가능(process_menu_choice 재사용).
    감시 모드의 `t`(타이머 재설정)에서도 이 루프로 전환해 재사용한다.
    정상 종료(q/연속실패/Ctrl+C) 시 반환.
    """
    MAX_CONSECUTIVE_FAILURES = 5

    while True:
        remaining = LOOP_INTERVAL

        while remaining > 0:
            next_run_str = get_next_run_time_str(remaining)
            print(f"\n{int(LOOP_INTERVAL/60)}분({LOOP_INTERVAL}초) 대기 중...")
            print(f"[다음 루틴 예정] {next_run_str}")
            log_status = "ON" if _log_enabled else "OFF"
            ntfy_status = "ON" if _ntfy_enabled else "OFF"
            print(f"(아무 키: 메뉴 / 로그 저장: {log_status} / ntfy: {ntfy_status})")

            wait_result, ntfy_msg = wait_with_keycheck(remaining)

            if wait_result == 0:
                remaining = 0
                break

            remaining = wait_result

            if ntfy_msg is not None:
                cmd = ntfy_msg.strip().lower()
                print(f"\n -> [ntfy 명령] '{cmd}'")

                if cmd in ('q', 't', 'd', 'r', 'n'):
                    remaining, consecutive_failures, should_reset = \
                        process_menu_choice(cmd, remaining, consecutive_failures)
                    if should_reset:
                        remaining = LOOP_INTERVAL
                    continue
                else:
                    notify(
                        "Commands:\nt) timer  d) resolution\n"
                        "r) log  n) ntfy toggle  q) quit",
                        priority=2, tags=["info"]
                    )
                    print(f" -> 남은 {remaining}초 이어서 대기합니다.")
                    continue

            print(f"\n>>> 무엇을 하시겠습니까? (남은 대기: {remaining}초)")
            print("    t) 타이머 재설정  d) 해상도 재설정")
            print("    r) 로그 저장 활성화  n) ntfy 알림 토글  q) 종료  Enter) 계속 진행")
            choice = input("    선택: ").strip().lower()

            remaining, consecutive_failures, should_reset = \
                process_menu_choice(choice, remaining, consecutive_failures)

            if should_reset:
                remaining = LOOP_INTERVAL
                continue

        if remaining <= 0:
            try:
                run_fishing_routine()
                consecutive_failures = 0

            except KeyboardInterrupt:
                print("\n매크로를 종료합니다.")
                break

            except Exception:
                consecutive_failures += 1
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                print(f"\n[오류] 루틴 실행 중 예외 발생 — {timestamp}")
                print(f"       연속 실패 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}회")
                traceback.print_exc()

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\n[경고] 연속 {MAX_CONSECUTIVE_FAILURES}회 실패. 프로그램을 종료합니다.")
                    break

                print(f" -> 다음 주기({int(LOOP_INTERVAL/60)}분 후)에 재시도합니다.")


# === [11. 프로그램 시작 지점] ===

if __name__ == "__main__":
    print(" * 안내: 종료하려면 Ctrl+C를 누르거나 창을 닫으세요.")

    start_ntfy_poller()   # ntfy 폴링 전용 스레드 기동(활성화 여부는 스레드가 확인)

    setup_resolution()

    while True:
        try:
            input_min = input("\n타이머를 설정해주세요(분, 0 또는 o=살림망 감시 모드): ").strip()

            if input_min.lower() in ("0", "o"):
                # 살림망 감시 모드 진입 (무한 루프, Ctrl+C로만 종료)
                watch_tank_mode()
                sys.exit(0)

            if not input_min.isdigit():
                print("숫자만 입력해주세요.")
                continue

            LOOP_INTERVAL = int(input_min) * 60
            print(f" -> 주기: {LOOP_INTERVAL}초 ({input_min}분) 설정 완료")
            break

        except KeyboardInterrupt:
            sys.exit()

    print("\n지금 실행하시겠습니까?")
    mode = input("Enter)지금  Any)나중에 : ")

    notify_settings("initial setup")

    if mode == "":
        print("\n[즉시 실행 모드] 5초 뒤에 작업을 시작합니다.")
        time.sleep(5)
        run_fishing_routine()
    else:
        print(f"\n[대기 모드] {int(LOOP_INTERVAL/60)}분 뒤에 시작합니다.")

    run_timer_loop(0)