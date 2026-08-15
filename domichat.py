# -*- coding: utf-8 -*-
"""domichat.py — domiserver 채팅 클라이언트 (tkinter GUI)

설계 문서는 domichat.md. 요약:
- 로그인 창 ⇄ 채팅방 리스트는 **같은 창에서 프레임만 교체**한다(창이 새로 뜨지 않음).
- 채팅방은 방마다 별도 Toplevel이며 여러 개를 동시에 열 수 있다.
  **리사이즈·최대화가 되는 창은 채팅방 창뿐**이고 나머지는 고정 크기.
- 네트워크는 세션 스레드가 담당하고 GUI와는 큐로만 오간다(tkinter는 메인 스레드 전용).
- 구독한 방은 창을 닫아도 계속 받아 로컬에 기록한다. 앱을 끄면 그동안의 대화는
  공백으로 남는다(백그라운드 동작을 상정하지 않는다 — 서버가 대화를 저장하지 않음).
- 계정 PW·방 비밀번호는 Windows DPAPI로 암호화해 보관한다(같은 PC·같은 사용자만 복호화).

`ttk` 위젯은 쓰지 않는다 — OS 테마 엔진이 배경색 지정을 무시해 블랙 테마가 깨진다.
"""

import base64
import ctypes
import hashlib
import io
import json
import os
import queue
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox

# **exe 번들에 없을 수 있는 것은 최상위 import 하지 않는다(함정, 260815c에서 고침):**
# domichat.py 는 exe에 데이터로만 들어가 runpy로 실행되므로 PyInstaller의 정적 분석
# 대상이 아니다. 여기서 새로 import 한 모듈이 번들에 없으면 **스크립트로는 되는데
# exe에서만 시작조차 못 한다.** 실제로 `import uuid` 하나 때문에 업데이트한 exe가
# ModuleNotFoundError로 죽었다(uuid 는 os.urandom 으로 대체해 아예 없앴다).
# filedialog·Pillow는 없으면 그 기능만 끄고 계속 돌아가게 한다.
try:
    from tkinter import filedialog
except Exception:
    filedialog = None

# 이미지 기능(PNG 변환·클립보드 붙여넣기·인라인 표시)에만 Pillow가 필요하다.
# 없으면 이미지 기능만 비활성되고 채팅은 그대로 동작한다.
try:
    from PIL import Image, ImageGrab
    HAS_PIL = True
except Exception:
    Image = ImageGrab = None
    HAS_PIL = False

# === [1. 상수 · 경로 · 테마] ===

APP_NAME = "domichat"
DEFAULT_PORT = 47821
FRAME_HEAD = struct.Struct(">IB")
MAX_FRAME = 1024 * 1024
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)     # 재연결 대기(초) — 마지막 값으로 고정 반복
# 접속 후 읽기 타임아웃(초). 서버가 15초마다 ping을 보내므로 이만큼 조용하면 죽은 연결이다.
# **접속 타임아웃을 그대로 두면 안 된다(함정):** create_connection(timeout=)이 준
# 타임아웃은 연결 뒤에도 소켓에 남아, 그 시간만큼 침묵하면 recv가 예외를 던진다.
# 그래서 접속 직후 이 값으로 다시 설정한다. 안 하면 대화가 없을 때 6초마다
# 끊고 재접속하는 고리에 빠진다(실측: 서버 로그에 접속/해제가 계속 반복됨).
READ_TIMEOUT = 60.0
CONNECT_TIMEOUT = 6.0
HISTORY_LOAD = 500                        # 창을 열 때 로컬 기록에서 읽어올 최대 줄 수
MSG_MAX = 4000

# --- 이미지 ---
FILE_HEAD = struct.Struct(">16sI")        # 'B' 프레임 머리: fid 16바이트 + seq 4바이트
IMG_CHUNK = 64 * 1024                     # 청크 크기(서버 상한과 같음)
IMG_MAX_BYTES = 32 * 1024 * 1024          # 변환 후 PNG 최대 크기(서버 기본값과 같음)
IMG_MAX_SIDE = 2560                       # 이 이상 크면 줄여서 보낸다(PNG는 쉽게 커진다)
IMG_VIEW_W = 320                          # 말풍선 안 표시 폭
IMG_EXTS = [("이미지", "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff"),
            ("모든 파일", "*.*")]


def new_fid():
    """전송 식별자(32자리 hex). uuid 모듈을 쓰지 않는다 — 위 import 주석 참고."""
    return os.urandom(16).hex()

# 데이터 폴더. DOMICHAT_DIR 환경변수로 바꿀 수 있다(테스트·다중 프로필용).
APP_DIR = os.environ.get("DOMICHAT_DIR") or os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SECRETS_PATH = os.path.join(APP_DIR, "secrets.dat")
HIST_DIR = os.path.join(APP_DIR, "history")
INDEX_PATH = os.path.join(HIST_DIR, "index.json")

BG = "#111111"            # 앱 배경(블랙)
BG_SOFT = "#1E1E1E"       # 입력란·리스트 행 배경
BG_ROW = "#191919"
FG = "#FFFFFF"
FG_DIM = "#888888"
BUB_OTHER = "#FFFFFF"     # 남의 말풍선
BUB_MINE = "#BFE8FF"      # 내 말풍선(하늘색)
BUB_TEXT = "#000000"      # 말풍선 글자는 둘 다 검정
BTN_BG = "#2C2C2C"
BTN_ACTIVE = "#3A3A3A"
ACCENT = "#4EA1D3"

FONT = ("맑은 고딕", 10)
FONT_SMALL = ("맑은 고딕", 8)
FONT_BIG = ("맑은 고딕", 12, "bold")

SORTS = ("이름 오름차순", "이름 내림차순", "오래된 순", "최근 순")
KIND_LABEL = {"open": "공개", "pw": "제한", "allow": "제한", "approve": "제한"}
KIND_DETAIL = {"open": "공개", "pw": "비밀번호", "allow": "사전 승인",
               "approve": "사후 승인"}

if os.name == "nt":       # 흐릿한 글자 방지. root 생성 전에 해둬야 한다
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# === [1-1. 버전 확인 + 수동 업데이트 (GitHub raw)] ===
# 버전 문자열 = "YYMMDD" + 알파벳 1글자. 자릿수가 고정이라 문자열 비교가 곧
# 날짜순+알파벳순이다(domiman과 같은 규약).
#
# exe 배포판은 domichat_launcher.py(빌드되는 domichat.exe 본체)가 이 파일
# (domichat.py)을 매 실행마다 runpy로 읽어서 돌린다. 실행 중인 exe 자체는
# Windows에서 덮어쓸 수 없으므로, 업데이트는 **옆에 있는 평범한 .py 파일만**
# 통째로 교체하는 것으로 끝난다. 업데이트는 사용자가 ⟳ 버튼을 눌러야만 한다.
#
# 리포는 domiman과 공유하고 **파일 이름으로 구분**한다(domichat_version.txt /
# domichat.py) — 리포를 새로 만들지 않아도 되고 domiman 업데이트와 섞이지 않는다.
APP_VERSION = "260815e"
UPDATE_REPO = "cheongbaek/domiman"
UPDATE_BRANCH = "main"
UPDATE_RAW_BASE = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}"
UPDATE_VERSION_FILE = "domichat_version.txt"
UPDATE_SOURCE_FILE = "domichat.py"


def _http_text(url, timeout=12):
    """표준 라이브러리만으로 텍스트 받기(requests 의존 없음)."""
    req = urllib.request.Request(url, headers={"User-Agent": "domichat"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_latest_version():
    """리포의 domichat_version.txt. 형식이 다르거나 통신 실패면 None."""
    try:
        v = _http_text(f"{UPDATE_RAW_BASE}/{UPDATE_VERSION_FILE}").strip()
        return v if re.fullmatch(r"\d{6}[a-z]", v) else None
    except Exception:
        return None


def download_latest_source():
    """리포의 domichat.py 원본 전체. 실패면 None."""
    try:
        src = _http_text(f"{UPDATE_RAW_BASE}/{UPDATE_SOURCE_FILE}", timeout=20)
        return src if src.strip() else None
    except Exception:
        return None


def apply_update_and_restart(new_source):
    """새 소스로 이 파일을 원자적으로 교체하고 재시작한다(반환하지 않음).
    쓰다가 중단돼도 os.replace 직전까지는 원본이 그대로 남아 안전하다.
    frozen(exe)이면 런처 자신을 인자 없이 다시 띄워 방금 교체된 domichat.py를
    읽게 하고, 스크립트 모드면 같은 인터프리터로 이 파일을 재실행한다.

    **교체 전 이전 버전을 `.bak`으로 남긴다(중요):** 새 버전이 시작조차 못 하면
    (예: exe 번들에 없는 모듈을 import) 앱을 켤 수 없어 ⟳ 버튼으로 되돌릴 수도
    없다. 런처가 실행 실패를 감지하면 이 백업으로 자동 복구한다
    (domichat_launcher.py 참고)."""
    target = os.path.abspath(__file__)
    tmp = target + ".new"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(new_source)
    try:
        with open(target, "r", encoding="utf-8") as cur:
            old = cur.read()
        with open(target + ".bak", "w", encoding="utf-8") as fp:
            fp.write(old)
    except Exception as e:
        print(f"[업데이트] 백업 실패(계속 진행): {e}")
    os.replace(tmp, target)
    if getattr(sys, "frozen", False):
        subprocess.Popen([sys.executable])
    else:
        subprocess.Popen([sys.executable, target])
    os._exit(0)


# === [2. 프레임 · 소켓 클라이언트] ===
# domiserver와 같은 규격: [길이 4바이트][종류 1바이트][본문]. 종류 'T' = UTF-8 JSON.


def _local_addrs():
    """이 PC가 가진 주소들. 서버와 같은 PC에서 '자기 공인 IP'로 접속할 때 쓴다."""
    addrs = {"127.0.0.1", "localhost", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(info[4][0])
    except Exception:
        pass
    return addrs


def connect_any(ip, port, timeout=CONNECT_TIMEOUT):
    """접속. 실패했는데 그 주소가 **이 PC 자신의 주소**면 127.0.0.1로 한 번 더 시도한다.
    domiserver가 도는 PC에서 같은 IP를 적어 접속하는 경우를 보장하기 위한 것으로,
    공유기·방화벽이 자기 공인 IP로의 되돌림(헤어핀)을 막아도 붙는다."""
    try:
        return socket.create_connection((ip, port), timeout)
    except OSError:
        if ip in _local_addrs():
            return socket.create_connection(("127.0.0.1", port), timeout)
        raise


class CertChanged(Exception):
    """고정해둔 서버 인증서 지문과 다르다 — 중간자이거나 서버를 재설치한 것이다."""

    def __init__(self, host, old, new):
        super().__init__(f"{host}: {old[:16]}… → {new[:16]}…")
        self.host, self.old, self.new = host, old, new


def _tls_context():
    """자체 서명 인증서를 쓰므로 체인·호스트명 검증은 끄고, **지문 고정**으로 신뢰한다
    (SSH와 같은 방식). 검증을 끈다고 암호화가 약해지는 건 아니며, 중간자 방어는
    지문 비교가 담당한다."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # **TLS 1.2로 고정 + 재협상 금지(중요):** 이 클라이언트는 수신 스레드가 읽고
    # 송신 스레드가 쓰므로 **한 SSL 소켓을 두 스레드가 읽고 쓴다.** TLS 1.3은
    # 핸드셰이크 후에도 세션 티켓·KeyUpdate가 오가 읽기 경로가 쓰기 상태를
    # 건드리기 때문에, 이 구조에서는 record layer가 깨진다(실측: 서버에
    # RECORD_LAYER_FAILURE, 이쪽은 갑작스러운 EOF). 1.2는 방향별 record layer가
    # 분리돼 안전하다. 서버(domiserver._pin_tls12)와 같은 이유·같은 설정이다.
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    if no_reneg:
        ctx.options |= no_reneg
    return ctx


def connect_secure(ip, port, want_tls, pinned):
    """(소켓, 지문|None) 반환. 지문이 고정값과 다르면 CertChanged.
    서버가 TLS를 안 쓰는 경우엔 평문으로 다시 붙는다(전환기 대응)."""
    raw = connect_any(ip, port)
    if not want_tls:
        return raw, None
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
    """세션 하나(접속 유지 + 자동 재연결). 수신·상태는 전부 self.q로 보낸다.

    큐에 들어가는 것 두 가지:
      - 서버 프레임 그대로 (dict, 't' 키)
      - 클라이언트 내부 사건 (dict, '_ev' 키): connected / connect_fail /
        disconnected / register_result
    """

    def __init__(self):
        self.q = queue.Queue()
        self.txq = queue.Queue()
        self.sock = None
        self.ip = None
        self.port = DEFAULT_PORT
        self.uid = None
        self.pw = None
        self.want = False          # 접속을 유지하고 싶은 상태(로그아웃하면 False)
        self.logged_in = threading.Event()
        self.first_try = True
        self.want_tls = True
        self.pinned = None         # 고정해둔 서버 인증서 지문(없으면 첫 접속에 기억)
        self._send_lock = threading.Lock()

    # ---------- 저수준 ----------
    def _raw_send(self, sock, obj):
        if isinstance(obj, tuple):          # ("B", fid, 프레임바이트) = 이미지 청크
            obj = obj[2]
        if isinstance(obj, bytes):          # 이미 프레임으로 만들어진 것
            with self._send_lock:
                sock.sendall(obj)
            return
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
        """'T'는 JSON dict, 'B'(이미지 청크)는 내부 이벤트 dict로 돌려준다."""
        head = self._recv_exact(sock, FRAME_HEAD.size)
        if head is None:
            return None
        ln, typ = FRAME_HEAD.unpack(head)
        if ln > MAX_FRAME:
            raise OSError("프레임 과대")
        body = self._recv_exact(sock, ln) if ln else b""
        if body is None:
            return None
        kind = chr(typ)
        if kind == "B":
            if len(body) < FILE_HEAD.size:
                return {}
            raw_fid, seq = FILE_HEAD.unpack(body[:FILE_HEAD.size])
            return {"t": "bin", "fid": raw_fid.hex(), "seq": seq,
                    "data": body[FILE_HEAD.size:]}
        if kind != "T":
            return {}
        return json.loads(body.decode("utf-8"))

    # ---------- 회원가입(단발) ----------
    def register(self, ip, port, uid, pw):
        def run():
            try:
                sock, fp = connect_secure(ip, port, self.want_tls, self.pinned)
                if fp and not self.pinned:
                    self.q.put({"_ev": "cert_pinned", "host": f"{ip}:{port}",
                                "fp": fp})
            except CertChanged as e:
                self.q.put({"_ev": "register_result", "ok": False,
                            "msg": f"서버 인증서가 바뀌었습니다({e}). 로그인으로 확인하세요."})
                return
            except OSError as e:
                self.q.put({"_ev": "register_result", "ok": False,
                            "msg": f"서버에 접속할 수 없습니다. ({e})"})
                return
            try:
                self._raw_send(sock, {"t": "register", "id": uid, "pw": pw})
                sock.settimeout(8)
                while True:
                    d = self._recv_obj(sock)
                    if d is None:
                        raise OSError("연결이 끊겼습니다")
                    if d.get("t") == "ok":
                        self.q.put({"_ev": "register_result", "ok": True})
                        return
                    if d.get("t") == "error":
                        self.q.put({"_ev": "register_result", "ok": False,
                                    "msg": d.get("msg", "가입에 실패했습니다.")})
                        return
            except Exception as e:
                self.q.put({"_ev": "register_result", "ok": False, "msg": str(e)})
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()

    # ---------- 세션 ----------
    def start(self, ip, port, uid, pw, want_tls=True, pinned=None):
        self.ip, self.port, self.uid, self.pw = ip, port, uid, pw
        self.want_tls, self.pinned = want_tls, pinned
        self.want = True
        self.first_try = True
        threading.Thread(target=self._session_loop, daemon=True).start()

    def _session_loop(self):
        idx = 0
        while self.want:
            try:
                sock, fp = connect_secure(self.ip, self.port, self.want_tls,
                                          self.pinned)
                if fp and not self.pinned:
                    # 첫 접속 — 이 지문을 기억해 다음부터 고정한다(TOFU)
                    self.pinned = fp
                    self.q.put({"_ev": "cert_pinned",
                                "host": f"{self.ip}:{self.port}", "fp": fp})
            except CertChanged as e:
                self.want = False
                self.q.put({"_ev": "cert_changed", "host": e.host,
                            "old": e.old, "new": e.new})
                return
            except OSError as e:
                if self.first_try:
                    self.want = False
                    self.q.put({"_ev": "connect_fail",
                                "msg": f"서버에 접속할 수 없습니다. ({e})"})
                    return
                self.q.put({"_ev": "disconnected", "msg": str(e)})
                time.sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
                idx += 1
                continue

            # 접속 타임아웃을 읽기 타임아웃으로 교체(위 READ_TIMEOUT 설명 참고)
            sock.settimeout(READ_TIMEOUT)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            self.sock = sock
            self.q.put({"_ev": "connected", "retry": not self.first_try})
            tx = threading.Thread(target=self._tx_loop, args=(sock,), daemon=True)
            tx.start()
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

            # 세션이 끝났으면 보내다 만 이미지는 포기한다(재접속 고리 방지)
            dropped = self._drop_pending_chunks()
            if dropped:
                self.q.put({"_ev": "file_aborted", "fids": dropped})

            if not self.want:
                break
            self.first_try = False
            # 한 번이라도 로그인에 성공했다면 백오프를 되돌린다 — 안 그러면 일시적인
            # 끊김이 몇 번 겹친 뒤부터 계속 30초씩 기다리게 된다.
            if logged:
                idx = 0
            self.q.put({"_ev": "disconnected", "msg": "연결이 끊겼습니다"})
            time.sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
            idx += 1

    def _tx_loop(self, sock):
        """로그인이 끝난 뒤부터 큐를 흘려보낸다. 연결이 끊긴 동안 입력한 메시지는
        큐에 남아 재로그인 후 순서대로 나간다."""
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
                # 채팅 메시지는 되돌려 넣어 재연결 후 보내지만, **이미지 청크는 버린다**
                # (이어 보낼 수 없고, 재접속 고리의 원인이 된다 — _drop_pending_chunks)
                if not (isinstance(obj, tuple) and obj[0] == "B"):
                    self.txq.put(obj)
                return

    def send(self, obj):
        self.txq.put(obj)

    def send_chunk(self, fid_hex, seq, data):
        """이미지 청크를 프레임으로 만들어 송신 큐에 넣는다(순서 보장).
        **fid를 함께 실어 둔다** — 연결이 끊기면 이 청크들은 버려야 하기 때문이다
        (아래 _drop_pending_chunks 참고)."""
        body = FILE_HEAD.pack(bytes.fromhex(fid_hex), seq) + data
        self.txq.put(("B", fid_hex, FRAME_HEAD.pack(len(body), ord("B")) + body))

    def _drop_pending_chunks(self):
        """큐에 남은 이미지 청크를 전부 버리고 그 fid 목록을 돌려준다.

        **끊긴 뒤 다시 보내면 안 된다(함정, 260815d에서 고침):** 전송 중 연결이
        끊기면 서버 쪽 전송 상태(tx_files)가 사라져 이어 보내도 의미가 없고,
        상대가 **이미지를 모르는 옛 서버**면 'B' 프레임을 받는 즉시 연결을 끊는다.
        그런데 큐에 청크가 남아 있으면 재접속할 때마다 그걸 다시 보내 **1초 간격
        재접속 고리**에 빠진다(실측: 12초에 18번). 그래서 세션이 끝나면 버린다."""
        kept, fids = [], set()
        while True:
            try:
                item = self.txq.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item[0] == "B":
                fids.add(item[1])
            else:
                kept.append(item)
        for item in kept:
            self.txq.put(item)
        return sorted(fids)

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


# === [2-1. 이미지 — PNG 변환 · 클립보드] ===
# **보내는 쪽에서 무조건 PNG로 바꿔 올린다.** 그러면 받는 쪽은 tkinter가 기본으로 읽는
# 형식만 다루면 되고(PhotoImage는 PNG/GIF만 읽는다), 채팅방에는 항상 PNG로 보인다.
# 서버는 저장하지 않고 흘려보내기만 하므로, 이미지는 **받은 쪽 로컬 기록으로 남는다**
# (그 방의 대화를 지우거나 방이 삭제될 때 함께 사라진다).


def to_png(img):
    """Pillow 이미지 → PNG 바이트. 너무 크면 줄인다(PNG는 사진에서 쉽게 수십MB가 된다)."""
    if img.mode not in ("RGB", "RGBA", "L", "P"):
        img = img.convert("RGBA")
    w, h = img.size
    if max(w, h) > IMG_MAX_SIDE:
        r = IMG_MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)
    buf = io.BytesIO()
    # optimize=True 는 쓰지 않는다 — 실측으로 이득이 없었다(노이즈가 많은 이미지에서는
    # 오히려 결과가 조금 더 컸다). 인코딩 자체는 빠르지만(1200x900 기준 0.1초) 큰
    # 사진에서는 시간이 늘어날 수 있어, 변환은 어차피 작업 스레드에서 돌린다.
    img.save(buf, format="PNG")
    return buf.getvalue(), img.size


def png_from_file(path):
    """이미지 파일을 PNG 바이트로. 실패하면 (None, 사유)."""
    if not HAS_PIL:
        return None, "이미지 기능에는 Pillow가 필요합니다."
    try:
        with Image.open(path) as im:
            im.load()
            data, size = to_png(im)
    except Exception as e:
        return None, f"이미지를 읽지 못했습니다: {e}"
    if len(data) > IMG_MAX_BYTES:
        return None, f"변환 후 크기가 너무 큽니다({len(data)//1024//1024}MB)."
    return (data, size), None


def png_from_clipboard():
    """클립보드의 이미지(또는 복사된 이미지 파일) → PNG 바이트. 없으면 (None, 사유)."""
    if not HAS_PIL:
        return None, "이미지 기능에는 Pillow가 필요합니다."
    try:
        got = ImageGrab.grabclipboard()
    except Exception as e:
        return None, f"클립보드를 읽지 못했습니다: {e}"
    if got is None:
        return None, None                     # 이미지가 아니다 → 평소 붙여넣기로
    if isinstance(got, list):                 # 파일을 복사한 경우
        for p in got:
            res, err = png_from_file(p)
            if res:
                return res, None
        return None, "복사된 파일에서 이미지를 찾지 못했습니다."
    try:
        data, size = to_png(got)
    except Exception as e:
        return None, f"이미지 변환 실패: {e}"
    if len(data) > IMG_MAX_BYTES:
        return None, "변환 후 크기가 너무 큽니다."
    return (data, size), None


def copy_png_to_clipboard(png_bytes):
    """PNG를 클립보드에 이미지로 넣는다(Windows CF_DIB).
    Pillow에는 클립보드 쓰기가 없어 BMP로 인코딩한 뒤 앞의 파일 헤더 14바이트를
    떼어 DIB로 만들어 넣는다 — 이게 표준적인 방법이다."""
    if not HAS_PIL or os.name != "nt":
        return False
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="BMP")
        dib = buf.getvalue()[14:]
    except Exception:
        return False
    CF_DIB, GMEM_MOVEABLE = 8, 0x0002
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    # **반환형을 반드시 지정해야 한다(64비트 함정):** ctypes 기본 반환형은 32비트 int라
    # GlobalAlloc/GlobalLock 이 돌려주는 64비트 핸들·포인터가 잘려 조용히 실패한다.
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    try:
        if not u32.OpenClipboard(None):
            return False
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
            if not h:
                return False
            ptr = k32.GlobalLock(h)
            if not ptr:
                k32.GlobalFree(h)
                return False
            ctypes.memmove(ptr, dib, len(dib))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(CF_DIB, h):
                k32.GlobalFree(h)   # 실패했으면 메모리 소유권이 넘어가지 않았다
                return False
            return True
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


# === [3. 로컬 저장 — 설정 · 비밀 · 대화 기록] ===


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    """Windows DPAPI 암·복호화. 실패하거나 Windows가 아니면 None."""
    if os.name != "nt":
        return None
    try:
        src = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                           ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        fn = (ctypes.windll.crypt32.CryptProtectData if protect
              else ctypes.windll.crypt32.CryptUnprotectData)
        ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
        if not ok:
            return None
        res = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return res
    except Exception:
        return None


class Store:
    """설정·비밀·대화 기록. 방 이름은 파일명으로 쓸 수 없어(한글·특수문자) sha1로 바꾼다."""

    def __init__(self):
        os.makedirs(HIST_DIR, exist_ok=True)
        self.cfg = {
            "ip": "", "id": "", "remember_ip": True, "remember_id": True,
            "auto_login": False, "notify": True, "sort": SORTS[0],
            "subs": [], "room_geom": {},
            "tls": True,          # 서버가 TLS를 안 쓰면 자동으로 평문으로 내려간다
            "server_fp": {},      # "IP:포트" -> 고정한 인증서 지문
        }
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fp:
                data = json.load(fp)
            for k, v in data.items():
                if k in self.cfg and isinstance(v, type(self.cfg[k])):
                    self.cfg[k] = v
        except Exception:
            pass
        self.secrets = self._load_secrets()

    # ---------- 설정 ----------
    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                json.dump(self.cfg, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[경고] 설정 저장 실패: {e}")

    # ---------- 비밀(계정 PW · 방 비밀번호) ----------
    def _load_secrets(self):
        try:
            with open(SECRETS_PATH, "rb") as fp:
                raw = fp.read()
        except Exception:
            return {"pw": "", "rooms": {}}
        plain = _dpapi(raw, protect=False)
        if plain is None:                 # DPAPI를 못 쓰는 환경(비Windows 등)
            plain = raw
        try:
            d = json.loads(plain.decode("utf-8"))
            return {"pw": d.get("pw", ""), "rooms": d.get("rooms", {})}
        except Exception:
            return {"pw": "", "rooms": {}}

    def save_secrets(self):
        raw = json.dumps(self.secrets, ensure_ascii=False).encode("utf-8")
        enc = _dpapi(raw, protect=True)
        try:
            with open(SECRETS_PATH, "wb") as fp:
                fp.write(enc if enc is not None else raw)
            if os.name != "nt":
                os.chmod(SECRETS_PATH, 0o600)
        except Exception as e:
            print(f"[경고] 자격 저장 실패: {e}")

    def room_pw(self, room):
        return self.secrets["rooms"].get(room, "")

    def set_room_pw(self, room, pw):
        self.secrets["rooms"][room] = pw
        self.save_secrets()

    def forget_room_pw(self, room):
        if self.secrets["rooms"].pop(room, None) is not None:
            self.save_secrets()

    # ---------- 구독 ----------
    def subs(self):
        return set(self.cfg["subs"])

    def set_sub(self, room, on):
        s = self.subs()
        s.add(room) if on else s.discard(room)
        self.cfg["subs"] = sorted(s)
        self.save()

    # ---------- 대화 기록 ----------
    @staticmethod
    def _hist_path(room):
        return os.path.join(
            HIST_DIR, hashlib.sha1(room.encode("utf-8")).hexdigest()[:16] + ".jsonl")

    def _touch_index(self, room):
        try:
            idx = {}
            if os.path.exists(INDEX_PATH):
                with open(INDEX_PATH, encoding="utf-8") as fp:
                    idx = json.load(fp)
            key = os.path.basename(self._hist_path(room))
            if idx.get(key) != room:
                idx[key] = room
                with open(INDEX_PATH, "w", encoding="utf-8") as fp:
                    json.dump(idx, fp, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def append_history(self, room, msg):
        """한 줄 append — 쓰다가 죽어도 앞부분이 안 깨진다."""
        try:
            self._touch_index(room)
            with open(self._hist_path(room), "a", encoding="utf-8") as fp:
                fp.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[경고] 기록 저장 실패({room}): {e}")

    def load_history(self, room, limit=HISTORY_LOAD):
        try:
            with open(self._hist_path(room), encoding="utf-8") as fp:
                lines = fp.readlines()[-limit:]
        except Exception:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out

    def clear_history(self, room):
        try:
            os.remove(self._hist_path(room))
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[경고] 기록 삭제 실패({room}): {e}")
        # 이미지도 그 방의 기록이다 — 대화를 지우면 함께 지운다
        d = self._img_dir(room)
        if os.path.isdir(d):
            try:
                for f in os.listdir(d):
                    os.remove(os.path.join(d, f))
                os.rmdir(d)
            except Exception as e:
                print(f"[경고] 이미지 삭제 실패({room}): {e}")

    # ---------- 이미지 파일 ----------
    @staticmethod
    def _img_dir(room):
        return os.path.join(
            HIST_DIR, "img_" + hashlib.sha1(room.encode("utf-8")).hexdigest()[:16])

    def img_path(self, room, fid):
        return os.path.join(self._img_dir(room), f"{fid}.png")

    def save_image(self, room, fid, png):
        """구독한 방만 디스크에 남긴다(대화 기록과 같은 규칙)."""
        d = self._img_dir(room)
        try:
            os.makedirs(d, exist_ok=True)
            p = self.img_path(room, fid)
            with open(p, "wb") as fp:
                fp.write(png)
            return p
        except Exception as e:
            print(f"[경고] 이미지 저장 실패({room}): {e}")
            return None

    def load_image(self, room, fid):
        try:
            with open(self.img_path(room, fid), "rb") as fp:
                return fp.read()
        except Exception:
            return None


# === [4. 알림] ===

try:
    from winotify import Notification as _WinNotif
except Exception:
    _WinNotif = None


def notify(root, title, body):
    """Windows 11 알림. winotify가 없으면 자체 토스트 창으로 대체한다.
    (표준 라이브러리만으로는 네이티브 토스트를 띄울 수 없다.)"""
    if _WinNotif is not None:
        try:
            n = _WinNotif(app_id=APP_NAME, title=title, msg=body)
            n.show()
            return
        except Exception:
            pass
    _toast(root, title, body)


def _toast(root, title, body, msec=4000):
    try:
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=ACCENT)
        wrap = tk.Frame(win, bg=BG_SOFT)
        wrap.pack(padx=2, pady=2, fill="both", expand=True)
        tk.Label(wrap, text=title, font=(FONT[0], 10, "bold"), bg=BG_SOFT,
                 fg=FG, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(wrap, text=body[:120], font=FONT_SMALL, bg=BG_SOFT, fg=FG_DIM,
                 anchor="w", justify="left", wraplength=260).pack(
                     fill="x", padx=10, pady=(2, 8))
        win.update_idletasks()
        w, h = max(280, win.winfo_reqwidth()), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")
        win.bind("<Button-1>", lambda _e: win.destroy())
        win.after(msec, win.destroy)
    except Exception:
        pass


# === [5. 공통 위젯] ===


def dark_btn(parent, text, cmd, width=None, font=FONT):
    return tk.Button(parent, text=text, command=cmd, font=font, bg=BTN_BG, fg=FG,
                     activebackground=BTN_ACTIVE, activeforeground=FG,
                     relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                     highlightthickness=0,
                     disabledforeground="#555555",
                     **({"width": width} if width else {}))


def dark_entry(parent, textvariable=None, show=None, width=22):
    return tk.Entry(parent, textvariable=textvariable, show=show, width=width,
                    font=FONT, bg=BG_SOFT, fg=FG, insertbackground=FG,
                    relief="flat", bd=4, highlightthickness=1,
                    highlightbackground="#333333", highlightcolor=ACCENT,
                    disabledbackground=BG_SOFT, disabledforeground=FG_DIM)


def dark_check(parent, text, var, cmd=None, font=FONT):
    return tk.Checkbutton(parent, text=text, variable=var, command=cmd, font=font,
                          bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                          selectcolor=BG_SOFT, relief="flat", bd=0,
                          highlightthickness=0, cursor="hand2",
                          disabledforeground="#4A4A4A")


def dark_radio(parent, text, var, value, cmd=None):
    return tk.Radiobutton(parent, text=text, variable=var, value=value, command=cmd,
                          font=FONT, bg=BG, fg=FG, activebackground=BG,
                          activeforeground=FG, selectcolor=BG_SOFT, relief="flat",
                          bd=0, highlightthickness=0, cursor="hand2",
                          disabledforeground="#4A4A4A")


class ScrollFrame(tk.Frame):
    """세로 스크롤 프레임(Canvas + 내부 Frame). 리스트와 말풍선 목록이 함께 쓴다."""

    def __init__(self, parent, bg=BG, height=None):
        tk.Frame.__init__(self, parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                **({"height": height} if height else {}))
        self.bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                bg=BTN_BG, troughcolor=BG, bd=0, relief="flat",
                                activebackground=BTN_ACTIVE, width=12,
                                highlightthickness=0)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        for w in (self.canvas, self.body):
            w.bind("<MouseWheel>", self._on_wheel)
        self.on_width = None                 # 폭이 바뀌면 알려준다(말풍선 줄바꿈 갱신)

    def _on_body(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        if self.on_width:
            self.on_width(e.width)

    def _on_wheel(self, e):
        self.canvas.yview_scroll(-1 * (e.delta // 120), "units")

    def at_bottom(self, tol=40):
        try:
            lo, hi = self.canvas.yview()
        except Exception:
            return True
        if hi >= 1.0:
            return True
        h = max(1, self.body.winfo_height())
        return (1.0 - hi) * h <= tol

    def to_bottom(self):
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def clear(self):
        for ch in list(self.body.winfo_children()):
            ch.destroy()


# === [6. 로그인 · 채팅방 리스트 화면] ===


class App:
    def __init__(self):
        self.store = Store()
        self.client = ChatClient()
        self.root = tk.Tk()
        self.root.title("domichat")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)     # 대기창은 고정 크기
        self._setup_scaling()
        self._set_icon()
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

        self.uid = None
        self.rooms = {}          # 방 이름 -> {kind, owner, created, allowed, ...}
        self.windows = {}        # 방 이름 -> RoomWindow
        self.joining = {}        # 방 이름 -> True (클릭해 입장 시도 중)
        # 입력한 방 비밀번호는 **메모리에만** 둔다. 구독한 방만 secrets에 영속되고,
        # 구독하지 않으면 방을 나가거나 앱을 끌 때 잊는다(설계 확정 사항).
        self.room_pw_mem = {}
        self.incoming = {}       # 받는 중인 이미지: fid -> {room,name,size,buf,...}
        self.server_ver = 1      # welcome 으로 갱신(2 이상이어야 이미지 전송 가능)
        self.busy = False
        self.updating = False
        self.logged_once = False
        self.ui_q = queue.Queue()   # 작업 스레드 → 메인 스레드 전달용(업데이트 결과 등)

        self.var_ip = tk.StringVar(value=self.store.cfg["ip"]
                                   if self.store.cfg["remember_ip"] else "")
        self.var_id = tk.StringVar(value=self.store.cfg["id"]
                                   if self.store.cfg["remember_id"] else "")
        self.var_pw = tk.StringVar()
        self.var_rip = tk.BooleanVar(value=self.store.cfg["remember_ip"])
        self.var_rid = tk.BooleanVar(value=self.store.cfg["remember_id"])
        self.var_auto = tk.BooleanVar(value=self.store.cfg["auto_login"])
        self.var_notify = tk.BooleanVar(value=self.store.cfg["notify"])
        self.var_sort = tk.StringVar(value=self.store.cfg["sort"])

        self._build_login()
        self._build_list()
        self.show_login()
        self._pump_id = self.root.after(50, self._pump)

        if self.store.cfg["auto_login"] and self.store.secrets["pw"] \
                and self.var_ip.get() and self.var_id.get():
            self.var_pw.set(self.store.secrets["pw"])
            self.root.after(200, self.do_login)

    def _set_icon(self):
        """창 아이콘. exe(frozen)에서는 _MEIPASS 안에, 스크립트 모드에서는 이 파일
        옆에 있는 domichat.ico를 쓴다. default= 형태라 이후 열리는 채팅방 창들도
        같은 아이콘을 물려받는다."""
        for base in (getattr(sys, "_MEIPASS", None),
                     os.path.dirname(os.path.abspath(__file__))):
            if not base:
                continue
            p = os.path.join(base, "domichat.ico")
            if os.path.isfile(p):
                try:
                    self.root.iconbitmap(default=p)
                    return
                except Exception:
                    pass

    def _setup_scaling(self):
        if os.name != "nt":
            return
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            if dpi and dpi != 96:
                self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    # ---------- 화면 전환 ----------
    def show_login(self):
        self.fr_list.pack_forget()
        self.fr_login.pack(fill="both", expand=True)
        self.root.geometry("")
        self.root.title(f"domichat {APP_VERSION} — 로그인")

    def show_list(self):
        self.fr_login.pack_forget()
        self.fr_list.pack(fill="both", expand=True)
        self.root.geometry("")
        self.root.title(f"domichat {APP_VERSION} — {self.uid}")

    # ---------- 로그인 프레임 ----------
    def _build_login(self):
        f = self.fr_login = tk.Frame(self.root, bg=BG, padx=22, pady=18)
        tk.Label(f, text="DOMICHAT", font=(FONT[0], 16, "bold"), bg=BG,
                 fg=FG).grid(row=0, column=0, columnspan=2, pady=(0, 16))
        rows = (("IP주소:", self.var_ip, None), ("ID:", self.var_id, None),
                ("PW:", self.var_pw, "*"))
        self.ent_login = []
        for i, (label, var, show) in enumerate(rows, start=1):
            tk.Label(f, text=label, font=FONT, bg=BG, fg=FG).grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            e = dark_entry(f, var, show=show)
            e.grid(row=i, column=1, sticky="we", pady=3)
            e.bind("<Return>", lambda _e: self.do_login())
            self.ent_login.append(e)

        box = tk.Frame(f, bg=BG)
        box.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        dark_check(box, "IP주소 기억하기", self.var_rip).pack(side="left")
        dark_check(box, "ID 기억하기", self.var_rid).pack(side="left", padx=(6, 0))
        dark_check(f, "자동 로그인", self.var_auto).grid(
            row=5, column=0, columnspan=2, sticky="w")

        btns = tk.Frame(f, bg=BG)
        btns.grid(row=6, column=0, columnspan=2, pady=(14, 0))
        self.bt_login = dark_btn(btns, "로그인", self.do_login, width=8)
        self.bt_login.pack(side="left", padx=4)
        self.bt_reg = dark_btn(btns, "회원가입", self.do_register, width=8)
        self.bt_reg.pack(side="left", padx=4)
        # 업데이트 확인 — 회원가입 버튼 오른쪽의 작은 아이콘 버튼
        self.bt_up_login = dark_btn(btns, "⟳", self.check_update, width=2,
                                    font=FONT_SMALL)
        self.bt_up_login.pack(side="left")

        self.lb_login_msg = tk.Label(f, text="", font=FONT_SMALL, bg=BG, fg=ACCENT,
                                     wraplength=280, justify="left")
        self.lb_login_msg.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        f.columnconfigure(1, weight=1)

    def _login_msg(self, text, color=ACCENT):
        self.lb_login_msg.configure(text=text, fg=color)

    def _set_busy(self, on):
        self.busy = on
        state = "disabled" if on else "normal"
        self.bt_login.configure(state=state)
        self.bt_reg.configure(state=state)

    def _fields(self):
        """IP 칸은 'IP:포트' 형식도 받는다."""
        raw = self.var_ip.get().strip()
        port = DEFAULT_PORT
        ip = raw
        if raw.count(":") == 1:                       # IPv6는 대상이 아니므로 단순 분리
            ip, _, p = raw.partition(":")
            if p.isdigit():
                port = int(p)
        return ip.strip(), port, self.var_id.get().strip(), self.var_pw.get()

    def do_login(self):
        if self.busy:
            return
        ip, port, uid, pw = self._fields()
        if not ip or not uid or not pw:
            return self._login_msg("IP주소·ID·PW를 모두 입력하세요.", "#FF8888")
        self._set_busy(True)
        self._login_msg("접속 중...")
        self.client = ChatClient()
        self.client.start(ip, port, uid, pw, want_tls=self.store.cfg["tls"],
                          pinned=self.store.cfg["server_fp"].get(f"{ip}:{port}"))

    def do_register(self):
        if self.busy:
            return
        ip, port, uid, pw = self._fields()
        if not ip or not uid or not pw:
            return self._login_msg("IP주소·ID·PW를 모두 입력하세요.", "#FF8888")
        self._set_busy(True)
        self._login_msg("가입 요청 중...")
        self.client.want_tls = self.store.cfg["tls"]
        self.client.pinned = self.store.cfg["server_fp"].get(f"{ip}:{port}")
        self.client.register(ip, port, uid, pw)

    def close_all_windows(self):
        """채팅방 창은 물론 승인 대기·블랙리스트·만들기 같은 부속 창까지 전부 닫는다.
        (딕셔너리만 훑으면 부속 창이 남아 로그인 화면 위에 떠 있게 된다.)"""
        ApprovalDialog.opened.clear()
        BlockDialog.opened.clear()
        for w in list(self.windows.values()):
            w.destroy(send_leave=False)
        self.windows.clear()
        for ch in list(self.root.winfo_children()):
            if isinstance(ch, tk.Toplevel):
                try:
                    ch.destroy()
                except Exception:
                    pass

    def do_logout(self):
        self.client.logout()
        self.close_all_windows()
        self.rooms.clear()
        self.room_pw_mem.clear()   # 구독하지 않은 방의 비번은 여기서 잊힌다
        self.uid = None
        self.logged_once = False
        # 로그아웃하면 입력란은 모두 비운다(저장된 값이 있어도 채우지 않는다)
        self.var_ip.set("")
        self.var_id.set("")
        self.var_pw.set("")
        self._login_msg("로그아웃했습니다.")
        self._set_busy(False)
        self.show_login()

    # ---------- 업데이트 ----------
    def _update_status(self, text, err=False):
        """현재 보이는 화면에 결과를 쓴다(로그인 화면이면 안내 줄, 리스트면 상태 줄)."""
        if self.uid:
            self.lb_list_msg.configure(text=text)
        else:
            self._login_msg(text, "#FF8888" if err else ACCENT)

    def check_update(self):
        if self.updating:
            return
        self.updating = True
        for b in (self.bt_up_login, self.bt_up_list):
            b.configure(state="disabled")
        self._update_status(f"업데이트 확인 중... (현재 {APP_VERSION})")
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        """네트워크는 작업 스레드에서. 결과는 **큐로만** 넘긴다 —
        작업 스레드가 tkinter(root.after 포함)를 직접 건드리면 안 된다."""
        latest = fetch_latest_version()
        src = download_latest_source() if (latest and latest > APP_VERSION) else None
        self.ui_q.put(("update_done", latest, src))

    def _update_done(self, latest, src):
        self.updating = False
        for b in (self.bt_up_login, self.bt_up_list):
            b.configure(state="normal")
        if not latest:
            return self._update_status("업데이트 정보를 가져오지 못했습니다.", err=True)
        if latest <= APP_VERSION:
            return self._update_status(f"이미 최신 버전입니다. ({APP_VERSION})")
        if not src:
            return self._update_status("새 버전을 내려받지 못했습니다.", err=True)
        if not messagebox.askokcancel(
                "domichat",
                f"새 버전 {latest} 이 있습니다. (현재 {APP_VERSION})\n"
                "지금 업데이트하고 프로그램을 다시 시작할까요?", parent=self.root):
            return self._update_status("업데이트를 취소했습니다.")
        try:
            self.store.cfg["notify"] = self.var_notify.get()
            self.store.save()
            self.client.logout()
            apply_update_and_restart(src)      # 성공하면 반환하지 않는다
        except Exception as e:
            self._update_status(f"업데이트 실패: {e}", err=True)

    def on_quit(self):
        if self.windows and not messagebox.askokcancel(
                "domichat", "채팅방 창까지 모두 닫고 종료할까요?", parent=self.root):
            return
        self.store.cfg["notify"] = self.var_notify.get()
        self.store.save()
        self.client.logout()
        # 예약된 큐 비우기를 취소해 둔다 — 안 하면 Tk가 사라진 콜백을 부르며
        # 콘솔에 'invalid command name ..._pump' 를 뱉는다.
        if self._pump_id is not None:
            try:
                self.root.after_cancel(self._pump_id)
            except Exception:
                pass
        self.root.destroy()

    # ---------- 리스트 프레임 ----------
    def _build_list(self):
        f = self.fr_list = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        head = tk.Frame(f, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="채팅방 목록", font=FONT_BIG, bg=BG, fg=FG).pack(side="left")
        dark_btn(head, "+", self.open_create, width=2).pack(side="right", padx=(6, 0))

        mb = tk.Menubutton(head, textvariable=self.var_sort, font=FONT_SMALL,
                           bg=BTN_BG, fg=FG, activebackground=BTN_ACTIVE,
                           activeforeground=FG, relief="flat", bd=0, padx=8, pady=3,
                           cursor="hand2", highlightthickness=0)
        menu = tk.Menu(mb, tearoff=0, bg=BG_SOFT, fg=FG, font=FONT_SMALL,
                       activebackground=ACCENT, activeforeground="#000000", bd=0)
        for s in SORTS:
            menu.add_radiobutton(label=s, variable=self.var_sort, value=s,
                                 command=self._sort_changed)
        mb.configure(menu=menu)
        mb.pack(side="right")

        self.list_sf = ScrollFrame(f, bg=BG, height=300)   # 10행 정도 보이는 고정 높이
        self.list_sf.pack(fill="both", expand=True, pady=(10, 8))
        self.lb_list_msg = tk.Label(f, text="", font=FONT_SMALL, bg=BG, fg=FG_DIM,
                                    anchor="w")
        self.lb_list_msg.pack(fill="x")

        bottom = tk.Frame(f, bg=BG)
        bottom.pack(fill="x", pady=(6, 0))
        dark_btn(bottom, "로그아웃", self.do_logout).pack(side="left")
        # 업데이트 확인 — 로그아웃 버튼 오른쪽의 작은 아이콘 버튼
        self.bt_up_list = dark_btn(bottom, "⟳", self.check_update, width=2,
                                   font=FONT_SMALL)
        self.bt_up_list.pack(side="left", padx=4)
        dark_check(bottom, "알림", self.var_notify, self._notify_changed,
                   font=FONT_SMALL).pack(side="right")

    def _notify_changed(self):
        self.store.cfg["notify"] = self.var_notify.get()
        self.store.save()

    def _sort_changed(self):
        self.store.cfg["sort"] = self.var_sort.get()
        self.store.save()
        self.refresh_list()

    def _sorted_rooms(self):
        items = list(self.rooms.values())
        s = self.var_sort.get()
        if s == SORTS[0]:
            items.sort(key=lambda r: r["name"])
        elif s == SORTS[1]:
            items.sort(key=lambda r: r["name"], reverse=True)
        elif s == SORTS[2]:
            items.sort(key=lambda r: r.get("created", 0))
        else:
            items.sort(key=lambda r: r.get("created", 0), reverse=True)
        return items

    def refresh_list(self):
        self.list_sf.clear()
        subs = self.store.subs()
        for r in self._sorted_rooms():
            name, kind = r["name"], r.get("kind", "open")
            row = tk.Frame(self.list_sf.body, bg=BG_ROW, height=28)
            row.pack(fill="x", pady=1)

            var = tk.BooleanVar(value=name in subs)
            chk = dark_check(row, "", var)
            chk.configure(bg=BG_ROW, activebackground=BG_ROW)
            if kind == "open":
                chk.configure(command=lambda n=name, v=var: self.toggle_sub(n, v))
            else:
                # 제한방은 상태 표시 전용 — 구독은 방 안에서만 토글한다
                chk.configure(state="disabled")
            chk.pack(side="left")

            text = f"({KIND_LABEL.get(kind, kind)}) {name}"
            lb = tk.Label(row, text=text, font=FONT, bg=BG_ROW, fg=FG, anchor="w",
                          cursor="hand2")
            lb.pack(side="left", fill="x", expand=True, padx=(2, 8))
            if r.get("owner") == self.uid:
                tk.Label(row, text="방장", font=FONT_SMALL, bg=BG_ROW,
                         fg=ACCENT).pack(side="right", padx=6)
            for w in (row, lb):
                w.bind("<Button-1>", lambda _e, n=name: self.enter_room(n))
                w.bind("<Enter>", lambda _e, x=row: x.configure(bg=BG_SOFT))
                w.bind("<Leave>", lambda _e, x=row: x.configure(bg=BG_ROW))
        self.lb_list_msg.configure(text=f"채팅방 {len(self.rooms)}개")

    def toggle_sub(self, room, var):
        on = var.get()
        self.store.set_sub(room, on)
        # 그 방의 창이 열려 있으면 창 안 체크박스도 같이 움직인다(반대 방향은
        # RoomWindow.toggle_sub 가 refresh_list 로 처리한다)
        w = self.windows.get(room)
        if w:
            w.sync_sub(on)
        if on:
            # 구독하려면 서버 팬아웃 대상이어야 하므로 입장부터 한다
            self.client.send({"t": "join", "room": room})
            self.joining[room] = "sub"
        else:
            self.client.send({"t": "sub", "room": room, "on": False})
            if room not in self.windows:
                self.client.send({"t": "leave", "room": room})

    # ---------- 입장 ----------
    def enter_room(self, name):
        if name in self.windows:
            self.windows[name].lift()
            return
        r = self.rooms.get(name, {})
        kind = r.get("kind", "open")
        if r.get("blocked"):
            return messagebox.showinfo("domichat", "강제 퇴장되어 입장할 수 없습니다.",
                                       parent=self.root)
        self.joining[name] = "open"
        if kind == "pw" and r.get("owner") != self.uid:
            # 구독한 방은 보관해둔 비번(secrets), 그 외에는 이번 실행 중에 입력한
            # 값(메모리)만 재사용한다. 둘 다 없으면 다시 물어본다.
            saved = self.room_pw_mem.get(name) or self.store.room_pw(name)
            if saved:
                self.client.send({"t": "join", "room": name, "pw": saved})
            else:
                self.ask_room_pw(name)
            return
        self.client.send({"t": "join", "room": name})

    def forget_room_pw(self, room):
        """그 방의 비밀번호를 잊는다(메모리 + 보관값). 구독 해제·강퇴·나가기 때 호출."""
        self.room_pw_mem.pop(room, None)
        self.store.forget_room_pw(room)

    def ask_room_pw(self, name, wrong=False):
        PasswordDialog(self, name, wrong)

    def join_with_pw(self, name, pw):
        self.joining[name] = "open"
        self.room_pw_mem[name] = pw
        if name in self.store.subs():      # 구독한 방만 다음 실행까지 기억한다
            self.store.set_room_pw(name, pw)
        self.client.send({"t": "join", "room": name, "pw": pw})

    def open_create(self):
        CreateRoomDialog(self)

    # ---------- 서버 이벤트 처리 ----------
    def _pump(self):
        """메인 스레드에서 50ms마다 큐를 비운다(tkinter는 메인 스레드 전용)."""
        try:
            while True:
                kind, *rest = self.ui_q.get_nowait()
                if kind == "update_done":
                    self._update_done(*rest)
                elif kind == "image_ready":
                    room, res, err, name = rest
                    w = self.windows.get(room)
                    if not w:
                        continue
                    if res:
                        w._send_png(res[0], res[1], name)
                    else:
                        w.set_status(err or "이미지를 읽지 못했습니다.")
        except queue.Empty:
            pass
        try:
            while True:
                d = self.client.q.get_nowait()
                try:
                    self._on_event(d)
                except Exception as e:
                    print(f"[경고] 이벤트 처리 실패: {d} ({e})")
        except queue.Empty:
            pass
        try:
            self._pump_id = self.root.after(50, self._pump)
        except tk.TclError:
            self._pump_id = None      # 창이 닫히는 중이면 조용히 멈춘다

    def _on_event(self, d):
        ev = d.get("_ev")
        if ev:
            return self._on_internal(ev, d)
        t = d.get("t")
        if t == "welcome":
            return self._on_welcome(d)
        if t == "rooms":
            self.rooms = {r["name"]: r for r in d.get("list", [])}
            return self.refresh_list()
        if t == "room_new":
            self.rooms[d["room"]] = {"name": d["room"], "kind": d.get("kind"),
                                     "owner": d.get("owner"),
                                     "created": time.time(), "allowed": True}
            return self.refresh_list()
        if t == "room_deleted":
            return self._on_room_deleted(d["room"])
        if t == "joined":
            return self._on_joined(d)
        if t == "denied":
            return self._on_denied(d)
        if t == "msg":
            return self._on_msg(d)
        if t in ("file_begin", "bin", "file_end", "file_abort"):
            return self._on_file(t, d)
        if t == "member":
            w = self.windows.get(d.get("room"))
            if w:
                w.on_member(d.get("id"), d.get("in"))
            return
        if t == "kicked":
            room = d.get("room")
            self.store.set_sub(room, False)   # 강퇴당하면 구독도 해제한다
            self.forget_room_pw(room)
            w = self.windows.get(room)
            if w:
                w.destroy(send_leave=False)
            messagebox.showinfo("domichat", f"[{room}] {d.get('msg', '강제 퇴장되었습니다.')}",
                                parent=self.root)
            return self.client.send({"t": "rooms"})
        if t == "approve_req":
            w = self.windows.get(d.get("room"))
            if w:
                w.on_approve_req(d.get("id"))
            elif self.var_notify.get():
                notify(self.root, "입장 요청",
                       f"[{d.get('room')}] {d.get('id')} 님이 입장을 기다립니다.")
            return
        if t == "approve_res":
            messagebox.showinfo("domichat", f"[{d.get('room')}] {d.get('msg', '')}",
                                parent=self.root)
            if d.get("ok"):
                self.client.send({"t": "rooms"})
                self.enter_room(d.get("room"))
            return
        if t == "pending":
            dlg = ApprovalDialog.opened.get(d.get("room"))
            if dlg:
                dlg.fill(d.get("ids", []))
            return
        if t == "blocklist":
            dlg = BlockDialog.opened.get(d.get("room"))
            if dlg:
                dlg.fill(d.get("ids", []))
            return
        if t == "ok":
            if d.get("of") == "file_end":
                w = None
                for room, win in self.windows.items():
                    if d.get("fid") in getattr(win, "sending", {}):
                        w = win
                        break
                if w:
                    w.finish_sending(d.get("fid"))
                return
            if d.get("of") == "room_pw":
                w = self.windows.get(d.get("room"))
                if w:
                    w.set_status("비밀번호를 변경했습니다."
                                 " (기존 참여자는 그대로 있고, 다시 들어올 때 새 비번이 필요합니다)")
            return
        if t == "error":
            return self._on_error(d)

    def _on_internal(self, ev, d):
        if ev == "register_result":
            self._set_busy(False)
            if d.get("ok"):
                self._login_msg("회원가입이 요청되었습니다. 서버 승인 후 로그인할 수 있습니다.")
            else:
                self._login_msg(d.get("msg", "가입에 실패했습니다."), "#FF8888")
            return
        if ev == "cert_pinned":
            # 첫 접속에서 받은 서버 인증서 지문을 기억한다(다음부터 이 값과 비교)
            self.store.cfg["server_fp"][d["host"]] = d["fp"]
            self.store.save()
            print(f"[TLS] {d['host']} 인증서 지문을 기억했습니다: {d['fp'][:16]}…")
            return
        if ev == "cert_changed":
            self._set_busy(False)
            host, new = d["host"], d["new"]
            msg = (f"서버 인증서가 바뀌었습니다.\n\n{host}\n"
                   f"이전: {d['old'][:32]}…\n새것: {new[:32]}…\n\n"
                   "서버를 재설치했다면 정상입니다. 그렇지 않다면 누군가 중간에서\n"
                   "가로채는 중일 수 있습니다. 새 인증서를 신뢰할까요?")
            if messagebox.askokcancel("domichat — 인증서 경고", msg, parent=self.root,
                                      icon="warning"):
                self.store.cfg["server_fp"][host] = new
                self.store.save()
                self._login_msg("새 인증서를 신뢰했습니다. 다시 로그인합니다.")
                return self.root.after(100, self.do_login)
            return self._login_msg("인증서가 바뀌어 접속을 중단했습니다.", "#FF8888")
        if ev == "file_aborted":
            for w in self.windows.values():
                for fid in d.get("fids", []):
                    w.fail_sending(fid)
            return
        if ev == "connect_fail":
            self._set_busy(False)
            return self._login_msg(d.get("msg", "접속 실패"), "#FF8888")
        if ev == "connected":
            if d.get("retry"):
                self._status("서버에 다시 접속했습니다.")
            return
        if ev == "disconnected":
            if self.uid:
                self._status("연결이 끊겼습니다. 다시 접속하는 중...")
            return

    def _status(self, text):
        if self.uid:
            self.lb_list_msg.configure(text=text)
        for w in self.windows.values():
            w.set_status(text)

    def _on_welcome(self, d):
        self.uid = d.get("id")
        # 서버 프로토콜 버전(1=텍스트만, 2=이미지 지원). 옛 서버에 이미지를 보내면
        # 그쪽이 연결을 끊어 재접속 고리에 빠지므로 **보내기 전에 막는다.**
        self.server_ver = int(d.get("ver") or 1)
        self.rooms = {r["name"]: r for r in d.get("rooms", [])}
        cfg = self.store.cfg
        ip, port, uid, pw = self._fields()
        cfg["remember_ip"], cfg["remember_id"] = self.var_rip.get(), self.var_rid.get()
        cfg["auto_login"] = self.var_auto.get()
        cfg["ip"] = self.var_ip.get().strip() if cfg["remember_ip"] else ""
        cfg["id"] = uid if cfg["remember_id"] else ""
        self.store.save()
        if cfg["auto_login"]:
            self.store.secrets["pw"] = pw
            self.store.save_secrets()

        if self.logged_once:                  # 재접속 — 열려 있던 방·구독 방에 재입장
            for room in set(self.windows) | self.store.subs():
                r = self.rooms.get(room)
                if not r:
                    continue
                pwd = self.store.room_pw(room) if r.get("kind") == "pw" else None
                msg = {"t": "join", "room": room}
                if pwd:
                    msg["pw"] = pwd
                self.client.send(msg)
                if room in self.store.subs():
                    self.client.send({"t": "sub", "room": room, "on": True})
            self.refresh_list()
            return self._status("서버에 다시 접속했습니다.")

        self.logged_once = True
        self._set_busy(False)
        self._login_msg("")
        self.var_pw.set("")
        self.show_list()
        self.refresh_list()
        for room in self.store.subs():         # 구독한 방은 창 없이도 받아둔다
            if room in self.rooms:
                r = self.rooms[room]
                msg = {"t": "join", "room": room}
                pwd = self.store.room_pw(room) if r.get("kind") == "pw" else None
                if pwd:
                    msg["pw"] = pwd
                self.client.send(msg)
                self.client.send({"t": "sub", "room": room, "on": True})

    def _on_joined(self, d):
        room = d["room"]
        info = self.rooms.setdefault(room, {"name": room})
        info.update({"kind": d.get("kind"), "owner": d.get("owner"),
                     "allowed": True, "blocked": False})
        why = self.joining.pop(room, None)
        if why == "sub":
            self.client.send({"t": "sub", "room": room, "on": True})
            self.refresh_list()
            return
        if room not in self.windows and why:
            self.windows[room] = RoomWindow(self, room)
        self.refresh_list()

    def _on_denied(self, d):
        room, reason = d["room"], d.get("reason")
        self.joining.pop(room, None)
        info = self.rooms.get(room)
        if info is not None and reason == "blocked":
            info["blocked"] = True
        if reason == "bad_pw_room":
            # 방장이 비번을 바꿨을 수 있다 — 보관값을 버리고 다시 물어본다
            self.forget_room_pw(room)
            return self.ask_room_pw(room, wrong=True)
        messagebox.showinfo("domichat", f"[{room}] {d.get('msg', '입장할 수 없습니다.')}",
                            parent=self.root)

    def _on_msg(self, d):
        room = d.get("room")
        w = self.windows.get(room)
        mine = d.get("from") == self.uid
        if room in self.store.subs():
            self.store.append_history(room, {"mid": d.get("mid"), "ts": d.get("ts"),
                                             "from": d.get("from"),
                                             "body": d.get("body")})
        if w:
            w.add_message(d, mine=mine)
        elif not mine and room in self.store.subs() and self.var_notify.get():
            notify(self.root, f"{room} — {d.get('from')}", d.get("body", ""))

    # ---------- 이미지 수신 ----------
    def _on_file(self, t, d):
        fid = d.get("fid")
        if t == "file_begin":
            room = d.get("room")
            if len(d.get("name") or "") == 0 or not fid:
                return
            self.incoming[fid] = {"room": room, "name": d.get("name"),
                                  "size": d.get("size") or 0, "from": d.get("from"),
                                  "ts": d.get("ts"), "mid": d.get("mid"),
                                  "sha256": d.get("sha256"), "buf": bytearray()}
            w = self.windows.get(room)
            if w:
                w.begin_incoming(fid, self.incoming[fid])
            return

        rec = self.incoming.get(fid)
        if not rec:
            return                       # 내가 보낸 것 또는 이미 끝난 전송
        room = rec["room"]
        w = self.windows.get(room)

        if t == "bin":
            rec["buf"] += d.get("data", b"")
            if w:
                w.update_incoming(fid, len(rec["buf"]), rec["size"])
            return
        if t == "file_abort":
            self.incoming.pop(fid, None)
            if w:
                w.fail_incoming(fid, "전송이 중단되었습니다.")
            return

        # file_end
        self.incoming.pop(fid, None)
        png = bytes(rec["buf"])
        ok = (not rec["size"] or len(png) == rec["size"])
        if ok and rec.get("sha256"):
            ok = hashlib.sha256(png).hexdigest() == rec["sha256"]
        if not ok:
            if w:
                w.fail_incoming(fid, "이미지가 깨져서 도착했습니다.")
            return
        if room in self.store.subs():
            self.store.save_image(room, fid, png)
            self.store.append_history(room, {
                "mid": rec["mid"], "ts": rec["ts"], "from": rec["from"],
                "kind": "img", "fid": fid, "name": rec["name"]})
        if w:
            w.finish_incoming(fid, png)
        elif room in self.store.subs() and self.var_notify.get():
            notify(self.root, f"{room} — {rec['from']}", f"이미지: {rec['name']}")

    def send_image(self, room, png, size, name):
        """PNG를 청크로 쪼개 보낸다. 큐에 넣기만 하므로 GUI가 멈추지 않는다."""
        fid = new_fid()
        self.client.send({"t": "file_begin", "room": room, "fid": fid, "name": name,
                          "size": len(png), "sha256": hashlib.sha256(png).hexdigest(),
                          "w": size[0], "h": size[1]})
        for i in range(0, len(png), IMG_CHUNK):
            self.client.send_chunk(fid, i // IMG_CHUNK, png[i:i + IMG_CHUNK])
        self.client.send({"t": "file_end", "room": room, "fid": fid})
        if room in self.store.subs():
            self.store.save_image(room, fid, png)
            self.store.append_history(room, {
                "mid": None, "ts": time.time(), "from": self.uid,
                "kind": "img", "fid": fid, "name": name})
        return fid

    def _on_room_deleted(self, room):
        self.rooms.pop(room, None)
        w = self.windows.get(room)
        if w:
            w.destroy(send_leave=False)
        self.store.set_sub(room, False)
        self.store.clear_history(room)         # 삭제된 방의 기록은 남기지 않는다
        self.store.secrets["rooms"].pop(room, None)
        self.store.save_secrets()
        self.refresh_list()
        self._status(f"채팅방 '{room}' 이(가) 삭제되었습니다. 대화 기록도 삭제했습니다.")

    def _on_error(self, d):
        code, msg = d.get("code"), d.get("msg", "오류")
        if not self.uid:
            self._set_busy(False)
            self.client.want = False
            return self._login_msg(msg, "#FF8888")
        if code in ("deleted", "kicked_conn"):
            self.client.want = False
            messagebox.showwarning("domichat", msg, parent=self.root)
            return self.do_logout()
        self._status(f"오류: {msg}")

    def run(self):
        self.root.mainloop()


# === [7. 채팅방 창] ===


class RoomWindow:
    """방 하나의 창. 리사이즈·최대화가 되는 유일한 창이다."""

    def __init__(self, app, room):
        self.app = app
        self.room = room
        self.info = app.rooms.get(room, {})
        self.bubbles = []            # 텍스트 말풍선 라벨 — 창 폭이 바뀌면 줄바꿈 갱신
        self.pending = {}            # cid -> 체크표시 라벨
        self.cid_seq = 0
        self.images = {}             # fid -> {holder, label, photo, png} (참조 유지 필수)
        self.sending = {}            # fid -> 진행 라벨(내가 보내는 중)

        self.win = tk.Toplevel(app.root)
        self.win.title(f"{room} — domichat")
        self.win.configure(bg=BG)
        self.win.geometry(app.store.cfg["room_geom"].get(room, "460x560"))
        self.win.minsize(360, 320)
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)

        head = tk.Frame(self.win, bg=BG, padx=8, pady=6)
        head.pack(fill="x")
        self.var_sub = tk.BooleanVar(value=room in app.store.subs())
        dark_check(head, "", self.var_sub, self.toggle_sub).pack(side="left")
        tk.Label(head, text=room, font=FONT_BIG, bg=BG, fg=FG).pack(side="left")
        tk.Label(head, text=f" ({KIND_DETAIL.get(self.info.get('kind'), '')})",
                 font=FONT_SMALL, bg=BG, fg=FG_DIM).pack(side="left")
        self.bt_gear = dark_btn(head, "⚙", self.open_menu, width=2)
        self.bt_gear.pack(side="right")

        self.sf = ScrollFrame(self.win, bg=BG)
        self.sf.pack(fill="both", expand=True, padx=4)
        self.sf.on_width = self._on_width
        # 위쪽 spacer — 메시지가 적을 때도 아래에서부터 쌓이게 한다
        self.spacer = tk.Frame(self.sf.body, bg=BG, height=1)
        self.spacer.pack(fill="both", expand=True)

        self.lb_status = tk.Label(self.win, text="", font=FONT_SMALL, bg=BG,
                                  fg=FG_DIM, anchor="w")
        self.lb_status.pack(fill="x", padx=8)

        bottom = tk.Frame(self.win, bg=BG, padx=6, pady=6)
        bottom.pack(fill="x")
        tk.Label(bottom, text=">", font=FONT, bg=BG, fg=FG_DIM).pack(side="left")
        self.txt = tk.Text(bottom, height=1, font=FONT, bg=BG_SOFT, fg=FG,
                           insertbackground=FG, relief="flat", bd=6, wrap="word",
                           highlightthickness=1, highlightbackground="#333333",
                           highlightcolor=ACCENT)
        self.txt.pack(side="left", fill="both", expand=True, padx=(4, 6))
        self.txt.bind("<Return>", self._on_return)
        self.txt.bind("<Shift-Return>", lambda _e: None)   # 줄바꿈은 기본 동작
        self.txt.bind("<KeyRelease>", self._grow)
        self.txt.bind("<Control-v>", self._on_paste)       # 클립보드 이미지면 바로 전송
        self.txt.bind("<Control-V>", self._on_paste)
        dark_btn(bottom, "보내기", self.send).pack(side="right")
        self.txt.focus_set()

        if room in app.store.subs():        # 구독한 방은 그동안 받은 기록을 보여준다
            for m in app.store.load_history(room):
                self.add_message(m, mine=(m.get("from") == app.uid), scroll=False)
            self.sf.to_bottom()

    # ---------- 창 ----------
    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def set_status(self, text):
        try:
            self.lb_status.configure(text=text)
        except Exception:
            pass

    def on_close(self):
        # 구독 중이면 leave 하지 않는다 — 창을 닫아도 계속 받아 기록한다
        subscribed = self.var_sub.get()
        if not subscribed:
            # 구독하지 않았다면 방을 나가는 순간 비밀번호를 잊는다 → 다시 들어올 때 재입력
            self.app.forget_room_pw(self.room)
        self.destroy(send_leave=not subscribed)

    def destroy(self, send_leave=True):
        try:
            self.app.store.cfg["room_geom"][self.room] = self.win.geometry()
            self.app.store.save()
        except Exception:
            pass
        if send_leave:
            self.app.client.send({"t": "leave", "room": self.room})
        self.app.windows.pop(self.room, None)
        try:
            self.win.destroy()
        except Exception:
            pass

    def sync_sub(self, on):
        """리스트 화면에서 구독을 켜고 끌 때 창 안 체크박스를 맞춰준다
        (여기서 toggle_sub 를 부르면 서버로 중복 요청이 나가므로 표시만 바꾼다)."""
        try:
            self.var_sub.set(bool(on))
        except Exception:
            pass
        self.set_status("구독했습니다. 창을 닫아도 대화가 기록됩니다." if on
                        else "구독을 해제했습니다.")

    def toggle_sub(self):
        on = self.var_sub.get()
        self.app.store.set_sub(self.room, on)
        # 구독하면 이번에 입력한 비번을 다음 실행까지 보관하고, 해제하면 잊는다
        if self.info.get("kind") == "pw" and self.info.get("owner") != self.app.uid:
            if on:
                pw = self.app.room_pw_mem.get(self.room)
                if pw:
                    self.app.store.set_room_pw(self.room, pw)
            else:
                self.app.store.forget_room_pw(self.room)
        self.app.client.send({"t": "sub", "room": self.room, "on": on})
        self.set_status("구독했습니다. 창을 닫아도 대화가 기록됩니다." if on
                        else "구독을 해제했습니다.")
        self.app.refresh_list()

    # ---------- 톱니바퀴 ----------
    def open_menu(self):
        m = tk.Menu(self.win, tearoff=0, bg=BG_SOFT, fg=FG, font=FONT,
                    activebackground=ACCENT, activeforeground="#000000", bd=0)
        m.add_command(label="채팅 지우기", command=self.clear_chat)
        kind, owner = self.info.get("kind"), self.info.get("owner")
        if owner and owner == self.app.uid:
            if kind == "pw":
                m.add_command(label="비밀번호 변경",
                              command=lambda: RoomPwDialog(self.app, self.room,
                                                           self.win))
            if kind == "approve":
                m.add_command(label="승인 대기",
                              command=lambda: ApprovalDialog(self.app, self.room))
            if kind != "open":
                m.add_command(label="블랙리스트",
                              command=lambda: BlockDialog(self.app, self.room))
                m.add_command(label="채팅방 삭제", command=self.delete_room)
        m.add_command(label="이미지 업로드", command=self.pick_image,
                      state="normal" if (HAS_PIL and filedialog) else "disabled")
        x = self.bt_gear.winfo_rootx()
        y = self.bt_gear.winfo_rooty() + self.bt_gear.winfo_height()
        m.post(x, y)

    def clear_chat(self):
        if not messagebox.askokcancel(
                "domichat", "이 방의 대화를 화면과 저장공간에서 지웁니다. 계속할까요?",
                parent=self.win):
            return
        for w in list(self.sf.body.winfo_children()):
            if w is not self.spacer:
                w.destroy()
        self.bubbles.clear()
        self.pending.clear()
        self.images.clear()
        self.sending.clear()
        self.app.store.clear_history(self.room)   # 이미지 파일까지 함께 지워진다
        self.set_status("대화와 이미지를 지웠습니다.")

    def delete_room(self):
        if not messagebox.askokcancel(
                "domichat",
                f"채팅방 '{self.room}' 을 삭제합니다.\n참여자 모두의 대화 기록도 삭제됩니다.",
                parent=self.win):
            return
        self.app.client.send({"t": "room_delete", "room": self.room})

    # ---------- 메시지 ----------
    def _on_return(self, _e):
        self.send()
        return "break"                 # Enter는 전송, 줄바꿈은 Shift+Enter

    def _grow(self, _e=None):
        lines = min(5, self.txt.get("1.0", "end-1c").count("\n") + 1)
        if int(self.txt.cget("height")) != lines:
            self.txt.configure(height=lines)

    def send(self):
        body = self.txt.get("1.0", "end-1c").strip()
        if not body:
            return
        if len(body) > MSG_MAX:
            return self.set_status(f"메시지가 너무 깁니다({len(body)}/{MSG_MAX}).")
        self.cid_seq += 1
        cid = f"{self.cid_seq}"
        self.txt.delete("1.0", "end")
        self._grow()
        mark = self._bubble(self.app.uid, body, time.time(), mine=True, cid=cid)
        self.pending[cid] = mark
        self.app.client.send({"t": "msg", "room": self.room, "body": body, "cid": cid})

    # ---------- 이미지 ----------
    def _convert_async(self, fn, name):
        """이미지 변환은 **작업 스레드**에서 한다 — 큰 사진의 PNG 인코딩은 몇 초가
        걸려 GUI 스레드에서 하면 창이 얼어붙는다. 결과는 큐로 받아 메인 스레드가
        처리한다(tkinter는 메인 스레드 전용)."""
        self.set_status("이미지 변환 중...")

        def work():
            try:
                res, err = fn()
            except Exception as e:
                res, err = None, f"이미지 변환 실패: {e}"
            self.app.ui_q.put(("image_ready", self.room, res, err, name))

        threading.Thread(target=work, daemon=True).start()

    def _on_paste(self, _e=None):
        """Ctrl+V — 클립보드에 이미지가 있으면 PNG로 바꿔 바로 보낸다.
        이미지가 아니면 아무것도 하지 않아 평소 텍스트 붙여넣기가 그대로 동작한다."""
        if not HAS_PIL:
            return None                    # 평소 텍스트 붙여넣기로
        try:                               # 이미지인지 여부는 빨리 판별된다
            got = ImageGrab.grabclipboard()
        except Exception:
            return None
        if got is None:
            return None
        self._convert_async(png_from_clipboard, "clipboard.png")
        return "break"

    def pick_image(self):
        if not HAS_PIL:
            return self.set_status("이미지 기능에는 Pillow가 필요합니다.")
        if filedialog is None:
            return self.set_status("이 빌드에는 파일 선택 창이 없습니다"
                                   " (Ctrl+V 로 클립보드 이미지는 보낼 수 있습니다).")
        path = filedialog.askopenfilename(title="보낼 이미지 선택", parent=self.win,
                                          filetypes=IMG_EXTS)
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0] + ".png"
        self._convert_async(lambda: png_from_file(path), name)

    def _send_png(self, png, size, name):
        if self.app.server_ver < 2:
            # 옛 서버는 이미지 청크를 받으면 연결을 끊는다 → 아예 보내지 않는다
            return self.set_status(
                "이 서버는 이미지 전송을 지원하지 않습니다(서버를 업데이트하세요).")
        fid = self.app.send_image(self.room, png, size, name)
        mark = self._image_bubble(self.app.uid, png, time.time(), mine=True,
                                 caption=f"{name}  ({len(png)//1024}KB)")
        self.sending[fid] = mark
        self.set_status(f"이미지 전송 중... ({len(png)//1024}KB)")

    def finish_sending(self, fid):
        mark = self.sending.pop(fid, None)
        if mark is not None:
            try:
                mark.configure(text="✓")
            except Exception:
                pass
        self.set_status("이미지를 보냈습니다.")

    def fail_sending(self, fid):
        """연결이 끊겨 전송이 취소됐을 때. 이어 보낼 수 없으므로 실패로 표시한다."""
        mark = self.sending.pop(fid, None)
        if mark is None:
            return
        try:
            mark.configure(text="✕", fg="#FF8888")
        except Exception:
            pass
        self.set_status("연결이 끊겨 이미지 전송이 취소되었습니다. 다시 보내주세요.")

    def begin_incoming(self, fid, rec):
        row = tk.Frame(self.sf.body, bg=BG)
        row.pack(fill="x", padx=6, pady=(3, 0))
        holder = tk.Frame(row, bg=BG)
        holder.pack(side="left")
        tk.Label(holder, text=f"{rec['from']}  {time.strftime('%H:%M')}",
                 font=FONT_SMALL, bg=BG, fg=FG_DIM, anchor="w").pack(fill="x")
        lb = tk.Label(holder, text=f"이미지 받는 중... {rec['name']}", font=FONT,
                      bg=BUB_OTHER, fg=BUB_TEXT, padx=10, pady=6)
        lb.pack(anchor="w")
        self.images[fid] = {"row": row, "holder": holder, "label": lb, "photo": None}
        if self.sf.at_bottom():
            self.win.after_idle(self.sf.to_bottom)

    def update_incoming(self, fid, got, size):
        it = self.images.get(fid)
        if it and size:
            pct = min(100, int(got * 100 / size))
            try:
                it["label"].configure(text=f"이미지 받는 중... {pct}%")
            except Exception:
                pass

    def fail_incoming(self, fid, msg):
        it = self.images.pop(fid, None)
        if it:
            try:
                it["label"].configure(text=msg)
            except Exception:
                pass

    def finish_incoming(self, fid, png):
        it = self.images.get(fid)
        if not it:
            return
        try:
            it["label"].destroy()
        except Exception:
            pass
        self._attach_image(it["holder"], fid, png, anchor="w")
        if self.sf.at_bottom():
            self.win.after_idle(self.sf.to_bottom)

    def _photo(self, png):
        """PNG 바이트 → 표시용 PhotoImage. Tk 8.6은 PNG를 읽지만 축소는 못 하므로
        Pillow로 표시 크기까지 줄인 PNG를 새로 만들어 넘긴다(없으면 원본 그대로)."""
        data = png
        if HAS_PIL:
            try:
                with Image.open(io.BytesIO(png)) as im:
                    if im.width > IMG_VIEW_W:
                        r = IMG_VIEW_W / im.width
                        im = im.resize((IMG_VIEW_W, max(1, int(im.height * r))),
                                       Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="PNG")
                    data = buf.getvalue()
            except Exception:
                data = png
        return tk.PhotoImage(data=base64.b64encode(data))

    def _attach_image(self, holder, fid, png, anchor="w", caption=None):
        try:
            photo = self._photo(png)
        except Exception as e:
            tk.Label(holder, text=f"이미지를 표시할 수 없습니다({e})", font=FONT_SMALL,
                     bg=BUB_OTHER, fg=BUB_TEXT, padx=10, pady=6).pack(anchor=anchor)
            return
        lb = tk.Label(holder, image=photo, bg=BUB_MINE if anchor == "e" else BUB_OTHER,
                      bd=0, padx=4, pady=4, cursor="hand2")
        lb.pack(anchor=anchor)
        if caption:
            tk.Label(holder, text=caption, font=FONT_SMALL, bg=BG, fg=FG_DIM,
                     anchor=anchor).pack(fill="x")
        # photo 참조를 붙들어야 한다 — 놓으면 tkinter가 지워 이미지가 빈칸이 된다
        self.images[fid] = {"holder": holder, "label": lb, "photo": photo, "png": png}
        lb.bind("<Button-3>", lambda e, f=fid: self._image_menu(e, f))
        lb.bind("<Control-c>", lambda _e, f=fid: self.copy_image(f))
        lb.bind("<Double-Button-1>", lambda _e, f=fid: self.save_image_as(f))

    def _image_menu(self, event, fid):
        m = tk.Menu(self.win, tearoff=0, bg=BG_SOFT, fg=FG, font=FONT,
                    activebackground=ACCENT, activeforeground="#000000", bd=0)
        m.add_command(label="클립보드로 복사 (Ctrl+C)",
                      command=lambda: self.copy_image(fid))
        m.add_command(label="다른 이름으로 저장", command=lambda: self.save_image_as(fid))
        m.post(event.x_root, event.y_root)

    def copy_text(self, text):
        """메시지 글자를 클립보드로. tkinter 클립보드는 창이 사라지면 내용도
        사라지므로, 창을 닫아도 남도록 update()로 소유권을 확정한다."""
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.win.update()
        except Exception as e:
            return self.set_status(f"복사 실패: {e}")
        preview = text.replace("\n", " ")[:20]
        self.set_status(f"복사했습니다: {preview}{'…' if len(text) > 20 else ''}")

    def _text_menu(self, event, text):
        m = tk.Menu(self.win, tearoff=0, bg=BG_SOFT, fg=FG, font=FONT,
                    activebackground=ACCENT, activeforeground="#000000", bd=0)
        m.add_command(label="복사 (더블클릭)", command=lambda: self.copy_text(text))
        m.post(event.x_root, event.y_root)

    def copy_image(self, fid):
        it = self.images.get(fid)
        if not it or not it.get("png"):
            return
        ok = copy_png_to_clipboard(it["png"])
        self.set_status("이미지를 클립보드로 복사했습니다." if ok
                        else "클립보드 복사에 실패했습니다.")

    def save_image_as(self, fid):
        it = self.images.get(fid)
        if not it or not it.get("png"):
            return
        if filedialog is None:
            return self.set_status("이 빌드에는 저장 창이 없습니다"
                                   " (우클릭 → 클립보드로 복사는 됩니다).")
        p = filedialog.asksaveasfilename(parent=self.win, defaultextension=".png",
                                         initialfile=f"{fid[:8]}.png",
                                         filetypes=[("PNG", "*.png")])
        if not p:
            return
        try:
            with open(p, "wb") as fp:
                fp.write(it["png"])
            self.set_status(f"저장했습니다: {p}")
        except Exception as e:
            self.set_status(f"저장 실패: {e}")

    def _image_bubble(self, sender, png, ts, mine=False, caption=None):
        stick = self.sf.at_bottom()
        row = tk.Frame(self.sf.body, bg=BG)
        row.pack(fill="x", padx=6, pady=(3, 0))
        holder = tk.Frame(row, bg=BG)
        holder.pack(side="right" if mine else "left")
        when = time.strftime("%H:%M", time.localtime(ts or time.time()))
        tk.Label(holder, text=when if mine else f"{sender}  {when}", font=FONT_SMALL,
                 bg=BG, fg=FG_DIM, anchor="e" if mine else "w").pack(
                     fill="x", anchor="e" if mine else "w")
        line = tk.Frame(holder, bg=BG)
        line.pack(anchor="e" if mine else "w")
        mark = None
        if mine:
            mark = tk.Label(line, text="···", font=FONT_SMALL, bg=BG, fg=FG_DIM)
            mark.pack(side="left", padx=(0, 4))
        box = tk.Frame(line, bg=BG)
        box.pack(side="left")
        self._attach_image(box, new_fid(), png,
                           anchor="e" if mine else "w", caption=caption)
        if stick:
            self.win.after_idle(self.sf.to_bottom)
        return mark

    def add_message(self, d, mine=False, scroll=True):
        if d.get("kind") == "img":          # 로컬 기록에서 되살리는 이미지
            png = self.app.store.load_image(self.room, d.get("fid"))
            if png:
                self._image_bubble(d.get("from"), png, d.get("ts"), mine=mine,
                                   caption=d.get("name"))
            return
        cid = d.get("cid")
        if cid and cid in self.pending:        # 내가 보낸 것의 에코 = 전송 확인
            lb = self.pending.pop(cid)
            try:
                lb.configure(text="✓")
            except Exception:
                pass
            return
        self._bubble(d.get("from"), d.get("body", ""), d.get("ts"), mine=mine,
                     scroll=scroll)

    def _bubble(self, sender, body, ts, mine=False, cid=None, scroll=True):
        stick = self.sf.at_bottom()
        row = tk.Frame(self.sf.body, bg=BG)
        row.pack(fill="x", padx=6, pady=(3, 0))
        holder = tk.Frame(row, bg=BG)
        holder.pack(side="right" if mine else "left")

        when = time.strftime("%H:%M", time.localtime(ts or time.time()))
        meta = when if mine else f"{sender}  {when}"
        tk.Label(holder, text=meta, font=FONT_SMALL, bg=BG, fg=FG_DIM,
                 anchor="e" if mine else "w").pack(
                     fill="x", anchor="e" if mine else "w")

        line = tk.Frame(holder, bg=BG)
        line.pack(anchor="e" if mine else "w")
        mark = None
        if mine:
            mark = tk.Label(line, text="···", font=FONT_SMALL, bg=BG,
                            fg=FG_DIM)
            mark.pack(side="left", padx=(0, 4))
        bub = tk.Label(line, text=body, font=FONT, bg=BUB_MINE if mine else BUB_OTHER,
                       fg=BUB_TEXT, justify="left", anchor="w", padx=10, pady=6,
                       wraplength=self._wrap(), cursor="hand2")
        bub.pack(side="left")
        # 더블클릭하면 그 메시지를 클립보드로 복사(우클릭 메뉴로도 같은 동작)
        bub.bind("<Double-Button-1>", lambda _e, t=body: self.copy_text(t))
        bub.bind("<Button-3>", lambda e, t=body: self._text_menu(e, t))
        self.bubbles.append(bub)
        if scroll and stick:
            self.win.after_idle(self.sf.to_bottom)
        return mark if mine else bub

    def _wrap(self):
        w = self.sf.canvas.winfo_width() or 460
        return max(160, int(w * 0.7))

    def _on_width(self, _w):
        wrap = self._wrap()
        for b in self.bubbles:
            try:
                b.configure(wraplength=wrap)
            except Exception:
                pass

    def on_member(self, uid, joined):
        self.set_status(f"{uid} 님이 {'들어왔습니다' if joined else '나갔습니다'}.")

    def on_approve_req(self, uid):
        self.set_status(f"{uid} 님이 입장 승인을 기다립니다. (톱니바퀴 → 승인 대기)")
        if self.app.var_notify.get():
            notify(self.app.root, "입장 요청", f"[{self.room}] {uid} 님이 기다립니다.")


# === [8. 부속 창 — 만들기 · 비밀번호 · 승인 대기 · 블랙리스트] ===


class Modal:
    """고정 크기 modal 창 공통(채팅방 창만 리사이즈 가능하다)."""

    def __init__(self, app, title, parent=None):
        self.app = app
        self.win = tk.Toplevel(parent or app.root)
        self.win.title(title)
        self.win.configure(bg=BG)
        self.win.resizable(False, False)
        self.win.transient(parent or app.root)
        self.frame = tk.Frame(self.win, bg=BG, padx=16, pady=14)
        self.frame.pack(fill="both", expand=True)

    def grab(self):
        self.win.update_idletasks()
        try:
            self.win.grab_set()
        except Exception:
            pass

    def close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()


class CreateRoomDialog(Modal):
    def __init__(self, app):
        Modal.__init__(self, app, "채팅방 만들기")
        self.var_open = tk.StringVar(value="open")     # open | limited
        self.var_kind = tk.StringVar(value="pw")       # pw | allow | approve
        self.var_name = tk.StringVar()
        self.var_pw = tk.StringVar()

        r1 = tk.Frame(self.frame, bg=BG)
        r1.pack(fill="x")
        dark_radio(r1, "공개", self.var_open, "open", self._mode).pack(side="left")
        dark_radio(r1, "제한", self.var_open, "limited", self._mode).pack(
            side="left", padx=(10, 0))

        r2 = tk.Frame(self.frame, bg=BG)
        r2.pack(fill="x", pady=(4, 10))
        self.kind_radios = []
        for label, val in (("비밀번호 설정", "pw"), ("사전 승인", "allow"),
                           ("사후 승인", "approve")):
            rb = dark_radio(r2, label, self.var_kind, val, self._mode)
            rb.pack(side="left", padx=(0, 8))
            self.kind_radios.append(rb)

        tk.Label(self.frame, text="채팅방 이름", font=FONT, bg=BG, fg=FG,
                 anchor="w").pack(fill="x")
        e = dark_entry(self.frame, self.var_name, width=30)
        e.pack(fill="x", pady=(2, 10))
        e.focus_set()

        self.extra = tk.Frame(self.frame, bg=BG)     # 유형에 따라 바뀌는 부분
        self.extra.pack(fill="x")
        self.ent_pw = None
        self.txt_allow = None

        btns = tk.Frame(self.frame, bg=BG)
        btns.pack(fill="x", pady=(14, 0))
        self.bt_ok = dark_btn(btns, "생성하기", self.create)
        self.bt_ok.pack(side="left")
        dark_btn(btns, "취소", self.close).pack(side="left", padx=6)
        self.lb_msg = tk.Label(self.frame, text="", font=FONT_SMALL, bg=BG,
                               fg="#FF8888", anchor="w", wraplength=300,
                               justify="left")
        self.lb_msg.pack(fill="x", pady=(8, 0))

        self.var_name.trace_add("write", lambda *_: self._validate())
        self.var_pw.trace_add("write", lambda *_: self._validate())
        self._mode()
        self.grab()

    def _mode(self):
        limited = self.var_open.get() == "limited"
        for rb in self.kind_radios:
            # '공개'면 세 선택지는 보이기만 하고 회색으로 봉인한다
            rb.configure(state="normal" if limited else "disabled")
        for ch in list(self.extra.winfo_children()):
            ch.destroy()
        self.ent_pw = self.txt_allow = None
        if limited and self.var_kind.get() == "pw":
            tk.Label(self.extra, text="비밀번호", font=FONT, bg=BG, fg=FG,
                     anchor="w").pack(fill="x")
            self.ent_pw = dark_entry(self.extra, self.var_pw, show="*", width=30)
            self.ent_pw.pack(fill="x", pady=(2, 0))
            tk.Label(self.extra, text="1~19자", font=FONT_SMALL, bg=BG,
                     fg=FG_DIM, anchor="w").pack(fill="x")
        elif limited and self.var_kind.get() == "allow":
            tk.Label(self.extra, text="사전 승인 ID", font=FONT, bg=BG, fg=FG,
                     anchor="w").pack(fill="x")
            self.txt_allow = tk.Text(self.extra, height=5, font=FONT, bg=BG_SOFT,
                                     fg=FG, insertbackground=FG, relief="flat",
                                     bd=6, highlightthickness=1,
                                     highlightbackground="#333333")
            self.txt_allow.pack(fill="x", pady=(2, 0))
            self.txt_allow.bind("<KeyRelease>", lambda _e: self._validate())
            tk.Label(self.extra, text="한 줄에 ID 하나씩", font=FONT_SMALL, bg=BG,
                     fg=FG_DIM, anchor="w").pack(fill="x")
        self._validate()

    def _allow_ids(self):
        if not self.txt_allow:
            return []
        raw = self.txt_allow.get("1.0", "end-1c")
        return [x.strip() for x in raw.splitlines() if x.strip()]

    def _kind(self):
        return "open" if self.var_open.get() == "open" else self.var_kind.get()

    def _validate(self):
        ok = bool(self.var_name.get().strip())
        kind = self._kind()
        if kind == "pw":
            ok = ok and 1 <= len(self.var_pw.get()) <= 19
        elif kind == "allow":
            ok = ok and bool(self._allow_ids())
        # 필요한 정보가 채워질 때까지 '생성하기'는 봉인한다
        self.bt_ok.configure(state="normal" if ok else "disabled")
        return ok

    def create(self):
        if not self._validate():
            return
        kind, name = self._kind(), self.var_name.get().strip()
        msg = {"t": "room_create", "name": name, "kind": kind}
        if kind == "pw":
            # 방장은 비번 없이 입장하므로 보관할 필요가 없다(보관하면 잊을 대상만 늘어난다)
            msg["pw"] = self.var_pw.get()
        elif kind == "allow":
            msg["allow"] = self._allow_ids()
        self.app.joining[name] = "open"        # 생성 성공 시 그 방 창이 열린다
        self.app.client.send(msg)
        self.close()


class PasswordDialog(Modal):
    def __init__(self, app, room, wrong=False):
        Modal.__init__(self, app, "비밀번호")
        self.room = room
        tk.Label(self.frame, text=f"'{room}' 비밀번호", font=FONT, bg=BG, fg=FG,
                 anchor="w").pack(fill="x")
        self.var = tk.StringVar()
        e = dark_entry(self.frame, self.var, show="*", width=26)
        e.pack(fill="x", pady=(4, 0))
        e.focus_set()
        e.bind("<Return>", lambda _e: self.ok())
        if wrong:
            tk.Label(self.frame, text="비밀번호가 틀렸습니다.", font=FONT_SMALL,
                     bg=BG, fg="#FF8888", anchor="w").pack(fill="x", pady=(4, 0))
        btns = tk.Frame(self.frame, bg=BG)
        btns.pack(fill="x", pady=(12, 0))
        dark_btn(btns, "입장", self.ok).pack(side="left")
        dark_btn(btns, "취소", self.close).pack(side="left", padx=6)
        self.grab()

    def ok(self):
        pw = self.var.get()
        if not pw:
            return
        self.app.join_with_pw(self.room, pw)
        self.close()


class RoomPwDialog(Modal):
    """비밀번호 방의 비밀번호 변경(방장 전용).
    바꾼 뒤 다른 사람이 보관해둔 옛 비번은 다음 입장에서 거절되고, 그쪽
    클라이언트가 보관값을 지우고 입력창을 띄운다."""

    def __init__(self, app, room, parent=None):
        Modal.__init__(self, app, "비밀번호 변경", parent=parent)
        self.room = room
        tk.Label(self.frame, text=f"'{room}' 새 비밀번호", font=FONT, bg=BG, fg=FG,
                 anchor="w").pack(fill="x")
        self.var = tk.StringVar()
        e = dark_entry(self.frame, self.var, show="*", width=26)
        e.pack(fill="x", pady=(4, 0))
        e.focus_set()
        e.bind("<Return>", lambda _e: self.ok())
        tk.Label(self.frame, text="1~19자", font=FONT_SMALL, bg=BG, fg=FG_DIM,
                 anchor="w").pack(fill="x")
        btns = tk.Frame(self.frame, bg=BG)
        btns.pack(fill="x", pady=(12, 0))
        self.bt_ok = dark_btn(btns, "변경", self.ok)
        self.bt_ok.pack(side="left")
        dark_btn(btns, "취소", self.close).pack(side="left", padx=6)
        self.var.trace_add("write", lambda *_: self._validate())
        self._validate()
        self.grab()

    def _validate(self):
        ok = 1 <= len(self.var.get()) <= 19
        self.bt_ok.configure(state="normal" if ok else "disabled")
        return ok

    def ok(self):
        if not self._validate():
            return
        self.app.client.send({"t": "room_pw", "room": self.room,
                              "pw": self.var.get()})
        self.close()


class ApprovalDialog(Modal):
    """승인 대기 리스트 — 각 줄에 [-] 거절 / [+] 수락."""

    opened = {}

    def __init__(self, app, room):
        old = ApprovalDialog.opened.get(room)
        if old:
            old.win.lift()
            return
        Modal.__init__(self, app, "승인 대기 리스트")
        self.room = room
        ApprovalDialog.opened[room] = self
        tk.Label(self.frame, text=f"'{room}' 입장 대기", font=FONT, bg=BG,
                 fg=FG, anchor="w").pack(fill="x", pady=(0, 8))
        self.body = tk.Frame(self.frame, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.lb = tk.Label(self.frame, text="", font=FONT_SMALL, bg=BG, fg=FG_DIM)
        self.lb.pack(fill="x", pady=(8, 0))
        dark_btn(self.frame, "닫기", self.close).pack(pady=(10, 0))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        app.client.send({"t": "pending", "room": room})

    def fill(self, ids):
        for ch in list(self.body.winfo_children()):
            ch.destroy()
        if not ids:
            self.lb.configure(text="대기 중인 요청이 없습니다.")
        else:
            self.lb.configure(text="")
        for uid in ids:
            row = tk.Frame(self.body, bg=BG_ROW)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=uid, font=FONT, bg=BG_ROW, fg=FG, anchor="w",
                     width=18).pack(side="left", padx=6, pady=3)
            dark_btn(row, "+", lambda u=uid: self.decide(u, True), width=2).pack(
                side="right", padx=(2, 6))
            dark_btn(row, "-", lambda u=uid: self.decide(u, False), width=2).pack(
                side="right")

    def decide(self, uid, ok):
        self.app.client.send({"t": "approve", "room": self.room, "id": uid,
                              "ok": ok})
        self.app.client.send({"t": "pending", "room": self.room})

    def close(self):
        ApprovalDialog.opened.pop(self.room, None)
        Modal.close(self)


class BlockDialog(Modal):
    """블랙리스트 관리 — 강제 퇴장된 ID를 보고 해제한다."""

    opened = {}

    def __init__(self, app, room):
        old = BlockDialog.opened.get(room)
        if old:
            old.win.lift()
            return
        Modal.__init__(self, app, "블랙리스트")
        self.room = room
        BlockDialog.opened[room] = self
        tk.Label(self.frame, text=f"'{room}' 블랙리스트", font=FONT, bg=BG, fg=FG,
                 anchor="w").pack(fill="x", pady=(0, 8))
        self.body = tk.Frame(self.frame, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.lb = tk.Label(self.frame, text="", font=FONT_SMALL, bg=BG, fg=FG_DIM)
        self.lb.pack(fill="x", pady=(8, 0))
        row = tk.Frame(self.frame, bg=BG)
        row.pack(fill="x", pady=(10, 0))
        self.var_kick = tk.StringVar()
        dark_entry(row, self.var_kick, width=14).pack(side="left")
        dark_btn(row, "강제 퇴장", self.kick).pack(side="left", padx=6)
        dark_btn(self.frame, "닫기", self.close).pack(pady=(10, 0))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        app.client.send({"t": "blocklist", "room": room})

    def fill(self, ids):
        for ch in list(self.body.winfo_children()):
            ch.destroy()
        self.lb.configure(text="블랙리스트가 비어 있습니다." if not ids else "")
        for uid in ids:
            row = tk.Frame(self.body, bg=BG_ROW)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=uid, font=FONT, bg=BG_ROW, fg=FG, anchor="w",
                     width=18).pack(side="left", padx=6, pady=3)
            dark_btn(row, "해제", lambda u=uid: self.unblock(u)).pack(
                side="right", padx=6)

    def kick(self):
        uid = self.var_kick.get().strip()
        if not uid:
            return
        if not messagebox.askokcancel(
                "domichat", f"'{uid}' 를 강제 퇴장하고 블랙리스트에 등재합니다.",
                parent=self.win):
            return
        self.var_kick.set("")
        self.app.client.send({"t": "kick", "room": self.room, "id": uid})
        self.app.client.send({"t": "blocklist", "room": self.room})

    def unblock(self, uid):
        self.app.client.send({"t": "unblock", "room": self.room, "id": uid})
        self.app.client.send({"t": "blocklist", "room": self.room})

    def close(self):
        BlockDialog.opened.pop(self.room, None)
        Modal.close(self)


# === [9. 진입점] ===


def main():
    app = App()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
