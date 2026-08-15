# -*- coding: utf-8 -*-
"""
domiman.py — 테일즈러너 낚시 자동화 GUI (낚시.py의 GUI 재구성판)
====================================================================
- Windows 전용. tkinter 기반 단일 창 GUI.
- 이 파일 + ocr_model 폴더만으로 동작한다 (낚시.py 불필요).
- 자동화 로직은 낚시.py에서 이식: FHD=pyautogui / QHD=WGC 캡처,
  윤곽선 매칭 퀴즈 풀이, 살림망 감시 모드, 미끼/낚싯대 자동 교체,
  접속 끊김 감지(GUI에서는 종료하지 않고 대기).
- domichat 원격 제어: 피제어 PC마다 채팅방 하나(domi_fishing_{ID})를 쓰고,
  최상단 제어PC 버튼으로 그 방에 들어가 간접 제어한다. 프로토콜 규격은
  [2. 설정 · 자격 + domichat 통신] 섹션 주석 참고. 서버 IP·ID·PC 리스트는
  domiman_config.json, 비밀번호는 domiman_secrets.dat(DPAPI 암호화)에 보존.
- exe 패키징(콘솔 창 없이 GUI만):
    pyinstaller --noconsole --onedir --add-data "ocr_model;ocr_model" domiman.py
"""
import ctypes
import hashlib
import time
import sys
import os
import re
import random
import json
import queue
import socket
import ssl
import struct
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
# [2. 설정 · 자격 + domichat 프로토콜 통신 (양방향 원격 제어)]
# ------------------------------------------------------------
# 메시지 규격은 ntfy 시절과 **글자 하나 다르지 않다**:
#   명령:   "(대상PC),(명령)[,인자...]"   예) GOD3,S / GOD3,T,30
#   응답:   "(요청자PC),Z,..."            예) BGOD,Z,0,1080,a,f,t,t
#   보고:   ",Z,F,(코드)[,(서브)]"        예) ,Z,F,y,b (미끼 교체 성공)
#
# 바뀐 것은 전송 계층뿐이다. ntfy 채널 하나를 여러 PC가 공유하던 방식에서,
# **피제어 PC마다 domichat 채팅방 하나**를 쓰는 방식으로 옮겼다:
#   - 방 이름 = domi_fishing_{피제어 PC의 로그인 ID}
#   - 방 유형 = 비밀번호 방(고정 비번). 일반 domichat 사용자의 오진입만 막는 용도로,
#     방장(피제어 PC)이 없어도 제어 PC가 바로 들어갈 수 있어 승인 왕복이 없다.
#   - 피제어 PC가 실행되면 자기 방을 만들고(있으면 입장) 방장이 된다.
#   - 제어 PC는 대상 ID로 방 이름을 계산해 들어가 구독하고, 예전처럼 S 질의부터 보낸다.
#   - 피제어 PC가 꺼지면 방장이 방에서 빠지고(member in=false) 제어가 자동 종료된다.
#
# **PC 이름 설정 항목은 없앴다.** 발신자 식별은 domichat 로그인 ID이며(서버가 찍어
# 주므로 위조 불가), 예전 ntfy 이름 설정은 무시한다.
# ============================================================
# 설정·자격 저장 위치. DOMIMAN_DIR 환경변수로 바꿀 수 있다(테스트·다중 프로필용).
_CFG_DIR = os.environ.get("DOMIMAN_DIR") or LOG_DIR
os.makedirs(_CFG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(_CFG_DIR, "domiman_config.json")
SECRETS_PATH = os.path.join(_CFG_DIR, "domiman_secrets.dat")

CHAT_PORT = 47821                       # domiserver 기본 포트
ROOM_PREFIX = "domi_fishing_"
ROOM_PW = "domi_fishing_9714"           # 고정(오진입 방지용)
FRAME_HEAD = struct.Struct(">IB")       # [길이 4][종류 1] — domichat.md 참고
MAX_FRAME = 1024 * 1024
CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 60.0                     # 서버 ping 15초 → 60초 침묵이면 죽은 연결
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)

chat_enabled = False                    # 'domichat 메시지' 체크박스와 연동
MY_ID = ""                              # 로그인 ID(예전 PC 이름 자리)
MY_ROOM = ""                            # domi_fishing_{MY_ID}


def room_of(uid):
    return f"{ROOM_PREFIX}{uid}"


def load_config():
    """설정 파일 로드. 없거나 깨졌으면 빈 dict(기본값 사용)."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fp:
            cfg = json.load(fp)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(ip, uid, pc_list):
    """서버 IP·로그인 ID·제어 대상 PC 리스트를 보존."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump({"ip": ip, "id": uid, "pc_list": pc_list},
                      fp, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[경고] 설정 저장 실패: {e}")


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    """Windows DPAPI 암·복호화(같은 PC·같은 사용자만 복호화된다). 실패하면 None."""
    try:
        src = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                           ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        fn = (ctypes.windll.crypt32.CryptProtectData if protect
              else ctypes.windll.crypt32.CryptUnprotectData)
        if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
            return None
        res = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return res
    except Exception:
        return None


def load_secret_pw():
    """저장된 로그인 비밀번호. 없으면 빈 문자열.
    평문으로 두지 않는다 — 파일이 복사돼도 다른 PC·사용자는 못 푼다."""
    try:
        with open(SECRETS_PATH, "rb") as fp:
            raw = fp.read()
    except Exception:
        return ""
    plain = _dpapi(raw, protect=False)
    try:
        return json.loads((plain or raw).decode("utf-8")).get("pw", "")
    except Exception:
        return ""


def save_secret_pw(pw):
    raw = json.dumps({"pw": pw}, ensure_ascii=False).encode("utf-8")
    enc = _dpapi(raw, protect=True)
    try:
        with open(SECRETS_PATH, "wb") as fp:
            fp.write(enc if enc is not None else raw)
    except Exception as e:
        print(f"[경고] 자격 저장 실패: {e}")


def clear_secret_pw():
    try:
        os.remove(SECRETS_PATH)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[경고] 자격 삭제 실패: {e}")


# ---------- 소켓 · TLS (domichat.py와 같은 규격 — domichat.md가 기준) ----------
# 클라이언트 계층은 domichat.py에서 **복제**했다. import 하지 않는 이유: 두 앱은
# 각자 자기 .py 하나만 교체하는 방식으로 업데이트되므로, 공용 모듈을 만들면 그
# 규약이 깨진다(자세한 것은 domichat.md '구현 시 주의').


class CertChanged(Exception):
    def __init__(self, host, old, new):
        super().__init__(f"{host}: {old[:16]}… → {new[:16]}…")
        self.host, self.old, self.new = host, old, new


def _local_addrs():
    addrs = {"127.0.0.1", "localhost", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(info[4][0])
    except Exception:
        pass
    return addrs


def connect_any(ip, port, timeout=CONNECT_TIMEOUT):
    """서버가 도는 PC에서 자기 IP를 적어도 붙도록 로컬 폴백을 둔다."""
    try:
        return socket.create_connection((ip, port), timeout)
    except OSError:
        if ip in _local_addrs():
            return socket.create_connection(("127.0.0.1", port), timeout)
        raise


def _tls_context():
    """자체 서명 인증서를 쓰므로 검증은 끄고 지문 고정으로 신뢰한다.
    **TLS 1.2로 고정**한다 — 수신·송신 스레드가 한 소켓을 나눠 쓰는 구조라
    1.3의 핸드셰이크 후 메시지 때문에 record layer가 깨진다(domichat.md 참고)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    if no_reneg:
        ctx.options |= no_reneg
    return ctx


def connect_secure(ip, port, pinned):
    """(소켓, 지문|None). 서버가 TLS를 안 쓰면 평문으로 다시 붙는다."""
    raw = connect_any(ip, port)
    try:
        sock = _tls_context().wrap_socket(raw)
        fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    except (ssl.SSLError, OSError) as e:
        try:
            raw.close()
        except Exception:
            pass
        print(f"[TLS] 서버가 TLS를 쓰지 않는 것 같습니다({e}) — 평문으로 접속합니다.")
        return connect_any(ip, port), None
    if pinned and pinned != fp:
        try:
            sock.close()
        except Exception:
            pass
        raise CertChanged(f"{ip}:{port}", pinned, fp)
    return sock, fp


class ChatClient:
    """domiserver 세션 하나(접속 유지 + 자동 재연결).
    수신 프레임과 내부 사건을 전부 self.q로 넘긴다(GUI는 메인 스레드에서만 처리)."""

    def __init__(self):
        self.q = queue.Queue()
        self.txq = queue.Queue()
        self.sock = None
        self.ip = self.uid = self.pw = None
        self.port = CHAT_PORT
        self.pinned = None
        self.want = False
        self.first_try = True
        self.logged_in = threading.Event()
        self._send_lock = threading.Lock()

    # ---------- 저수준 ----------
    def _raw_send(self, sock, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        with self._send_lock:
            sock.sendall(FRAME_HEAD.pack(len(data), ord("T")) + data)

    @staticmethod
    def _recv_exact(sock, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _recv_obj(self, sock):
        head = self._recv_exact(sock, FRAME_HEAD.size)
        if head is None:
            return None
        ln, typ = FRAME_HEAD.unpack(head)
        if ln > MAX_FRAME:
            raise OSError("프레임 과대")
        body = self._recv_exact(sock, ln) if ln else b""
        if body is None:
            return None
        if chr(typ) != "T":
            return {}                      # 이미지 등 — 매크로는 쓰지 않는다
        return json.loads(body.decode("utf-8"))

    # ---------- 세션 ----------
    def start(self, ip, port, uid, pw, pinned=None):
        self.ip, self.port, self.uid, self.pw = ip, port, uid, pw
        self.pinned = pinned
        self.want = True
        self.first_try = True
        threading.Thread(target=self._session_loop, daemon=True).start()

    def _session_loop(self):
        idx = 0
        while self.want:
            try:
                sock, fp = connect_secure(self.ip, self.port, self.pinned)
                if fp and not self.pinned:
                    self.pinned = fp
                    self.q.put({"_ev": "cert_pinned", "fp": fp})
            except CertChanged as e:
                self.want = False
                self.q.put({"_ev": "cert_changed", "old": e.old, "new": e.new})
                return
            except OSError as e:
                if self.first_try:
                    self.want = False
                    self.q.put({"_ev": "connect_fail", "msg": str(e)})
                    return
                self.q.put({"_ev": "disconnected", "msg": str(e)})
                time.sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
                idx += 1
                continue

            # 접속 타임아웃이 소켓에 남으면 조용할 때마다 끊긴다 → 읽기 타임아웃으로 교체
            sock.settimeout(READ_TIMEOUT)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            self.sock = sock
            threading.Thread(target=self._tx_loop, args=(sock,), daemon=True).start()
            logged = False
            try:
                self._raw_send(sock, {"t": "login", "id": self.uid, "pw": self.pw})
                while self.want:
                    d = self._recv_obj(sock)
                    if d is None:
                        break
                    t = d.get("t")
                    if t == "ping":
                        self._raw_send(sock, {"t": "pong"})
                        continue
                    if t == "welcome":
                        self.logged_in.set()
                        logged = True
                    self.q.put(d)
            except Exception:
                pass
            finally:
                self.logged_in.clear()
                try:
                    sock.close()
                except Exception:
                    pass
                self.sock = None

            if not self.want:
                break
            self.first_try = False
            if logged:
                idx = 0            # 한 번이라도 붙었으면 백오프를 되돌린다
            self.q.put({"_ev": "disconnected", "msg": "연결이 끊겼습니다"})
            time.sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
            idx += 1

    def _tx_loop(self, sock):
        while self.want and self.sock is sock:
            if not self.logged_in.wait(0.2):
                continue
            try:
                obj = self.txq.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._raw_send(sock, obj)
            except Exception:
                self.txq.put(obj)          # 재연결 후 다시 보낸다
                return

    def send(self, obj, wait=False):
        """wait=True면 지금 소켓으로 바로 보낸다(종료 직전 응답처럼 순서가 중요한 것)."""
        if wait:
            sock = self.sock
            if sock is None or not self.logged_in.is_set():
                return False
            try:
                self._raw_send(sock, obj)
                return True
            except Exception:
                return False
        self.txq.put(obj)
        return True

    def logout(self):
        self.want = False
        self.logged_in.clear()
        sock = self.sock
        if sock:
            try:
                self._raw_send(sock, {"t": "logout"})
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        self.sock = None
        with self.txq.mutex:
            self.txq.queue.clear()


CHAT = ChatClient()


def chat_send(body, room=None, wait=False):
    """프로토콜 메시지 발신. room 을 안 주면 **내 방**(피제어 역할)으로 보낸다.
    ntfy 시절 발신 함수 자리를 그대로 대신한다."""
    if not chat_enabled or not CHAT.logged_in.is_set():
        return False
    target = room or MY_ROOM
    if not target:
        return False
    ok = CHAT.send({"t": "msg", "room": target, "body": body}, wait=wait)
    print(f"[발신] {body}" if ok else f"[발신 보류] {body} (연결 없음)")
    return ok


def send_report(code):
    """상황 보고 ',Z,F,(코드)' 발신 — 낚시 루틴(워커 스레드)에서 동기 호출.
    코드: s(루틴 시작) g(회수 성공) f(회수 실패)
          rs/bs(낚싯대/미끼 교체 시작) y,r/y,b(낚싯대/미끼 교체 성공)
          x,d/x,r/x,b(튕김/낚싯대/미끼 실패)"""
    chat_send(f",Z,F,{code}")



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
APP_VERSION = "260815c"
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
REGION_MAX_TIME = (1035, 916, 80, 34)   # 최대 획득 시간 'n초' (만료 교차 확인용)
TANK_COLLECT_MARGIN = 5                 # 회수 조건: cur >= max - 5 (가득차기 5칸 전)

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

    def get_frame_raw(self):
        """축소하지 않은 원본 프레임(BGR). **작은 글자를 OCR할 때 쓴다** —
        1080p로 줄인 뒤 자르면 그만큼 정보를 버리고 시작하는 셈이라, 숫자처럼
        작은 글자는 원본에서 잘라 확대하는 편이 훨씬 잘 읽힌다."""
        with self._lock:
            return self._latest


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


def click_real(coords, delay=0.5):
    """coords는 항상 FHD 좌표. to_screen이 실제 화면 좌표로 변환.
    클릭 후 대기(delay)는 **전 호출부 0.5초로 통일**되어 있다 — 호출부에서
    delay를 따로 넘기지 말고 이 기본값을 쓸 것(값 대장 참고)."""
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
            click_real(best_center)
            return True
        elif best_center:
            print(f"    [경고] 오차가 높음 ({best_diff:.1f}), 그래도 최선: {best_index}번 칸")
            click_real(best_center)
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


def _stop_watch_capture():
    """감시용 WGC 캡처 정지. 회수 루틴(run_fishing_routine)은 1440p면 필요한
    캡처를 스스로 켰다가 끄므로, 넘기기 전에 소유권을 놓아준다."""
    if game_capture is not None and game_capture.is_running:
        game_capture.stop()


def _watch_grab_region(region):
    """FHD(1920x1080) 기준 영역을 잘라 RGB로 돌려준다.

    **원본 프레임에서 자른다(중요):** 예전에는 1080p로 축소한 프레임을 잘랐는데,
    QHD 화면이면 2560→1920으로 뭉갠 뒤 130x36짜리 숫자를 읽는 셈이라 정보를
    버리고 시작한다. 좌표만 원본 배율로 환산해 자르면 화면이 더 클수록 오히려
    더 선명한 글자를 얻는다(FHD면 배율 1이라 예전과 같다)."""
    x, y, w, h = region
    if game_capture is not None and game_capture.is_running:
        frame = game_capture.get_frame_raw()
        if frame is not None and frame.size:
            fh, fw = frame.shape[:2]
            sx, sy = fw / 1920.0, fh / 1080.0
            crop = frame[int(round(y * sy)):int(round((y + h) * sy)),
                         int(round(x * sx)):int(round((x + w) * sx))]
            if crop.size:
                return cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    return grab_region_rgb(region)


OCR_NUM_SCALE = 3            # 숫자 영역 확대 배율(아래 실측 근거)


def _prep_number_ocr(img):
    """살림망 수량·획득 시간처럼 **작은 숫자**를 읽기 전 전처리: 3배 확대 + 흑백.

    실측(20260815121853.jpg의 세 영역을 1.0/0.8/0.6/0.5배로 흐리게 만들어
    10가지 전처리를 비교, 총 12케이스):
        x3+흑백 7 · x3+이진화 7 · 지금(그대로) 6 · x2 6 · 샤픈+x3 6 ·
        x3+CLAHE 6 · x3 5 · x4 4
    가장 나은 축에 속하면서 부작용이 없는 것으로 x3+흑백을 골랐다(이진화는
    배경이 밝은 화면에서 글자가 통째로 날아갈 위험이 있다).

    **숫자 화이트리스트(allowlist)는 쓰지 않는다 — 측정에서 오히려 나빴다**
    (같은 표본에서 9케이스 중 3→2로 감소: '414/480'을 '473480'으로 만드는 등
    슬래시를 잃고 숫자로 밀어붙이는 부작용). 논리적으로 좋아 보여도 실측이
    아니라고 하면 넣지 않는다."""
    if img is None or not img.size:
        return img
    big = cv2.resize(img, None, fx=OCR_NUM_SCALE, fy=OCR_NUM_SCALE,
                     interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(cv2.cvtColor(big, cv2.COLOR_RGB2GRAY), cv2.COLOR_GRAY2RGB)


def _ocr_region(region, numeric=False):
    """영역 OCR. numeric=True면 숫자 전용 전처리를 거친다(위 함수 참고).
    한글을 읽는 곳은 기존 동작 그대로 둔다 — 그쪽은 부분매칭이라 이미 관대하고,
    전처리를 바꿨을 때의 영향을 측정하지 않았기 때문이다."""
    img = _watch_grab_region(region)
    if img is None:
        return ""
    if numeric:
        img = _prep_number_ocr(img)
    return " ".join(reader.readtext(img, detail=0)).replace(" ", "")


# --- 판독 실패 진단용 크롭 저장 ---
OCR_DUMP_DIR = os.path.join(LOG_DIR, "ocr_dump")
OCR_DUMP_MAX = 40            # 폴더가 무한정 커지지 않게 상한
_ocr_dump_count = 0


def _dump_ocr_crop(tag, region, text):
    """읽기에 실패했거나 수상한 값이 나온 **그 순간의 크롭**을 파일로 남긴다.
    화면을 볼 수 없는 상태에서 원인을 가리는 유일한 방법이다 — 영역이 어긋난
    것인지, 프레임이 튄 것인지, 정말 글자가 뭉개진 것인지는 그림을 봐야 안다."""
    global _ocr_dump_count
    if _ocr_dump_count >= OCR_DUMP_MAX:
        return
    try:
        img = _watch_grab_region(region)
        if img is None or not img.size:
            return
        os.makedirs(OCR_DUMP_DIR, exist_ok=True)
        name = f"{tag}_{time.strftime('%H%M%S')}_{_ocr_dump_count:02d}.png"
        path = os.path.join(OCR_DUMP_DIR, name)
        ok, buf = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        if ok:
            buf.tofile(path)     # 한글 경로 대응(cv2.imwrite는 못 쓴다)
            _ocr_dump_count += 1
            print(f"    [진단] 판독 실패 크롭 저장: {path} (OCR='{text}')")
    except Exception:
        pass


# 마지막으로 파싱한 살림망 수량. (cur, mx)면 성공, None이면 직전 실패/미파싱.
# 원래 '실시간 수량확인'이 캐시로 읽었으나, 260728b에서 캐시 우선 분기를
# 없애며(항상 창을 불러 새로 읽음) 읽는 쪽이 사라졌다 — 지금은 기록만 되고
# 참조하는 곳이 없다.
_last_tank = None
_last_tank_ocr = ""   # 마지막으로 읽은 수량 OCR 원문(판독 실패 원인 추적용)
_last_tank_max = None # 마지막으로 받아들인 살림망 최대치(바뀌면 한 번 더 확인)


def read_tank_quantity(retries=4, delay=0.3):
    """살림망 수량 (current, max) 또는 None. 프레임 재시도 포함.

    **cur > mx인 초과 상태도 그대로 돌려준다(중요 — 260809c에서 고침):**
    최대 살림망이 더 작은 낚싯대로 바꾸면 이미 쌓인 물고기가 최대치를 넘은
    상태(예: 520/470)가 된다. 예전의 `0 <= cur <= mx` 가드는 이걸 OCR
    오인식으로 보고 버려서 None을 돌려줬는데, 그러면 감시 루프가 매 사이클
    '수량 파싱 실패'만 찍고 회수 조건 판정까지 가지 못했다. 게다가 초과
    상태에선 낚시 자체가 안 걸려 수량이 줄지도 않으므로 **스스로 빠져나올 수
    없는 영구 정체**가 된다(낚싯대 교체 직후 한순간이 아니라 계속 남는 상태라,
    교체 루틴 쪽에서만 열어주는 걸로는 부족했다).

    **값 범위로 오인식을 거르려는 가드는 두지 않는다(교훈):** 위 `cur <= mx`가
    바로 그런 가드였고, 예상 못 한 정상 값을 조용히 버려 이 버그를 만들었다.
    임의 상한(mx의 n배 따위)은 잡는 범위도 원리적이지 않다 — 앞자리가 빠진
    '52/470' 같은 더 흔한 오인식은 어차피 통과한다. 오인식은 아래 프레임
    재시도와 감시 루프의 매 사이클 재파싱으로 자연히 씻겨 나가므로 그쪽에
    맡긴다. `mx > 0`만 남기는데, 이건 오인식 휴리스틱이 아니라 구조적으로
    무의미한 값을 막는 것(max가 0이면 `cur >= mx-5`가 항상 참이라 회수가
    무한 반복된다)."""
    global _last_tank_ocr, _last_tank_max
    votes = []
    for i in range(retries):
        txt = _ocr_region(REGION_TANK_QTY, numeric=True)
        if txt:
            _last_tank_ocr = txt
            m = re.search(r'(\d+)\D+(\d+)', txt)
            if m:
                cur, mx = int(m.group(1)), int(m.group(2))
                if mx > 0:
                    votes.append((cur, mx))
                    # 최대치가 직전과 같으면 그대로 신뢰(가장 흔한 정상 경로)
                    if _last_tank_max is not None and mx == _last_tank_max:
                        return cur, mx
        if i < retries - 1:
            time.sleep(delay)

    if not votes:
        _dump_ocr_crop("tank_fail", REGION_TANK_QTY, _last_tank_ocr)
        return None

    # **최대치가 바뀌어 보이면 한 번 더 확인하고 받는다(260815c):**
    # 낚싯대를 갈면 최대치가 실제로 바뀌므로 값을 버리면 안 되지만, '417/480'이
    # '40/4'로 읽히는 식의 한 프레임 오인식도 최대치가 바뀐 것처럼 보인다.
    # 그래서 **버리지 않고 같은 값이 두 번 나오는지**만 본다 — 진짜 교체는
    # 계속 같은 값이 나오므로 통과하고, 한 번 튄 값은 걸러진다.
    best = max(set(votes), key=votes.count)
    if _last_tank_max is not None and best[1] != _last_tank_max \
            and votes.count(best) < 2:
        _dump_ocr_crop("tank_maxchange", REGION_TANK_QTY, _last_tank_ocr)
        print(f"    [수량 재확인] 최대치가 {_last_tank_max} → {best[1]} 로 바뀐 것처럼 "
              f"읽혔습니다(OCR='{_last_tank_ocr}'). 한 번 더 확인합니다.")
        time.sleep(delay)
        txt = _ocr_region(REGION_TANK_QTY, numeric=True)
        m = re.search(r'(\d+)\D+(\d+)', txt or "")
        if not m or int(m.group(2)) != best[1]:
            return None            # 다음 사이클에 다시 읽는다
    _last_tank_max = best[1]
    return best


def _tank_needs_collect(qty):
    """(cur, mx)가 살림망 한계에 닿았는가. `cur + 5 >= mx`와 같은 식이다
    (가득차기 TANK_COLLECT_MARGIN칸 전부터 True, 최대치 초과 cur > mx도 포함).

    한 기준을 세 곳이 공유한다:
      1. 감시 루프의 회수 조건
      2. 낚싯대 교체 직후 재확인(_restart_or_collect_after_rod_swap)
      3. 실시간 수량확인(_tank_check_and_resume)의 재개 클릭 생략 판정
    낚싯대 교체 트리거(만료 확인 or `cur>mx`)는 이 함수를 쓰지 **않는다** —
    한때 이 조건 전체를 트리거로 썼다가 멀쩡한 낚싯대까지 갈아 끼워 뺐고,
    지금 남은 건 더 좁은 `cur > mx`(초과)뿐이다."""
    cur, mx = qty
    return cur >= mx - TANK_COLLECT_MARGIN


def _read_gain_time(region, retries=3, delay=0.2):
    """획득 시간 영역('n초')에서 초를 읽는다. 실패면 None.

    **먼저 읽힌 값을 그냥 쓰지 않고 여러 프레임의 다수결로 고른다(260815c):**
    '15초'가 한 프레임에서 '1초'로 튀는 사례가 있었는데, 첫 성공을 그대로
    쓰면 그 한 번에 낚싯대를 갈아 끼우게 된다. 같은 비용(재시도 횟수)으로
    가장 많이 나온 값을 고르는 편이 낫다."""
    votes = []
    for i in range(retries):
        txt = _ocr_region(region, numeric=True)
        if txt:
            m = re.search(r'(\d+)', txt)
            if m:
                sec = int(m.group(1))
                if 0 < sec <= 600:
                    votes.append(sec)
        if i < retries - 1:
            time.sleep(delay)
    if not votes:
        return None
    return max(set(votes), key=votes.count)


def read_min_gain_time(retries=3, delay=0.2):
    """최소 획득 시간(초) 또는 None. 폴링 간격 + 낚싯대 만료 신호로 쓴다."""
    return _read_gain_time(REGION_MIN_TIME, retries, delay)


def read_max_gain_time(retries=3, delay=0.2):
    """최대 획득 시간(초) 또는 None. **낚싯대 만료 교차 확인 전용**이라
    `minsec == 1`일 때만 읽는다(평시 사이클의 OCR 부담은 그대로).

    왜 필요한가(실측): easyocr이 **'11초'를 '1초'로 잘못 읽는다.** 리포의
    `20260813001332.jpg`(최소 11초 / 최대 12초인 정상 낚시 화면)에서 최소가
    '1초'로 읽혀 낚싯대 교체가 헛발동했다. **확신도로는 못 거른다** — 틀린
    '1초'가 0.57~0.95, 맞는 '11초'가 0.51~0.54로 오히려 뒤집힌다. 영역을
    넓혀도(잘림이 아니라 인식 오류라) 그대로이고, 확대 배율에 따라 답이
    오락가락한다(x2/x3는 '11초', x4는 '1초').

    대신 **낚싯대 기간이 만료되면 최소·최대가 둘 다 '1초'** 라는 성질을 쓴다.
    보유 표본 실측: `expire_20260719025713`은 최소 1초/최대 1초, 정상 화면은
    30/60·20/70·11/12로 **최대가 항상 더 크다.** 따라서 최대가 1이 아니면
    최소의 '1'은 오인식이다.

    영역 (1035,916,80,34)은 후보 3개를 위 표본 전부에 돌려 고른 것 —
    60초·1초·70초·12초를 모두 맞히면서 최저 확신도가 0.83으로 가장 높았다
    (다른 후보들은 특정 표본에서 0.39까지 떨어졌다)."""
    return _read_gain_time(REGION_MAX_TIME, retries, delay)


ROD_EXPIRY_CONFIRM_READS = 2     # '1초/1초'를 다시 읽어 확인하는 횟수
ROD_EXPIRY_FROZEN_CYCLES = 10    # 낚시 중으로 보여도 이만큼 수량이 멈춰 있으면 교체 허용


def _confirm_rod_expiry(frozen_cycles):
    """'최소·최대 획득 시간이 둘 다 1초'가 **진짜 만료인지** 한 번 더 확인한다.

    **왜 필요한가(실측, 260815b):** 최소·최대가 둘 다 **15초**인 정상 화면에서
    둘 다 '1초'로 읽혀 낚싯대를 헛교체한 사례가 나왔다(사용자 제공
    `20260815121853.jpg`). 그 스크린샷을 지금 좌표·배율로 다시 읽으면
    **'15초'가 확신도 0.99~1.00으로 정확히** 나온다 — 즉 좌표나 전처리 문제가
    아니라 **실시간 캡처에서 한 프레임이 튄 것**이다. 그래서 '최대도 1초'라는
    교차 확인(260813a)만으로는 못 거른다. 한 번의 판독으로 낚싯대를 갈지 않는다:

    1. 잠깐 뒤 **다시 읽어** 최소·최대가 계속 1초인지 본다. 한 프레임짜리
       오인식은 여기서 걸러진다(진짜 만료면 계속 1초로 남아 있다).
    2. 그래도 낚시가 **진행 중**('낚시 취소')으로 보이면 만료가 아니라고 본다.
       단 그 판독마저 틀릴 수 있으므로, **살림망 수량이 오랫동안 멈춰 있으면**
       (기본 10사이클 ≈ 30초) 진행 중 표시를 무시하고 교체한다 — 진짜 만료는
       수량이 영원히 늘지 않으므로 여기서 반드시 빠져나온다.
    """
    for _ in range(ROD_EXPIRY_CONFIRM_READS):
        time.sleep(0.4)
        again_min, again_max = read_min_gain_time(), read_max_gain_time()
        if again_min != 1 or again_max != 1:
            print(f"[{time.strftime('%H:%M:%S')}] 다시 읽으니 획득 시간이 "
                  f"{again_min}초/{again_max}초 — 한 프레임 오인식으로 보고 "
                  f"낚싯대를 교체하지 않습니다.")
            return False
    if is_fishing_active() is True and frozen_cycles < ROD_EXPIRY_FROZEN_CYCLES:
        print(f"[{time.strftime('%H:%M:%S')}] 획득 시간이 1초로 읽히지만 낚시가 "
              f"진행 중이고 수량도 도는 중({frozen_cycles}회) — 교체하지 않습니다.")
        return False
    return True


def _detect_no_bait_popup():
    """'미끼가 부족합니다' 팝업 감지 ('미끼가'/'부족' 부분매칭)."""
    txt = _ocr_region(REGION_NO_BAIT)
    return ("미끼가" in txt) or ("부족" in txt)


def _detect_disconnect():
    """서버 접속 끊김 대화상자 감지 ('서버와' 부분매칭)."""
    return "서버와" in _ocr_region(REGION_DISCONNECT)


def is_fishing_active():
    """낚시 진행 여부. COORD_FISHING_BTN 글자가 '취소'면 진행 중(True),
    '시작'이면 대기 중(False). 둘 다 판독 안 되면 None(호출측이 무시).

    판정은 `_fishing_state_from_text`가 한다(그쪽 주석 참고)."""
    txt = _ocr_region(REGION_FISHING_BTN)
    state = _fishing_state_from_text(txt)
    if state is None:
        print(f"[낚시 상태 판독 실패] REGION_FISHING_BTN OCR 원문='{txt}'")
    return state


def _fishing_state_from_text(txt):
    """버튼 글자 → True(진행 중) / False(대기 중) / None(모르겠음).

    **완전일치로 보면 안 된다(실측):** 같은 버튼의 글자만 바뀌는 구조라 후보는
    '낚시 취소'와 '낚시 시작' **둘뿐인데**, easyocr이 '낚시 시작'을 '낚시시직'
    으로 읽는 일이 잦다('작'→'직'). 앞의 '낚시'를 떼고 **뒤 두 글자만** 두
    후보와 대조해, 겹치는 글자가 더 많은 쪽을 고른다:
      '시직' → 시작과 '시' 하나 겹침, 취소와는 0  → 대기 중
      '쥐소' → 취소와 '소' 하나 겹침, 시작과는 0  → 진행 중
    '시'는 '낚시'에도 들어가 그대로 세면 '시작' 쪽이 늘 유리해지므로 반드시
    앞부분을 떼고 본다. 오인식 종류를 나열하는 것보다 새 오인식에도 강하다."""
    t = (txt or "").replace(" ", "").strip("'\"`")
    tail = t.split("낚시")[-1] if "낚시" in t else t
    cancel = sum(1 for ch in "취소" if ch in tail)
    start = sum(1 for ch in "시작" if ch in tail)
    if cancel > start:
        return True
    if start > cancel:
        return False
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


def _use_card(row, col):
    """'사용하기' 클릭 -> ESC 2번(리스트 창 + 밑에 깔린 팝업). '낚시 시작'은
    누르지 않는다 — 누르기 전에 뭔가 더 확인할 게 있는 쪽(낚싯대 교체)이
    이 단계까지만 쓴다."""
    click_real(BAIT_USE_BTNS[row][col])
    press_esc(delay=0.5)
    press_esc(delay=1.0)


def _use_card_and_restart(row, col):
    """'사용하기' 클릭 -> ESC 2번(리스트 창 + 밑에 깔린 팝업) -> '낚시 시작'."""
    _use_card(row, col)
    click_real(COORD_FISHING_BTN)


def _resume_fishing():
    """ESC 3번(혹시 떠있을 팝업 정리) -> '낚시 시작' 클릭. is_fishing_active()가
    False(대기 중)로 확인된 경우에만 호출할 것."""
    press_esc(delay=0.5)
    press_esc(delay=0.5)
    press_esc(delay=0.5)
    click_real(COORD_FISHING_BTN)


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
    click_real(COORD_BAIT_LIST_BTN)

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
            click_real(COORD_BAIT_NEXT_BTN)

    fallback = found is None
    if fallback:
        print(" -> [감지 실패] 좌상단 미끼를 대신 사용합니다.")
        found = (0, 0)

    _use_card_and_restart(*found)
    send_report("x,b" if fallback else "y,b")
    print("=== [미끼 교체 완료] 낚시를 재개합니다 ===")
    set_status("fishing")


def _restart_or_collect_after_rod_swap():
    """낚싯대 교체 직후, '낚시 시작'을 누르기 전에 살림망 수량을 다시 읽는다.

    **낚싯대마다 최대 살림망 개수가 다르므로 교체로 mx가 바뀐다.** 그래서 교체
    전 판단을 그대로 쓸 수 없고, 새 낚싯대 기준으로 한 번 더 보고 갈라야 한다:
    - `cur + 5 < mx` (예: 465 -> 465/920으로 최대치가 커짐) → 회수할 필요가
      없어졌다. 그대로 '낚시 시작'을 눌러 재개한다.
    - `cur + 5 >= mx` (최대치가 그대로거나 더 작아 초과, 예: 535/470) →
      '낚시 시작'을 눌러도 낚시가 걸리지 않는다. 누르지 않고 곧바로 회수
      루틴으로 넘긴다. 회수 루틴은 맨 앞에서 is_fishing_active()로 버튼 글자를
      읽어 '낚시 시작'(대기 중)이면 취소 절차를 알아서 생략하므로, 별도의 감시
      조건 없이 바로 불러도 안전하다.
    - 판독 실패 → 평소 동작(그냥 시작)으로 폴백. 정말 초과였다면 수량이
      정체되므로 감시 루프가 다음 사이클에 다시 잡는다.

    판정은 `_tank_needs_collect`로 감시 루프와 같은 기준을 쓴다.

    반환값: 회수 루틴을 실행했으면 True, 그냥 낚시를 재개했으면 False."""
    time.sleep(0.5)          # 교체한 낚싯대 기준으로 수량 표시가 갱신될 시간
                             # (갱신 자체는 즉시. 화면 지연·순간 오류 대비분)
    qty = read_tank_quantity()
    if qty is None:
        print(" -> [살림망 확인 실패] 판독 불가 — 평소대로 낚시를 시작합니다.")
        click_real(COORD_FISHING_BTN)
        return False

    cur, mx = qty
    if not _tank_needs_collect(qty):
        print(f" -> [살림망 {cur}/{mx}] 여유 있음 — 낚시를 시작합니다.")
        click_real(COORD_FISHING_BTN)
        return False

    reason = "최대치 초과" if cur > mx else "회수 조건 충족"
    print(f" -> [살림망 {cur}/{mx}] {reason} — '낚시 시작' 대신 회수 루틴으로 넘어갑니다.")
    _stop_watch_capture()
    # 교체 루틴이 방금 ESC로 화면을 정리했고 '낚시 시작'도 누르지 않았으므로
    # 회수 루틴의 초반 절차(Enter+ESC 반복 + '낚시 취소')는 건너뛴다.
    run_fishing_routine(skip_cancel=True)
    return True


def run_rod_swap_routine():
    """낚싯대 자동 교체: ESC 3회(혹시 떠있을 팝업 정리) -> 리스트 직접 진입 ->
    '매직'/'스타'/'장미'/'푸' 중 하나라도 걸리면 채택.
    대상 미감지 시 좌상단 폴백을 쓰고 실패(x,r)로 보고한다.
    교체 후에는 '낚시 시작'을 바로 누르지 않고 살림망 수량부터 확인한다
    (_restart_or_collect_after_rod_swap 참고)."""
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
    click_real(COORD_ROD_LIST_BTN)

    cards = _find_cards_by_pattern(ROD_TARGET_PATTERN)
    if cards:
        row, col, ntext = random.choice(cards)
        print(f" -> [낚싯대 감지] {len(cards)}개 매칭, '{ntext}' 선택 ({row + 1}행 {col + 1}열)")
        _use_card(row, col)
        send_report("y,r")
        print("=== [낚싯대 교체 완료] 살림망을 확인합니다 ===")
    else:
        print(" -> [감지 실패] 좌상단 낚싯대를 대신 사용합니다.")
        _use_card(0, 0)
        send_report("x,r")
        print("=== [낚싯대 교체(폴백)] 살림망을 확인합니다 ===")

    # 바뀐 낚싯대의 최대 살림망이 더 작을 수 있으므로 시작 전에 수량 확인
    if _restart_or_collect_after_rod_swap():
        return          # 회수 루틴이 상태 문구·보고를 이미 처리했다
    set_status("fishing")


def run_fishing_routine(skip_cancel=False):
    """살림망 수거(퀴즈 풀이) 루틴. 성공 여부와 무관하게 낚시를 재시작한다.

    skip_cancel=True면 맨 앞의 Enter+ESC 반복과 '낚시 취소' 클릭을 **무조건**
    건너뛰고 바로 '살림망 확인'으로 직행한다. 낚싯대 교체 직후처럼 부르는 쪽이
    이미 ESC로 화면을 정리했고 낚시가 멈춰 있는 것이 확실한 경우에 쓴다 —
    닫을 창이 없는데 연타할 이유가 없고, 취소할 게 없는 상태에서 같은 좌표를
    누르면 오히려 낚시가 새로 시작돼 버리는 함정도 피한다."""
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
        # skip_cancel이면 판독조차 하지 않고 생략한다(부르는 쪽이 이미 앎).
        if skip_cancel:
            print("1. 낚싯대 교체 직후 — 화면이 이미 정리돼 있어 취소 절차 생략")
        elif is_fishing_active() is not False:
            print("1. 낚시 취소")
            # Enter+ESC 연타는 혹시 떠 있을 **게임 내부 창**을 닫기 위한 것.
            # (게임 창들은 게임 자체 레이아웃이며 별도 Windows 창으로 뜨지 않음)
            for _ in range(4):
                press_key(VK_RETURN, 0.5)
                press_key(VK_ESCAPE, 0.5)

            click_real(COORD_FISHING_BTN)
            print(f" -> [종료 시각] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("1. '낚시 시작' 상태 확인 — 취소 절차 생략")

        print("2. 살림망 확인")
        click_real(COORD_TANK_BTN)

        answer_slots = get_answer_slot_regions(REGION_ANSWERS)
        max_retries = 10
        attempt = 0
        verify_success = False

        while attempt < max_retries:
            attempt += 1
            print(f"\n[시도 {attempt}/{max_retries}] 퀴즈 풀이 프로세스 시작")
            click_real(COORD_MYROOM_BTN)
            solve_quiz_step(REGION_Q_LEFT, answer_slots, "왼쪽")
            time.sleep(0.5)
            solve_quiz_step(REGION_Q_RIGHT, answer_slots, "오른쪽")

            print("3. 완료 확인 (OCR 판독)")
            if verify_fishing_success():
                print(" -> [확인 성공] '살림망' 텍스트를 감지했습니다!")
                verify_success = True
                break
            if attempt < max_retries:
                # 실패했다는 건 이전 시도의 창/팝업이 화면에 그대로 남아 있다는
                # 뜻이라, '마이룸 보내기'부터 다시 눌러 봐야 같은 화면을 헛돈다.
                # ESC 3번으로 종류 불문 정리한 뒤 '살림망 확인'부터 다시 밟아
                # 퀴즈 창을 새로 연다. (ESC/클릭의 기본 0.5초 대기가 곧 재시도
                #  간격이 되므로 별도 sleep은 두지 않는다)
                print(" -> [확인 실패] 화면을 정리하고 살림망 확인부터 다시 시도합니다...")
                press_esc(delay=0.5)
                press_esc(delay=0.5)
                press_esc(delay=0.5)
                click_real(COORD_TANK_BTN)
            else:
                print(" -> [경고] 최대 재시도(10회) 초과. 강제 진행합니다.")

        print("4. 완료 확인")
        click_real(COORD_CONFIRM_BTN)

        print("5. 낚시 다시 시작")
        print(f" -> [시작 시각] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        click_real(COORD_FISHING_BTN)

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
        misread_warned = False    # 같은 오인식 상태의 로그 반복 억제

        while not self.stop_event.is_set():
            _ensure_watch_capture()

            qty = read_tank_quantity()
            _last_tank = qty          # 기록만 (성공=(cur,mx)/실패=None, 위 정의 주석 참고)
            minsec = read_min_gain_time()

            # '1초'는 낚싯대 만료 신호지만 **'11초'의 오인식일 수도** 있다
            # (실측 사례는 read_max_gain_time 참고). 만료면 최대 획득 시간도
            # 1초이므로 그걸로 교차 확인한다. 확인 안 되면 minsec을 버려
            # 폴링 간격까지 1초로 오염되는 것도 막는다(그러면 3초마다 폴링).
            maybe_expired = False
            if minsec == 1:
                maxsec = read_max_gain_time()
                maybe_expired = (maxsec == 1)
                if not maybe_expired:
                    if not misread_warned:
                        detail = ("판독 실패" if maxsec is None else f"{maxsec}초")
                        print(f"[{time.strftime('%H:%M:%S')}] 최소 획득 시간이 "
                              f"'1초'로 읽혔으나 최대 획득 시간이 {detail} — "
                              f"만료가 아니라고 보고 교체하지 않습니다.")
                        misread_warned = True
                    minsec = None
            else:
                misread_warned = False

            if minsec is not None:
                last_interval = float(minsec)
            interval = max(3.0, last_interval)

            now = time.strftime('%H:%M:%S')
            if qty is not None:
                fail_streak = 0
                set_status("fishing")

                if qty == same_qty:
                    same_count += 1
                else:
                    same_qty, same_count = qty, 1

                # --- 낚싯대 교체 트리거 (둘 중 하나면 OR로 발동) ---
                #  ① 최소·최대 획득 시간이 둘 다 '1초'(= rod_expired) — 교체가
                #     필요하면 게임이 그렇게 표시한다. 최소만 보면 '11초'를
                #     '1초'로 읽는 오인식에 걸리므로 위에서 교차 확인해 둔다.
                #  ② cur > mx (예: 570/420) — 최대치가 더 작은 낚싯대로 바뀌어
                #     살림망이 넘친 상태. 이때는 '낚시 시작'을 눌러도 낚시가 걸리지
                #     않으므로 회수만으로는 재개하지 못한다. 최대치가 더 큰 낚싯대로
                #     갈아 끼워 초과 상태 자체를 벗어난다.
                # ②는 아래 회수 조건(cur+5>=mx)의 부분집합이라 반드시 그보다
                # **먼저** 본다(뒤에 두면 회수 분기의 continue가 삼켜 도달 못 함).
                # 260811a에서 뺀 것은 ②가 아니라 회수 조건 **전체**(cur+5>=mx)였다
                # — 그건 467/470처럼 멀쩡한 낚싯대까지 갈아 끼웠다. cur>mx는 낚싯대
                # 최대치가 실제로 모자란 상태만 가리키므로 그 오작동이 없다.
                rod_expired = maybe_expired and _confirm_rod_expiry(same_count)
                if self.rod_swap and (rod_expired or qty[0] > qty[1]):
                    run_rod_swap_routine()
                    self.stop_event.wait(0.5)
                    continue

                # 회수 경로 (살림망이 찼을 때의 정상 처리)
                if _tank_needs_collect(qty):
                    print(" -> [회수 조건 충족] 회수 루틴을 실행합니다.")
                    _stop_watch_capture()
                    run_fishing_routine()
                    self.stop_event.wait(0.5)
                    continue

                # 매 사이클 팝업 확인(체크박스로 개별 on/off)
                if self.bait_swap and _detect_no_bait_popup():
                    run_bait_swap_routine()
                    self.stop_event.wait(0.5)
                    continue

                # 살림망 수량이 3회 이상 그대로 — 낚시가 멈춰있을 수 있으니 확인
                if same_count >= 3:
                    active = is_fishing_active()
                    if active is False:
                        print(f" -> [낚시 정지 감지] 살림망 수량이 {same_count}회 연속 "
                              f"동일 + '낚시 시작' 확인 — 재개합니다.")
                        _resume_fishing()
                        self.stop_event.wait(0.5)
                        continue
            else:
                fail_streak += 1
                set_status("parsefail")
                print(f"[{now}] 수량 파싱 실패({fail_streak}) — "
                      f"직전 간격 {interval:.0f}초 유지 "
                      f"(OCR 원문='{_last_tank_ocr}')")
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

        # --- 원격 제어(domichat) 상태 ---
        _cfg = load_config()
        self.server_ip = str(_cfg.get("ip") or "")
        self.saved_id = str(_cfg.get("id") or "")
        self.cert_fp = str(_cfg.get("cert_fp") or "") or None
        self.pc_list = [str(n) for n in _cfg.get("pc_list", [])
                        if re.fullmatch(r"[A-Za-z0-9_-]+", str(n))]
        self.remote_target = None        # None=로컬(이 PC), str=제어 중인 PC(ID)
        self.pending = None              # {"kind": str, "sent": epoch} 응답 대기
        self.remote_running_shown = None # 원격 시작/중지 버튼 표시 상태
        self.remote_exit_deadline = None # 원격 예약 종료(표시 전용)
        self._local_snapshot = None      # 원격 진입 전 로컬 설정 백업
        self._applying_remote = False    # 원격 상태 반영 중(트레이스 발신 억제)
        self._timer_debounce_id = None   # 원격 타이머 3초 디바운스
        self._sched_top = None           # 예약 종료 팝업(원격) 위젯들
        self._res_top = None             # 해상도 팝업 위젯들
        self._login_top = None           # 로그인 창
        self.my_room_ready = False       # 내 방(피제어용) 준비 완료 여부
        self.joining_room = None         # 제어 대상 방 입장 시도 중

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
        self._set_login_display()
        self._apply_ui_locks()

        global _status_cb
        _status_cb = self._post_status

        threading.Thread(target=self._load_ocr, daemon=True).start()
        self.root.after(300, self._auto_detect_resolution)
        self.root.after(100, self._poll_log_queue)
        self.root.after(250, self._poll_chat_queue)

        print("[시스템] domiman 시작. OCR 모델을 불러오는 중입니다...")

        # 체크박스 기본값을 전역 chat_enabled 에 반영(둘이 어긋나지 않도록 토글
        # 핸들러를 그대로 재사용한다).
        self.on_chat_toggle()
        # 로그아웃하지 않았다면 저장된 자격으로 자동 로그인한다.
        if self.server_ip and self.saved_id and load_secret_pw():
            self.root.after(400, lambda: self.do_login(
                self.server_ip, self.saved_id, load_secret_pw(), auto=True))

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
        self.bt_pc = tk.Button(f, text=MY_ID, font=FONT, bg=BTN_GRAY,
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
        # 기본 켜짐. 실제 활성화(chat_enabled)는 __init__ 끝의 on_chat_toggle()이
        # 이 값에서 맞춰가므로, 기본값은 여기 한 곳만 고치면 된다.
        self.var_chat = tk.BooleanVar(value=True)
        self.cb_chat = tk.Checkbutton(f, text="domichat 메시지", font=FONT,
                                      variable=self.var_chat, command=self.on_chat_toggle)
        self.cb_chat.grid(row=4, column=0, sticky="w", **pad)

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

        # -- 로그인/로그아웃 + 접속 정보 표시 + 예약 종료/즉시 회수 --
        # 예전의 '이름'·'채널' 입력란 자리를 그대로 쓴다: 라벨 자리에는 버튼,
        # 입력란 자리에는 **읽기 전용 표시**(위=IP, 아래=ID).
        self.bt_login = tk.Button(f, text="로그인", font=FONT, bg=BTN_GRAY,
                                  command=self.open_login)
        self.bt_login.grid(row=7, column=0, sticky="ew", **pad)
        self.var_name = tk.StringVar(value="")      # 접속된 서버 IP 표시
        self.en_name = tk.Label(f, textvariable=self.var_name, font=FONT,
                                width=18, anchor="w")
        self.en_name.grid(row=7, column=1, sticky="w", **pad)

        self.bt_sched_exit = tk.Button(f, text="예약 종료", font=FONT, bg=BTN_GRAY,
                                       command=self.on_sched_exit)
        self.bt_sched_exit.grid(row=7, column=2, sticky="ew", **pad_r)
        self.bt_collect_now = tk.Button(f, text="즉시 회수", font=FONT, bg=BTN_GRAY,
                                        command=self.on_collect_now)
        self.bt_collect_now.grid(row=7, column=3, sticky="ew", **pad)

        self.bt_logout = tk.Button(f, text="로그아웃", font=FONT, bg=BTN_GRAY,
                                   command=self.do_logout)
        self.bt_logout.grid(row=8, column=0, sticky="ew", **pad)
        self.var_chan = tk.StringVar(value="")      # 로그인한 ID 표시
        self.en_chan = tk.Label(f, textvariable=self.var_chan, font=FONT,
                                width=18, anchor="w")
        self.en_chan.grid(row=8, column=1, sticky="w", **pad)

        self.lb_name_warn = tk.Label(f, text="", font=FONT_SMALL, fg="red")
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
                  self.lb_timer_hint, self.lb_swap_hint,
                  self.lb_status_t, self.lb_status, self.lb_log_t,
                  # 접속 정보 표시(예전 이름·채널 입력란 자리, 이제 읽기 전용 라벨)
                  self.en_name, self.en_chan]
        for lb in labels:
            lb.configure(bg=t["bg"], fg=t["fg"])
        self.lb_name_warn.configure(bg=t["bg"])   # 경고는 항상 빨간 글씨
        for cb in (self.cb_chat, self.cb_rod, self.cb_bait, self.cb_logsave):
            cb.configure(bg=t["bg"], fg=t["fg"], activebackground=t["bg"],
                         activeforeground=t["fg"], selectcolor=t["select"],
                         disabledforeground="#888888")
        for en in (self.en_timer,):
            en.configure(bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                         disabledbackground=t["bg"], disabledforeground="#888888")
        self.txt_log.configure(bg=t["log_bg"], fg=t["fg"], insertbackground=t["fg"])
        # 버튼은 회색 유지, 시작/중지 색 불변
        for bt in (self.bt_pc, self.bt_update, self.bt_res_manual, self.bt_res_auto,
                   self.bt_tank_check, self.bt_sched_exit, self.bt_collect_now,
                   self.bt_dark, self.bt_exit, self.bt_log_fold, self.bt_log_clear,
                   self.bt_log_export, self.bt_login, self.bt_logout):
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

    # ---------- 로그인 / 로그아웃 ----------
    def _set_login_display(self):
        """접속 정보 표시(위=IP, 아래=ID)와 버튼 상태를 현재 상태에 맞춘다."""
        self.var_name.set(f"{self.server_ip}" if MY_ID else "(로그인 안 됨)")
        self.var_chan.set(MY_ID or "-")
        self.bt_pc.configure(text=(self.remote_target or MY_ID or "로그인 필요"))

    def open_login(self):
        """로그인 창(IP·ID·PW). 회원가입은 두지 않는다 — 계정은 domichat 쪽에서
        만들고 서버 콘솔에서 승인한다."""
        if self._login_top is not None:
            try:
                self._login_top.lift()
                return
            except Exception:
                self._login_top = None
        top = tk.Toplevel(self.root)
        self._login_top = top
        top.title("domichat 로그인")
        top.resizable(False, False)
        top.grab_set()
        t = THEME["dark" if self.dark else "light"]
        top.configure(bg=t["bg"])

        body = tk.Frame(top, bg=t["bg"], padx=14, pady=12)
        body.pack(fill="both", expand=True)
        vars_ = []
        for i, (label, init, show) in enumerate(
                (("IP 주소", self.server_ip, None), ("ID", self.saved_id, None),
                 ("PW", "", "*"))):
            tk.Label(body, text=label, font=FONT, bg=t["bg"], fg=t["fg"]).grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            v = tk.StringVar(value=init)
            e = tk.Entry(body, textvariable=v, font=FONT, width=22, show=show,
                         bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"])
            e.grid(row=i, column=1, pady=3)
            vars_.append(v)
        msg = tk.Label(body, text="", font=FONT_SMALL, bg=t["bg"], fg="#d9302e",
                       wraplength=240, justify="left")
        msg.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._login_msg = msg

        btns = tk.Frame(body, bg=t["bg"])
        btns.grid(row=4, column=0, columnspan=2, pady=(10, 0))

        def do_ok():
            ip, uid, pw = (v.get().strip() for v in vars_)
            if not ip or not uid or not pw:
                msg.configure(text="IP·ID·PW를 모두 입력하세요.")
                return
            self.do_login(ip, uid, pw)

        tk.Button(btns, text="로그인", font=FONT, bg=BTN_GRAY, width=8,
                  command=do_ok).pack(side="left", padx=4)
        tk.Button(btns, text="취소", font=FONT, bg=BTN_GRAY, width=8,
                  command=self._close_login).pack(side="left", padx=4)
        top.protocol("WM_DELETE_WINDOW", self._close_login)

    def _close_login(self):
        if self._login_top is not None:
            try:
                self._login_top.destroy()
            except Exception:
                pass
        self._login_top = None
        self._login_msg = None

    def do_login(self, ip, uid, pw, auto=False):
        """domiserver 접속 시도. 'IP:포트' 형식도 받는다."""
        port = CHAT_PORT
        if ip.count(":") == 1:
            ip, _, p = ip.partition(":")
            if p.isdigit():
                port = int(p)
        self._pending_login = (ip.strip(), port, uid, pw, auto)
        self.server_ip = ip.strip() if port == CHAT_PORT else f"{ip.strip()}:{port}"
        print(f"[domichat] {self.server_ip} 에 '{uid}'로 접속합니다...")
        CHAT.logout()
        globals()["CHAT"] = ChatClient()
        CHAT.start(ip.strip(), port, uid, pw, pinned=self.cert_fp)

    def do_logout(self):
        """로그인 정보를 지운다. 다시 실행해도 자동 로그인하지 않는다."""
        global MY_ID, MY_ROOM
        if self.remote_target:
            self._exit_remote()
        CHAT.logout()
        MY_ID, MY_ROOM = "", ""
        self.my_room_ready = False
        self.saved_id = ""
        clear_secret_pw()
        save_config(self.server_ip, "", self.pc_list)
        self._set_login_display()
        self._apply_ui_locks()
        print("[domichat] 로그아웃했습니다.")

    # ---------- 체크박스 ----------
    def on_chat_toggle(self):
        """'domichat 메시지' — 수/발신 전체 스위치. 끄면 접속을 끊는다."""
        global chat_enabled
        chat_enabled = self.var_chat.get()
        if chat_enabled:
            print(" -> [설정] domichat 수/발신 활성화")
            if MY_ID == "" and self.server_ip and self.saved_id and load_secret_pw():
                self.do_login(self.server_ip, self.saved_id, load_secret_pw(),
                              auto=True)
        else:
            print(" -> [설정] domichat 비활성화")
            CHAT.logout()

    # ---------- 실시간 수량 확인 ----------
    def _tank_check_and_resume(self, on_result):
        """실시간 수량확인의 공용 로직(로컬 버튼 + 원격 N 질의가 공유).
        회수 루틴 시작 때처럼 **게임 창을 앞으로 불러** 살림망 수량을 새로
        읽고(3초 렌더 대기), **동시에 낚시 취소/시작 버튼을 확인해 '낚시 시작'
        (대기 중)이면 눌러서 낚시를 재개**한다('낚시 취소'=진행 중이면 그대로
        둔다). 결과 (cur,mx)|None을 on_result로 전달.

        **항상 창 호출 방식으로 동작한다(워커 가동 여부와 무관):** 과거엔
        감시 워커가 돌고 있으면 캐시(_last_tank)만 즉시 돌려주고 창을 안
        건드렸으나, 워커 스레드는 살아 있는데 게임 낚시가 조용히 멈춘 경우
        수량이 정체돼도 창을 안 불러 확인/재개가 안 되는 문제가 있었다. 이제는
        신호가 오면 무조건 창을 앞으로 불러 새로 읽고 낚시 상태를 확인해
        '낚시 시작'이면 재개한다. (정상 진행 중이면 is_fishing_active()가 True라
        재개 클릭은 나가지 않으므로, 워커의 정상 낚시를 방해하지 않는다.)"""
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
                # 낚시 상태 확인 후 '낚시 시작'(대기 중)이면 눌러서 재개.
                # 단 회수해야 할 수량(특히 최대치 초과)이면 눌러도 낚시가 안
                # 걸리므로 헛클릭 대신 알리기만 한다 — 감시 워커가 돌고 있으면
                # 다음 사이클에 회수 루틴이 받아간다.
                if qty is not None and _tank_needs_collect(qty):
                    print(f"[낚시 상태 확인] 살림망 {qty[0]}/{qty[1]} — 회수가 필요한 "
                          "수량이라 재개 클릭을 생략합니다(회수 후 자동 재개).")
                elif is_fishing_active() is False:
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
        name = MY_ID

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
    # domichat 프로토콜 — 수신 디스패치 (메인 스레드, 250ms 주기)
    # 스트림 스레드가 큐에 넣은 메시지를 메인 스레드에서 꺼내 처리(tkinter 안전).
    # ============================================================
    def _poll_chat_queue(self):
        """domichat 수신·상태 이벤트를 메인 스레드에서 처리(tkinter 안전)."""
        try:
            while True:
                d = CHAT.q.get_nowait()
                try:
                    self._on_chat_event(d)
                except Exception:
                    print(f"[경고] 메시지 처리 실패: {d}")
                    traceback.print_exc()
        except queue.Empty:
            pass
        self._check_pending_timeout()
        self.root.after(250, self._poll_chat_queue)

    def _on_chat_event(self, d):
        """서버 프레임과 클라이언트 내부 사건을 분배한다."""
        ev = d.get("_ev")
        if ev == "connect_fail":
            return self._login_failed(f"서버에 접속할 수 없습니다. ({d.get('msg')})")
        if ev == "cert_changed":
            return self._login_failed(
                "서버 인증서가 바뀌었습니다. 서버를 재설치한 것이 아니라면 "
                "누군가 가로채는 중일 수 있습니다.")
        if ev == "cert_pinned":
            self.cert_fp = d.get("fp")
            save_config(self.server_ip, self.saved_id, self.pc_list)
            return
        if ev == "disconnected":
            if MY_ID:
                print("[domichat] 연결이 끊겼습니다. 다시 접속하는 중...")
                self.my_room_ready = False
            return
        if ev:
            return

        t = d.get("t")
        if t == "welcome":
            return self._on_chat_welcome(d)
        if t == "msg":
            frm = d.get("from")
            if frm == MY_ID:
                return                      # 서버는 발신자에게도 돌려준다 — 내 것은 무시
            return self._dispatch_msg(frm, d.get("body", ""))
        if t == "ok":
            of = d.get("of")
            if of == "room_create":
                return self._after_my_room(d.get("room"))
            return
        if t == "joined":
            room = d.get("room")
            if room == MY_ROOM:
                return self._after_my_room(room)
            if room == self.joining_room:
                self.joining_room = None
                CHAT.send({"t": "sub", "room": room, "on": True})
                print(f"[원격] '{self.remote_target}' 채팅방에 들어갔습니다. 상태를 질의합니다...")
                self._send_command("S", "connect")
            return
        if t == "denied":
            room, reason = d.get("room"), d.get("reason")
            if room == self.joining_room:
                self.joining_room = None
                print(f"[원격] 입장할 수 없습니다({reason}). 이 PC 제어로 복귀합니다.")
                self._exit_remote()
            return
        if t == "member":
            # 방장(피제어 PC)이 방에서 빠지면 제어를 끝낸다
            if (self.remote_target and not d.get("in")
                    and d.get("id") == self.remote_target
                    and d.get("room") == room_of(self.remote_target)):
                print(f"[원격] {self.remote_target}가 접속을 종료했습니다. "
                      "이 PC 제어로 복귀합니다.")
                self._exit_remote()
            return
        if t == "room_deleted":
            if self.remote_target and d.get("room") == room_of(self.remote_target):
                self._exit_remote()
            elif d.get("room") == MY_ROOM:
                self.my_room_ready = False
                self._ensure_my_room()
            return
        if t == "error":
            code, msg = d.get("code"), d.get("msg", "")
            if code == "room_name_taken":
                # 내 방이 이미 있다 — 만들 필요 없이 들어가면 된다
                return CHAT.send({"t": "join", "room": MY_ROOM, "pw": ROOM_PW})
            if code in ("bad_login", "already_online", "disabled"):
                return self._login_failed(msg or "로그인에 실패했습니다.")
            print(f"[domichat] 오류: {msg} ({code})")
            return

    def _login_failed(self, msg):
        CHAT.logout()
        print(f"[domichat] {msg}")
        if self._login_top is not None and self._login_msg is not None:
            try:
                self._login_msg.configure(text=msg)
            except Exception:
                pass
        else:
            self.lb_name_warn.configure(text=msg)
            self.lb_name_warn.grid()
            self.root.after(6000, self.lb_name_warn.grid_remove)

    def _on_chat_welcome(self, d):
        """로그인 성공 — 자격을 저장하고 내 방을 준비한다."""
        global MY_ID, MY_ROOM
        MY_ID = d.get("id") or ""
        MY_ROOM = room_of(MY_ID)
        self.saved_id = MY_ID
        pend = getattr(self, "_pending_login", None)
        if pend:
            save_secret_pw(pend[3])
            self._pending_login = None
        save_config(self.server_ip, MY_ID, self.pc_list)
        self._close_login()
        self._set_login_display()
        self._apply_ui_locks()
        print(f"[domichat] '{MY_ID}'로 로그인했습니다. (서버 {self.server_ip})")

        rooms = {r.get("name") for r in d.get("rooms", [])}
        if MY_ROOM in rooms:
            CHAT.send({"t": "join", "room": MY_ROOM, "pw": ROOM_PW})
        else:
            self._ensure_my_room()
        if self.remote_target:            # 재접속이면 제어하던 방에도 다시 들어간다
            self.joining_room = room_of(self.remote_target)
            CHAT.send({"t": "join", "room": self.joining_room, "pw": ROOM_PW})

    def _ensure_my_room(self):
        """내 방(domi_fishing_{내ID})을 만든다. 이미 있으면 error(room_name_taken)가
        오고 그때 입장한다 — '없으면 만들고 있으면 들어간다'."""
        if not MY_ROOM:
            return
        CHAT.send({"t": "room_create", "name": MY_ROOM, "kind": "pw", "pw": ROOM_PW})

    def _after_my_room(self, room):
        if room != MY_ROOM or self.my_room_ready:
            return
        self.my_room_ready = True
        CHAT.send({"t": "sub", "room": MY_ROOM, "on": True})
        print(f"[domichat] 내 채팅방 '{MY_ROOM}' 준비 완료 — 원격 제어를 받을 수 있습니다.")

    def _dispatch_msg(self, title, body):
        """수신 메시지 분배. 규격에 맞지 않는 메시지는 전부 무시.
        - 원격 제어 중(dB): 내 앞으로 온 응답({내이름},Z,*)과 제어 대상의
          보고(,Z,F,*)만 처리. 내 앞으로 온 '명령'은 무시(제어에만 집중).
        - 로컬 모드(d3): 내 이름 앞으로 온 명령만 처리해 실행·응답."""
        parts = [p.strip() for p in body.split(",")]
        if len(parts) < 2:
            return

        if self.remote_target:
            if (parts[0] == MY_ID and parts[1] == "Z"
                    and title == self.remote_target):
                self._handle_remote_reply(parts[2:], body)
            elif (parts[0] == "" and parts[1] == "Z"
                  and len(parts) >= 3 and parts[2] == "F"
                  and title == self.remote_target):
                self._handle_remote_report(parts[3:])
            return

        # 로컬 모드: 나를 지목한 명령만 처리 (Z=응답은 명령이 아님)
        if parts[0] == MY_ID and parts[1] != "Z":
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
            chat_send(f"{sender},Z,{tail}")

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
            chat_send(f"{sender},Z,Q", wait=True)   # 종료 전 동기 발신
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
        chat_send(f"{self.remote_target},{cmdbody}", room_of(self.remote_target))
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
        print("[원격] 응답이 없습니다")
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
                chat_send(f"{target},Y,0", room_of(target))
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
        print(f"[원격 응답] {raw}")
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
            lbx.insert("end", f"{MY_ID} (이 PC)")
            for n in self.pc_list:
                lbx.insert("end", n)
            target = select_name or self.remote_target or MY_ID
            idx = 0
            if target != MY_ID and target in self.pc_list:
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
            if name == MY_ID or name in self.pc_list:
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
            target = MY_ID if not sel or sel[0] == 0 else self.pc_list[sel[0] - 1]
            save_config(self.server_ip, MY_ID, self.pc_list)
            top.destroy()
            if target == MY_ID:
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
        """원격 제어 모드 진입: 대상 PC의 채팅방에 들어간다.
        입장에 성공하면(joined) 구독하고 예전처럼 S 질의로 상태를 맞춘다."""
        global chat_enabled
        if not MY_ID:
            print("[원격] 먼저 domichat에 로그인하세요.")
            return
        if self._local_snapshot is None:
            self._local_snapshot = {
                "timer": self.var_timer.get(),
                "log": self.var_logsave.get(),
                "rod": self.var_rod.get(),
                "bait": self.var_bait.get(),
                "res_label": self.lb_res.cget("text"),
                "status": self.current_status_key,
            }
        if not self.var_chat.get():          # 메시지는 강제 활성화 후 봉인
            self.var_chat.set(True)
            chat_enabled = True
            print(" -> [설정] 원격 제어를 위해 domichat 메시지를 활성화합니다.")

        self.remote_target = target
        self.remote_running_shown = False
        self.remote_exit_deadline = None
        self.bt_pc.configure(text=target)
        self._set_start_button_remote()
        self.joining_room = room_of(target)
        print(f"\n[원격] '{target}' 제어를 시작합니다. 채팅방에 입장합니다...")
        CHAT.send({"t": "join", "room": self.joining_room, "pw": ROOM_PW})

    def _exit_remote(self):
        """원격 제어 종료: 상대 방에서 나오고 이 PC 제어로 복귀, 로컬 설정 복원."""
        if self.remote_target:
            room = room_of(self.remote_target)
            CHAT.send({"t": "sub", "room": room, "on": False})
            CHAT.send({"t": "leave", "room": room})
        self.joining_room = None
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
            finally:
                self._applying_remote = False
        self._set_login_display()
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
        # 'domichat 메시지' — 원격에선 항상 봉인, 로컬에선 실행 중 봉인
        st(self.cb_chat, (not running) and (not remote))
        # 로그인/로그아웃은 **낚시 중에도 잠그지 않는다** — 원격 제어가 끊겼을 때
        # 되살릴 수단이 필요하기 때문이다. 원격 제어 중에만 봉인한다.
        st(self.bt_login, not remote and not MY_ID)
        st(self.bt_logout, not remote and bool(MY_ID))
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
                chat_send(f"{target},Y,0", room_of(target))

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
        global chat_enabled
        chat_enabled = self.var_chat.get()

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
            save_config(self.server_ip, MY_ID, self.pc_list)
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
            save_config(self.server_ip, MY_ID, self.pc_list)
            # 방장이 방을 나가는 것을 상대가 곧바로 알도록 정상 로그아웃을 보낸다
            # (이걸 안 하면 서버가 최대 45초 뒤에야 이탈로 처리한다).
            CHAT.logout()
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
