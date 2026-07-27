# -*- coding: utf-8 -*-
"""
domiman.py — 테일즈러너 낚시 자동화 GUI (낚시.py의 GUI 재구성판)
====================================================================
- Windows 전용. tkinter 기반 단일 창 GUI.
- 이 파일 + ocr_model 폴더만으로 동작한다 (낚시.py 불필요).
- 자동화 로직은 낚시.py에서 이식: FHD=pyautogui / QHD=WGC 캡처,
  윤곽선 매칭 퀴즈 풀이, 살림망 감시 모드, 미끼/낚싯대 자동 교체,
  접속 끊김 감지(GUI에서는 종료하지 않고 대기).
- ntfy 원격 제어: 같은 채널의 다른 PC(domiman)를 최상단 제어PC 버튼으로
  선택해 간접 제어. 프로토콜 규격은 [2. 설정 파일 + ntfy] 섹션 주석 참고.
  이름/채널/PC 리스트는 domiman_config.json에 보존.
- exe 패키징(콘솔 창 없이 GUI만):
    pyinstaller --noconsole --onedir --add-data "ocr_model;ocr_model" domiman.py
"""
import ctypes
import time
import sys
import os
import re
import random
import json
import queue
import socket
import subprocess
import threading
import traceback
import warnings

import tkinter as tk
from tkinter import font as tkfont

import cv2
import numpy as np
import pyautogui
import win32gui
import win32con
import win32api
import win32process
import requests

# WGC는 QHD 모드에서만 필수. FHD 전용 사용자는 설치 없이도 동작.
try:
    from windows_capture import WindowsCapture
    _WGC_AVAILABLE = True
except Exception:
    WindowsCapture = None
    _WGC_AVAILABLE = False

warnings.filterwarnings("ignore", message=".*pin_memory.*")
pyautogui.FAILSAFE = False

# ============================================================
# [0. 경로/DPI/윈도우 API 상수]
# ============================================================
if getattr(sys, 'frozen', False):
    # PyInstaller: 번들 데이터(ocr_model)는 _MEIPASS 아래. 로그는 exe 옆에.
    SCRIPT_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    LOG_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    LOG_DIR = SCRIPT_DIR

ICON_PATH = os.path.join(SCRIPT_DIR, "app.ico")   # GUI 창(제목표시줄/작업표시줄) 아이콘

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
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ============================================================
# [1. 로그 시스템 — 콘솔 없이 GUI 로그 창 + 내부 버퍼로 기록]
# ============================================================
class LogWriter:
    """sys.stdout/stderr 대체. print()가 그대로 GUI 로그로 흘러든다.
    - queue: GUI(Text 위젯)가 100ms 주기로 비움
    - buffer: 내보내기/종료 시 저장용 (x 버튼으로 지우면 저장 대상에서도 제외)
    - 콘솔(VS Code 등)이 있으면 그쪽에도 에코"""

    def __init__(self):
        self.q = queue.Queue()
        self.buffer = []
        self.lock = threading.Lock()
        self._console = sys.__stdout__   # pythonw/exe에서는 None

    def write(self, data):
        if not data:
            return
        with self.lock:
            self.buffer.append(data)
        self.q.put(data)
        if self._console:
            try:
                self._console.write(data)
                self._console.flush()   # 파이프/파일 리다이렉트 시 블록 버퍼링 방지
            except Exception:
                pass

    def flush(self):
        if self._console:
            try:
                self._console.flush()
            except Exception:
                pass

    def clear(self):
        with self.lock:
            self.buffer.clear()

    def dump(self):
        with self.lock:
            return "".join(self.buffer)


_log = LogWriter()
sys.stdout = _log
sys.stderr = _log


def _unhandled_exception_hook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"\n{'=' * 60}\n[치명적 오류] {time.strftime('%Y-%m-%d %H:%M:%S')}\n{msg}{'=' * 60}\n")


sys.excepthook = _unhandled_exception_hook


# ============================================================
# [2. 설정 파일 + ntfy.sh 프로토콜 통신 (양방향 원격 제어)]
# ------------------------------------------------------------
# 메시지 규격 (같은 채널의 PC들이 이름으로 서로를 지목, 대소문자 구분):
#   명령:   "(대상PC),(명령)[,인자...]"   예) GOD3,S / GOD3,T,30
#   응답:   "(요청자PC),Z,..."            예) BGOD,Z,0,1080,a,f,t,t
#           (요청자가 무명(휴대폰 등)이면 ",Z,..." 로 시작) /.
#   보고:   ",Z,F,(코드)[,(서브)]"        예) ,Z,F,y,b (미끼 교체 성공)
# 발신자 식별은 ntfy Title(=발신 PC 이름). 규격 외 메시지는 무시.
# ============================================================
CONFIG_PATH = os.path.join(LOG_DIR, "domiman_config.json")
NTFY_SERVER = "https://ntfy.sh"
DEFAULT_NTFY_TOPIC = "talesrunnerisgood"
_ntfy_enabled = False


def load_config():
    """설정 파일 로드. 없거나 깨졌으면 빈 dict(기본값 사용)."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fp:
            cfg = json.load(fp)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(name, channel, pc_list):
    """이름/채널/저장된 PC 리스트를 설정 파일에 보존."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump({"name": name, "channel": channel, "pc_list": pc_list},
                      fp, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 설정 저장 실패: {e}")


# 기본 이름: 호스트명(영문+숫자가 아니면 "TR"). 설정 파일 값이 있으면 그것을 우선.
_default_name = socket.gethostname()
if not re.fullmatch(r"[A-Za-z0-9]+", _default_name or ""):
    _default_name = "TR"
_cfg = load_config()
PC_NAME = _cfg.get("name") if re.fullmatch(
    r"[A-Za-z0-9]+", str(_cfg.get("name") or "")) else _default_name
NTFY_TOPIC = _cfg.get("channel") if re.fullmatch(
    r"[A-Za-z0-9_\-]+", str(_cfg.get("channel") or "")) else DEFAULT_NTFY_TOPIC
NTFY_URL = f"{NTFY_SERVER}/{NTFY_TOPIC}"


def ntfy_send(body, wait=False):
    """프로토콜 메시지 발신(Title=내 PC 이름). 기본 비동기(GUI 블로킹 방지).
    ntfy 비활성화 상태면 무시."""
    if not _ntfy_enabled:
        return

    def _run():
        try:
            requests.post(NTFY_URL, data=body.encode("utf-8"),
                          headers={"Title": PC_NAME, "Priority": "3"}, timeout=10)
            print(f"[ntfy 발신] {body}")
        except Exception as e:
            print(f"[ntfy 발신 실패] {body} ({e})")

    if wait:
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()


def send_report(code):
    """상황 보고 ',Z,F,(코드)' 발신 — 낚시 루틴(워커 스레드)에서 동기 호출.
    코드: s(루틴 시작) g(회수 성공) f(회수 실패)
          rs/bs(낚싯대/미끼 교체 시작) y,r/y,b(낚싯대/미끼 교체 성공)
          x,d/x,r/x,b(튕김/낚싯대/미끼 실패)"""
    ntfy_send(f",Z,F,{code}", wait=True)


_ntfy_queue = queue.Queue()
_ntfy_stream_resp = None          # 현재 스트리밍 응답(외부에서 중단할 핸들)
_ntfy_stream_lock = threading.Lock()


def ntfy_stream_disconnect():
    """열려 있는 스트리밍 연결을 강제 종료.
    ntfy 비활성화·채널 변경 시 메인 스레드가 호출하면, 스트림 스레드가
    블로킹 읽기에서 즉시 풀려 나와 조건(중단/재연결)을 다시 판단한다.
    (keepalive만 기다리면 반응이 최대 수십 초 늦으므로 능동적으로 끊는다.)"""
    global _ntfy_stream_resp
    with _ntfy_stream_lock:
        r, _ntfy_stream_resp = _ntfy_stream_resp, None
    if r is not None:
        try:
            r.close()
        except Exception:
            pass


def _ntfy_stream_loop():
    """전용 데몬 스레드: ntfy 스트리밍 구독을 지속 유지한다(폴링 대체).

    반복 GET 폴링(5초당 1요청) 대신 연결 하나(`/json` 스트림)를 열어두고
    도착하는 메시지를 실시간 수신한다. 요청 토큰을 **연결당 1회만** 소비하므로
    (ntfy 무료 한도는 GET/POST가 같은 IP 버킷을 공유·5~10초당 1개 충전),
    같은 공유기(같은 공인 IP)를 쓰는 휴대폰의 발신이 한도에 밀려 실패하던
    문제를 없애고 수신 지연도 폴링(최대 5초)에서 사실상 즉시로 줄인다.

    수신 즉시 큐에 넣기만 하고, 실제 프로토콜 처리(응답 매칭/명령 실행)는
    기존과 동일하게 메인 스레드(_poll_ntfy_queue)가 한다.

    ※ since는 쓰지 않는다 — 살아있는 연결은 메시지를 정확히 한 번만 전달하므로,
      재연결 시 과거 명령을 되받아 **중복 실행**할 위험이 없다. 재연결 공백에
      끼어 놓친 명령은 15초 응답 타임아웃 후 재시도로 처리한다(중복 실행보다
      깔끔한 실패-재시도가 안전).
    ※ 자기 이름(PC_NAME) Title로 되돌아오는 자기 발신분은 여기서 걸러낸다
      (기존 poll_ntfy와 동일)."""
    global _ntfy_stream_resp
    while True:
        if not _ntfy_enabled:
            time.sleep(0.5)
            continue
        active_topic = NTFY_TOPIC
        url = f"{NTFY_SERVER}/{active_topic}/json"
        resp = None
        try:
            # connect=10s / read=90s. 정상 연결은 ntfy keepalive(기본 45s)가
            # 계속 도착해 유지된다. keepalive마저 끊기면 read 타임아웃으로 재연결.
            resp = requests.get(url, stream=True, timeout=(10, 90))
            if resp.status_code == 200:
                with _ntfy_stream_lock:
                    _ntfy_stream_resp = resp
                for line in resp.iter_lines(decode_unicode=True):
                    # 비활성화·채널 변경이면 이 연결을 끊고 루프 top에서 재판단
                    if not _ntfy_enabled or NTFY_TOPIC != active_topic:
                        break
                    if not line:
                        continue                     # keepalive 사이 빈 줄
                    try:
                        data = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if data.get("event") != "message":
                        continue                     # open/keepalive 이벤트 무시
                    title = data.get("title", "")
                    if title == PC_NAME:
                        continue                     # 내가 보낸 것 제외
                    _ntfy_queue.put((title, data.get("message", "").strip()))
        except Exception:
            pass          # 네트워크 블립·강제 종료 → finally 후 재연결
        finally:
            with _ntfy_stream_lock:
                _ntfy_stream_resp = None
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        if _ntfy_enabled:
            time.sleep(1.0)   # 재연결 전 짧은 대기(연타 방지)


# ============================================================
# [2-1. 버전 확인 + 수동 업데이트 (GitHub raw 파일)]
# ------------------------------------------------------------
# 버전 문자열 = "YYMMDD" + 알파벳 1글자(a,b,c...), 예) "260725a" < "260725b"
# < "260726a". 자릿수가 고정이라 문자열 비교 그대로가 날짜순+알파벳순과 같다.
# 리포(cheongbaek/domiman)의 version.txt가 APP_VERSION보다 "크면" 업데이트
# 대상. 업데이트는 항상 사용자가 버튼을 눌러야만 확인/적용된다(자동 없음).
# exe 배포판은 launcher.py(빌드되는 domiman.exe 본체)가 이 파일(domiman.py)을
# 매 실행마다 runpy로 그대로 읽어서 돌리는 구조라, 업데이트는 domiman.py만
# 통째로 교체하면 된다(실행 중인 exe 자체는 덮어쓸 수 없는 Windows 제약을
# 이렇게 피한다). frozen 상태에서 재시작은 exe(launcher) 자신을 다시 띄우는
# 것으로 충분 — 재시작된 launcher가 방금 교체된 새 domiman.py를 다시 읽는다.
# ============================================================
APP_VERSION = "260728a"
UPDATE_REPO = "cheongbaek/domiman"
UPDATE_BRANCH = "main"
UPDATE_RAW_BASE = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}"


def fetch_latest_version():
    """리포의 version.txt를 읽어온다. 형식이 다르거나 통신 실패면 None."""
    try:
        resp = requests.get(f"{UPDATE_RAW_BASE}/version.txt", timeout=10)
        resp.raise_for_status()
        v = resp.text.strip()
        return v if re.fullmatch(r"\d{6}[a-z]", v) else None
    except Exception:
        return None


def download_latest_source():
    """리포의 domiman.py 원본 전체를 텍스트로 받아온다. 실패면 None."""
    try:
        resp = requests.get(f"{UPDATE_RAW_BASE}/domiman.py", timeout=15)
        resp.raise_for_status()
        return resp.text if resp.text.strip() else None
    except Exception:
        return None


def apply_update_and_restart(new_source):
    """새 소스로 이 파일을 원자적으로 교체하고 재시작한다.
    쓰다가 중단돼도 os.replace 직전까지는 원본이 그대로 남아 안전하다.
    성공하면 반환하지 않음(재시작 프로세스 기동 후 os._exit).
    frozen(exe)이면 launcher(domiman.exe) 자신을 인자 없이 재기동해 새
    domiman.py를 다시 읽게 하고, 스크립트 모드면 같은 인터프리터로
    domiman.py를 직접 재실행한다."""
    target = os.path.abspath(__file__)
    tmp = target + ".new"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(new_source)
    os.replace(tmp, target)
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable])
    else:
        subprocess.Popen([sys.executable, target])
    os._exit(0)   # 새 프로세스를 이미 띄웠으므로 네이티브 스레드 정리 대기 없이 즉시 종료


# ============================================================
# [3. 좌표 및 설정 — 전부 FHD(1920x1080) 기준 (낚시.py와 동일)]
# ============================================================
COORD_FISHING_BTN = (1086, 988)   # 낚시 취소/시작 토글
COORD_TANK_BTN = (1007, 1006)     # 살림망 확인
COORD_MYROOM_BTN = (1138, 245)    # 마이룸 보내기
COORD_CONFIRM_BTN = (958, 575)    # 확인(팝업)
REGION_FISHING_BTN = (1040, 983, 88, 40)   # 위 버튼 글자('취소'/'시작' 부분매칭, 실측+여유)

REGION_Q_LEFT = (742, 406, 78, 69)
REGION_Q_RIGHT = (830, 407, 78, 69)
REGION_ANSWERS = (972, 433, 226, 296)
REGION_VERIFY_TEXT = (832, 503, 255, 38)

REGION_TANK_QTY = (825, 989, 130, 36)   # 살림망 수량 'cur/max'
REGION_MIN_TIME = (940, 916, 72, 34)    # 최소 획득 시간 'n초'

# --- 미끼 자동 교체 ---
REGION_NO_BAIT = (890, 499, 142, 36)      # '미끼가 부족합니다' 팝업
COORD_BAIT_LIST_BTN = (903, 913)          # 보유 미끼 리스트 열기(직접 진입, 실측)
COORD_BAIT_NEXT_BTN = (1291, 565)         # 다음 페이지 화살표
REGION_BAIT_NAMES = (640, 483, 640, 202)  # 카드 이름 바 8칸(2행x4열)
BAIT_USE_BTNS = [
    [(708, 544), (874, 543), (1038, 540), (1209, 541)],
    [(714, 702), (877, 699), (1042, 702), (1201, 701)],
]
BAIT_COL_X = (715, 878, 1041, 1204)
BAIT_NAME_ROW_Y = (503, 660)
BAIT_TARGET_PATTERN = r"[갯개]지[렁렇령]"   # '갯지렁이' OCR 오인식 흡수
BAIT_MAX_PAGE_MOVES = 4

# --- 낚싯대 자동 교체 ---
COORD_ROD_LIST_BTN = (814, 917)             # 보유 낚싯대 리스트 열기(직접 진입, 실측)
ROD_TARGET_PATTERN = r"매직|스타|장미|푸"   # 조각 중 하나라도 걸리면 채택(오인식에 강함)

# --- 접속 끊김 감지 ---
REGION_DISCONNECT = (769, 386, 258, 40)     # '서버와 접속이 끊어졌습니다.'

GAME_KEYWORD = "tales runner"
WGC_WINDOW_NAME = "Tales Runner"
GAME_HWND = None
CURRENT_RESOLUTION = None    # "1080p" | "1440p" | None

reader = None                # easyocr Reader (백그라운드 로드)


# ============================================================
# [4. 상태 문구 (엑셀 J8:K18 그대로)]
# ============================================================
STATUS_TEXT = {
    "loading":    "강태공이 낚시터에 들어오고 있습니다.",
    "idle":       "강태공이 낚시를 준비합니다.",
    "fishing":    "강태공이 낚시를 시작했습니다.",
    "collect":    "강태공이 월척을 낚았습니다.",
    "collect3":   "강태공이 3초 후에 낚싯대를 올리기 시작합니다.",
    "rod":        "강태공이 낚싯대를 재정비합니다.",
    "bait":       "강태공이 미끼를 바꿉니다.",
    "parsefail":  "강태공이 볼일을 보러 나갔습니다.",
    "ocrfail":    "강태공이 오던 중 교통사고를 당했습니다.",
    "nowindow":   "강태공이 낚싯대를 잃어버렸습니다.",
    "disconnect": "강태공이 물에 빠졌습니다.",
    "noresp":     "강태공이 의식을 잃었습니다.",
}

# 원격 보고(,Z,F,*) -> 로그 문구/상태 변환표. {name}=제어 중인 PC 이름.
REPORT_TEXT = {
    ("s",):      "{name}의 낚시 루틴이 시작되었습니다.",
    ("g",):      "{name}의 살림망 회수가 완료되었습니다.",
    ("f",):      "{name}의 살림망 회수가 실패하였습니다.",
    ("rs",):     "{name}이(가) 낚싯대 교체를 시작합니다.",
    ("y", "r"):  "{name}의 낚싯대 교체가 성공하였습니다.",
    ("x", "r"):  "{name}의 낚싯대 교체가 실패하였습니다.",
    ("bs",):     "{name}이(가) 미끼 교체를 시작합니다.",
    ("y", "b"):  "{name}의 미끼 교체가 성공하였습니다.",
    ("x", "b"):  "{name}의 미끼 교체가 실패하였습니다.",
    ("x", "d"):  "{name}의 게임 연결이 끊겼습니다.",
}
REPORT_STATUS = {
    ("s",): "collect", ("g",): "fishing", ("f",): "fishing",
    ("rs",): "rod", ("y", "r"): "fishing", ("x", "r"): "fishing",
    ("bs",): "bait", ("y", "b"): "fishing", ("x", "b"): "fishing",
    ("x", "d"): "disconnect",
}

_status_cb = None   # GUI가 등록하는 콜백(스레드 안전 처리 포함)


def set_status(key):
    if _status_cb:
        _status_cb(key)


# ============================================================
# [5. WGC 백그라운드 캡처 (낚시.py 이식)]
# ============================================================
class GameCapture:
    """WGC로 게임 창을 연속 캡처, 최신 프레임 보관. get_frame_1080()이 축소본 반환."""

    def __init__(self, window_name):
        self.window_name = window_name
        self._latest = None
        self._lock = threading.Lock()
        self._control = None
        self._running = False

    def start(self):
        if self._running:
            return
        if not _WGC_AVAILABLE:
            raise RuntimeError("windows-capture 패키지가 없습니다.")
        cap = WindowsCapture(cursor_capture=False, draw_border=False,
                             window_name=self.window_name)

        @cap.event
        def on_frame_arrived(frame, capture_control):
            bgr = cv2.cvtColor(frame.frame_buffer, cv2.COLOR_BGRA2BGR)
            with self._lock:
                self._latest = bgr

        @cap.event
        def on_closed():
            pass

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


game_capture = None


# ============================================================
# [6. 창 탐색 / 해상도 감지 / 좌표 변환 / 입력 (낚시.py 이식)]
# ============================================================
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
    """진짜 렌더링 창 선택(숨은 중복 창 제외, 최소화 아닌 창 우선)."""
    wins = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if keyword.lower() not in win32gui.GetWindowText(hwnd).lower():
                return
            ico = bool(win32gui.IsIconic(hwnd))
            cl, ct, cr, cbm = win32gui.GetClientRect(hwnd)
            wins.append((hwnd, ico, cr - cl, cbm - ct))
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
    return max(pool, key=lambda x: x[2] * x[3])[0]


def _monitor_scale_is_100(hmon):
    try:
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(
            int(hmon), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return dpi_x.value == 96
    except Exception:
        return True


def detect_resolution(keyword=GAME_KEYWORD):
    """게임 창이 있는 모니터로 모드 판별. (mode, mw, mh, is_primary) 또는 None."""
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
    """FHD 좌표 region의 화면 조각을 RGB numpy로. 실패 시 None."""
    x, y, w, h = region
    if CURRENT_RESOLUTION == "1080p":
        shot = pyautogui.screenshot(region=region)
        return np.array(shot)
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
    if not hwnd:
        print("[경고] 게임 창을 찾지 못해 좌표 보정을 건너뜁니다.")
        return int(fx), int(fy)
    cl, ct, cr, cbm = win32gui.GetClientRect(hwnd)
    cw, ch = cr - cl, cbm - ct
    ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
    return int(ox + fx / 1920.0 * cw), int(oy + fy / 1080.0 * ch)


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


def press_key(vk, delay=0.5, label=""):
    """키를 게임에 전송(클릭과 동일한 ctypes 채널) 후 delay초 대기."""
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    if label:
        print(f"[Key] {label}")
    time.sleep(delay)


def press_esc(delay=1.0):
    press_key(VK_ESCAPE, delay, "ESC")


def _force_foreground(hwnd, timeout=2.0):
    """창을 확실히 포그라운드로(AttachThreadInput + ALT 트릭). 이미 앞이면 무조작."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
    except Exception:
        pass
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
        try:
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)
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
            cl, ct, cr, cbm = win32gui.GetClientRect(hwnd)
            candidates.append((hwnd, (cr - cl) * (cbm - ct)))
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

    candidates.sort(key=lambda c: c[1], reverse=True)
    target_hwnd = candidates[0][0]
    GAME_HWND = target_hwnd
    try:
        real_title = win32gui.GetWindowText(target_hwnd)
        print(f"\n[시스템] '{real_title}' 창을 찾았습니다! 화면 앞으로 불러옵니다.")
    except Exception:
        print("\n[시스템] 대상 창을 찾았습니다. 화면 앞으로 불러옵니다.")

    for _ in range(3):
        if _force_foreground(target_hwnd):
            time.sleep(0.5)
            return True
        time.sleep(0.4)
    print("[경고] 포커스 전환 실패 → 창 전환 없이 루틴 계속")
    return True


# ============================================================
# [7. 윤곽선 매칭 퀴즈 풀이 (낚시.py 이식)]
# ============================================================
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
    return cv2.resize(mask[y:y + bh, x:x + bw], (64, 64), interpolation=cv2.INTER_AREA)


def solve_quiz_step(region_q, answer_slots, side_label=""):
    print(f"  [{side_label}] 실루엣 분석 중... (윤곽선 매칭)")
    try:
        q_mask = get_normalized_fish_mask(region_q)
        if q_mask is None:
            print("    [실패] 실루엣 마스크 추출 실패")
            return False
        best_center, best_index, best_diff = None, -1, float('inf')
        for slot in answer_slots:
            ans_mask = get_normalized_fish_mask(slot["region"])
            if ans_mask is None:
                continue
            diff = np.mean(cv2.absdiff(q_mask, ans_mask))
            if diff < best_diff:
                best_diff, best_center, best_index = diff, slot["center"], slot["index"]
        if best_center and best_diff < 80.0:
            print(f"    매칭 답안: {best_index}번 칸 (유사도 오차: {best_diff:.1f})")
            click_real(best_center, delay=0.5)
            return True
        elif best_center:
            print(f"    [경고] 오차가 높음 ({best_diff:.1f}), 그래도 최선: {best_index}번 칸")
            click_real(best_center, delay=0.5)
            return True
        print("    [실패] 매칭 답안을 찾지 못함")
        return False
    except Exception:
        print(f"    [에러] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        traceback.print_exc()
        return False


def get_answer_slot_regions(grid_region, rows=4, cols=3):
    gx, gy, gw, gh = grid_region
    cell_w, cell_h = gw / cols, gh / rows
    slots = []
    for r in range(rows):
        for c in range(cols):
            index = r * cols + c + 1
            if index > 10:
                break
            sx, sy = int(gx + c * cell_w), int(gy + r * cell_h)
            slots.append({
                "region": (sx, sy, int(cell_w), int(cell_h)),
                "center": (int(sx + cell_w / 2), int(sy + cell_h / 2)),
                "index": index,
            })
    return slots


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
            if '림망' in detected_text.replace(" ", ""):
                return True
        except Exception:
            print(f" -> [OCR 에러] {time.strftime('%Y-%m-%d %H:%M:%S')}")
            traceback.print_exc()
        time.sleep(0.3)
    return False


# ============================================================
# [8. 감시 모드 OCR 헬퍼 (낚시.py 이식)]
# ============================================================
def _ensure_watch_capture():
    """감시용 WGC 캡처 보장(FHD/QHD 공통 — 가림·포커스에 강함)."""
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
    x, y, w, h = region
    if game_capture is not None and game_capture.is_running:
        frame = game_capture.get_frame_1080()
        if frame is not None:
            crop = frame[y:y + h, x:x + w]
            if crop.size:
                return cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    return grab_region_rgb(region)


def _ocr_region(region):
    img = _watch_grab_region(region)
    if img is None:
        return ""
    return " ".join(reader.readtext(img, detail=0)).replace(" ", "")


# 마지막으로 파싱한 살림망 수량. (cur, mx)면 성공, None이면 직전 실패/미파싱.
# 감시 루프가 매 사이클 갱신하고, '실시간 수량확인' 버튼(_query_tank)이 읽는다.
_last_tank = None


def read_tank_quantity(retries=4, delay=0.3):
    """살림망 수량 (current, max) 또는 None. 프레임 재시도 포함."""
    for i in range(retries):
        txt = _ocr_region(REGION_TANK_QTY)
        if txt:
            m = re.search(r'(\d+)\D+(\d+)', txt)
            if m:
                cur, mx = int(m.group(1)), int(m.group(2))
                if mx > 0 and 0 <= cur <= mx:
                    return cur, mx
        if i < retries - 1:
            time.sleep(delay)
    return None


def read_min_gain_time(retries=3, delay=0.2):
    """최소 획득 시간(초) 또는 None."""
    for i in range(retries):
        txt = _ocr_region(REGION_MIN_TIME)
        if txt:
            m = re.search(r'(\d+)', txt)
            if m:
                sec = int(m.group(1))
                if 0 < sec <= 600:
                    return sec
        if i < retries - 1:
            time.sleep(delay)
    return None


def _detect_no_bait_popup():
    """'미끼가 부족합니다' 팝업 감지 ('미끼가'/'부족' 부분매칭)."""
    txt = _ocr_region(REGION_NO_BAIT)
    return ("미끼가" in txt) or ("부족" in txt)


def _detect_disconnect():
    """서버 접속 끊김 대화상자 감지 ('서버와' 부분매칭)."""
    return "서버와" in _ocr_region(REGION_DISCONNECT)


def is_fishing_active():
    """낚시 진행 여부. COORD_FISHING_BTN 글자가 '취소'면 진행 중(True),
    '시작'이면 대기 중(False). 둘 다 판독 안 되면 None(호출측이 무시)."""
    txt = _ocr_region(REGION_FISHING_BTN)
    if "취소" in txt:
        return True
    if "시작" in txt:
        return False
    print(f"[낚시 상태 판독 실패] REGION_FISHING_BTN OCR 원문='{txt}'")
    return None


def _find_cards_by_pattern(pattern):
    """보유 미끼/낚싯대 창(그리드 동일)에서 이름 매칭 카드 탐색.
    [(row, col, 이름), ...] 좌상단 순. 없으면 []."""
    x0, y0 = REGION_BAIT_NAMES[0], REGION_BAIT_NAMES[1]
    for _ in range(2):
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
            if abs(cy - BAIT_NAME_ROW_Y[row]) > 30:
                continue
            found.append((row, col, ntext))
        if found:
            return sorted(found)
        if results:
            return []
        time.sleep(0.5)
    return []


def _use_card_and_restart(row, col):
    """'사용하기' 클릭 -> ESC 2번(리스트 창 + 밑에 깔린 팝업) -> '낚시 시작'."""
    click_real(BAIT_USE_BTNS[row][col], delay=1.0)
    press_esc(delay=0.5)
    press_esc(delay=1.0)
    click_real(COORD_FISHING_BTN, delay=1.0)


def _resume_fishing():
    """ESC 3번(혹시 떠있을 팝업 정리) -> '낚시 시작' 클릭. is_fishing_active()가
    False(대기 중)로 확인된 경우에만 호출할 것."""
    press_esc(delay=0.5)
    press_esc(delay=0.5)
    press_esc(delay=0.5)
    click_real(COORD_FISHING_BTN, delay=1.0)


# ============================================================
# [9. 자동화 루틴 (낚시.py 이식 — GUI 상태 연동)]
# ============================================================
def run_bait_swap_routine():
    """미끼 자동 교체: ESC 3회(혹시 떠있을 팝업 정리) -> 보유 미끼 리스트 직접
    진입 -> 페이지 탐색(최대 4번 넘김) -> 사용 -> 재개.
    대상 미감지로 좌상단 폴백을 쓰면 실패(x,b)로 보고한다."""
    print("\n=== [미끼 자동 교체] '미끼가 부족합니다' 감지 ===")
    set_status("bait")
    send_report("bs")          # 미끼 교체 시작 보고

    if not bring_game_to_front(GAME_KEYWORD):
        set_status("nowindow")
        print("[경고] 게임 창을 찾지 못했습니다. 교체를 건너뜁니다.")
        return

    press_esc(delay=0.5)
    press_esc(delay=0.5)
    press_esc(delay=0.5)
    click_real(COORD_BAIT_LIST_BTN, delay=1.5)

    found = None
    for page in range(BAIT_MAX_PAGE_MOVES + 1):
        cards = _find_cards_by_pattern(BAIT_TARGET_PATTERN)
        if cards:
            row, col, ntext = cards[0]
            print(f" -> [미끼 감지] '{ntext}' -> {page + 1}페이지 {row + 1}행 {col + 1}열")
            found = (row, col)
            break
        if page < BAIT_MAX_PAGE_MOVES:
            print(f" -> {page + 1}페이지 미감지 — 다음 페이지로 넘깁니다.")
            click_real(COORD_BAIT_NEXT_BTN, delay=1.2)

    fallback = found is None
    if fallback:
        print(" -> [감지 실패] 좌상단 미끼를 대신 사용합니다.")
        found = (0, 0)

    _use_card_and_restart(*found)
    send_report("x,b" if fallback else "y,b")
    print("=== [미끼 교체 완료] 낚시를 재개합니다 ===")
    set_status("fishing")


def run_rod_swap_routine():
    """낚싯대 자동 교체: ESC 3회(혹시 떠있을 팝업 정리) -> 리스트 직접 진입 ->
    '매직'/'스타'/'장미'/'푸' 중 하나라도 걸리면 채택.
    대상 미감지 시 좌상단 폴백을 쓰고 실패(x,r)로 보고한다."""
    print("\n=== [낚싯대 자동 교체] 최소 획득 시간 1초 감지 ===")
    set_status("rod")
    send_report("rs")          # 낚싯대 교체 시작 보고

    if not bring_game_to_front(GAME_KEYWORD):
        set_status("nowindow")
        print("[경고] 게임 창을 찾지 못했습니다. 교체를 건너뜁니다.")
        return

    press_esc(delay=0.5)
    press_esc(delay=0.5)
    press_esc(delay=0.5)
    click_real(COORD_ROD_LIST_BTN, delay=1.5)

    cards = _find_cards_by_pattern(ROD_TARGET_PATTERN)
    if cards:
        row, col, ntext = random.choice(cards)
        print(f" -> [낚싯대 감지] {len(cards)}개 매칭, '{ntext}' 선택 ({row + 1}행 {col + 1}열)")
        _use_card_and_restart(row, col)
        send_report("y,r")
        print("=== [낚싯대 교체 완료] 낚시를 재개합니다 ===")
    else:
        print(" -> [감지 실패] 좌상단 낚싯대를 대신 사용합니다.")
        _use_card_and_restart(0, 0)
        send_report("x,r")
        print("=== [낚싯대 교체(폴백)] 낚시를 재개합니다 ===")
    set_status("fishing")


def run_fishing_routine():
    """살림망 수거(퀴즈 풀이) 루틴. 성공 여부와 무관하게 낚시를 재시작한다."""
    print("\n=== 낚시 수거 루틴 시작 ===")
    set_status("collect")

    if not bring_game_to_front(GAME_KEYWORD):
        set_status("nowindow")
        print("[경고] 게임 창을 찾지 못했습니다. 루틴을 건너뜁니다.")
        return

    send_report("s")

    capture_started = False
    if CURRENT_RESOLUTION == "1440p" and game_capture is not None:
        try:
            if not game_capture.is_running:
                game_capture.start()
                capture_started = True
            if not game_capture.wait_ready(timeout=5.0):
                print("[경고] WGC 첫 프레임 수신 실패 — 인식이 실패할 수 있습니다.")
        except Exception:
            print("[오류] WGC 캡처 시작 실패")
            traceback.print_exc()

    try:
        # 낚시 취소(진행 중)면 기존 그대로, 낚시 시작(대기 중)이 확인되면
        # 취소할 게 없으므로 ESC/Enter+취소 클릭을 생략하고 바로 수량 확인으로.
        # 판독 불가(None)면 안전하게 기존 동작(진행 중 가정) 유지.
        if is_fishing_active() is not False:
            print("1. 낚시 취소")
            for _ in range(4):
                press_key(VK_RETURN, 0.5)
                press_key(VK_ESCAPE, 0.5)

            click_real(COORD_FISHING_BTN, delay=2)
            print(f" -> [종료 시각] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("1. '낚시 시작' 상태 확인 — 취소 절차 생략")

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
        print(f" -> [시작 시각] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        click_real(COORD_FISHING_BTN, delay=1)

        result_str = "SUCCESS" if verify_success else f"FAIL (tried {attempt})"
        print(f"=== 루틴 완료 [{result_str}] ===")
        send_report("g" if verify_success else "f")
        set_status("fishing")
    finally:
        if capture_started:
            game_capture.stop()


# ============================================================
# [10. 워커 스레드 — 감시 모드 / 타이머 모드]
# ============================================================
class FishingWorker(threading.Thread):
    """낚시 자동화 실행 스레드. stop_event로 중지. GUI와는 큐/콜백으로만 통신."""

    def __init__(self, mode, interval_min, rod_swap, bait_swap, on_end):
        super().__init__(daemon=True)
        self.mode = mode                  # 'watch' | 'timer'
        self.interval_min = interval_min  # timer 모드 주기(분)
        self.rod_swap = rod_swap
        self.bait_swap = bait_swap
        self.on_end = on_end              # 종료 콜백(메인 스레드로 전달됨)
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            if self.mode == 'watch':
                self._watch_loop()
            else:
                self._timer_loop()
        except Exception:
            print("[오류] 자동화 루프에서 예외 발생")
            traceback.print_exc()
        finally:
            if game_capture is not None and game_capture.is_running:
                game_capture.stop()
            self.on_end()

    # --- 살림망 감시 모드 (낚시.py watch_tank_mode 이식) ---
    def _watch_loop(self):
        global _last_tank
        print("\n=== [살림망 감시 모드] ===")
        print("살림망 수량이 (최대-5)에 도달하면 자동 회수합니다.")
        set_status("fishing")

        last_interval = 20.0
        fail_streak = 0
        same_qty = None
        same_count = 0

        while not self.stop_event.is_set():
            _ensure_watch_capture()

            qty = read_tank_quantity()
            _last_tank = qty          # 실시간 수량확인 버튼용 캐시(성공=(cur,mx)/실패=None)
            minsec = read_min_gain_time()

            if minsec is not None:
                last_interval = float(minsec)
            interval = max(3.0, last_interval)

            now = time.strftime('%H:%M:%S')
            if qty is not None:
                fail_streak = 0
                set_status("fishing")
                cur, mx = qty

                if qty == same_qty:
                    same_count += 1
                else:
                    same_qty, same_count = qty, 1

                if cur >= mx - 5:
                    print(" -> [회수 조건 충족] 회수 루틴을 실행합니다.")
                    if game_capture is not None and game_capture.is_running:
                        game_capture.stop()
                    run_fishing_routine()
                    self.stop_event.wait(3.0)
                    continue

                # 매 사이클 팝업 확인(체크박스로 개별 on/off)
                if self.bait_swap and _detect_no_bait_popup():
                    run_bait_swap_routine()
                    self.stop_event.wait(2.0)
                    continue
                if self.rod_swap and minsec == 1:
                    run_rod_swap_routine()
                    self.stop_event.wait(2.0)
                    continue

                # 살림망 수량이 3회 이상 그대로 — 낚시가 멈춰있을 수 있으니 확인
                if same_count >= 3:
                    active = is_fishing_active()
                    if active is False:
                        print(f" -> [낚시 정지 감지] 살림망 수량이 {same_count}회 연속 "
                              f"동일 + '낚시 시작' 확인 — 재개합니다.")
                        _resume_fishing()
                        self.stop_event.wait(2.0)
                        continue
            else:
                fail_streak += 1
                set_status("parsefail")
                print(f"[{now}] 수량 파싱 실패({fail_streak}) — "
                      f"직전 간격 {interval:.0f}초 유지")
                # 접속 끊김 확인 — GUI에서는 종료하지 않고 '대기'로 전환
                if _detect_disconnect():
                    print("\n[긴급] 게임이 튕겼습니다")
                    send_report("x,d")
                    set_status("disconnect")
                    print("[안내] 낚시를 중지하고 대기합니다. 게임 재접속 후 "
                          "'자동 감지' -> '시작'을 눌러 재개하세요.")
                    return

            self.stop_event.wait(interval)

    # --- 일반 타이머 모드 ---
    def _timer_loop(self):
        interval_sec = self.interval_min * 60.0
        print(f"\n=== [타이머 모드] {self.interval_min:g}분 주기 ===")
        set_status("fishing")

        while not self.stop_event.is_set():
            run_fishing_routine()
            if self.stop_event.is_set():
                break
            next_time = time.strftime('%H:%M:%S', time.localtime(time.time() + interval_sec))
            print(f"[대기] 다음 실행: {next_time} ({self.interval_min:g}분 후)")
            self.stop_event.wait(interval_sec)


# ============================================================
# [11. GUI]
# ============================================================
FONT = ("맑은 고딕", 10)
FONT_SMALL = ("맑은 고딕", 9)
FONT_BIG = ("맑은 고딕", 14, "bold")
BTN_GRAY = "#d9d9d9"

THEME = {
    "light": {"bg": "white", "fg": "black", "entry_bg": "white",
              "log_bg": "white", "select": "white"},
    "dark":  {"bg": "black", "fg": "white", "entry_bg": "#1f1f1f",
              "log_bg": "black", "select": "#333333"},
}


class DomimanApp:
    def __init__(self, root):
        self.root = root
        self.worker = None
        self.dark = False
        self.res_auto = None       # True=자동감지, False=직접설정, None=미설정
        self.ocr_ready = False
        self.current_status_key = "loading"
        self.exit_after_id = None    # 예약 종료 after() 핸들 (로컬)
        self.exit_deadline = None    # 예약 종료 시각(epoch), None=비활성 (로컬)
        self._sched_ticking = False
        self.manual_collect = None   # 즉시 회수 스레드

        # --- 원격 제어(ntfy) 상태 ---
        self.pc_list = [str(n) for n in _cfg.get("pc_list", [])
                        if re.fullmatch(r"[A-Za-z0-9]+", str(n)) and n != PC_NAME]
        self.remote_target = None        # None=로컬(이 PC), str=제어 중인 PC명
        self.pending = None              # {"kind": str, "sent": epoch} 응답 대기
        self.remote_running_shown = None # 원격 시작/중지 버튼 표시 상태
        self.remote_exit_deadline = None # 원격 예약 종료(표시 전용)
        self._local_snapshot = None      # 원격 진입 전 로컬 설정 백업
        self._applying_remote = False    # 원격 상태 반영 중(트레이스 발신 억제)
        self._timer_debounce_id = None   # 원격 타이머 3초 디바운스
        self._chan_debounce_id = None    # 채널 변경 후 스트림 재연결 디바운스
        self._sched_top = None           # 예약 종료 팝업(원격) 위젯들
        self._res_top = None             # 해상도 팝업 위젯들

        root.title("domiman.py")
        try:
            root.iconbitmap(ICON_PATH)   # 기본 tkinter 깃털 아이콘 대신 앱 아이콘
        except Exception:
            pass
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_exit)

        self._build_widgets()
        self._apply_theme()
        self._update_swap_visibility()
        self.set_status("loading")
        self._apply_ui_locks()

        global _status_cb
        _status_cb = self._post_status

        threading.Thread(target=_ntfy_stream_loop, daemon=True).start()
        threading.Thread(target=self._load_ocr, daemon=True).start()
        self.root.after(300, self._auto_detect_resolution)
        self.root.after(100, self._poll_log_queue)
        self.root.after(250, self._poll_ntfy_queue)

        print(f"[시스템] domiman 시작 (이름: {PC_NAME}, 채널: {NTFY_TOPIC}). "
              "OCR 모델을 불러오는 중입니다...")
        if PC_NAME != _default_name:
            print(f"[안내] 설정 파일의 사용자 지정 이름 '{PC_NAME}'을 사용 중입니다 "
                  f"(이 PC 호스트명: {_default_name}). 이름칸에서 바꿀 수 있습니다.")

    # ---------- 위젯 구성 (엑셀 B2:H26 목업 기반) ----------
    def _build_widgets(self):
        r = self.root
        pad = {"padx": 6, "pady": 3}

        self.frame = tk.Frame(r)
        self.frame.pack(fill="both", expand=True, padx=8, pady=8)
        f = self.frame

        # 우측 버튼 열(col 2) 왼쪽 여백(상태 문구는 고정 2줄 줄바꿈으로 처리)
        pad_r = {"padx": (12, 6), "pady": 3}

        # -- 최상단: 제어PC 변경 버튼(창 폭 대부분) + 업데이트 확인 버튼(우측 끝) --
        self.bt_pc = tk.Button(f, text=PC_NAME, font=FONT, bg=BTN_GRAY,
                               command=self.on_pc_button)
        self.bt_pc.grid(row=0, column=0, columnspan=3, sticky="ew",
                        padx=6, pady=(3, 8))
        self.bt_update = tk.Button(f, text="⟳", font=FONT, bg=BTN_GRAY,
                                   command=self.on_check_update)
        self.bt_update.grid(row=0, column=3, sticky="ew", padx=6, pady=(3, 8))

        # -- 해상도 행 --
        self.lb_res_t = tk.Label(f, text="해상도", font=FONT)
        self.lb_res_t.grid(row=1, column=0, sticky="w", **pad)
        self.lb_res = tk.Label(f, text="감지 중", font=FONT, width=24, anchor="w")
        self.lb_res.grid(row=1, column=1, sticky="w", **pad)
        self.bt_res_manual = tk.Button(f, text="직접 설정", font=FONT, bg=BTN_GRAY,
                                       command=self.on_res_manual)
        self.bt_res_manual.grid(row=1, column=2, sticky="ew", **pad_r)
        self.bt_res_auto = tk.Button(f, text="자동 감지", font=FONT, bg=BTN_GRAY,
                                     command=self.on_res_auto)
        self.bt_res_auto.grid(row=1, column=3, sticky="ew", **pad)

        # -- 타이머 행 (숫자만 입력 가능) --
        self.lb_timer_t = tk.Label(f, text="타이머", font=FONT)
        self.lb_timer_t.grid(row=2, column=0, sticky="w", **pad)
        self.var_timer = tk.StringVar(value="0")
        self.var_timer.trace_add("write", lambda *a: self._on_timer_changed())
        vcmd_num = (r.register(
            lambda P: re.fullmatch(r"\d*\.?\d*", P) is not None), "%P")
        self.en_timer = tk.Entry(f, textvariable=self.var_timer, font=FONT,
                                 width=10, validate="key", validatecommand=vcmd_num)
        self.en_timer.grid(row=2, column=1, sticky="w", **pad)
        self.lb_timer_min = tk.Label(f, text="(분)", font=FONT)
        self.lb_timer_min.grid(row=2, column=2, sticky="w", **pad)

        self.lb_timer_hint = tk.Label(
            f, text="0을 입력하면 살림망 감시 모드로 작동합니다.", font=FONT_SMALL)
        self.lb_timer_hint.grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        # -- 체크박스 2x2 + 시작/중지 큰 버튼 --
        self.var_ntfy = tk.BooleanVar(value=False)
        self.cb_ntfy = tk.Checkbutton(f, text="ntfy 메시지", font=FONT,
                                      variable=self.var_ntfy, command=self.on_ntfy_toggle)
        self.cb_ntfy.grid(row=4, column=0, sticky="w", **pad)

        self.var_rod = tk.BooleanVar(value=True)
        self.cb_rod = tk.Checkbutton(f, text="낚싯대 자동교체", font=FONT,
                                     variable=self.var_rod,
                                     command=self._on_flag_toggle)
        self.cb_rod.grid(row=4, column=1, sticky="w", **pad)

        self.bt_tank_check = tk.Button(f, text="실시간 수량확인", font=FONT, bg=BTN_GRAY,
                                       command=self.on_tank_check)
        self.bt_tank_check.grid(row=5, column=0, sticky="w", **pad)

        self.var_bait = tk.BooleanVar(value=True)
        self.cb_bait = tk.Checkbutton(f, text="미끼 자동교체", font=FONT,
                                      variable=self.var_bait,
                                      command=self._on_flag_toggle)
        self.cb_bait.grid(row=5, column=1, sticky="w", **pad)

        self.bt_start = tk.Button(f, text="시작", font=FONT_BIG, bg="#2eab4f",
                                  fg="white", width=10, height=2,
                                  command=self.on_start_stop)
        self.bt_start.grid(row=4, column=2, rowspan=3, columnspan=2,
                           sticky="nsew", padx=(12, 6), pady=4)

        self.lb_swap_hint = tk.Label(
            f, text="낚싯대, 미끼 자동교체는 살림망 감시 모드에서만 사용 가능합니다.",
            font=FONT_SMALL)
        self.lb_swap_hint.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        # -- 이름 입력 + 예약 종료/즉시 회수 (상시 작동 버튼) --
        self.lb_name_t = tk.Label(f, text="ntfy/SMS에 표시될 이름", font=FONT)
        self.lb_name_t.grid(row=7, column=0, sticky="w", **pad)
        self.var_name = tk.StringVar(value=PC_NAME)
        self.var_name.trace_add("write", lambda *a: self._validate_name())
        self.en_name = tk.Entry(f, textvariable=self.var_name, font=FONT, width=18)
        self.en_name.grid(row=7, column=1, sticky="w", **pad)

        self.bt_sched_exit = tk.Button(f, text="예약 종료", font=FONT, bg=BTN_GRAY,
                                       command=self.on_sched_exit)
        self.bt_sched_exit.grid(row=7, column=2, sticky="ew", **pad_r)
        self.bt_collect_now = tk.Button(f, text="즉시 회수", font=FONT, bg=BTN_GRAY,
                                        command=self.on_collect_now)
        self.bt_collect_now.grid(row=7, column=3, sticky="ew", **pad)

        # -- ntfy 채널 이름 --
        self.lb_chan_t = tk.Label(f, text="ntfy 채널 이름", font=FONT)
        self.lb_chan_t.grid(row=8, column=0, sticky="w", **pad)
        self.var_chan = tk.StringVar(value=NTFY_TOPIC)
        self.var_chan.trace_add("write", lambda *a: self._on_channel_changed())
        vcmd_chan = (r.register(
            lambda P: re.fullmatch(r"[A-Za-z0-9_\-]*", P) is not None), "%P")
        self.en_chan = tk.Entry(f, textvariable=self.var_chan, font=FONT,
                                width=18, validate="key", validatecommand=vcmd_chan)
        self.en_chan.grid(row=8, column=1, sticky="w", **pad)

        self.lb_name_warn = tk.Label(f, text="영문+숫자만 이용 가능합니다.",
                                     font=FONT_SMALL, fg="red")
        self.lb_name_warn.grid(row=9, column=0, columnspan=2, sticky="w", **pad)
        self.lb_name_warn.grid_remove()

        # -- 상태 문구 + 다크모드/종료 --
        # 긴 문구는 라벨 안에서 줄바꿈. 높이 2줄을 항상 확보해 두어
        # 줄바꿈이 생겨도 창 크기가 변하지 않는다.
        self.lb_status_t = tk.Label(f, text="상태 문구", font=FONT)
        self.lb_status_t.grid(row=10, column=0, sticky="w", **pad)
        _status_w = 30
        _wrap_px = tkfont.Font(font=FONT).measure("0") * _status_w - 8
        self.lb_status = tk.Label(f, text="", font=FONT, anchor="nw",
                                  justify="left", width=_status_w, height=2,
                                  wraplength=_wrap_px)
        self.lb_status.grid(row=10, column=1, sticky="w", **pad)
        self.bt_dark = tk.Button(f, text="다크모드", font=FONT, bg=BTN_GRAY,
                                 command=self.on_dark_toggle)
        self.bt_dark.grid(row=10, column=2, sticky="ew", **pad_r)
        self.bt_exit = tk.Button(f, text="프로그램 종료", font=FONT, bg=BTN_GRAY,
                                 command=self.on_exit_button)
        self.bt_exit.grid(row=10, column=3, sticky="ew", **pad)

        # -- 로그 헤더 --
        self.lb_log_t = tk.Label(f, text="로그", font=FONT)
        self.lb_log_t.grid(row=11, column=0, sticky="w", **pad)
        hdr = tk.Frame(f)
        hdr.grid(row=11, column=1, sticky="w", **pad)
        self.frame_loghdr = hdr
        self.bt_log_fold = tk.Button(hdr, text="vvv", font=FONT, bg=BTN_GRAY,
                                     width=5, command=self.on_log_fold)
        self.bt_log_fold.pack(side="left", padx=(0, 4))
        self.bt_log_clear = tk.Button(hdr, text="x", font=FONT, bg=BTN_GRAY,
                                      width=3, command=self.on_log_clear)
        self.bt_log_clear.pack(side="left")
        self.var_logsave = tk.BooleanVar(value=False)
        self.cb_logsave = tk.Checkbutton(f, text="로그 저장", font=FONT,
                                         variable=self.var_logsave,
                                         command=self._on_flag_toggle)
        self.cb_logsave.grid(row=11, column=2, sticky="w", **pad)
        self.bt_log_export = tk.Button(f, text="로그 내보내기", font=FONT, bg=BTN_GRAY,
                                       command=self.on_log_export)
        self.bt_log_export.grid(row=11, column=3, sticky="ew", **pad)

        # -- 로그 영역 (기본 숨김) --
        self.frame_log = tk.Frame(f)
        self.frame_log.grid(row=12, column=0, columnspan=4, sticky="nsew", **pad)
        self.txt_log = tk.Text(self.frame_log, height=10, width=78,
                               font=("Consolas", 9), state="disabled", wrap="none")
        self.scroll_log = tk.Scrollbar(self.frame_log, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=self.scroll_log.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        self.scroll_log.pack(side="right", fill="y")
        self.frame_log.grid_remove()
        self.log_visible = False

    # ---------- 테마 ----------
    def _apply_theme(self):
        t = THEME["dark" if self.dark else "light"]
        self.root.configure(bg=t["bg"])
        for fr in (self.frame, self.frame_loghdr, self.frame_log):
            fr.configure(bg=t["bg"])
        labels = [self.lb_res_t, self.lb_res, self.lb_timer_t, self.lb_timer_min,
                  self.lb_timer_hint, self.lb_swap_hint, self.lb_name_t,
                  self.lb_chan_t, self.lb_status_t, self.lb_status, self.lb_log_t]
        for lb in labels:
            lb.configure(bg=t["bg"], fg=t["fg"])
        self.lb_name_warn.configure(bg=t["bg"])   # 경고는 항상 빨간 글씨
        for cb in (self.cb_ntfy, self.cb_rod, self.cb_bait, self.cb_logsave):
            cb.configure(bg=t["bg"], fg=t["fg"], activebackground=t["bg"],
                         activeforeground=t["fg"], selectcolor=t["select"],
                         disabledforeground="#888888")
        for en in (self.en_timer, self.en_name, self.en_chan):
            en.configure(bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                         disabledbackground=t["bg"], disabledforeground="#888888")
        self.txt_log.configure(bg=t["log_bg"], fg=t["fg"], insertbackground=t["fg"])
        # 버튼은 회색 유지, 시작/중지 색 불변
        for bt in (self.bt_pc, self.bt_update, self.bt_res_manual, self.bt_res_auto,
                   self.bt_tank_check, self.bt_sched_exit, self.bt_collect_now,
                   self.bt_dark, self.bt_exit, self.bt_log_fold, self.bt_log_clear,
                   self.bt_log_export):
            bt.configure(bg=BTN_GRAY, fg="black")

    def on_dark_toggle(self):
        self.dark = not self.dark
        self.bt_dark.configure(text="화이트모드" if self.dark else "다크모드")
        self._apply_theme()

    # ---------- 상태 문구 ----------
    def set_status(self, key):
        self.current_status_key = key
        self._refresh_status_display()

    def _refresh_status_display(self):
        """예약 종료가 활성화되면 기존 상태문구 대신 남은 시간을 표시.
        원격 모드에서는 상대(피제어 PC)의 예약을 표시한다."""
        deadline = (self.remote_exit_deadline if self.remote_target
                    else self.exit_deadline)
        if deadline is not None:
            remain = max(0.0, deadline - time.time())
            n = max(1, int((remain + 59) // 60))   # 분 단위 올림
            self.lb_status.configure(text=f"{n}분 뒤에 종료합니다.")
        else:
            self.lb_status.configure(
                text=STATUS_TEXT.get(self.current_status_key, self.current_status_key))

    def _post_status(self, key):
        """워커 스레드에서 호출되는 상태 콜백(메인 스레드로 전달)."""
        try:
            self.root.after(0, lambda: self.set_status(key))
        except Exception:
            pass

    # ---------- OCR 로드 ----------
    def _load_ocr(self):
        global reader
        try:
            import easyocr
            r = easyocr.Reader(['ko', 'en'], gpu=False,
                               model_storage_directory=os.path.join(SCRIPT_DIR, "ocr_model"),
                               download_enabled=False)
            reader = r
            self.ocr_ready = True
            print("[시스템] OCR 모델 로드 완료.")
            self.root.after(0, lambda: self.set_status("idle"))
        except Exception:
            print("[오류] OCR 모델 로드 실패")
            traceback.print_exc()
            self.root.after(0, lambda: self.set_status("ocrfail"))

    # ---------- 해상도 ----------
    def _auto_detect_resolution(self):
        self.lb_res.configure(text="감지 중")
        threading.Thread(target=self._detect_resolution_bg, daemon=True).start()

    def _detect_resolution_bg(self):
        detected = detect_resolution()
        self.root.after(0, lambda: self._apply_detect_result(detected))

    def _apply_detect_result(self, detected):
        if detected is None:
            self.lb_res.configure(text="감지 실패")
            self.set_status("nowindow")
            print("[자동 감지 실패] Tales Runner 창을 찾지 못했습니다. "
                  "게임 실행 후 '자동 감지'를 누르세요.")
            return
        mode, mw, mh, is_primary = detected
        loc = "주 모니터" if is_primary else "보조 모니터"
        print(f"[자동 감지] 게임 위치: {loc} {mw}x{mh} -> {mode}")
        self._set_resolution(mode, auto=True)
        if self.ocr_ready:
            self.set_status("idle")

    def _set_resolution(self, mode, auto):
        global CURRENT_RESOLUTION, game_capture
        if mode == "1080p":
            CURRENT_RESOLUTION = "1080p"
            label = "1920 x 1080"
        else:
            if not _WGC_AVAILABLE:
                print("[오류] 1440p 모드에는 windows-capture 패키지가 필요합니다.")
                self.lb_res.configure(text="감지 실패")
                return
            CURRENT_RESOLUTION = "1440p"
            if game_capture is None:
                game_capture = GameCapture(WGC_WINDOW_NAME)
            label = "2560 x 1440"
        self.res_auto = auto
        self.lb_res.configure(text=f"{label}{' (자동 감지됨)' if auto else ''}")
        print(f" -> [설정] {'1080p (화면 직접 캡처)' if mode == '1080p' else '1440p (WGC 캡처)'}")

    def on_res_auto(self):
        if self.remote_target:
            if self.pending is None:
                self._send_command("V,a", "V")
            return
        if self._running():
            return
        self._auto_detect_resolution()

    def on_res_manual(self):
        if self._running() and not self.remote_target:
            return
        if self.remote_target and self.pending is not None:
            return
        top = tk.Toplevel(self.root)
        top.title("직접 설정")
        top.resizable(False, False)
        top.grab_set()
        t = THEME["dark" if self.dark else "light"]
        top.configure(bg=t["bg"])
        lb = tk.Label(top, text="해상도를 선택하세요.", font=FONT, bg=t["bg"], fg=t["fg"])
        lb.pack(padx=16, pady=(12, 6))

        bt1080 = tk.Button(top, text="1920 x 1080", font=FONT, bg=BTN_GRAY, width=16)
        bt1440 = tk.Button(top, text="2560 x 1440", font=FONT, bg=BTN_GRAY, width=16)
        bt1080.pack(padx=16, pady=4)
        bt1440.pack(padx=16, pady=(4, 12))

        def pick(mode):
            if self.remote_target:
                # 원격: 신호 발송 후 응답(또는 15초 무응답)까지 창을 열어두고 봉인
                if self.pending is not None:
                    return
                self._send_command(f"V,{'1080' if mode == '1080p' else '1440'}", "V")
                bt1080.configure(state="disabled")
                bt1440.configure(state="disabled")
                self._res_top = top
            else:
                self._set_resolution(mode, auto=False)
                top.destroy()

        bt1080.configure(command=lambda: pick("1080p"))
        bt1440.configure(command=lambda: pick("1440p"))
        top.protocol("WM_DELETE_WINDOW",
                     lambda: None if self.pending else top.destroy())

    def _close_res_dialog(self):
        if self._res_top is not None:
            try:
                self._res_top.destroy()
            except Exception:
                pass
            self._res_top = None

    # ---------- 입력 검증/표시 토글 ----------
    def _is_watch_value(self, s):
        """타이머 문자열이 살림망 감시 모드(0)인지."""
        try:
            return float(s.strip()) == 0.0
        except ValueError:
            return False

    def _update_swap_visibility(self):
        """타이머 값이 0일 때만 자동교체 체크박스 표시(안내문은 상시 표시)."""
        watch = self._is_watch_value(self.var_timer.get())
        for w in (self.cb_rod, self.cb_bait):
            if watch:
                w.grid()
            else:
                w.grid_remove()

    def _on_timer_changed(self):
        """타이머 입력 트레이스: 표시 갱신 + 원격 모드면 3초 디바운스 후 T 발송."""
        self._update_swap_visibility()
        if self._timer_debounce_id is not None:
            self.root.after_cancel(self._timer_debounce_id)
            self._timer_debounce_id = None
        if self.remote_target and not self._applying_remote:
            self._timer_debounce_id = self.root.after(3000, self._send_timer)

    def _send_timer(self):
        self._timer_debounce_id = None
        if not self.remote_target:
            return
        if self.pending is not None:          # 다른 응답 대기 중이면 잠시 후 재시도
            self._timer_debounce_id = self.root.after(1000, self._send_timer)
            return
        val = self.var_timer.get().strip()
        try:
            float(val)
        except ValueError:
            return
        self._send_command(f"T,{val}", "T")

    def _validate_name(self):
        global PC_NAME
        val = self.var_name.get()
        if re.fullmatch(r"[A-Za-z0-9]*", val):
            self.lb_name_warn.grid_remove()
            # 로컬 모드에서 유효한 이름이면 즉시 반영(상단 버튼/ntfy 제목)
            if (val and not self.remote_target and not self._applying_remote
                    and val != PC_NAME):
                PC_NAME = val
                self.bt_pc.configure(text=val)
            return True
        self.lb_name_warn.grid()
        return False

    def _on_channel_changed(self):
        """ntfy 채널 입력 트레이스: 유효하면 URL 즉시 반영 +
        타이핑이 멈춘 뒤(1.5초) 스트림을 새 채널로 재연결(키 입력마다 재연결 방지)."""
        global NTFY_TOPIC, NTFY_URL
        val = self.var_chan.get().strip()
        if re.fullmatch(r"[A-Za-z0-9_\-]+", val) and val != NTFY_TOPIC:
            NTFY_TOPIC = val
            NTFY_URL = f"{NTFY_SERVER}/{NTFY_TOPIC}"
            if self._chan_debounce_id is not None:
                self.root.after_cancel(self._chan_debounce_id)
            self._chan_debounce_id = self.root.after(1500, self._reconnect_stream)

    def _reconnect_stream(self):
        """스트림 스레드를 현재 채널로 재연결시킨다(연결만 끊으면 루프가 재접속)."""
        self._chan_debounce_id = None
        if _ntfy_enabled:
            ntfy_stream_disconnect()

    # ---------- 체크박스 ----------
    def on_ntfy_toggle(self):
        global _ntfy_enabled
        _ntfy_enabled = self.var_ntfy.get()
        if _ntfy_enabled:
            print(f" -> [설정] ntfy 수/발신 활성화 (이름: {PC_NAME}, 채널: {NTFY_TOPIC})")
        else:
            print(" -> [설정] ntfy 비활성화")
            ntfy_stream_disconnect()   # 열린 스트림을 즉시 끊어 반응 지연 없앰

    # ---------- 실시간 수량 확인 ----------
    def _tank_check_and_resume(self, on_result):
        """실시간 수량확인의 공용 로직(로컬 버튼 + 원격 N 질의가 공유).
        회수 루틴 시작 때처럼 **게임 창을 앞으로 불러** 살림망 수량을 새로
        읽고(3초 렌더 대기), **동시에 낚시 취소/시작 버튼을 확인해 '낚시 시작'
        (대기 중)이면 눌러서 낚시를 재개**한다('낚시 취소'=진행 중이면 그대로
        둔다). 결과 (cur,mx)|None을 on_result로 전달.

        단, **감시 워커가 돌고 있으면**(self._running()) 워커가 매 사이클
        창을 앞으로 불러 살림망을 읽고 낚시 상태도 스스로 관리하므로, 여기서
        또 창을 뺏어 ESC/클릭을 하면 워커 루틴과 충돌한다 → 이 경우엔 워커가
        갱신해 둔 캐시(_last_tank)만 즉시 돌려주고 창을 건드리지 않는다.
        (자동 재개 기능은 매크로가 멈춰 있을 때 쓰라고 있는 것이므로 워커
        가동 중엔 불필요.)"""
        if self._running():
            on_result(_last_tank)       # 워커가 매 사이클 갱신하는 신선한 값
            return
        if not self.ocr_ready or CURRENT_RESOLUTION is None:
            on_result(None)             # OCR/해상도 미준비 → 파싱 불가
            return

        def _bg():
            global _last_tank
            try:
                bring_game_to_front(GAME_KEYWORD)
                _ensure_watch_capture()
                time.sleep(3.0)         # 창이 떠 숫자가 렌더될 시간
                qty = read_tank_quantity()
                if qty is not None:
                    _last_tank = qty
                on_result(qty)          # 결과 먼저 알림(원격 N 응답을 빠르게)
                # 낚시 상태 확인 후 '낚시 시작'(대기 중)이면 눌러서 재개
                if is_fishing_active() is False:
                    print("[낚시 상태 확인] '낚시 시작' 상태 — 자동으로 재개합니다.")
                    _resume_fishing()
            except Exception:
                traceback.print_exc()
                on_result(None)
            finally:
                # 내가 이번에 켠 캡처는 정리(워커가 돌면 워커 소유이므로 건드리지 않음)
                if (not self._running() and game_capture is not None
                        and game_capture.is_running):
                    game_capture.stop()

        threading.Thread(target=_bg, daemon=True).start()

    def on_tank_check(self):
        """'실시간 수량확인' 버튼. 로컬이면 창을 불러 수량과 낚시 상태를 함께
        새로 확인(대기 중 = '낚시 시작'이면 자동 재개), 원격이면 N 명령 발송.
        낚시 진행 여부와 무관하게 상시 작동(원격은 응답 대기 중만 봉인)."""
        if self.remote_target:
            if self.pending is None:
                self._send_command("N", "N")
            return
        name = PC_NAME

        def report(qty):
            if qty is None:
                print(f"[실시간 수량 확인] {name}: 수량 파싱 실패")
            else:
                print(f"[실시간 수량 확인] {name}: 살림망 {qty[0]}/{qty[1]}")
        self._tank_check_and_resume(report)

    def _on_flag_toggle(self):
        """로그 저장/낚싯대/미끼 체크박스 클릭 — 원격 모드면 C 명령 발송."""
        if not self.remote_target or self._applying_remote:
            return
        if self.pending is not None:
            return
        tf = lambda b: "t" if b else "f"   # noqa: E731
        body = f"C,{tf(self.var_logsave.get())}"
        if self._is_watch_value(self.var_timer.get()):
            body += f",{tf(self.var_rod.get())},{tf(self.var_bait.get())}"
        self._send_command(body, "C")

    # ---------- 로그 ----------
    def on_log_fold(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.frame_log.grid()
            self.bt_log_fold.configure(text="^^^")
        else:
            self.frame_log.grid_remove()
            self.bt_log_fold.configure(text="vvv")

    def on_log_clear(self):
        _log.clear()
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def on_log_export(self):
        path = os.path.join(LOG_DIR, f"fishing_log_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        try:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(_log.dump())
            print(f"[시스템] 로그를 내보냈습니다: {path}")
        except Exception as e:
            print(f"[오류] 로그 내보내기 실패: {e}")

    def _save_log_on_exit(self):
        if not self.var_logsave.get():
            return
        path = os.path.join(LOG_DIR, f"fishing_full_{time.strftime('%Y%m%d_%H%M%S')}.log")
        try:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(_log.dump())
        except Exception:
            pass

    def _poll_log_queue(self):
        chunks = []
        try:
            while True:
                chunks.append(_log.q.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", "".join(chunks))
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.root.after(100, self._poll_log_queue)

    # ============================================================
    # ntfy 프로토콜 — 수신 디스패치 (메인 스레드, 250ms 주기)
    # 스트림 스레드가 큐에 넣은 메시지를 메인 스레드에서 꺼내 처리(tkinter 안전).
    # ============================================================
    def _poll_ntfy_queue(self):
        try:
            while True:
                title, body = _ntfy_queue.get_nowait()
                try:
                    self._dispatch_ntfy(title, body)
                except Exception:
                    print(f"[경고] ntfy 메시지 처리 실패: {body}")
                    traceback.print_exc()
        except queue.Empty:
            pass
        self._check_pending_timeout()
        self.root.after(250, self._poll_ntfy_queue)

    def _dispatch_ntfy(self, title, body):
        """수신 메시지 분배. 규격에 맞지 않는 메시지는 전부 무시.
        - 원격 제어 중(dB): 내 앞으로 온 응답({내이름},Z,*)과 제어 대상의
          보고(,Z,F,*)만 처리. 내 앞으로 온 '명령'은 무시(제어에만 집중).
        - 로컬 모드(d3): 내 이름 앞으로 온 명령만 처리해 실행·응답."""
        parts = [p.strip() for p in body.split(",")]
        if len(parts) < 2:
            return

        if self.remote_target:
            if (parts[0] == PC_NAME and parts[1] == "Z"
                    and title == self.remote_target):
                self._handle_remote_reply(parts[2:], body)
            elif (parts[0] == "" and parts[1] == "Z"
                  and len(parts) >= 3 and parts[2] == "F"
                  and title == self.remote_target):
                self._handle_remote_report(parts[3:])
            return

        # 로컬 모드: 나를 지목한 명령만 처리 (Z=응답은 명령이 아님)
        if parts[0] == PC_NAME and parts[1] != "Z":
            self._handle_command(title, parts[1:], body)

    # ---------- 피제어(응답) 측 ----------
    def _status_string(self):
        """,Z 뒤에 붙는 현재 상태 문자열: 타이머,해상도,a|m,로그,실행중[,낚싯대,미끼]
        '실행중'을 항상 있는 필드(4번째 뒤)로 둔 이유: 감시모드에서만 붙는
        낚싯대/미끼처럼 있다 없다 하면 자리가 밀려 파싱이 꼬인다."""
        tval = self.var_timer.get().strip() or "0"
        res = {"1080p": "1080", "1440p": "1440"}.get(CURRENT_RESOLUTION, "0")
        am = "a" if self.res_auto else "m"
        tf = lambda b: "t" if b else "f"   # noqa: E731
        s = f"{tval},{res},{am},{tf(self.var_logsave.get())},{tf(self._running())}"
        if self._is_watch_value(tval):
            s += f",{tf(self.var_rod.get())},{tf(self.var_bait.get())}"
        return s

    def _handle_command(self, sender, args, raw):
        """원격 명령 실행(메인 스레드). sender=""이면 무명(휴대폰 등) 요청."""
        cmd = args[0]
        if cmd not in ("S", "G", "P", "Y", "W", "Q", "V", "T", "C", "N"):
            return                     # 규격 외 — 무시
        print(f"[원격 명령] {raw} (from '{sender or '무명'}')")

        def reply(tail):
            ntfy_send(f"{sender},Z,{tail}")

        if cmd == "S":
            reply(self._status_string())

        elif cmd == "G":
            ok = self._running() or self._start_fishing()
            reply("G" if ok else "P")

        elif cmd == "P":
            if self._running():
                self._stop_fishing()
            reply("P")

        elif cmd == "Y":
            if len(args) == 1:
                reply("Y")             # 생존 확인용 1단계 ack
                return
            try:
                n = float(args[1])
            except ValueError:
                return
            if n < 0:
                return
            if n == 0:
                self._cancel_sched_exit()
            else:
                self._schedule_exit(n)
            reply(f"Y,{args[1]}")

        elif cmd == "W":
            reply("W")                 # 명령 수신 ack — 결과는 F 보고로 전달
            self.on_collect_now()

        elif cmd == "Q":
            ntfy_send(f"{sender},Z,Q", wait=True)   # 종료 전 동기 발신
            self.on_exit()

        elif cmd == "V":
            if len(args) < 2:
                return
            if not self._running():
                if args[1] == "a":
                    self._apply_detect_result(detect_resolution())
                elif args[1] == "1080":
                    self._set_resolution("1080p", auto=False)
                elif args[1] == "1440":
                    self._set_resolution("1440p", auto=False)
                else:
                    return
            reply(self._status_string())

        elif cmd == "T":
            if len(args) < 2:
                return
            try:
                float(args[1])
            except ValueError:
                return
            if not self._running():
                self.var_timer.set(args[1])
            reply(self._status_string())

        elif cmd == "C":
            flags = args[1:]
            if not flags or any(x not in ("t", "f") for x in flags):
                return
            if not self._running():
                self.var_logsave.set(flags[0] == "t")
                if self._is_watch_value(self.var_timer.get()) and len(flags) >= 3:
                    self.var_rod.set(flags[1] == "t")
                    self.var_bait.set(flags[2] == "t")
            reply(self._status_string())

        elif cmd == "N":
            # 로컬 버튼과 동일 로직(_tank_check_and_resume): 워커 미가동이면 창을
            # 앞으로 불러 살림망을 새로 읽고 '낚시 시작'이면 눌러 재개, 워커
            # 가동 중이면 캐시값 즉시 반환. 배경 스레드라 완료 시점에 reply가 나간다.
            self._tank_check_and_resume(lambda qty: reply(
                "N," + (f"{qty[0]},{qty[1]}" if qty is not None else "fail")))

    # ---------- 제어(요청) 측 ----------
    def _send_command(self, cmdbody, kind):
        """제어 명령 발송 + 응답 대기(pending) 진입. 대기 중엔 대부분 봉인."""
        ntfy_send(f"{self.remote_target},{cmdbody}")
        self.pending = {"kind": kind, "sent": time.time()}
        self._apply_ui_locks()

    def _resolve_pending(self):
        p, self.pending = self.pending, None
        self._apply_ui_locks()
        return p

    def _check_pending_timeout(self):
        if self.pending is None or time.time() - self.pending["sent"] <= 15.0:
            return
        p = self._resolve_pending()
        print("[ntfy] 응답이 없습니다")
        self._flash_noresp()
        kind = p["kind"]
        if kind == "connect":
            print("[원격] 상대가 응답하지 않아 이 PC 제어로 복귀합니다.")
            self._exit_remote()
        elif kind in ("G", "P"):
            self._set_start_button_remote()      # '대기' -> 이전 상태 복원
        elif kind == "Y2":
            target = self.remote_target
            self._close_sched_dialog()
            if target:
                ntfy_send(f"{target},Y,0")
        elif kind == "V":
            self._close_res_dialog()

    def _flash_noresp(self):
        """'강태공이 의식을 잃었습니다'를 5초간 표시 후 원래 문구로 복귀."""
        prev = self.current_status_key
        self.set_status("noresp")

        def restore():
            if self.current_status_key == "noresp":
                self.set_status(prev)
        self.root.after(5000, restore)

    def _handle_remote_reply(self, rest, raw):
        """제어 대상에게서 온 응답({내이름},Z,...) 처리."""
        print(f"[ntfy 응답] {raw}")
        p = self._resolve_pending() if self.pending else None
        first = rest[0] if rest else ""

        if first == "G":
            self.remote_running_shown = True
            self._set_start_button_remote()
            self.set_status("fishing")
        elif first == "P":
            self.remote_running_shown = False
            self._set_start_button_remote()
            self.set_status("idle")
        elif first == "Y" and len(rest) == 1:
            if p and p["kind"] == "Y1":
                self._open_sched_dialog_remote()
        elif first == "Y":
            try:
                n = float(rest[1])
            except (ValueError, IndexError):
                n = None
            if n is not None:
                self.remote_exit_deadline = (time.time() + n * 60.0) if n > 0 else None
                self._ensure_tick()
                self._refresh_status_display()
                print(f"[원격] 예약 종료 {'해제됨' if n == 0 else f'{n:g}분 설정됨'}")
            self._close_sched_dialog()
        elif first == "W":
            print(f"[원격] {self.remote_target}가 즉시 회수 명령을 받았습니다.")
            self.set_status("collect3")
        elif first == "N":
            # 수량 응답: rest = ['N','12','470'] 또는 ['N','fail']
            if len(rest) >= 3:
                print(f"[실시간 수량 확인] {self.remote_target}: "
                      f"살림망 {rest[1]}/{rest[2]}")
            else:
                print(f"[실시간 수량 확인] {self.remote_target}: 수량 파싱 실패")
        elif first == "Q":
            print(f"[원격] {self.remote_target}가 종료되었습니다. 이 PC 제어로 복귀합니다.")
            self._exit_remote()
        elif re.fullmatch(r"\d+(\.\d+)?", first or ""):
            self._apply_remote_status(rest)
            self._close_res_dialog()
            if p and p["kind"] == "connect":
                print(f"[원격] {self.remote_target} 연결 완료 — 상태를 동기화했습니다.")
                self.set_status("idle")
        # 그 외 규격 밖 응답은 pending 해제만 하고 무시

    def _apply_remote_status(self, rest):
        """상태 응답(타이머,해상도,a|m,로그,실행중[,낚싯대,미끼])을 UI에 반영.
        실행중 필드로 시작/중지 버튼을 즉시 실제 상태에 맞춰 갱신한다(과거엔
        이 필드가 없어 접속 직후 무조건 '시작'으로 뜨다가 버튼을 눌러야만
        교정됐음)."""
        if len(rest) < 5:
            return
        self._applying_remote = True
        try:
            self.var_timer.set(rest[0])
            res, am = rest[1], rest[2]
            if res in ("1080", "1440"):
                label = "1920 x 1080" if res == "1080" else "2560 x 1440"
                if am == "a":
                    label += " (자동 감지됨)"
            else:
                label = "감지 실패"
            self.lb_res.configure(text=label)
            self.var_logsave.set(rest[3] == "t")
            if len(rest) >= 7:
                self.var_rod.set(rest[5] == "t")
                self.var_bait.set(rest[6] == "t")
        finally:
            self._applying_remote = False
        self.remote_running_shown = (rest[4] == "t")
        self._set_start_button_remote()

    def _handle_remote_report(self, rest):
        """제어 대상의 상황 보고(,Z,F,*)를 로그 문구로 변환·표시."""
        key = tuple(rest[:2]) if len(rest) >= 2 else tuple(rest[:1])
        text = REPORT_TEXT.get(key)
        if text is None:
            return                     # 규격 외 코드 — 무시
        print(f"[원격 보고] {text.format(name=self.remote_target)}")
        status = REPORT_STATUS.get(key)
        if status:
            self.set_status(status)

    # ---------- 제어PC 변경 ----------
    def on_pc_button(self):
        """저장된 PC 리스트 창. 다른 PC 선택+확인 -> 원격 제어 모드 진입."""
        if self._running():
            return
        top = tk.Toplevel(self.root)
        top.title("PC 원격제어")
        top.resizable(False, False)
        top.grab_set()
        t = THEME["dark" if self.dark else "light"]
        top.configure(bg=t["bg"])

        head = tk.Frame(top, bg=t["bg"])
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(head, text="저장된 PC 리스트", font=FONT,
                 bg=t["bg"], fg=t["fg"]).pack(side="left")
        bt_del = tk.Button(head, text="-", font=FONT, bg=BTN_GRAY, width=3)
        bt_del.pack(side="right")

        lbx = tk.Listbox(top, font=FONT, height=6, width=28, activestyle="none",
                         selectmode="browse", exportselection=False,
                         bg=t["entry_bg"], fg=t["fg"],
                         selectbackground="#2e6fd0", selectforeground="white")
        lbx.pack(fill="x", padx=12, pady=4)

        def refill(select_name=None):
            lbx.delete(0, "end")
            lbx.insert("end", f"{PC_NAME} (이 PC)")
            for n in self.pc_list:
                lbx.insert("end", n)
            target = select_name or self.remote_target or PC_NAME
            idx = 0
            if target != PC_NAME and target in self.pc_list:
                idx = 1 + self.pc_list.index(target)
            lbx.selection_set(idx)
            lbx.see(idx)

        refill()

        foot = tk.Frame(top, bg=t["bg"])
        foot.pack(fill="x", padx=12, pady=2)
        var_new = tk.StringVar()
        en_new = tk.Entry(foot, textvariable=var_new, font=FONT, width=20,
                          bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"])
        en_new.pack(side="left", fill="x", expand=True)
        bt_add = tk.Button(foot, text="+", font=FONT, bg=BTN_GRAY, width=3)
        bt_add.pack(side="right", padx=(6, 0))

        def on_add():
            name = var_new.get().strip()
            if not re.fullmatch(r"[A-Za-z0-9]+", name):
                print("[안내] PC 이름은 영문+숫자만 가능합니다.")
                return
            if name == PC_NAME or name in self.pc_list:
                print("[안내] 이미 리스트에 있는 이름입니다.")
                return
            self.pc_list.append(name)
            refill(select_name=name)
            var_new.set("")

        def on_del():
            sel = lbx.curselection()
            if not sel:
                return
            if sel[0] == 0:
                print("[안내] 현재 PC는 제거할 수 없습니다.")
                return
            removed = self.pc_list.pop(sel[0] - 1)
            print(f"[설정] PC 리스트에서 '{removed}' 제거")
            refill()

        def on_ok():
            sel = lbx.curselection()
            target = PC_NAME if not sel or sel[0] == 0 else self.pc_list[sel[0] - 1]
            save_config(PC_NAME, NTFY_TOPIC, self.pc_list)
            top.destroy()
            if target == PC_NAME:
                if self.remote_target:
                    self._exit_remote()
            else:
                self.pending = None      # 이전 대기 취소 후 새 대상 연결
                self._enter_remote(target)

        bt_add.configure(command=on_add)
        bt_del.configure(command=on_del)
        en_new.bind("<Return>", lambda e: on_add())
        tk.Button(top, text="확인", font=FONT, bg=BTN_GRAY, width=10,
                  command=on_ok).pack(pady=(6, 12))

    def _enter_remote(self, target):
        """원격 제어 모드 진입: 로컬 설정 백업 -> S 질의로 상태 동기화."""
        global _ntfy_enabled
        if self._local_snapshot is None:
            self._local_snapshot = {
                "timer": self.var_timer.get(),
                "log": self.var_logsave.get(),
                "rod": self.var_rod.get(),
                "bait": self.var_bait.get(),
                "res_label": self.lb_res.cget("text"),
                "name": self.var_name.get(),
                "status": self.current_status_key,
            }
        if not self.var_ntfy.get():          # ntfy는 강제 활성화 후 봉인
            self.var_ntfy.set(True)
            _ntfy_enabled = True
            print(" -> [설정] 원격 제어를 위해 ntfy를 활성화합니다.")

        self.remote_target = target
        self.remote_running_shown = False
        self.remote_exit_deadline = None
        self._applying_remote = True
        self.var_name.set(target)
        self._applying_remote = False
        self.bt_pc.configure(text=target)
        self._set_start_button_remote()
        print(f"\n[원격] '{target}' 제어를 시작합니다. 상태를 질의합니다...")
        self._send_command("S", "connect")

    def _exit_remote(self):
        """원격 제어 종료: 이 PC 제어로 복귀, 로컬 설정 복원."""
        self.remote_target = None
        self.pending = None
        self.remote_running_shown = None
        self.remote_exit_deadline = None
        if self._timer_debounce_id is not None:
            self.root.after_cancel(self._timer_debounce_id)
            self._timer_debounce_id = None
        self._close_sched_dialog()
        self._close_res_dialog()

        snap, self._local_snapshot = self._local_snapshot, None
        if snap:
            self._applying_remote = True
            try:
                self.var_timer.set(snap["timer"])
                self.var_logsave.set(snap["log"])
                self.var_rod.set(snap["rod"])
                self.var_bait.set(snap["bait"])
                self.lb_res.configure(text=snap["res_label"])
                self.var_name.set(snap["name"])
            finally:
                self._applying_remote = False
        self.bt_pc.configure(text=PC_NAME)
        if self._running():
            self.bt_start.configure(text="중지", bg="#d9302e", fg="white",
                                    state="normal")
        else:
            self.bt_start.configure(text="시작", bg="#2eab4f", fg="white",
                                    state="normal")
        self.set_status(snap["status"] if snap else "idle")
        self._apply_ui_locks()
        print("[원격] 이 PC 제어로 복귀했습니다.")

    def _set_start_button_remote(self, waiting=False):
        """원격 모드 시작/중지 버튼 표시 갱신. waiting=응답 대기(회색 '대기')."""
        if waiting:
            self.bt_start.configure(text="대기", bg=BTN_GRAY, fg="black",
                                    state="disabled")
        elif self.remote_running_shown:
            self.bt_start.configure(text="중지", bg="#d9302e", fg="white",
                                    state="normal")
        else:
            self.bt_start.configure(text="시작", bg="#2eab4f", fg="white",
                                    state="normal")

    # ---------- UI 봉인(중앙 관리) ----------
    def _apply_ui_locks(self):
        """모드/실행/응답대기 상태에 따라 위젯 활성/비활성을 일괄 적용.
        항상 활성: 로그 접기/지우기/내보내기, 다크모드.
        제어PC 변경 버튼: 로컬 낚시 실행 중에만 봉인(모드 무관)."""
        running = self._running()
        remote = self.remote_target is not None
        pending = self.pending is not None

        def st(w, enabled):
            w.configure(state="normal" if enabled else "disabled")

        st(self.bt_pc, not running)
        st(self.bt_update, not running)
        # 이름/채널/ntfy — 원격에선 항상 봉인, 로컬에선 실행 중 봉인
        settings_ok = (not running) and (not remote)
        for w in (self.en_name, self.en_chan, self.cb_ntfy):
            st(w, settings_ok)
        # 타이머/자동교체/로그저장/해상도 — 원격에선 응답 대기 중만 봉인
        flags_ok = (not pending) if remote else (not running)
        for w in (self.en_timer, self.cb_rod, self.cb_bait, self.cb_logsave,
                  self.bt_res_manual, self.bt_res_auto):
            st(w, flags_ok)
        # 예약 종료/즉시 회수/실시간 수량확인/프로그램 종료 — 로컬 상시, 원격은 대기 중 봉인
        always_ok = (not pending) if remote else True
        for w in (self.bt_sched_exit, self.bt_collect_now, self.bt_tank_check,
                  self.bt_exit):
            st(w, always_ok)
        # 시작/중지 — 원격 대기 중 봉인('대기' 표시는 G/P 전용 별도 처리)
        if remote:
            if pending and self.pending.get("kind") not in ("G", "P"):
                st(self.bt_start, False)
            elif not pending:
                st(self.bt_start, True)

    # ---------- 예약 종료 ----------
    def on_sched_exit(self):
        """몇 분 뒤 종료할지 입력받는 창. 0 입력/취소 = 예약 해제(또는 유지 안 함).
        원격 모드에서는 먼저 Y를 보내 생존 확인 후(,Z,Y 수신 시) 창을 띄운다."""
        if self.remote_target:
            if self.pending is None:
                self._send_command("Y", "Y1")
            return
        top = tk.Toplevel(self.root)
        top.title("예약 종료")
        top.resizable(False, False)
        top.grab_set()
        t = THEME["dark" if self.dark else "light"]
        top.configure(bg=t["bg"])

        tk.Label(top, text="몇 분 뒤에 종료할까요? (0 = 예약 해제)",
                 font=FONT, bg=t["bg"], fg=t["fg"]).pack(padx=16, pady=(12, 6))
        # 이미 예약돼 있으면 남은 분을 기본값으로
        if self.exit_deadline is not None:
            default = str(max(1, int((self.exit_deadline - time.time() + 59) // 60)))
        else:
            default = "0"
        var = tk.StringVar(value=default)
        en = tk.Entry(top, textvariable=var, font=FONT, width=10, justify="center",
                      bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"])
        en.pack(padx=16, pady=4)
        en.select_range(0, "end")
        en.focus_set()
        warn = tk.Label(top, text="숫자를 입력하세요.", font=FONT_SMALL,
                        fg="red", bg=t["bg"])

        def on_ok(event=None):
            try:
                minutes = float(var.get().strip())
                if minutes < 0:
                    raise ValueError
            except ValueError:
                warn.pack(padx=16, pady=(0, 4))
                return
            top.destroy()
            if minutes == 0:
                self._cancel_sched_exit()
            else:
                self._schedule_exit(minutes)

        row = tk.Frame(top, bg=t["bg"])
        row.pack(padx=16, pady=(6, 12))
        tk.Button(row, text="취소", font=FONT, bg=BTN_GRAY, width=8,
                  command=top.destroy).pack(side="left", padx=(0, 6))
        tk.Button(row, text="확인", font=FONT, bg=BTN_GRAY, width=8,
                  command=on_ok).pack(side="left")
        top.bind("<Return>", on_ok)

    def _schedule_exit(self, minutes):
        if self.exit_after_id is not None:
            self.root.after_cancel(self.exit_after_id)
        self.exit_deadline = time.time() + minutes * 60.0
        self.exit_after_id = self.root.after(int(minutes * 60 * 1000),
                                             self._scheduled_exit_fire)
        when = time.strftime('%H:%M:%S', time.localtime(self.exit_deadline))
        print(f"[예약 종료] {minutes:g}분 뒤({when})에 프로그램을 종료합니다.")
        self._refresh_status_display()
        if not self._sched_ticking:
            self._sched_ticking = True
            self.root.after(1000, self._tick_sched)

    def _cancel_sched_exit(self):
        if self.exit_after_id is not None:
            self.root.after_cancel(self.exit_after_id)
            print("[예약 종료] 예약이 해제되었습니다.")
        self.exit_after_id = None
        self.exit_deadline = None
        self._refresh_status_display()

    def _ensure_tick(self):
        if not self._sched_ticking:
            self._sched_ticking = True
            self.root.after(1000, self._tick_sched)

    def _tick_sched(self):
        """예약 종료(로컬/원격 표시) 활성 중 남은 시간 표시를 주기 갱신."""
        if self.exit_deadline is None and self.remote_exit_deadline is None:
            self._sched_ticking = False
            return
        self._refresh_status_display()
        self.root.after(1000, self._tick_sched)

    def _scheduled_exit_fire(self):
        # 수거/교체 루틴이 진행 중이어도 지체 없이 종료(워커는 데몬 스레드).
        print("\n[예약 종료] 예약된 시각이 되어 프로그램을 종료합니다.")
        self.on_exit()

    def _open_sched_dialog_remote(self):
        """원격 예약 종료 창(,Z,Y 수신 후). 확인 -> Y,n 발송 후 응답까지
        취소 외 봉인. 취소/15초 무응답 -> Y,0 발송 후 그냥 닫힘."""
        top = tk.Toplevel(self.root)
        top.title("예약 종료")
        top.resizable(False, False)
        top.grab_set()
        t = THEME["dark" if self.dark else "light"]
        top.configure(bg=t["bg"])

        tk.Label(top, text=f"몇 분 뒤에 {self.remote_target}를 종료할까요? (0 = 예약 해제)",
                 font=FONT, bg=t["bg"], fg=t["fg"]).pack(padx=16, pady=(12, 6))
        var = tk.StringVar(value="0")
        en = tk.Entry(top, textvariable=var, font=FONT, width=10, justify="center",
                      bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"])
        en.pack(padx=16, pady=4)
        en.select_range(0, "end")
        en.focus_set()
        warn = tk.Label(top, text="숫자를 입력하세요.", font=FONT_SMALL,
                        fg="red", bg=t["bg"])

        row = tk.Frame(top, bg=t["bg"])
        row.pack(padx=16, pady=(6, 12))
        bt_cancel = tk.Button(row, text="취소", font=FONT, bg=BTN_GRAY, width=8)
        bt_ok = tk.Button(row, text="확인", font=FONT, bg=BTN_GRAY, width=8)
        bt_cancel.pack(side="left", padx=(0, 6))
        bt_ok.pack(side="left")

        def on_ok(event=None):
            if self.pending is not None:
                return
            try:
                minutes = float(var.get().strip())
                if minutes < 0:
                    raise ValueError
            except ValueError:
                warn.pack(padx=16, pady=(0, 4))
                return
            self._send_command(f"Y,{minutes:g}", "Y2")
            en.configure(state="disabled")   # 응답 전까지 취소만 가능
            bt_ok.configure(state="disabled")

        def on_cancel():
            target = self.remote_target
            if self.pending is not None and self.pending.get("kind") == "Y2":
                self._resolve_pending()      # 응답 대기 포기
            self._close_sched_dialog()
            if target:
                ntfy_send(f"{target},Y,0")

        bt_ok.configure(command=on_ok)
        bt_cancel.configure(command=on_cancel)
        top.bind("<Return>", on_ok)
        top.protocol("WM_DELETE_WINDOW", on_cancel)
        self._sched_top = top

    def _close_sched_dialog(self):
        if self._sched_top is not None:
            try:
                self._sched_top.destroy()
            except Exception:
                pass
            self._sched_top = None

    # ---------- 즉시 회수 ----------
    def _can_operate(self):
        """즉시 회수·낚시 시작 공통 가드: OCR/해상도 준비 안 되면 차단."""
        if not self.ocr_ready:
            print("[안내] OCR 모델이 준비되지 않아 실행할 수 없습니다.")
            return False
        if CURRENT_RESOLUTION is None:
            print("[안내] 해상도가 설정되지 않았습니다. "
                  "'자동 감지' 또는 '직접 설정'을 먼저 하세요.")
            return False
        return True

    def on_collect_now(self):
        """3초 뒤 게임을 전면으로 불러와 수거 루틴을 1회 실행(상시 작동).
        원격 모드에서는 W 명령을 발송한다."""
        if self.remote_target:
            if self.pending is None:
                self._send_command("W", "W")
            return
        if not self._can_operate():
            return
        if self.manual_collect is not None and self.manual_collect.is_alive():
            print("[안내] 이미 즉시 회수가 진행 중입니다.")
            return

        print("\n[즉시 회수] 3초 후 수거 루틴을 시작합니다.")
        self.set_status("collect3")

        def _run():
            time.sleep(3.0)
            run_fishing_routine()
            # 중지 상태에서 눌렀다면 루틴 종료 후 준비 상태로 복귀
            if not self._running():
                self._post_status("idle")

        self.manual_collect = threading.Thread(target=_run, daemon=True)
        self.manual_collect.start()

    # ---------- 시작/중지 ----------
    def _running(self):
        return self.worker is not None and self.worker.is_alive()

    def _start_fishing(self):
        """로컬 낚시 시작. 성공 시 True (원격 G 명령도 이 경로를 공유)."""
        if not self._can_operate():
            return False
        tval = self.var_timer.get().strip()
        if self._is_watch_value(tval):
            mode, interval_min = "watch", 0.0
        else:
            try:
                interval_min = float(tval)
                if interval_min <= 0:
                    raise ValueError
                mode = "timer"
            except ValueError:
                print("[안내] 타이머 값이 올바르지 않습니다. (분 단위 숫자, 0=감시 모드)")
                return False
        name = self.var_name.get()
        if not re.fullmatch(r"[A-Za-z0-9]+", name):
            print("[안내] 이름은 영문+숫자만 가능합니다.")
            self.lb_name_warn.grid()
            return False

        global PC_NAME, _ntfy_enabled
        if not self.remote_target:   # 원격 모드에선 이름칸=상대 이름 — 내 이름 갱신 금지
            PC_NAME = name
        _ntfy_enabled = self.var_ntfy.get()

        self.worker = FishingWorker(
            mode=mode, interval_min=interval_min,
            rod_swap=self.var_rod.get(), bait_swap=self.var_bait.get(),
            on_end=lambda: self.root.after(0, self._on_worker_end),
        )
        self.bt_start.configure(text="중지", bg="#d9302e", fg="white",
                                state="normal")
        self.worker.start()
        self._apply_ui_locks()
        return True

    def _stop_fishing(self):
        print("\n[시스템] 중지 요청 — 진행 중인 동작을 마치는 대로 멈춥니다.")
        self.bt_start.configure(text="중지 중...", state="disabled")
        self.worker.stop()

    def on_start_stop(self):
        if self.remote_target:
            # 원격: G/P 발송 후 응답 전까지 회색 '대기'
            if self.pending is not None:
                return
            self._send_command("P" if self.remote_running_shown else "G",
                               "P" if self.remote_running_shown else "G")
            self._set_start_button_remote(waiting=True)
            return

        if self._running():
            self._stop_fishing()
        else:
            self._start_fishing()

    def _on_worker_end(self):
        self.bt_start.configure(text="시작", bg="#2eab4f", fg="white",
                                state="normal")
        self._apply_ui_locks()
        # 끊김 상태 문구는 유지(사용자에게 상황 안내), 그 외엔 준비 상태로
        if getattr(self, "current_status_key", "") not in ("disconnect", "nowindow", "ocrfail"):
            self.set_status("idle")
        print("[시스템] 낚시 자동화가 중지되었습니다.")

    # ---------- 업데이트 ----------
    def on_check_update(self):
        """'⟳' 버튼 — 수동 업데이트 확인. 로컬 낚시 실행 중엔 잠겨 있어
        호출되지 않음(_apply_ui_locks). 원격 제어 중에는 미지원(그 PC에서
        직접 확인해야 함)."""
        if self.remote_target:
            print("[업데이트] 원격 제어 중에는 이 PC의 업데이트를 확인할 수 없습니다.")
            return

        self.bt_update.configure(state="disabled")
        print(f"[업데이트] 버전 확인 중... (현재 {APP_VERSION})")

        def _bg():
            latest = fetch_latest_version()
            self.root.after(0, lambda: self._on_version_checked(latest))

        threading.Thread(target=_bg, daemon=True).start()

    def _on_version_checked(self, latest):
        if not self._running():
            self.bt_update.configure(state="normal")
        if latest is None:
            print("[업데이트] 버전 확인 실패(네트워크 오류).")
        elif latest == APP_VERSION:
            print(f"[업데이트] 이미 최신 버전입니다. ({APP_VERSION})")
            self.lb_status.configure(text="업데이트가 없습니다.")
            self.root.after(3000, self._refresh_status_display)
        elif latest > APP_VERSION:
            self._show_update_dialog(latest)
        else:
            print(f"[업데이트] 현재 버전({APP_VERSION})이 리포 버전({latest})보다"
                  " 최신이거나 같습니다.")

    def _show_update_dialog(self, latest):
        """업데이트 발견 시 뜨는 안내 팝업. [업데이트]=다운로드+재시작, [취소]=무시."""
        top = tk.Toplevel(self.root)
        top.title("업데이트")
        top.resizable(False, False)
        top.grab_set()
        t = THEME["dark" if self.dark else "light"]
        top.configure(bg=t["bg"])

        tk.Label(top, text="업데이트가 있습니다.", font=FONT,
                 bg=t["bg"], fg=t["fg"]).pack(padx=24, pady=(14, 6))
        tk.Label(top, text=f"현재 : {APP_VERSION}", font=FONT,
                 bg=t["bg"], fg=t["fg"]).pack(padx=24, pady=0, anchor="w")
        tk.Label(top, text=f"신규 : {latest}", font=FONT,
                 bg=t["bg"], fg=t["fg"]).pack(padx=24, pady=(0, 10), anchor="w")

        def on_update():
            top.destroy()
            self._download_and_apply_update(latest)

        row = tk.Frame(top, bg=t["bg"])
        row.pack(padx=16, pady=(0, 14))
        tk.Button(row, text="업데이트", font=FONT, bg=BTN_GRAY, width=8,
                  command=on_update).pack(side="left", padx=(0, 6))
        tk.Button(row, text="취소", font=FONT, bg=BTN_GRAY, width=8,
                  command=top.destroy).pack(side="left")

    def _download_and_apply_update(self, latest):
        self.bt_update.configure(state="disabled")
        print(f"[업데이트] {latest} 다운로드 중...")

        def _bg():
            src = download_latest_source()
            self.root.after(0, lambda: self._on_update_downloaded(src))

        threading.Thread(target=_bg, daemon=True).start()

    def _on_update_downloaded(self, src):
        if not src:
            print("[업데이트] 다운로드 실패. 잠시 후 다시 시도해 주세요.")
            if not self._running():
                self.bt_update.configure(state="normal")
            return
        print("[업데이트] 적용 후 재시작합니다...")
        try:
            save_config(PC_NAME, NTFY_TOPIC, self.pc_list)
            self._save_log_on_exit()
        except Exception:
            pass
        apply_update_and_restart(src)   # 반환하지 않음(재시작 프로세스 기동 후 os._exit)

    # ---------- 종료 ----------
    def on_exit_button(self):
        """'프로그램 종료' 버튼: 로컬이면 이 PC 종료, 원격이면 상대(Q) 종료."""
        if self.remote_target:
            if self.pending is None:
                self._send_command("Q", "Q")
            return
        self.on_exit()

    def on_exit(self):
        """이 PC의 domiman 종료 (창 X 버튼은 모드와 무관하게 항상 이 경로).
        필요한 저장만 마치고 os._exit로 '즉시' 끝낸다. 과거엔 root.destroy 후
        인터프리터 종료 때 torch/OpenMP 등 네이티브 스레드 정리를 기다리느라
        (로그 저장과 무관하게) 창이 '응답 없음'으로 늦게 꺼졌다 → 데몬/네이티브
        스레드 대기를 건너뛰는 os._exit(0)로 해결. 소켓은 OS가 닫아준다."""
        try:
            # 원격 모드로 종료돼도 '이 PC'의 이름만 저장(상대 이름 유출 방지)
            name = PC_NAME
            if self.remote_target and self._local_snapshot:
                snap_name = self._local_snapshot.get("name", "")
                if re.fullmatch(r"[A-Za-z0-9]+", snap_name):
                    name = snap_name
            save_config(name, NTFY_TOPIC, self.pc_list)
            if self._running():
                self.worker.stop()
            self._save_log_on_exit()   # 로그 저장 OFF면 즉시 반환
        except Exception:
            pass
        finally:
            os._exit(0)   # 즉시 종료(네이티브 스레드 정리 대기 없음)


# ============================================================
# [12. 시작 지점]
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = DomimanApp(root)
    root.mainloop()
