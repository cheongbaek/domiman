# -*- coding: utf-8 -*-
"""
domiman_m.py — DOMIMAN 모바일(안드로이드) 원격제어 앱 Kotlin 포팅 레퍼런스
====================================================================
`앱UI설명.xlsx`(Sheet1=로그인 화면, Sheet2=메인 제어 화면)를 기준으로 만든
모바일 전용 모듈. domiman.py에서 데스크톱 전용 코드(tkinter GUI, pyautogui/
win32 클릭, WGC 캡처, OCR, 낚시 자동화 루틴)를 전부 제외하고, ① domichat
통신 프로토콜 ② 로그인/최근 로그인 저장·자동로그인 규칙 ③ 화면 상태 규칙만
다룬다. **"제어(dB) 역할"만** 맡는다 — 자기 앞으로 온 '명령'을 받아 실행하는
'피제어(d3)' 쪽 로직(domiman.py의 _handle_command)은 필요 없다.

■ 전송 계층: ntfy → domichat (260815a에서 PC판이 갈아탄 것을 모바일로 이식)
  메시지 규격은 **글자 하나 다르지 않다**:
      명령:  "(대상PC),(명령)[,인자...]"   예) seoul,S / seoul,T,30
      응답:  "(요청자ID),Z,..."            예) mypho,Z,0,1080,a,f,t,t
      보고:  ",Z,F,(코드)[,(서브)]"        예) ,Z,F,y,b (미끼 교체 성공)
      수량:  ",Z,N,(cur),(mx)" / ",Z,N,fail"   예) ,Z,N,114,570
             (260828a 추가 — 피제어 PC가 살림망을 읽을 때마다 요청 없이
              방송한다. 요청자 자리가 비어 있어 N **응답**과 구분된다.
              위젯이 '새로고침을 눌렀을 때만' 갱신되던 제약을 없애는 신호다.)
  바뀐 것은 전송 계층뿐이다:
    - 방 이름 = domi_fishing_{피제어 PC의 domichat 로그인 ID}
    - 방 유형 = 비밀번호 방, 고정 비번 `domi_fishing_9714`
      (일반 domichat 사용자의 오진입만 막는 용도. 방장이 없어도 입장 가능해
       승인 왕복이 없다)
    - 방장 = 피제어 PC. **휴대폰은 방을 만들지 않는다**(제어 전용). 대상 PC가
      한 번도 접속한 적이 없으면 방이 없어 error(room_missing)가 온다.
    - 발신자 식별은 domichat 로그인 ID이며 서버가 `from`에 찍어주므로 위조 불가.
      ntfy 시절 Title 자리를 그대로 대신한다.
  규격의 단일 기준 문서는 `domichat.md`. 클라이언트 계층은 domichat.py/
  domiman.py에서 **복제**했다(공용 모듈을 만들면 "자기 파일 하나만 교체"라는
  각 앱의 업데이트 규약이 깨진다).

■ 화면 구성 (앱UI설명.xlsx 기준 + domichat 이식으로 바뀐 부분)
  로그인 화면   — domiserver IP / domichat ID / domichat PW 입력 +
                  자동로그인 체크 + [로그인]/[…] 버튼.
  최근 로그인   — […] 버튼으로 진입. 짧게 탭=즉시 로그인, 길게 눌러 수정/삭제.
                  **자동 로그인을 체크하고 로그인했을 때만 목록에 남는다.**
  메인 제어     — 최상단에 '제어 PC 선택하기' 박스(기본 미선택). 고르면 그
                  PC의 방(domi_fishing_{PC})에 입장·구독하고 S 질의로 상태를
                  받아온다. 그 아래는 PC GUI와 동일(해상도/타이머/체크박스/
                  시작-중지/상태문구/예약종료/즉시회수/실시간수량확인/로그).

■ Kotlin(Chaquopy) 경계 규칙: PyObject dict 변환을 Kotlin에서 다루지 않도록
  **오가는 값은 전부 JSON 문자열**이다(`*_json` 함수들). 새 반환값이 생기면
  PyObject API를 파헤치지 말고 이 패턴을 따를 것.

■ 스레드: DomimanSession이 자체 스레드(세션·송신·펌프)를 돌리고, Kotlin은
  `poll_event_json(timeout)`으로 이벤트를 하나씩 꺼내간다. 콜백을 파이썬으로
  넘기지 않는 이유는 로그아웃/종료 때 Kotlin 람다가 죽은 스코프를 붙잡아
  블로킹되는 사고를 막기 위해서다(응답없음/ANR 대응).

단독 실행하면 로그인 → PC 선택 → 상태 질의까지의 스모크 테스트가 된다:
    python domiman_m.py <서버IP> <domichat ID> <PW> [제어할 PC 이름]
"""
import base64
import hashlib
import json
import queue
import re
import socket
import ssl
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# ---------- domichat 규격 상수 (domichat.md가 기준) ----------
CHAT_PORT = 47821                       # domiserver 기본 포트
ROOM_PREFIX = "domi_fishing_"
ROOM_PW = "domi_fishing_9714"           # 고정(오진입 방지용)
FRAME_HEAD = struct.Struct(">IB")       # [길이 4][종류 1]
FILE_HEAD = struct.Struct(">16sI")      # 'B'(이미지 청크) 머리: fid 16B + seq 4B
MAX_FRAME = 1024 * 1024
CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 60.0                     # 서버 ping 15초 → 60초 침묵이면 죽은 연결
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)

PENDING_TIMEOUT_SEC = 15.0   # domiman.py _check_pending_timeout과 동일
LOGIN_TIMEOUT_SEC = 20.0     # 접속+로그인 왕복 대기 상한
TARGET_TIMEOUT_SEC = 20.0    # 방 입장 + S 응답 대기 상한
SHOT_START_TIMEOUT = 15.0    # ack 이후 사진(file_begin)이 시작될 때까지 대기 상한
SHOT_STALL_TIMEOUT = 10.0    # 전송 시작 후 청크가 이 시간 넘게 끊기면 포기(domiman.py와 동일)

# domichat 계정 ID 규칙(domiserver.ID_RE와 동일). 제어할 PC 이름도 그 PC의
# domichat 로그인 ID이므로 같은 규칙을 쓴다.
ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,20}")


def is_valid_id(name):
    """domichat ID / 제어 대상 PC 이름 형식 검사."""
    return bool(ID_RE.fullmatch(name or ""))


def room_of(uid):
    """피제어 PC 하나당 방 하나 — 이름 규칙이 고정이라 계산으로 찾는다."""
    return f"{ROOM_PREFIX}{uid}"


def split_server(text):
    """'IP' 또는 'IP:포트' → (host, port). 포트가 없거나 이상하면 기본 포트."""
    s = (text or "").strip()
    if ":" in s:
        host, _, port = s.rpartition(":")
        try:
            return host.strip(), int(port)
        except ValueError:
            return s, CHAT_PORT
    return s, CHAT_PORT


# 상태 문구 (domiman.py STATUS_TEXT 그대로 포팅 — 엑셀 J8:K18 원본).
# 메인 화면 '[상태 메시지]' 자리에 띄울 텍스트.
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
# rs/bs(낚싯대/미끼 교체 시작)는 domiman.py 260728a에서 추가된 코드.
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

# 알림 설정 체크박스 <-> 보고 코드 매핑 (안드로이드 전용). 키 순서 = 앱 알림
# 설정 화면의 체크박스 순서(마스터 '알림 켜기'는 코드가 없어 여기 없음).
NOTIFY_KEYS = {
    "routine_start": ("s",),       # 살림망 회수 시작
    "routine_success": ("g",),     # 살림망 회수 성공
    "routine_fail":  ("f",),       # 살림망 회수 실패
    "rod_start":     ("rs",),      # 낚싯대 교체 시작
    "rod_success":   ("y", "r"),   # 낚싯대 교체 성공
    "rod_fail":      ("x", "r"),   # 낚싯대 교체 실패
    "bait_start":    ("bs",),      # 미끼 교체 시작
    "bait_success":  ("y", "b"),   # 미끼 교체 성공
    "bait_fail":     ("x", "b"),   # 미끼 교체 실패
    "crash":         ("x", "d"),   # 게임 튕김
}


def notify_key_for_report(rest):
    """보고 필드(rest, 예 ['y','r'] 또는 ['rs'])를 알림 설정 키로 역변환.
    매칭 없으면 None. Kotlin이 어느 알림 체크박스에 해당하는지 판단할 때 씀."""
    key = tuple(rest[:2]) if len(rest) >= 2 and (rest[0], rest[1]) in REPORT_STATUS \
        else tuple(rest[:1])
    for name, code in NOTIFY_KEYS.items():
        if code == key:
            return name
    return None


# ============================================================
# [1. 전송 계층: domichat 소켓 · TLS (domichat.md 규격)]
# ============================================================
class CertChanged(Exception):
    """고정해둔 서버 인증서 지문이 바뀌었다 — 서버 재설치가 아니라면 중간자."""

    def __init__(self, host, old, new):
        super().__init__(f"{host}: {old[:16]}… → {new[:16]}…")
        self.host, self.old, self.new = host, old, new


def _tls_context():
    """자체 서명 인증서를 쓰므로 체인·호스트명 검증은 끄고 **지문 고정(TOFU)**으로
    신뢰한다. **TLS 1.2로 고정**한다 — 수신·송신 스레드가 한 소켓을 나눠 쓰는
    구조라 1.3의 핸드셰이크 후 메시지(NewSessionTicket/KeyUpdate) 때문에 record
    layer가 깨진다(domichat.md '§1 TLS' 참고)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    if no_reneg:
        ctx.options |= no_reneg
    return ctx


def connect_secure(ip, port, pinned, timeout=CONNECT_TIMEOUT):
    """(소켓, 지문|None). 서버가 TLS를 안 쓰면 평문으로 다시 붙는다(하위 호환).
    PC판의 '자기 IP면 127.0.0.1로 폴백'은 휴대폰에선 의미가 없어(자기 자신에게
    붙어버린다) 두지 않는다."""
    raw = socket.create_connection((ip, port), timeout)
    try:
        sock = _tls_context().wrap_socket(raw)
        fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    except (ssl.SSLError, OSError):
        try:
            raw.close()
        except Exception:
            pass
        return socket.create_connection((ip, port), timeout), None
    if pinned and pinned != fp:
        try:
            sock.close()
        except Exception:
            pass
        raise CertChanged(f"{ip}:{port}", pinned, fp)
    return sock, fp


class ChatClient:
    """domiserver 세션 하나(접속 유지 + 자동 재연결).
    수신 프레임과 내부 사건을 전부 self.q로 넘긴다 — 상위(DomimanSession)가
    한 스레드에서 순서대로 꺼내 처리한다."""

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
        # 재연결 대기를 time.sleep 대신 Event로 — 로그아웃이 최대 30초 잠든
        # 스레드를 기다리지 않고 즉시 깨울 수 있어야 한다(응답없음 대응).
        self._wake = threading.Event()

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
        kind = chr(typ)
        if kind == "B":                    # 이미지 청크(스크린샷) — domichat.py와 동일 디코드
            if len(body) < FILE_HEAD.size:
                return {}
            raw_fid, seq = FILE_HEAD.unpack(body[:FILE_HEAD.size])
            return {"t": "bin", "fid": raw_fid.hex(), "seq": seq,
                    "data": body[FILE_HEAD.size:]}
        if kind != "T":
            return {}
        return json.loads(body.decode("utf-8"))

    # ---------- 세션 ----------
    def start(self, ip, port, uid, pw, pinned=None):
        self.ip, self.port, self.uid, self.pw = ip, port, uid, pw
        self.pinned = pinned
        self.want = True
        self.first_try = True
        self._wake.clear()
        threading.Thread(target=self._session_loop, daemon=True,
                         name="domichat-session").start()

    def _sleep(self, secs):
        """재연결 백오프 — logout()이 _wake를 세우면 즉시 깬다."""
        self._wake.wait(secs)

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
                self._sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
                idx += 1
                continue

            # 접속 타임아웃이 소켓에 남으면 조용할 때마다 끊긴다(domichat.md의
            # '최대 함정') → 읽기 타임아웃으로 교체하고 OS keepalive도 켠다.
            sock.settimeout(READ_TIMEOUT)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            self.sock = sock
            threading.Thread(target=self._tx_loop, args=(sock,), daemon=True,
                             name="domichat-tx").start()
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
            self._sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
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
        """wait=True면 지금 소켓으로 바로 보낸다(순서가 중요한 것). 기본은 송신
        큐 경유 — 연결이 끊긴 동안 쌓였다가 복구되면 순서대로 나간다."""
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
        """**절대 블로킹하지 않는다**(호출부가 UI 스레드일 수 있다). 예의상
        logout 프레임을 짧은 타임아웃으로 한 번 시도하고, 잠금을 못 잡으면
        건너뛰고 소켓을 shutdown+close 해 수신 스레드를 즉시 깨운다."""
        self.want = False
        self.logged_in.clear()
        self._wake.set()                   # 백오프 대기 중인 세션 스레드를 깨움
        sock, self.sock = self.sock, None
        if sock is not None:
            if self._send_lock.acquire(timeout=0.5):
                try:
                    sock.settimeout(1.0)
                    data = json.dumps({"t": "logout"}).encode("utf-8")
                    sock.sendall(FRAME_HEAD.pack(len(data), ord("T")) + data)
                except Exception:
                    pass
                finally:
                    self._send_lock.release()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        with self.txq.mutex:
            self.txq.queue.clear()


# ============================================================
# [2. 프로토콜: 제어(dB) 세션 — 방 입장·구독·명령·수신 분배]
# ============================================================
class DomimanSession:
    """domichat 로그인 하나 + '지금 제어 중인 PC' 하나를 들고 있는 세션.

    Kotlin(포그라운드 서비스)이 쓰는 방식:
        s = DomimanSession()
        s.start(ip, uid, pw, pinned)        # 비동기 접속 시작
        s.wait_login_json(20)               # {"ok":true,...} / {"ok":false,...}
        s.select_target("seoul")            # 제어할 PC 선택(방 입장+구독+S 질의)
        s.wait_target_json(20)              # {"ok":true,"status":{...}}
        while ...: s.poll_event_json(0.5)   # 이벤트 하나씩(없으면 "null")
        s.cmd_start() / s.cmd_stop() / ...
        s.stop()                            # 로그아웃(즉시 반환)
    """

    def __init__(self):
        self.chat = ChatClient()
        self.q = queue.Queue()      # Kotlin이 가져갈 앱 이벤트 큐
        self.my_id = ""             # domichat 로그인 ID(= 응답이 돌아올 이름)
        self.target = ""            # 제어 중인 PC 이름(= 그 PC의 domichat ID)
        self.pending = None         # {"kind": str, "sent": epoch}
        self.shot_wait = None       # 스크린샷 ack 후 사진 도착 대기 상태(domiman.py와 동일 개념)
        self.server = ""            # "ip:port"(지문 저장 키)
        self.joined_target = ""     # 실제로 입장까지 끝난 대상
        self._stop = threading.Event()
        self._pump = None
        self._login_ev = threading.Event()
        self._login_res = None
        self._target_ev = threading.Event()
        self._target_res = None

    # ---------- 수명 ----------
    def start(self, ip, uid, pw, pinned=None):
        """접속 시작(비동기). ip는 'IP' 또는 'IP:포트'."""
        host, port = split_server(ip)
        self.server = f"{host}:{port}"
        self.my_id = uid
        self._login_ev.clear()
        self._login_res = None
        self._stop.clear()
        self._pump = threading.Thread(target=self._pump_loop, daemon=True,
                                      name="domiman-pump")
        self._pump.start()
        self.chat.start(host, port, uid, pw, pinned or None)

    def stop(self):
        """로그아웃 — 즉시 반환한다(스레드 join 없음). 남은 이벤트는 버린다."""
        self._stop.set()
        self.target = ""
        self.joined_target = ""
        self.pending = None
        try:
            self.chat.logout()
        except Exception:
            pass
        self._finish_login(False, "로그아웃")
        self._finish_target(False, "logout")

    def is_connected(self):
        return bool(self.chat.logged_in.is_set())

    # ---------- 대상 PC 선택 ----------
    def select_target(self, pc):
        """제어할 PC를 고른다 — 이전 방에서 나오고 새 방에 입장·구독한다.
        입장 결과는 wait_target_json()으로 받는다. pc가 빈 값이면 해제만."""
        old = self.target
        if old and old != pc:
            self.chat.send({"t": "sub", "room": room_of(old), "on": False})
            self.chat.send({"t": "leave", "room": room_of(old)})
        self.target = pc or ""
        self.joined_target = ""
        self.pending = None
        self._target_res = None
        self._target_ev.clear()
        if not self.target:
            return
        self.chat.send({"t": "join", "room": room_of(self.target), "pw": ROOM_PW})

    def clear_target(self):
        self.select_target("")

    def resync(self):
        """포그라운드 복귀(ON_RESUME)·서비스 재시작 때 호출. 방에 아직 못 들어가
        있으면(대상 PC가 꺼져 있어 거절당했거나 재접속 직후) 다시 입장을 시도하고,
        이미 들어가 있으면 S 질의로 상태만 맞춘다. 이 한 번의 재시도가 '대상 PC를
        나중에 켰을 때' 앱을 다시 붙여주는 유일한 경로다."""
        if not self.target:
            return
        if self.joined_target == self.target:
            self.cmd_login()
            return
        self._target_res = None
        self._target_ev.clear()
        self.chat.send({"t": "join", "room": room_of(self.target), "pw": ROOM_PW})

    # ---------- 발신 ----------
    def _send_body(self, body):
        if not self.target:
            return False
        return self.chat.send({"t": "msg", "room": room_of(self.target), "body": body})

    def send_command(self, cmdbody, kind):
        """`{대상},{명령}` 발신 + pending 진입. kind는 응답 대응용 태그."""
        ok = self._send_body(f"{self.target},{cmdbody}")
        if ok:
            self.pending = {"kind": kind, "sent": time.time()}
        return ok

    def resolve_pending(self):
        p, self.pending = self.pending, None
        return p

    def check_pending_timeout(self):
        """15초 무응답이면 pending을 비우고 반환(호출부가 '응답없음' 처리)."""
        if self.pending is None:
            return None
        if time.time() - self.pending["sent"] <= PENDING_TIMEOUT_SEC:
            return None
        return self.resolve_pending()

    # ---------- 명령 빌더 (domiman.py 프로토콜 규격 그대로) ----------
    def cmd_login(self):
        """상태 질의(S). 대상 PC 선택 직후와 포그라운드 복귀 시 상태 동기화용."""
        self.send_command("S", "connect")

    def cmd_start(self):
        self.send_command("G", "G")

    def cmd_stop(self):
        self.send_command("P", "P")

    def cmd_sched_exit_ask(self):
        """예약 종료 1단계: 생존 확인 겸 분 입력 다이얼로그 오픈 트리거."""
        self.send_command("Y", "Y1")

    def cmd_sched_exit_set(self, minutes):
        """예약 종료 2단계 확정. minutes=0이면 예약 해제."""
        self.send_command(f"Y,{minutes:g}", "Y2")

    def cmd_collect_now(self):
        self.send_command("W", "W")

    def cmd_exit_program(self):
        self.send_command("Q", "Q")

    def cmd_set_resolution(self, mode):
        """mode: 'a'(자동감지) | '1080' | '1440'."""
        self.send_command(f"V,{mode}", "V")

    def cmd_set_timer(self, minutes):
        self.send_command(f"T,{minutes:g}", "T")

    def cmd_set_flags(self, logsave, rod=None, bait=None):
        """C,로그[,낚싯대,미끼] — 낚싯대/미끼 필드는 감시 모드(타이머=0)일 때만."""
        parts = ["t" if logsave else "f"]
        if rod is not None and bait is not None:
            parts.append("t" if rod else "f")
            parts.append("t" if bait else "f")
        self.send_command("C," + ",".join(parts), "C")

    def cmd_tank_query(self):
        """'실시간 수량확인'(N). 응답: ',Z,N,<cur>,<mx>' 또는 ',Z,N,fail'.
        피제어 PC는 마지막 파싱값이 있으면 즉시, 없으면 창을 띄우고 3초 뒤 1회
        파싱해 응답하므로 최대 5초 안팎 걸릴 수 있다(15초 타임아웃 내)."""
        self.send_command("N", "N")

    def cmd_screenshot(self):
        """'스크린샷'(I). 이미 사진을 기다리는 중이면 보내지 않는다(중복 요청
        방지 — domiman.py on_screenshot과 동일한 정책)."""
        if self.shot_wait is not None:
            return False
        return self.send_command("I", "I")

    # ---------- 스크린샷 수신(ack 이후 file_begin→bin×N→file_end) ----------
    def _begin_shot_wait(self):
        self.shot_wait = {"t0": time.time(), "last": time.time(), "fid": None,
                          "size": 0, "sha256": None, "name": "screenshot.png",
                          "buf": None}

    def _end_shot_wait(self):
        self.shot_wait = None

    def check_shot_timeout(self):
        """ack 후 사진이 시작되지 않거나(SHOT_START_TIMEOUT), 시작된 뒤 청크가
        멈추면(SHOT_STALL_TIMEOUT) 포기하고 실패 이벤트를 낸다. _pump_loop가
        유휴 주기(0.2초)마다 불러 domiman.py의 250ms 틱과 같은 역할을 한다."""
        w = self.shot_wait
        if w is None:
            return
        now = time.time()
        if w["fid"] is None:
            if now - w["t0"] <= SHOT_START_TIMEOUT:
                return
        elif now - w["last"] <= SHOT_STALL_TIMEOUT:
            return
        self._end_shot_wait()
        self._emit({"ev": "screenshot", "ok": False, "reason": "timeout"})

    def _on_file(self, t, d):
        """스크린샷 이미지 수신. 기다리는 중이 아니거나 제어 대상이 보낸 것이
        아니면 버린다(domiman.py _on_file과 동일한 방어)."""
        w = self.shot_wait
        if w is None or not self.target:
            return
        fid = d.get("fid")
        if t == "file_begin":
            if d.get("from") != self.target or d.get("room") != room_of(self.target):
                return
            w.update({"fid": fid, "size": int(d.get("size") or 0),
                      "sha256": d.get("sha256"), "buf": bytearray(),
                      "name": d.get("name") or "screenshot.png",
                      "last": time.time()})
            return
        if fid is None or fid != w.get("fid"):
            return                          # 다른 전송 — 무시
        if t == "bin":
            w["buf"] += d.get("data", b"")
            w["last"] = time.time()
            return
        if t == "file_abort":
            self._end_shot_wait()
            return self._emit({"ev": "screenshot", "ok": False, "reason": "aborted"})

        # file_end — 크기·sha256을 확인하고 base64로 실어 보낸다(Kotlin 경계는
        # JSON 문자열뿐이라 PyObject를 직접 건네지 않는다는 규칙을 이미지도 지킨다).
        png, name = bytes(w["buf"]), w["name"]
        ok = (not w["size"] or len(png) == w["size"])
        if ok and w.get("sha256"):
            ok = hashlib.sha256(png).hexdigest() == w["sha256"]
        self._end_shot_wait()
        if not ok:
            return self._emit({"ev": "screenshot", "ok": False, "reason": "corrupt"})
        self._emit({"ev": "screenshot", "ok": True, "name": name,
                    "png_b64": base64.b64encode(png).decode("ascii")})

    # ---------- 수신 펌프 ----------
    def _emit(self, obj):
        self.q.put(obj)

    def _finish_login(self, ok, msg=None, code=None):
        if self._login_ev.is_set():
            return
        self._login_res = {"ok": ok, "id": self.my_id, "msg": msg, "code": code,
                           "fp": self.chat.pinned, "server": self.server}
        self._login_ev.set()

    def _finish_target(self, ok, reason=None, status=None):
        if self._target_ev.is_set():
            return
        self._target_res = {"ok": ok, "pc": self.target, "reason": reason,
                            "status": status}
        self._target_ev.set()

    def _pump_loop(self):
        """ChatClient가 넘긴 프레임/사건을 앱 이벤트로 번역한다(단일 스레드)."""
        while not self._stop.is_set():
            try:
                d = self.chat.q.get(timeout=0.2)
            except queue.Empty:
                self.check_shot_timeout()   # 유휴 주기에도 타임아웃은 검사한다
                continue
            try:
                self._handle(d)
            except Exception as e:      # 한 프레임의 오류로 펌프가 죽지 않게
                self._emit({"ev": "error", "msg": f"수신 처리 실패: {e}"})
            self.check_shot_timeout()

    def _handle(self, d):
        ev = d.get("_ev")
        if ev == "connect_fail":
            self._finish_login(False, f"서버에 접속할 수 없습니다. ({d.get('msg')})",
                               "connect_fail")
            return self._emit({"ev": "login_fail", "msg": self._login_res["msg"]})
        if ev == "cert_changed":
            msg = ("서버 인증서가 바뀌었습니다. 서버를 재설치한 것이 아니라면 "
                   "누군가 가로채는 중일 수 있습니다.")
            self._finish_login(False, msg, "cert_changed")
            return self._emit({"ev": "cert_changed", "msg": msg,
                               "old": d.get("old"), "new": d.get("new")})
        if ev == "cert_pinned":
            return self._emit({"ev": "cert_pinned", "fp": d.get("fp"),
                               "server": self.server})
        if ev == "disconnected":
            self.joined_target = ""
            return self._emit({"ev": "disconnected", "msg": d.get("msg")})
        if ev:
            return

        t = d.get("t")
        if t in ("file_begin", "bin", "file_end", "file_abort"):
            return self._on_file(t, d)
        if t == "welcome":
            return self._on_welcome(d)
        if t == "msg":
            frm = d.get("from")
            if frm == self.my_id:
                return                 # 서버는 발신자에게도 돌려준다 — 내 것은 무시
            return self._on_msg(frm, (d.get("body") or "").strip())
        if t == "joined":
            room = d.get("room")
            if self.target and room == room_of(self.target):
                self.joined_target = self.target
                self.chat.send({"t": "sub", "room": room, "on": True})
                self._emit({"ev": "target_joined", "pc": self.target})
                self.cmd_login()       # 예전처럼 S 질의로 상태를 맞춘다
            return
        if t == "denied":
            room, reason = d.get("room"), d.get("reason")
            if self.target and room == room_of(self.target):
                self._finish_target(False, reason)
                self._emit({"ev": "target_denied", "pc": self.target,
                            "reason": reason, "msg": d.get("msg")})
            return
        if t == "member":
            # 방장(피제어 PC)이 방에서 빠지면 제어를 끝낸다
            if (self.target and not d.get("in") and d.get("id") == self.target
                    and d.get("room") == room_of(self.target)):
                self.joined_target = ""
                self._finish_target(False, "owner_gone")
                self._emit({"ev": "target_gone", "pc": self.target})
            return
        if t == "room_deleted":
            if self.target and d.get("room") == room_of(self.target):
                self.joined_target = ""
                self._finish_target(False, "room_deleted")
                self._emit({"ev": "target_gone", "pc": self.target})
            return
        if t == "error":
            return self._on_error(d)

    def _on_welcome(self, d):
        self.my_id = d.get("id") or self.my_id
        first = not self._login_ev.is_set()
        self._finish_login(True)
        self._emit({"ev": "login_ok" if first else "reconnected", "id": self.my_id,
                    "server": self.server})
        # 재접속이면 제어하던 방에 다시 들어간다(구독은 서버가 기억하지 않는다).
        if self.target:
            self.chat.send({"t": "join", "room": room_of(self.target), "pw": ROOM_PW})

    def _on_error(self, d):
        code, msg = d.get("code"), d.get("msg", "")
        if code in ("bad_login", "already_online", "disabled", "bad_id", "bad_pw"):
            self._finish_login(False, msg or "로그인에 실패했습니다.", code)
            self.chat.logout()
            return self._emit({"ev": "login_fail", "msg": msg, "code": code})
        if code == "room_missing" and self.target:
            # 그 PC가 domichat에 한 번도 접속한 적이 없어 방 자체가 없다.
            self._finish_target(False, "room_missing")
            return self._emit({"ev": "target_denied", "pc": self.target,
                               "reason": "room_missing", "msg": msg})
        self._emit({"ev": "error", "msg": msg, "code": code})

    def _on_msg(self, frm, body):
        kind, rest = self.dispatch(frm, body)
        if kind is None:
            return
        out = dispatch_result(self, kind, rest)
        if kind == "reply":
            self.resolve_pending()
            # 대상 선택 직후의 첫 상태 응답이 '입장 성공' 확정이다.
            if out.get("status") is not None:
                self._finish_target(True, None, out["status"])
        self._emit(out)

    # ---------- 수신 분배 (domiman.py _dispatch_ntfy의 'dB 원격 모드' 분기) ----------
    def dispatch(self, frm, body):
        """메시지 하나를 분류해 반환. ntfy 시절 Title 자리에 domichat `from`이
        들어온 것 말고는 규칙이 같다:
          ("reply", [필드...])   -- 내 질의/명령에 대한 응답 ({my_id},Z,...)
          ("report", [필드...])  -- 대상 PC의 상황 보고 (,Z,F,... 브로드캐스트)
          ("tank", [필드...])    -- 살림망 수량 상시 방송 (,Z,N,... 브로드캐스트)
          (None, None)           -- 무시 대상(대상 PC 것이 아니거나 규격 밖)

        **'tank'는 응답이 아니다(중요):** 요청 없이 감시 사이클마다 오므로
        pending(응답 대기)을 소모해선 안 된다 — 명령 응답을 기다리는 중에
        방송이 끼어들어 그 대기를 풀어버리면 UI가 잘못 열린다. 그래서 _on_msg의
        resolve_pending()은 'reply'에만 걸려 있다."""
        if not self.target or frm != self.target:
            return None, None
        parts = [p.strip() for p in body.split(",")]
        if len(parts) < 2:
            return None, None
        if parts[0] == self.my_id and parts[1] == "Z":
            return "reply", parts[2:]
        if parts[0] == "" and parts[1] == "Z" and len(parts) >= 3:
            if parts[2] == "F":
                return "report", parts[3:]
            if parts[2] == "N":
                return "tank", parts[3:]
        return None, None

    # ---------- Kotlin 경계(JSON 문자열) ----------
    def poll_event_json(self, timeout=0.5):
        """앱 이벤트 하나를 꺼내 JSON으로 반환. 없으면 "null".
        Kotlin은 Dispatchers.IO 코루틴에서 이 함수를 반복 호출한다."""
        try:
            return json.dumps(self.q.get(timeout=timeout), ensure_ascii=False)
        except queue.Empty:
            return "null"

    def wait_login_json(self, timeout=LOGIN_TIMEOUT_SEC):
        """접속+로그인 결과를 기다린다. 시간 초과면 ok=false."""
        if self._login_ev.wait(timeout):
            return json.dumps(self._login_res, ensure_ascii=False)
        return json.dumps({"ok": False, "code": "timeout",
                           "msg": "서버가 응답하지 않습니다."}, ensure_ascii=False)

    def wait_target_json(self, timeout=TARGET_TIMEOUT_SEC):
        """대상 PC 입장 + 첫 상태(S 응답)까지 기다린다.
        - 입장 실패(비번/블랙/방 없음) → ok=false + reason
        - 입장은 됐는데 PC가 응답 없음 → ok=false + reason='no_response'
          (방은 남아있지만 그 PC가 꺼져 있는 흔한 경우)"""
        if self._target_ev.wait(timeout):
            return json.dumps(self._target_res, ensure_ascii=False)
        joined = bool(self.joined_target)
        return json.dumps({"ok": False, "pc": self.target,
                           "reason": "no_response" if joined else "timeout",
                           "status": None}, ensure_ascii=False)

    def status_json(self):
        """현재 세션 요약(위젯·복구 판단용)."""
        return json.dumps({"connected": self.is_connected(), "id": self.my_id,
                           "server": self.server, "target": self.target,
                           "joined": bool(self.joined_target)}, ensure_ascii=False)

    # ---------- 파서 (ntfy 시절과 동일 — 메시지 규격이 안 바뀌었다) ----------
    @staticmethod
    def parse_status(rest):
        """S/V/T/C 응답 뒤 필드 파싱: 타이머,해상도,a|m,로그,실행중[,낚싯대,미끼].
        '실행중' 필드(domiman.py 260725d에서 추가)는 감시모드 여부와 무관하게
        항상 5번째 고정 위치 — 낚싯대/미끼처럼 있다 없다 하면 자리가 밀린다."""
        if len(rest) < 5:
            return None
        status = {
            "timer": rest[0],
            "resolution": rest[1],
            "res_auto": rest[2] == "a",
            "logsave": rest[3] == "t",
            "running": rest[4] == "t",
        }
        if len(rest) >= 7:
            status["rod"] = rest[5] == "t"
            status["bait"] = rest[6] == "t"
        return status

    @staticmethod
    def report_text(rest, name):
        """보고 필드 -> (로그/알림에 띄울 문장, 상태 문구 키) 또는 (None, None)."""
        key = tuple(rest[:2]) if len(rest) >= 2 else tuple(rest[:1])
        text = REPORT_TEXT.get(key)
        if text is None:
            return None, None
        return text.format(name=name), REPORT_STATUS.get(key)

    @staticmethod
    def parse_tank_reply(rest):
        """N 응답(rest[0]=='N' 가정) 뒤 필드 -> (cur, mx) 또는 None(파싱 실패).
        rest=['N','12','470'] -> (12,470),  ['N','fail'] -> None."""
        return DomimanSession.parse_tank_fields(rest[1:])

    @staticmethod
    def parse_tank_fields(fields):
        """['12','470'] -> (12,470). 'fail'이나 규격 밖이면 None(판독 실패).
        N 응답(위)과 수량 방송(',Z,N,...')이 **같은 파서를 쓴다** — 두 경로가
        어긋나면 응답과 방송이 서로 다른 값을 보여주게 된다."""
        if len(fields) >= 2:
            try:
                return int(fields[0]), int(fields[1])
            except ValueError:
                return None
        return None


def dispatch_result(session, kind, rest):
    """수신 메시지 하나를 Kotlin이 쓰는 이벤트 dict로 만든다. 스키마:
      {"ev": "reply"|"report"|"tank",
       "status": {...}|null,            # 상태 응답(S/V/T/C)일 때
       "tank": [cur,mx]|null,           # N(수량) 응답 또는 수량 방송일 때
       "tank_fail": bool,               # 위와 같되 파싱 실패(",Z,N,fail")
       "echo": "G"|"P"|"W"|"Q"|"Y"|"I"|null, # 상태 없는 명령 에코일 때
       "sched_minutes": str|null,       # echo=="Y"에 분 인자가 붙은 경우
       "shot_fail": bool,               # echo=="I"인데 ',Z,I,fail'(캡처 실패)일 때만 true
       "report_text": str|null,         # ev=="report"일 때 로그에 띄울 문장
       "report_status_key": str|null,   # 위와 같이 온 상태문구 키(STATUS_TEXT)
       "report_notify_key": str|null}   # 위와 같이 온 알림 설정 키(NOTIFY_KEYS)

    ※ G/P/W/Q/Y 응답은 domiman.py가 상태 필드 없이 명령 글자만 되돌려주는
    '에코'다(예: 시작 성공→',Z,G'). 상태 응답(S/V/T/C)은 첫 필드가 타이머
    숫자라 이 글자들과 겹치지 않으므로 rest[0]로 안전하게 구분된다. 과거엔
    이 분기가 없어 G/P 응답이 parse_status→None으로 버려져, 모바일에서
    '시작/중지'를 눌러도 running/pending이 갱신되지 않던 버그가 있었다."""
    out = {"ev": kind}
    if kind == "reply":
        first = rest[0] if rest else ""
        if first == "N":
            tank = DomimanSession.parse_tank_reply(rest)
            out["tank"] = list(tank) if tank else None
            out["tank_fail"] = tank is None
        elif first == "I":
            # 스크린샷 ack — ',Z,I'면 사진을 기다리기 시작하고, ',Z,I,fail'이면
            # 그 자리에서 끝(domiman.py _handle_remote_reply의 'I' 분기와 동일).
            out["echo"] = "I"
            if len(rest) >= 2 and rest[1] == "fail":
                out["shot_fail"] = True
            else:
                session._begin_shot_wait()
        elif first in ("G", "P", "W", "Q", "Y"):
            out["echo"] = first
            if first == "Y" and len(rest) >= 2:
                out["sched_minutes"] = rest[1]
        else:
            out["status"] = DomimanSession.parse_status(rest)
    elif kind == "report":
        text, status_key = DomimanSession.report_text(rest, session.target)
        out["report_text"] = text
        out["report_status_key"] = status_key
        out["report_notify_key"] = notify_key_for_report(rest)
    elif kind == "tank":
        # 살림망 수량 상시 방송(260828a). 필드 모양은 N 응답과 같지만 'N' 글자가
        # 앞에 없다. **알림·상태문구는 붙이지 않는다** — 사이클마다 오는 신호라
        # 알림을 띄우면 그것만으로 알림창이 가득 찬다. 받는 쪽(Kotlin)은 이
        # 이벤트로 **위젯 표시만** 갱신하고, 화면 로그에는 남기지 않는다.
        tank = DomimanSession.parse_tank_fields(rest)
        out["tank"] = list(tank) if tank else None
        out["tank_fail"] = tank is None
    return out


def dispatch_json(session, frm, body):
    """dispatch_result()의 단발 버전(테스트·재사용용). 규격 밖이면 {"ev":null}."""
    kind, rest = session.dispatch(frm, body)
    if kind is None:
        return json.dumps({"ev": None})
    return json.dumps(dispatch_result(session, kind, rest), ensure_ascii=False)


# ============================================================
# [3. 로그인 정보 저장 (최근 로그인 목록 + 자동로그인 무장상태)]
# ============================================================
@dataclass
class SavedLogin:
    """'최근 로그인' 한 행. domichat 이식으로 (ID, 피제어PC, 채널) →
    **(서버 IP, domichat ID, PW)** 로 바뀌었다 — 제어할 PC는 로그인이 아니라
    메인 화면 상단에서 고르기 때문."""
    ip: str
    uid: str
    pw: str

    def key(self):
        """같은 서버의 같은 계정이면 한 행(비밀번호만 갱신)."""
        return (self.ip, self.uid)

    def to_dict(self):
        return {"ip": self.ip, "id": self.uid, "pw": self.pw}

    @staticmethod
    def from_dict(d):
        return SavedLogin(d.get("ip", ""), d.get("id", ""), d.get("pw", ""))


class LoginStore:
    """'최근 로그인' 목록 + 자동로그인 '무장(armed)' 상태 + 서버 인증서 지문.
    실제 영속화(SharedPreferences)는 Kotlin 쪽 책임이며, **캐시/데이터 삭제 시
    함께 지워지는 저장소**를 쓸 것. 이 클래스는 데이터/규칙만 담당한다.

    사용자 확정 정책:
      - **자동 로그인을 체크하고 로그인했을 때만** 목록에 남는다. 체크 없이
        로그인하면 그 세션에서만 쓰이고 로그아웃하면 기억하지 않는다.
      - 같은 (서버IP, ID)로 다시 로그인하면 기존 행을 갱신하고 맨 위로 옮긴다.
      - 목록 개수 상한 없음(무제한 보관).
      - 인증서 지문(fingerprints)은 "IP:포트" → SHA-256 지문. 첫 접속에 기억해
        고정하고(TOFU), 이후 바뀌면 접속을 끊는다."""

    def __init__(self, recent=None, auto_login_enabled=False, fingerprints=None):
        self.recent = list(recent) if recent else []   # recent[0] = 최신
        self.auto_login_enabled = auto_login_enabled
        self.fingerprints = dict(fingerprints) if fingerprints else {}

    def add_or_bump(self, entry):
        """로그인 성공 + 자동로그인 체크됨일 때만 호출. 동일 항목이 있으면
        갱신 후 맨 앞으로."""
        self.recent = [e for e in self.recent if e.key() != entry.key()]
        self.recent.insert(0, entry)

    def remove(self, entry):
        """길게 눌러 '삭제'."""
        self.recent = [e for e in self.recent if e.key() != entry.key()]

    def update(self, old_entry, new_entry):
        """길게 눌러 '수정' 확정. 기존 자리를 새 값으로 교체만 하고 맨 앞으로
        옮기지는 않는다 — 재로그인이 아니라 단순 정보 수정."""
        for i, e in enumerate(self.recent):
            if e.key() == old_entry.key():
                self.recent[i] = new_entry
                return True
        return False

    def last(self):
        """가장 최근 로그인(자동로그인 대상). 없으면 None."""
        return self.recent[0] if self.recent else None

    def fingerprint_of(self, server):
        return self.fingerprints.get(server or "")

    def pin(self, server, fp):
        if server and fp:
            self.fingerprints[server] = fp

    def to_dict(self):
        return {"recent": [e.to_dict() for e in self.recent],
                "auto_login_enabled": self.auto_login_enabled,
                "fingerprints": self.fingerprints}

    @staticmethod
    def from_dict(d):
        return LoginStore(
            recent=[SavedLogin.from_dict(x) for x in d.get("recent", [])],
            auto_login_enabled=bool(d.get("auto_login_enabled", False)),
            fingerprints=d.get("fingerprints") or {})

    def to_json(self):
        """SharedPreferences처럼 문자열 하나만 다루는 저장소에 그대로 넣을 수
        있는 형태(Kotlin에서 dict/list 변환을 직접 다루지 않아도 되게)."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @staticmethod
    def from_json(s):
        """비어있거나 손상된 값이면 빈 LoginStore(첫 실행과 동일하게 안전 처리)."""
        if not s:
            return LoginStore()
        try:
            return LoginStore.from_dict(json.loads(s))
        except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
            return LoginStore()


# 제어 대상 PC 이름 기본 목록(메인 화면 상단 '제어 PC 선택하기' 리스트).
# 사용자가 추가·삭제할 수 있고, 실제 영속화는 Kotlin(SharedPreferences)이 한다.
DEFAULT_PC_LIST = ["seoul", "chungju", "galaxy"]


# ============================================================
# [4. 화면 상태 모델 (Kotlin ViewModel 상태 필드 대응 참고용)]
# ============================================================
class Screen(Enum):
    LOGIN = "login"
    RECENT_LOGINS = "recent"
    MAIN = "main"


@dataclass
class LoginFormState:
    """로그인 입력 폼 상태. mode='edit'이면 자동로그인 체크박스가 숨겨지고
    버튼이 [로그인]/[…] → [수정]/[취소]로 바뀐다."""
    ip: str = ""
    uid: str = ""
    pw: str = ""
    auto_login_checked: bool = False
    mode: str = "login"            # 'login' | 'edit'
    editing: SavedLogin = field(default=None)


# ============================================================
# [5. 로그 창 (항상 표시, 마지막 8줄만 유지)]
# ============================================================
class MobileLogBuffer:
    """PC와 달리 접기 기능이 없고 항상 펼쳐진 상태로 마지막 maxlen줄만 유지한다.
    'x' 버튼을 누르면 clear()."""

    def __init__(self, maxlen=8):
        self._lines = deque(maxlen=maxlen)

    def add(self, line):
        self._lines.append(line)

    def clear(self):
        self._lines.clear()

    def lines(self):
        return list(self._lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) not in (4, 5):
        print("사용법: python domiman_m.py <서버IP[:포트]> <domichat ID> <PW> [제어할 PC]")
        raise SystemExit(1)

    ip, uid, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    pc = sys.argv[4] if len(sys.argv) == 5 else ""

    store = LoginStore()
    sess = DomimanSession()
    print(f"[테스트] {ip} 에 '{uid}'로 접속합니다...")
    host, port = split_server(ip)
    sess.start(ip, uid, pw, store.fingerprint_of(f"{host}:{port}"))
    res = json.loads(sess.wait_login_json())
    print(f"[로그인] {res}")
    if not res.get("ok"):
        raise SystemExit(1)

    store.pin(res.get("server"), res.get("fp"))
    store.add_or_bump(SavedLogin(ip, uid, pw))
    store.auto_login_enabled = True
    print(f"[최근 로그인] {store.to_json()}")

    if pc:
        print(f"[테스트] '{pc}' 제어를 시작합니다...")
        sess.select_target(pc)
        tres = json.loads(sess.wait_target_json())
        print(f"[대상] {tres}")
        deadline = time.time() + 10
        while time.time() < deadline:
            ev = sess.poll_event_json(0.5)
            if ev != "null":
                print(f"[이벤트] {ev}")
    sess.stop()
    print("[테스트] 로그아웃했습니다.")
