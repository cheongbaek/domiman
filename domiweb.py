# -*- coding: utf-8 -*-
"""
domiweb.py — 브라우저(웹앱)용 원격제어 중계 서버
================================================
`https://cheongbaek.github.io/domiman/` 에서 받은 정적 웹앱과 domiserver 사이를
잇는 다리. **브라우저는 raw TCP를 못 열고 domichat의 지문 고정(TOFU)도 못 쓴다** —
그래서 이 프로세스가 브라우저 쪽은 WebSocket(WSS, 브라우저가 신뢰하는 인증서)으로,
domiserver 쪽은 기존 domichat 프로토콜로 말한다.

■ 이 파일과 `domiserver.py` [8. 웹 중계]의 관계 (260914d — 되살린 이유)
  260914a 에 이 파일이 서버 안으로 통합됐는데, 그 통합본은 **계정을 없앤 것이
  아니라** 가상 연결(`HubConn`)에 `web` 이라는 이름만 그대로 달아 `ONLINE` 에
  올린다 — "한 파일이 되었으니 접속·계정이 필요 없다"는 설명과 달리, 계정으로
  자리를 잡는 구조 자체는 그대로다. 그럴 바에는 **원래대로 계정 하나로
  127.0.0.1 에 붙는 편**이 낫다는 판단이다(사용자 확정, 260914d):
  - 서버 코드에 웹 전용 상태(브라우저 목록·업로드·방 캐시)가 섞이지 않는다.
  - 중계가 죽어도 채팅 서버는 멀쩡하고, 중계만 따로 껐다 켤 수 있다.
  - 계정·비밀번호·지문 고정이 바깥 클라이언트와 **똑같은 길**을 탄다(예외 없음).
  **둘을 동시에 띄우지 말 것** — 47822 를 두 번 열 수 없고, 같은 ID(`web`)로
  두 번 로그인할 수도 없다(`already_online`). 서버의 `domiserver.json` 에서
  `"web": false` 로 내장 중계를 끄고 이 파일을 띄운다.

■ 왜 '바이트 파이프'가 아니라 '중계'인가 (설계 결정)
  domiserver는 **같은 ID 동시 접속을 불허**한다. 브라우저마다 domichat 로그인을
  시키면 기기 하나만 쓸 수 있다. 그래서 domiweb 혼자 `web` 계정으로 127.0.0.1에
  붙고, 브라우저 여러 대를 그 세션 하나에 **다중화**한다.
  덕분에 **브라우저에는 계정·비밀번호가 아예 실리지 않는다**(공개 정적 사이트에
  자격을 박는 문제가 구조적으로 없어진다).

■ 흐름
  브라우저 ──wss://<호스트>:47822/ws── domiweb ──TLS 47821── domiserver
                                                                  ▲
                                            피제어 PC(seoul 등) ───┘
  - **브라우저가 지목한 PC의 방에만 들어간다**(마지막 사람이 그 PC를 떠나면 방에서
    나온다). 동시에 여러 PC를 돌릴 일이 없고, 보지도 않는 방의 수량 방송을 계속
    받을 이유도 없다.
  - 방 이름·비번·명령 문자열은 domichat.md / domiman.py 규격 그대로다. domiweb는
    `web,Z,...` 응답과 `,Z,F,*`·`,Z,N,*` 방송을 **해석하지 않고 그대로 넘긴다**
    (파싱은 브라우저가 한다 — 규격의 단일 소유자를 늘리지 않기 위해서).
  - 예외는 스크린샷뿐이다: 'B' 프레임(이미지 청크)은 여기서 조립해 완성된 PNG를
    base64로 넘긴다(브라우저에 이진 프레임 조립 로직을 또 두지 않는다).

■ 브라우저 ↔ domiweb 프레임 (WebSocket, JSON 텍스트)
  받는 것: {"t":"hello"} / {"t":"select","pc":..} / {"t":"cmd","pc":..,"body":"S"}
           {"t":"add_pc","pc":..} / {"t":"del_pc","pc":..} / {"t":"pong"}
  주는 것: {"t":"ready","my_id":..,"pcs":[..],"connected":bool,"snap":{pc:{...}}}
           {"t":"msg","pc":..,"body":..}        — 방에서 온 원문 그대로
           {"t":"pcs","pcs":[..]}               — 목록 변경
           {"t":"pc","pc":..,"online":bool|null,"joined":bool,"reason":..}
           {"t":"up","connected":bool,"msg":..} — 상류(domiserver) 연결 상태
           {"t":"shot","pc":..,"ok":bool,"name":..,"b64":..,"reason":..}
           {"t":"err","msg":..}

■ 채팅(260913a) — 같은 연결에 얹은 두 번째 기능
  받는 것: {"t":"rooms"}                        — 방 목록 요청
           {"t":"room_open","room":..,"pw":?}   / {"t":"room_close","room":..}
           {"t":"chat","room":..,"body":..,"cid":..}
           {"t":"img_begin","room":..,"fid":32hex,"name":..,"size":n,"sha256":..}
           {"t":"img_chunk","fid":..,"seq":n,"b64":..} / {"t":"img_end","fid":..}
  주는 것: {"t":"rooms","list":[{name,kind,owner,created,allowed,blocked,waiting}]}
           {"t":"room","room":..,"state":"joined|denied|closed|deleted|kicked|
                                          approve_res",...}
           {"t":"chat","room":..,"from":..,"body":..,"mid":..,"ts":..,"cid":?}
           {"t":"chat_img","room":..,"from":..,"fid":..,"name":..,"b64":..,"ts":..}
           {"t":"chat_hist","room":..,"items":[{"t":"chat",...}]}
           {"t":"chat_err","room":..,"msg":..}

  - **브라우저는 모두 domiweb의 한 계정(`web`)을 공유한다.** 그래서 방 입장 자격도
    그 ID 기준이고(사후 승인 방은 `web`을 승인해야 한다), 다른 브라우저가 보낸 말도
    서로에게 '내 말'로 보인다. 웹앱에 계정을 두지 않기로 한 대가이며, 제어 기능과
    같은 구조다.
  - **구독·알림은 웹에 없다.** 방을 보고 있는 브라우저가 하나도 없으면 그 방에서
    나온다(제어 방의 `_maybe_leave`와 같은 규칙). 서버가 대화를 저장하지 않으므로
    그동안의 대화는 공백으로 남는다.
  - **제어용 방(`domi_fishing_*`)은 채팅 목록에서 감춘다.** 사이클마다 오는 수량
    방송이 대화창을 뒤덮고, 아무 문자열이나 그 방에 흘리면 명령 화이트리스트
    (`CMD_RE`)를 우회하는 통로가 된다.

■ 지키는 함정 (domichat.md에서 이미 값을 치른 것들)
  - 접속 직후 `settimeout(READ_TIMEOUT)` — create_connection의 타임아웃이 남으면
    조용할 때마다 끊긴다.
  - **TLS는 1.2로 고정**(상류·하류 모두). 한 소켓을 수신 스레드와 송신 스레드가
    나눠 쓰는 구조라 1.3의 NewSessionTicket/KeyUpdate가 record layer를 깬다.
  - 서버 ping(15초)에 pong 즉답. 45초 무응답이면 서버가 끊는다.
  - 발신은 **스로틀**을 거친다 — domiserver는 한 연결에 10초 20건 제한(MSG_BURST)을
    걸고, 여기서는 브라우저 여러 대의 명령이 **한 연결로** 합쳐지므로 그 한도를
    쉽게 넘길 수 있다.
  - 상류가 끊기면 대기 중인 명령은 **버린다**. 재접속 후 늦게 나가는 'G'(시작)는
    사용자가 이미 포기한 명령이라 위험하다.

실행:  python domiweb.py        (설정은 옆의 domiweb.json — 없으면 만들어진다)
"""
import base64
import hashlib
import json
import os
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
from collections import deque

APP_VERSION = "260914d"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "domiweb.json")

# === [1. 설정] ===

DEFAULT_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 47822,          # 브라우저(WSS)용. domiserver 47821 옆자리
    "server": "127.0.0.1:47821",   # 상류 domiserver
    "id": "web",                   # domichat 계정 — domiweb 전용
    "pw": "hello",
    "pcs": ["seoul", "chungju", "domi"],   # 기본 피제어 PC 목록(웹에서 추가 가능)
    "certfile": "",                # 브라우저가 신뢰하는 인증서(fullchain PEM)
    "keyfile": "",                 # 그 개인키(PEM). 비우면 평문 ws(개발용)
    "allow_origins": [],           # 빈 배열이면 Origin 검사 없음(오픈 방침)
    "server_fp": {},               # "ip:port" -> 상류 인증서 지문(TOFU)
    "chat": True,                  # 브라우저 채팅 중계(끄면 목록·입장·발신 전부 거절)
                                   # ※ 서버 내장 중계 쪽 같은 항목은 `web_chat` 이다
}

CONFIG = dict(DEFAULT_CONFIG)

# domichat 규격 (domichat.md 기준 — domiman.py/domiman_m.py와 같은 값)
ROOM_PREFIX = "domi_fishing_"
ROOM_PW = "domi_fishing_9714"
FRAME_HEAD = struct.Struct(">IB")
FILE_HEAD = struct.Struct(">16sI")
MAX_FRAME = 1024 * 1024
CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 60.0
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)

# 발신 스로틀 — domiserver는 MSG_BURST=20 / MSG_WINDOW=10초. 브라우저 여러 대의
# 명령이 이 연결 하나로 합쳐지므로 한도보다 낮게 잡아 여유를 남긴다.
SEND_BURST, SEND_WINDOW = 12, 10.0

SHOT_MAX_BYTES = 16 * 1024 * 1024   # 스크린샷 상한(실제 2MB 남짓). 메모리 보호
SHOT_WAIT_SEC = 40.0                # 브라우저의 사진 대기 유효시간
ROOM_RETRY_SEC = 60.0               # 방이 없던 PC를 다시 찾아보는 주기
WS_PING_SEC = 20.0
WS_MAX_RX = 64 * 1024               # 브라우저가 보내는 프레임 상한(이미지 청크 포함)
LOG_BACKLOG = 30                    # PC별로 보관하는 최근 원문 수(새 브라우저용)

# --- 채팅 ---
IMG_CHUNK = 64 * 1024               # 상류 'B' 청크 크기(domichat.md 상한과 같음)
CHAT_IMG_MAX = 16 * 1024 * 1024     # 중계할 이미지 상한. 서버 기본값은 32MB지만
                                    # base64로 브라우저 여러 대에 밀어넣는 구조라
                                    # 절반으로 조인다(스크린샷 상한과 같은 값)
CHAT_MSG_MAX = 4000                 # domiserver msg_max_len 기본값과 같음
CHAT_BACKLOG = 80                   # 방마다 보관하는 최근 대화 수(재입장·다중 접속용)
CHAT_BURST, CHAT_WINDOW = 8, 10.0   # 브라우저 **한 대**의 발신 상한(상류 스로틀과 별개)
CHAT_UPLOAD_MAX = 2                 # 브라우저 한 대가 동시에 올릴 수 있는 이미지 수
ROOMS_REFRESH_SEC = 2.0             # 방 목록 재조회 최소 간격(room_new 연타 방지)
FID_RE = re.compile(r"[0-9a-f]{32}")

ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,20}")
# 명령 화이트리스트. 누구나 붙을 수 있는 공개 중계이므로, 방에 흘려보낼 수 있는
# 문자열을 **domiman 명령 규격으로만** 제한한다(채팅방 스팸 통로가 되지 않게).
CMD_RE = re.compile(r"[SGPYWQVTCNI](,[A-Za-z0-9.\-]{1,12}){0,3}")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    global CONFIG
    CONFIG = dict(DEFAULT_CONFIG)
    try:
        # utf-8-sig: PowerShell의 `Set-Content -Encoding UTF8`은 **BOM을 붙인다.**
        # 그냥 utf-8로 읽으면 json이 BOM에서 깨져 설정이 조용히 기본값으로 돌아가고,
        # 그러면 인증서 경로가 비어 평문 ws로 열려 "브라우저가 못 붙는다".
        with open(CONFIG_PATH, encoding="utf-8-sig") as fp:
            data = json.load(fp)
        for k, v in data.items():
            if k in DEFAULT_CONFIG and isinstance(v, type(DEFAULT_CONFIG[k])):
                CONFIG[k] = v
    except FileNotFoundError:
        save_config()
    except Exception as e:
        log(f"[경고] 설정을 읽지 못해 기본값으로 갑니다: {e}")


def save_config():
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(CONFIG, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        log(f"[경고] 설정 저장 실패: {e}")


def room_of(uid):
    return f"{ROOM_PREFIX}{uid}"


def split_server(text):
    s = (text or "").strip()
    if ":" in s:
        host, _, port = s.rpartition(":")
        try:
            return host.strip(), int(port)
        except ValueError:
            return s, 47821
    return s, 47821


# === [2. 상류: domiserver 세션 (domichat 프로토콜)] ===


def _upstream_tls_context():
    """상류는 자체 서명 인증서 + 지문 고정(TOFU)이라 체인 검증을 끈다.
    **TLS 1.2 고정** — 수신/송신 스레드가 한 소켓을 나눠 쓰므로 1.3은 깨진다
    (domichat.md '§1 TLS')."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    if no_reneg:
        ctx.options |= no_reneg
    return ctx


def connect_upstream(ip, port, pinned):
    """(소켓, 지문|None). 서버가 TLS를 안 쓰면 평문으로 다시 붙는다.
    지문이 바뀌면 붙지 않고 예외를 낸다(중간자 방어)."""
    raw = socket.create_connection((ip, port), CONNECT_TIMEOUT)
    try:
        sock = _upstream_tls_context().wrap_socket(raw)
        fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    except (ssl.SSLError, OSError):
        try:
            raw.close()
        except Exception:
            pass
        return socket.create_connection((ip, port), CONNECT_TIMEOUT), None
    if pinned and pinned != fp:
        try:
            sock.close()
        except Exception:
            pass
        raise ssl.SSLError(f"인증서 지문이 바뀌었습니다: {pinned[:16]}… → {fp[:16]}…")
    return sock, fp


class ChatClient:
    """domiserver 연결 하나(자동 재연결). 받은 프레임은 전부 self.q로 넘긴다.
    domiman_m.ChatClient의 복제인데, **첫 접속 실패에도 포기하지 않는다** —
    이쪽은 사람이 보고 있는 앱이 아니라 상시 구동 데몬이다."""

    def __init__(self):
        self.q = queue.Queue()
        self.txq = queue.Queue()
        self.sock = None
        self.ip = self.uid = self.pw = None
        self.port = 47821
        self.pinned = None
        self.want = False
        self.server_ver = 1       # welcome의 ver (1=텍스트만, 2=이미지 지원)
        self.logged_in = threading.Event()
        self._send_lock = threading.Lock()
        self._wake = threading.Event()

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
        if kind == "B":                     # 이미지 청크 — 머리를 떼어 넘긴다
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
        self._wake.clear()
        threading.Thread(target=self._session_loop, daemon=True,
                         name="up-session").start()

    def _sleep(self, secs):
        self._wake.wait(secs)

    def _session_loop(self):
        idx = 0
        while self.want:
            try:
                sock, fp = connect_upstream(self.ip, self.port, self.pinned)
                if fp and not self.pinned:
                    self.pinned = fp
                    self.q.put({"_ev": "cert_pinned", "fp": fp})
            except Exception as e:
                self.q.put({"_ev": "disconnected", "msg": str(e)})
                self._sleep(RECONNECT_BACKOFF[min(idx, len(RECONNECT_BACKOFF) - 1)])
                idx += 1
                continue

            # create_connection의 타임아웃이 소켓에 남으면 조용할 때마다 끊긴다.
            sock.settimeout(READ_TIMEOUT)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            self.sock = sock
            threading.Thread(target=self._tx_loop, args=(sock,), daemon=True,
                             name="up-tx").start()
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
                        try:
                            self.server_ver = int(d.get("ver") or 1)
                        except (TypeError, ValueError):
                            self.server_ver = 1
                        self.logged_in.set()
                        logged = True
                    self.q.put(d)
            except Exception as e:
                self.q.put({"_ev": "note", "msg": f"상류 수신 종료: {e}"})
            finally:
                self.logged_in.clear()
                try:
                    sock.close()
                except Exception:
                    pass
                self.sock = None

            if not self.want:
                break
            # 끊긴 동안 쌓인 명령은 버린다 — 사용자가 이미 포기한 명령이 재접속
            # 뒤에 늦게 나가면(예: 'G' 낚시 시작) 오히려 위험하다.
            self.drop_queued()
            if logged:
                idx = 0
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
                return

    def drop_queued(self):
        with self.txq.mutex:
            self.txq.queue.clear()

    def send(self, obj):
        self.txq.put(obj)
        return True

    def send_chunk(self, fid_hex, seq, data):
        """이미지 청크를 프레임으로 만들어 송신 큐에 넣는다(순서 보장).

        **끊기면 이 청크들은 `drop_queued()`가 버린다** — 이어 보낼 수 없고(서버 쪽
        전송 상태가 사라진다), 이미지를 모르는 옛 서버는 'B'를 받는 즉시 연결을
        끊어 재접속마다 같은 청크가 다시 나가는 고리에 빠진다(domichat.md의 사고)."""
        body = FILE_HEAD.pack(bytes.fromhex(fid_hex), seq) + data
        self.txq.put(("B", fid_hex, FRAME_HEAD.pack(len(body), ord("B")) + body))

    def send_now(self, obj):
        """지금 소켓으로 바로 보낸다(순서가 중요한 것)."""
        sock = self.sock
        if sock is None or not self.logged_in.is_set():
            return False
        try:
            self._raw_send(sock, obj)
            return True
        except Exception:
            return False

    def stop(self):
        self.want = False
        self.logged_in.clear()
        self._wake.set()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


# === [3. WebSocket — 핸드셰이크 · 프레임 코덱 (RFC 6455, 표준 라이브러리만)] ===


class WSClosed(Exception):
    pass


class SockReader:
    """소켓 위의 버퍼 리더. WS 프레임 경계와 HTTP 헤더 경계를 여기서 자른다."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WSClosed("상대가 연결을 닫음")
        self.buf += chunk

    def take(self, n):
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_until(self, sep, limit):
        while True:
            i = self.buf.find(sep)
            if i >= 0:
                out = bytes(self.buf[:i])
                del self.buf[:i + len(sep)]
                return out
            if len(self.buf) > limit:
                raise WSClosed("헤더가 너무 깁니다")
            self._fill()


def ws_frame(opcode, payload=b""):
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def ws_read_frame(reader):
    """(opcode, payload). 클라이언트 프레임은 반드시 마스킹돼 있어야 한다."""
    b0, b1 = reader.take(2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", reader.take(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", reader.take(8))[0]
    if ln > WS_MAX_RX:
        raise WSClosed(f"프레임 과대({ln})")
    if not masked:
        raise WSClosed("마스킹되지 않은 클라이언트 프레임")
    key = reader.take(4)
    data = bytearray(reader.take(ln))
    for i in range(ln):
        data[i] ^= key[i & 3]
    return opcode, bytes(data)


HTTP_INFO = (
    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
    "Connection: close\r\nContent-Length: {n}\r\n\r\n{body}"
)


def ws_handshake(reader, sock):
    """WebSocket 업그레이드. 업그레이드가 아니면 짧은 안내 페이지를 주고 False.
    (브라우저로 https://호스트:47822/ 를 열어 인증서·생존을 눈으로 볼 수 있게)"""
    raw = reader.read_until(b"\r\n\r\n", 16384).decode("latin-1")
    lines = raw.split("\r\n")
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()

    if "websocket" not in headers.get("upgrade", "").lower():
        body = f"domiweb {APP_VERSION} — 살아 있습니다. 웹앱에서 /ws 로 접속하세요.\n"
        sock.sendall(HTTP_INFO.format(n=len(body.encode()), body=body).encode("utf-8"))
        return False

    origins = CONFIG["allow_origins"]
    origin = headers.get("origin", "")
    if origins and origin not in origins:
        sock.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
        raise WSClosed(f"허용되지 않은 Origin({origin})")

    key = headers.get("sec-websocket-key", "")
    if not key:
        sock.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        raise WSClosed("Sec-WebSocket-Key 없음")
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    sock.sendall(
        ("HTTP/1.1 101 Switching Protocols\r\n"
         "Upgrade: websocket\r\nConnection: Upgrade\r\n"
         f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
    return True


# === [4. 브라우저 연결] ===


class WebConn:
    """브라우저 하나. 수신은 이 객체를 만든 스레드가, 송신은 전용 스레드가 한다
    (한 TLS 소켓에 쓰는 스레드는 하나뿐이어야 한다)."""

    def __init__(self, hub, sock, addr):
        self.hub = hub
        self.sock = sock
        self.addr = addr
        self.txq = queue.Queue()
        self.alive = True
        self.pc = ""                 # 지금 보고 있는 PC
        self.shot_wait = 0.0         # 스크린샷을 기다리기 시작한 시각
        self.rooms = set()           # 지금 열어 둔 채팅방(UI 기준)
        self.chat_times = deque()    # 채팅 발신 시각(브라우저 한 대의 스로틀)
        self.uploads = set()         # 올리는 중인 이미지 fid
        threading.Thread(target=self._tx_loop, daemon=True, name="web-tx").start()

    def who(self):
        return f"{self.addr[0]}:{self.addr[1]}"

    def send(self, obj):
        if self.alive:
            self.txq.put(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _tx_loop(self):
        last_ping = time.time()
        while self.alive:
            try:
                data = self.txq.get(timeout=0.5)
            except queue.Empty:
                data = None
            try:
                if data is not None:
                    self.sock.sendall(ws_frame(0x1, data))
                if time.time() - last_ping >= WS_PING_SEC:
                    self.sock.sendall(ws_frame(0x9))    # ping — 브라우저가 자동 pong
                    last_ping = time.time()
            except Exception:
                self.close()
                return

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    # ---------- 수신 ----------
    def serve(self, reader):
        while self.alive:
            opcode, data = ws_read_frame(reader)
            if opcode == 0x8:                       # close
                raise WSClosed("브라우저가 닫음")
            if opcode in (0x9, 0xA):                # ping/pong — 브라우저 JS는
                continue                            # ping을 보낼 수 없다(무시)
            if opcode not in (0x1, 0x2, 0x0):
                continue
            try:
                d = json.loads(data.decode("utf-8"))
            except Exception:
                self.send({"t": "err", "msg": "JSON을 해석할 수 없습니다."})
                continue
            if isinstance(d, dict):
                self.hub.on_web(self, d)


# === [5. 중계 허브] ===


class Hub:
    """상류 세션 하나 + 브라우저 여러 대. 모든 상태 변경은 이 객체를 거친다.

    상류 메시지는 **해석하지 않고 그대로** 브라우저에 넘기는 것이 원칙이다.
    다만 나중에 접속한 브라우저에게 '지금 상태'를 즉시 그려주려면 마지막 값이
    필요하므로, 접두어 세 가지(상태 응답 / 수량 방송 / 보고)만 구분해 캐시한다.
    파싱은 브라우저가 한다 — 규격의 소유자를 늘리지 않는다."""

    def __init__(self):
        self.chat = ChatClient()
        self.lock = threading.RLock()
        self.clients = set()
        self.state = {}          # pc -> dict
        self.rooms_known = set()
        self.files = {}          # fid -> 조립 중인 이미지
        self.txq = queue.Queue()  # 상류로 나갈 프레임 — 스로틀 통과 대기
        self.last_query = {}     # pc -> 마지막 S 질의 시각(중복 억제)
        self.connected = False
        # --- 채팅 ---
        self.chat_rooms = {}     # room -> {joined, kind, owner, pw, log, joining}
        self.room_list = []      # 마지막 방 목록(새 브라우저에게 바로 그려주기 위함)
        self.rooms_at = 0.0      # 마지막 목록 재조회 시각(room_new 연타 방지)
        self.uploads = {}        # fid -> 브라우저가 올리는 중인 이미지
        for pc in CONFIG["pcs"]:
            self._ensure_state(pc)

    # ---------- 상태 ----------
    def _ensure_state(self, pc):
        return self.state.setdefault(pc, {
            "joined": False, "online": None, "reason": "", "join_at": 0.0,
            "status": None, "tank": None, "reports": deque(maxlen=LOG_BACKLOG),
        })

    def snapshot(self, pc):
        st = self._ensure_state(pc)
        return {"t": "snap", "pc": pc, "joined": st["joined"], "online": st["online"],
                "reason": st["reason"], "status": st["status"], "tank": st["tank"],
                "reports": list(st["reports"])}

    # ---------- 브라우저 팬아웃 ----------
    def broadcast(self, obj, pc=None):
        """pc를 주면 그 PC를 보고 있는 브라우저에게만 보낸다."""
        with self.lock:
            targets = [c for c in self.clients
                       if pc is None or c.pc == pc]
        for c in targets:
            c.send(obj)

    def add_client(self, conn):
        with self.lock:
            self.clients.add(conn)
        log(f"[웹] 접속 {conn.who()} (총 {len(self.clients)}명)")

    def drop_client(self, conn):
        with self.lock:
            self.clients.discard(conn)
            n = len(self.clients)
        log(f"[웹] 해제 {conn.who()} (총 {n}명)")
        self._maybe_leave(conn.pc)      # 마지막 사람이 나가면 그 방도 뜬다
        for room in list(conn.rooms):   # 채팅방도 같은 규칙으로 정리한다
            conn.rooms.discard(room)
            self._maybe_leave_room(room)
        for fid in list(conn.uploads):
            self._img_drop(fid, "연결이 끊겼습니다.")

    # ---------- 브라우저 → 상류 ----------
    # 큐에 담기는 것은 (종류, 내용, 로그문구)이다. 종류가 "msg"인 것만 스로틀을
    # 거친다 — domiserver의 MSG_BURST는 `msg` 프레임만 센다. 입장·퇴장·이미지
    # 프레임까지 같은 큐로 보내는 이유는 **순서**다(file_begin → 청크 → file_end).
    def queue_cmd(self, pc, body):
        self.txq.put(("msg", {"t": "msg", "room": room_of(pc), "body": f"{pc},{body}"},
                      f"{pc},{body}"))

    def queue_chat(self, room, body, cid):
        obj = {"t": "msg", "room": room, "body": body}
        if cid:
            obj["cid"] = cid
        self.txq.put(("msg", obj, f"[{room}] 대화 {len(body)}자"))

    def queue_up(self, obj):
        self.txq.put(("raw", obj, None))

    def queue_chunk(self, fid, seq, data):
        self.txq.put(("chunk", (fid, seq, data), None))

    def drop_chunks(self):
        """상류가 끊기면 아직 안 나간 이미지 청크를 버린다(ChatClient.drop_queued와
        같은 이유 — 이어 보낼 수 없고, 재접속마다 다시 나가면 고리가 된다)."""
        with self.txq.mutex:
            keep = [it for it in self.txq.queue if it[0] != "chunk"]
            self.txq.queue.clear()
            self.txq.queue.extend(keep)

    def _sender_loop(self):
        """발신 스로틀. domiserver는 한 연결에 10초 SEND_BURST건까지만 받아주고,
        여기서는 브라우저 여러 대의 명령이 한 연결로 합쳐진다."""
        times = deque()
        while True:
            kind, payload, note = self.txq.get()
            if kind == "msg":
                while True:
                    now = time.time()
                    while times and now - times[0] > SEND_WINDOW:
                        times.popleft()
                    if len(times) < SEND_BURST:
                        break
                    time.sleep(min(1.0, SEND_WINDOW - (now - times[0]) + 0.05))
            if not self.chat.logged_in.is_set():
                self.broadcast({"t": "err", "msg": "서버에 연결되어 있지 않습니다."})
                continue
            if kind == "chunk":
                fid, seq, data = payload
                self.chat.send_chunk(fid, seq, data)
                continue
            self.chat.send(payload)
            if kind == "msg":
                times.append(time.time())
                log(f"[발신] {note}")

    def on_web(self, conn, d):
        t = d.get("t")
        if t == "hello":
            conn.send({"t": "ready", "my_id": CONFIG["id"], "pcs": list(CONFIG["pcs"]),
                       "connected": self.chat.logged_in.is_set(),
                       "chat": bool(CONFIG["chat"]), "version": APP_VERSION})
            return
        if t == "select":
            pc = (d.get("pc") or "").strip()
            if pc and pc not in CONFIG["pcs"]:
                return conn.send({"t": "err", "msg": "목록에 없는 PC입니다."})
            old_pc, conn.pc = conn.pc, pc
            if old_pc and old_pc != pc:
                self._maybe_leave(old_pc)
            if not pc:
                return
            conn.send(self.snapshot(pc))
            if not self._ensure_state(pc)["joined"]:
                self._ensure_join(pc)
            elif time.time() - self.last_query.get(pc, 0) > 5.0:
                # 이미 들어가 있는 방이면 상태만 한 번 맞춘다(5초 내 중복은 생략).
                self.last_query[pc] = time.time()
                self.queue_cmd(pc, "S")
            return
        if t == "cmd":
            pc = (d.get("pc") or "").strip()
            body = (d.get("body") or "").strip()
            if pc not in CONFIG["pcs"]:
                return conn.send({"t": "err", "msg": "목록에 없는 PC입니다."})
            if not CMD_RE.fullmatch(body):
                return conn.send({"t": "err", "msg": f"규격 밖 명령입니다: {body}"})
            if not self._ensure_state(pc)["joined"]:
                # 방에 못 들어간 상태로 보내면 서버가 not_joined로 되돌려준다.
                self._ensure_join(pc)
                return conn.send({"t": "err", "msg": f"'{pc}'의 방에 아직 들어가지 못했습니다."})
            if body == "I":
                conn.shot_wait = time.time()
            self.queue_cmd(pc, body)
            return
        if t in ("add_pc", "del_pc"):
            return self._edit_pcs(conn, t, (d.get("pc") or "").strip())
        if t == "pong":
            return
        if t in ("rooms", "room_open", "room_close", "chat",
                 "img_begin", "img_chunk", "img_end"):
            return self._on_web_chat(conn, t, d)

    # ---------- 브라우저 → 채팅 ----------
    def _on_web_chat(self, conn, t, d):
        if not CONFIG["chat"]:
            return conn.send({"t": "err", "msg": "이 중계는 채팅을 중계하지 않습니다."})
        if t == "rooms":
            conn.send({"t": "rooms", "list": self.room_list})
            return self.refresh_rooms()
        if t == "room_open":
            return self._chat_open(conn, (d.get("room") or "").strip(), d.get("pw"))
        if t == "room_close":
            return self._chat_close(conn, (d.get("room") or "").strip())
        if t == "chat":
            return self._chat_say(conn, d)
        if t == "img_begin":
            return self._img_begin(conn, d)
        if t == "img_chunk":
            return self._img_chunk(conn, d)
        if t == "img_end":
            return self._img_end(conn, d)

    def _chat_state(self, room):
        return self.chat_rooms.setdefault(room, {
            "joined": False, "joining": False, "join_at": 0.0, "kind": "",
            "owner": None, "pw": None, "log": deque(maxlen=CHAT_BACKLOG)})

    def _chat_viewers(self, room):
        with self.lock:
            return [c for c in self.clients if room in c.rooms]

    def _to_room(self, room, obj):
        for c in self._chat_viewers(room):
            c.send(obj)

    @staticmethod
    def _room_err(conn, room, msg):
        conn.send({"t": "chat_err", "room": room, "msg": msg})

    def refresh_rooms(self, force=False):
        """상류에 방 목록을 다시 청한다. `room_new`가 연달아 와도 서버를 때리지
        않도록 최소 간격을 둔다(응답이 오면 모든 브라우저에게 뿌려진다)."""
        now = time.time()
        if not force and now - self.rooms_at < ROOMS_REFRESH_SEC:
            return
        self.rooms_at = now
        if self.chat.logged_in.is_set():
            self.chat.send({"t": "rooms"})

    def _chat_open(self, conn, room, pw):
        """방 하나를 연다. 상류 연결은 하나뿐이라 **이미 들어가 있는 방이면 바로
        열어주고**(그동안 받아 둔 대화까지 함께), 아니면 입장을 청한다."""
        if not room or len(room) > 30:
            return self._room_err(conn, room, "방 이름이 규격에 맞지 않습니다.")
        if room.startswith(ROOM_PREFIX):
            # 제어용 방은 채팅으로 열지 않는다 — 수량 방송이 대화창을 뒤덮고,
            # 아무 문자열이나 흘리면 명령 화이트리스트를 우회하는 통로가 된다.
            return self._room_err(conn, room, "제어용 방은 채팅으로 열 수 없습니다.")
        if not self.chat.logged_in.is_set():
            return self._room_err(conn, room, "서버에 연결되어 있지 않습니다.")
        st = self._chat_state(room)
        conn.rooms.add(room)
        if st["joined"]:
            conn.send({"t": "room", "room": room, "state": "joined",
                       "kind": st["kind"], "owner": st["owner"]})
            if st["log"]:
                conn.send({"t": "chat_hist", "room": room, "items": list(st["log"])})
            return
        if isinstance(pw, str) and pw:
            # 방 비밀번호는 **메모리에만** 둔다(디스크에 남기지 않는다 —
            # domichat.md '비밀번호를 기억하는 범위'). 마지막 사람이 방을 닫으면
            # 방 상태와 함께 사라진다. 상류가 끊겼다 붙을 때 재입장에 쓴다.
            st["pw"] = pw[:64]
        if st["joining"] and time.time() - st["join_at"] < 5.0:
            return                      # 이미 청해 둔 상태 — joined/denied를 같이 받는다
        # 5초가 지나도 답이 없으면 다시 청한다 — 상류가 그 사이에 끊겨 요청이
        # 버려졌을 수 있고, 그때 `joining`을 영영 세워 두면 그 방에 못 들어간다.
        st["joining"], st["join_at"] = True, time.time()
        obj = {"t": "join", "room": room}
        if st["pw"]:
            obj["pw"] = st["pw"]
        self.queue_up(obj)

    def _chat_close(self, conn, room, tell=True):
        if room not in conn.rooms:
            return
        conn.rooms.discard(room)
        if tell:
            conn.send({"t": "room", "room": room, "state": "closed"})
        self._maybe_leave_room(room)

    def _maybe_leave_room(self, room):
        """보는 사람이 아무도 없으면 그 방에서 나온다(제어 방의 `_maybe_leave`와
        같은 규칙). 웹에는 **구독이 없으므로** 나간 동안의 대화는 공백으로 남는다 —
        서버가 대화를 저장하지 않기 때문이다."""
        st = self.chat_rooms.get(room)
        if st is None or self._chat_viewers(room):
            return
        if st["joined"] or st["joining"]:
            self.queue_up({"t": "leave", "room": room})
            log(f"[채팅] '{room}' 방에서 나왔습니다(보는 사람 없음).")
        self.chat_rooms.pop(room, None)

    def _chat_allow(self, conn):
        """브라우저 **한 대**의 발신 상한. 상류 스로틀(SEND_BURST)은 연결 전체를
        보호하는 것이라, 한 대가 그 몫을 다 먹어 제어 명령이 밀리는 것을 막는다."""
        now = time.time()
        while conn.chat_times and now - conn.chat_times[0] > CHAT_WINDOW:
            conn.chat_times.popleft()
        if len(conn.chat_times) >= CHAT_BURST:
            return False
        conn.chat_times.append(now)
        return True

    def _chat_say(self, conn, d):
        room = (d.get("room") or "").strip()
        body = d.get("body")
        st = self.chat_rooms.get(room)
        if room not in conn.rooms or st is None or not st["joined"]:
            return self._room_err(conn, room, "입장한 방이 아닙니다.")
        if not isinstance(body, str) or not body.strip():
            return
        if len(body) > CHAT_MSG_MAX:
            return self._room_err(conn, room, f"메시지는 {CHAT_MSG_MAX}자까지 보낼 수 있습니다.")
        if not self._chat_allow(conn):
            return self._room_err(conn, room, "너무 빠르게 보내고 있습니다.")
        cid = d.get("cid")
        self.queue_chat(room, body, cid if isinstance(cid, str) else None)

    # ---------- 브라우저가 올리는 이미지 ----------
    def _img_begin(self, conn, d):
        room = (d.get("room") or "").strip()
        fid = d.get("fid")
        name = (d.get("name") or "image.png")[:100]
        size = d.get("size")
        st = self.chat_rooms.get(room)
        if room not in conn.rooms or st is None or not st["joined"]:
            return self._room_err(conn, room, "입장한 방이 아닙니다.")
        if not isinstance(fid, str) or not FID_RE.fullmatch(fid) or fid in self.uploads:
            return self._room_err(conn, room, "이미지 식별자가 규격에 맞지 않습니다.")
        if not isinstance(size, int) or not (0 < size <= CHAT_IMG_MAX):
            return self._room_err(
                conn, room, f"이미지는 {CHAT_IMG_MAX // 1048576}MB까지 보낼 수 있습니다.")
        if self.chat.server_ver < 2:
            # 옛 서버는 'B' 프레임을 받는 즉시 연결을 끊는다(domichat.md의 사고).
            return self._room_err(conn, room, "이 서버는 이미지 전송을 지원하지 않습니다.")
        if len(conn.uploads) >= CHAT_UPLOAD_MAX:
            return self._room_err(conn, room, "이미지를 너무 많이 동시에 올리고 있습니다.")
        self.uploads[fid] = {"conn": conn, "room": room, "size": size, "name": name,
                             "sha256": d.get("sha256"), "buf": bytearray(),
                             "t0": time.time()}
        conn.uploads.add(fid)
        self.queue_up({"t": "file_begin", "room": room, "fid": fid, "name": name,
                       "size": size, "sha256": d.get("sha256"),
                       "w": d.get("w"), "h": d.get("h")})

    def _img_chunk(self, conn, d):
        u = self.uploads.get(d.get("fid"))
        if u is None or u["conn"] is not conn:
            return
        try:
            data = base64.b64decode(d.get("b64") or "", validate=True)
        except Exception:
            return self._img_drop(d.get("fid"), "청크를 해석할 수 없습니다.")
        u["buf"] += data
        if len(u["buf"]) > u["size"]:
            return self._img_drop(d.get("fid"), "선언한 크기보다 많이 보냈습니다.")
        seq = d.get("seq")
        self.queue_chunk(d["fid"], seq if isinstance(seq, int) else 0, data)

    def _img_end(self, conn, d):
        fid = d.get("fid")
        u = self.uploads.get(fid)
        if u is None or u["conn"] is not conn:
            return
        self.uploads.pop(fid, None)
        conn.uploads.discard(fid)
        png = bytes(u["buf"])
        if len(png) != u["size"]:
            return self._room_err(conn, u["room"], "이미지를 다 받지 못했습니다.")
        self.queue_up({"t": "file_end", "room": u["room"], "fid": fid})
        # **서버는 보낸 연결을 팬아웃에서 제외한다**(exclude=conn). 즉 이 이미지는
        # 상류에서 되돌아오지 않으므로, 우리 브라우저들에게는 여기서 직접 준다 —
        # 올린 본인은 물론 **같은 방을 보고 있는 다른 브라우저**도 봐야 한다.
        self._chat_image_out(u["room"], CONFIG["id"], fid, u["name"], png)
        log(f"[채팅] '{u['room']}' 이미지 발신 {len(png) // 1024}KB ({conn.who()})")

    def _img_drop(self, fid, msg):
        u = self.uploads.pop(fid, None)
        if u is None:
            return
        u["conn"].uploads.discard(fid)
        self.queue_up({"t": "file_abort", "room": u["room"], "fid": fid})
        self._room_err(u["conn"], u["room"], msg)

    def _chat_image_out(self, room, frm, fid, name, png):
        self._to_room(room, {"t": "chat_img", "room": room, "from": frm, "fid": fid,
                             "name": name, "ts": round(time.time(), 3),
                             "b64": base64.b64encode(png).decode("ascii")})

    def _edit_pcs(self, conn, t, pc):
        if not ID_RE.fullmatch(pc):
            return conn.send({"t": "err", "msg": "PC 이름 형식이 아닙니다."})
        pcs = list(CONFIG["pcs"])
        if t == "add_pc":
            if pc in pcs:
                return conn.send({"t": "err", "msg": "이미 목록에 있습니다."})
            pcs.append(pc)
            CONFIG["pcs"] = pcs
            save_config()
            self._ensure_state(pc)
            log(f"[목록] '{pc}' 추가 ({conn.who()})")
        else:
            if pc not in pcs:
                return conn.send({"t": "err", "msg": "목록에 없습니다."})
            pcs.remove(pc)
            CONFIG["pcs"] = pcs
            save_config()
            with self.lock:
                for c in self.clients:
                    if c.pc == pc:
                        c.pc = ""
            st = self.state.pop(pc, None)
            if st and st["joined"]:
                self.chat.send({"t": "sub", "room": room_of(pc), "on": False})
                self.chat.send({"t": "leave", "room": room_of(pc)})
            log(f"[목록] '{pc}' 삭제 ({conn.who()})")
        self.broadcast({"t": "pcs", "pcs": list(CONFIG["pcs"])})

    # ---------- 상류 수신 ----------
    def _watchers(self, pc):
        with self.lock:
            return [c for c in self.clients if c.pc == pc]

    def _watched_pcs(self):
        with self.lock:
            return {c.pc for c in self.clients if c.pc}

    def _notify_pc(self, pc):
        st = self._ensure_state(pc)
        self.broadcast({"t": "pc", "pc": pc, "joined": st["joined"],
                        "online": st["online"], "reason": st["reason"]})

    def _ensure_join(self, pc):
        """**지목된 PC의 방에만** 들어간다.

        예전에는 시작할 때 목록의 방을 전부 잡아 두었다. 그러면 보지도 않는 PC의
        수량 방송(사이클마다)을 계속 받고, 그 방 참가자 목록에도 `web`이 늘 떠
        있게 된다. 동시에 여러 PC를 돌릴 일이 없으므로 필요할 때만 붙는다."""
        st = self._ensure_state(pc)
        if st["joined"] or not self.chat.logged_in.is_set():
            return
        # 방금 보낸 join의 답을 기다리는 중이면 또 보내지 않는다 — 브라우저가 PC를
        # 고른 직후에 방 목록 응답이 오면 `_rejoin_watched`가 같은 방에 한 번 더
        # 들어가 **`S` 질의가 두 번** 나갔다(실측). 5초가 지나면 다시 시도한다
        # (답을 못 받은 경우까지 영영 막으면 그 PC를 영영 못 본다).
        if time.time() - st["join_at"] < 5.0:
            return
        room = room_of(pc)
        if room in self.rooms_known:
            st["join_at"] = time.time()
            self.chat.send({"t": "join", "room": room, "pw": ROOM_PW})
            return
        # 그 PC가 domichat에 접속한 적이 없어 방이 아직 없다. 방금 켰을 수도 있으니
        # 목록을 새로 받아 본다(응답이 오면 rooms 분기가 다시 이 함수를 부른다).
        if st["reason"] != "no_room":
            st["reason"] = "no_room"
            self._notify_pc(pc)
        self.chat.send({"t": "rooms"})

    def _maybe_leave(self, pc):
        """보는 사람이 아무도 없으면 그 방에서 나온다. 캐시도 버린다 — 다시 들어갈
        때 옛 상태를 잠깐 보여주면 '지금 값'으로 오해하게 된다(입장 직후 S 질의로
        새로 받는다)."""
        st = self.state.get(pc) if pc else None
        if st is None or not st["joined"] or self._watchers(pc):
            return
        self.chat.send({"t": "sub", "room": room_of(pc), "on": False})
        self.chat.send({"t": "leave", "room": room_of(pc)})
        st.update({"joined": False, "online": None, "reason": "", "join_at": 0.0,
                   "status": None, "tank": None})
        st["reports"].clear()
        self.last_query.pop(pc, None)
        log(f"[상류] '{pc}' 방에서 나왔습니다(보는 사람 없음).")

    def _rejoin_watched(self):
        """재접속·목록 갱신 뒤, 지금 누군가 보고 있는 PC의 방에만 다시 들어간다."""
        for pc in self._watched_pcs():
            self._ensure_join(pc)

    def _pump_loop(self):
        while True:
            try:
                d = self.chat.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_up(d)
            except Exception as e:
                log(f"[경고] 상류 프레임 처리 실패: {e}")

    def _handle_up(self, d):
        ev = d.get("_ev")
        if ev:
            if ev == "cert_pinned":
                CONFIG["server_fp"][CONFIG["server"]] = d.get("fp")
                save_config()
                log(f"[상류] 인증서 지문을 고정했습니다: {str(d.get('fp'))[:16]}…")
            elif ev == "disconnected":
                self.connected = False
                for st in self.state.values():
                    st["joined"], st["join_at"] = False, 0.0
                for room, cst in self.chat_rooms.items():
                    cst["joined"] = cst["joining"] = False
                    cst["join_at"] = 0.0
                    self._to_room(room, {"t": "room", "room": room, "state": "denied",
                                         "reason": "offline",
                                         "msg": "서버 연결이 끊겼습니다."})
                self.drop_chunks()
                for fid in list(self.uploads):
                    self._img_drop(fid, "서버 연결이 끊겨 이미지를 보내지 못했습니다.")
                log(f"[상류] {d.get('msg')}")
                self.broadcast({"t": "up", "connected": False, "msg": d.get("msg")})
            elif ev == "note":
                log(f"[상류] {d.get('msg')}")
            return

        t = d.get("t")
        if t == "welcome":
            self.connected = True
            self.rooms_known = {r.get("name") for r in (d.get("rooms") or [])}
            log(f"[상류] '{d.get('id')}'로 로그인했습니다. (방 {len(self.rooms_known)}개)")
            self.broadcast({"t": "up", "connected": True, "msg": "연결됨"})
            self._set_room_list(d.get("rooms") or [])
            self._rejoin_watched()
            self._rejoin_chat()
            return
        if t == "rooms":
            self.rooms_known = {r.get("name") for r in (d.get("list") or [])}
            self._set_room_list(d.get("list") or [])
            self._rejoin_watched()
            return
        if t == "room_new":
            self.rooms_known.add(d.get("room"))
            return self.refresh_rooms()
        if t == "joined":
            pc = self._pc_of_room(d.get("room"))
            if pc is None:
                return self._on_chat_joined(d)
            if pc:
                st = self._ensure_state(pc)
                st["joined"], st["reason"], st["join_at"] = True, "", 0.0
                self.chat.send({"t": "sub", "room": d.get("room"), "on": True})
                log(f"[상류] '{pc}' 방에 입장했습니다.")
                self.broadcast({"t": "pc", "pc": pc, "joined": True,
                                "online": st["online"], "reason": ""})
                self.queue_cmd(pc, "S")
                self.last_query[pc] = time.time()
            return
        if t == "denied":
            pc = self._pc_of_room(d.get("room"))
            if pc is None:
                return self._on_chat_denied(d)
            if pc:
                st = self._ensure_state(pc)
                st["joined"], st["join_at"] = False, 0.0
                st["reason"] = d.get("reason") or "denied"
                log(f"[상류] '{pc}' 방 입장 거절: {d.get('reason')} {d.get('msg') or ''}")
                self.broadcast({"t": "pc", "pc": pc, "joined": False,
                                "online": None, "reason": st["reason"]})
            return
        if t == "member":
            pc = self._pc_of_room(d.get("room"))
            if pc is None:
                room = d.get("room")
                if room in self.chat_rooms:
                    self._to_room(room, {"t": "room", "room": room, "state": "member",
                                         "id": d.get("id"), "in": bool(d.get("in"))})
                return
            if pc and d.get("id") == pc:
                st = self._ensure_state(pc)
                st["online"] = bool(d.get("in"))
                log(f"[상류] '{pc}' {'접속' if st['online'] else '접속 종료'}")
                self.broadcast({"t": "pc", "pc": pc, "joined": st["joined"],
                                "online": st["online"], "reason": st["reason"]})
            return
        if t == "room_deleted":
            pc = self._pc_of_room(d.get("room"))
            if pc is None:
                room = d.get("room")
                self.rooms_known.discard(room)
                self.chat_rooms.pop(room, None)
                self._to_room(room, {"t": "room", "room": room, "state": "deleted",
                                     "msg": "채팅방이 삭제되었습니다."})
                self._forget_room(room)
                return self.refresh_rooms(force=True)
            if pc:
                st = self._ensure_state(pc)
                st["joined"], st["online"] = False, None
                st["reason"] = "room_deleted"
                self.rooms_known.discard(d.get("room"))
                self.broadcast({"t": "pc", "pc": pc, "joined": False, "online": None,
                                "reason": "room_deleted"})
            return
        if t == "kicked":
            room = d.get("room")
            self.chat_rooms.pop(room, None)
            self._to_room(room, {"t": "room", "room": room, "state": "kicked",
                                 "msg": d.get("msg") or "강제 퇴장되었습니다."})
            self._forget_room(room)
            return
        if t == "approve_res":
            # 승인 결과는 **요청한 브라우저가 누구였는지 모른다**(요청은 domiweb의
            # 계정으로 나갔다). 전원에게 알리고, 승인됐으면 그 방을 보고 있던
            # 브라우저가 바로 다시 열 수 있게 목록도 새로 받는다.
            self.broadcast({"t": "room", "room": d.get("room"), "state": "approve_res",
                            "ok": bool(d.get("ok")), "msg": d.get("msg") or ""})
            return self.refresh_rooms(force=True)
        if t == "msg":
            return self._on_room_msg(d)
        if t in ("file_begin", "bin", "file_end", "file_abort"):
            return self._on_file(t, d)
        if t == "error":
            code = d.get("code")
            log(f"[상류 오류] {code}: {d.get('msg')}")
            if code in ("too_long", "rate_limited", "file_too_big", "file_busy",
                        "not_joined", "bad_frame"):
                # 채팅 발신이 서버에서 막힌 것 — 조용히 삼키면 브라우저는 보낸 줄
                # 안다(말풍선이 '전송 중…'에 영원히 머문다).
                self.broadcast({"t": "err", "msg": d.get("msg") or code})
            if code == "room_missing":
                self.chat.send({"t": "rooms"})
            elif code in ("bad_login", "already_online", "disabled"):
                # 계정 문제는 사람이 손봐야 하지만, 데몬이므로 포기하지 않고
                # 재접속을 계속 시도한다(상대편이 로그아웃하면 저절로 풀린다).
                self.broadcast({"t": "up", "connected": False, "msg": d.get("msg")})
            return

    @staticmethod
    def _pc_of_room(room):
        if isinstance(room, str) and room.startswith(ROOM_PREFIX):
            pc = room[len(ROOM_PREFIX):]
            return pc or None
        return None

    # ---------- 채팅방 상태 ----------
    def _set_room_list(self, rows):
        """방 목록을 갈무리해 브라우저에 뿌린다. **제어용 방은 빼고 준다** —
        채팅 화면에 뜰 이유가 없고, 열려고 해도 `_chat_open`이 막는다."""
        self.room_list = [r for r in rows
                          if isinstance(r, dict)
                          and not str(r.get("name") or "").startswith(ROOM_PREFIX)]
        if CONFIG["chat"]:
            self.broadcast({"t": "rooms", "list": self.room_list})

    def _rejoin_chat(self):
        """상류가 다시 붙었을 때, 지금 누군가 열어 둔 방에만 다시 들어간다.
        비밀번호 방은 **메모리에 남은 비번**으로 자동 재입장한다(없으면 거절이
        오고 브라우저가 다시 묻는다)."""
        for room, st in list(self.chat_rooms.items()):
            if not self._chat_viewers(room):
                self.chat_rooms.pop(room, None)
                continue
            st["joined"], st["joining"], st["join_at"] = False, True, time.time()
            obj = {"t": "join", "room": room}
            if st["pw"]:
                obj["pw"] = st["pw"]
            self.queue_up(obj)

    def _forget_room(self, room):
        """쫓겨났거나 방이 사라졌을 때 — 브라우저의 '열어 둔 방' 표시도 지운다.
        안 지우면 재접속 때 없는 방에 계속 다시 들어가려 한다."""
        with self.lock:
            for c in self.clients:
                c.rooms.discard(room)

    def _on_chat_joined(self, d):
        room = d.get("room")
        viewers = self._chat_viewers(room)
        if not viewers:
            # 입장을 청해 놓고 그 사이에 브라우저가 다 떠났다 — 바로 나온다.
            self.chat_rooms.pop(room, None)
            self.queue_up({"t": "leave", "room": room})
            return
        st = self._chat_state(room)
        st.update({"joined": True, "joining": False, "join_at": 0.0,
                   "kind": d.get("kind") or "", "owner": d.get("owner")})
        log(f"[채팅] '{room}' 방에 입장했습니다. ({len(viewers)}명이 보는 중)")
        for c in viewers:
            c.send({"t": "room", "room": room, "state": "joined",
                    "kind": st["kind"], "owner": st["owner"]})
            if st["log"]:
                c.send({"t": "chat_hist", "room": room, "items": list(st["log"])})

    def _on_chat_denied(self, d):
        room, reason = d.get("room"), d.get("reason") or "denied"
        st = self.chat_rooms.get(room)
        if st is not None:
            st["joining"] = False
            if reason == "bad_pw_room":
                st["pw"] = None          # 틀린 비번은 들고 있지 않는다
        viewers = self._chat_viewers(room)
        log(f"[채팅] '{room}' 입장 거절: {reason}")
        for c in viewers:
            c.rooms.discard(room)        # 못 들어갔으니 '열어 둔 방'이 아니다
            c.send({"t": "room", "room": room, "state": "denied",
                    "reason": reason, "msg": d.get("msg") or ""})
        self.chat_rooms.pop(room, None)

    def _on_chat_msg(self, d):
        room = d.get("room")
        st = self.chat_rooms.get(room)
        if st is None:
            return
        out = {"t": "chat", "room": room, "from": d.get("from"),
               "body": d.get("body") or "", "mid": d.get("mid"),
               "ts": d.get("ts") or round(time.time(), 3)}
        st["log"].append(out)
        if d.get("cid"):
            # cid는 보낸 브라우저가 '전송됨(✓)'을 켜는 열쇠다. 어느 브라우저가
            # 보냈는지는 모르지만 cid는 그쪽이 만든 난수라 남의 것과 겹치지 않는다.
            out = dict(out, cid=d.get("cid"))
        self._to_room(room, out)

    def _on_room_msg(self, d):
        frm, body = d.get("from"), (d.get("body") or "").strip()
        pc = self._pc_of_room(d.get("room"))
        if pc is None:
            return self._on_chat_msg(d)
        if not pc or frm != pc:
            return              # 그 방의 주인(피제어 PC)이 보낸 것만 의미가 있다
        st = self._ensure_state(pc)
        if st["online"] is not True:
            st["online"] = True
            self.broadcast({"t": "pc", "pc": pc, "joined": st["joined"],
                            "online": True, "reason": st["reason"]})

        # 캐시용 최소 분류(접두어 셋만 본다 — 파싱은 브라우저 몫)
        parts = [p.strip() for p in body.split(",")]
        if len(parts) >= 3 and parts[1] == "Z":
            if parts[0] == CONFIG["id"] and parts[2] not in (
                    "N", "F", "G", "P", "W", "Q", "Y", "I"):
                st["status"] = body
            elif parts[0] == "" and parts[2] == "N":
                st["tank"] = body
            elif parts[0] == "" and parts[2] == "F":
                st["reports"].append([round(time.time(), 3), body])
                log(f"[보고] {pc}: {body}")
        self.broadcast({"t": "msg", "pc": pc, "body": body}, pc=pc)

    # ---------- 이미지 조립 (스크린샷 · 채팅) ----------
    # 이진 청크를 조립하는 곳은 여기 하나뿐이다 — 브라우저에 같은 로직을 또 두지
    # 않는다. 조립이 끝나면 **완성된 PNG**를 base64로 넘긴다. 스크린샷이냐 채팅
    # 이미지냐는 `file_begin`의 방 이름으로 갈리며, 그 판정을 `mode`에 적어 둔다
    # (뒤따르는 청크·file_end 프레임에는 방 이름이 없다).
    def _on_file(self, t, d):
        fid = d.get("fid")
        if t == "file_begin":
            return self._file_begin(d)
        f = self.files.get(fid)
        if f is None:
            return
        if t == "bin":
            f["buf"] += d.get("data", b"")
            if len(f["buf"]) > f["size"]:
                self.files.pop(fid, None)
                self._file_fail(f, "too_big")
            return
        if t == "file_abort":
            self.files.pop(fid, None)
            return self._file_fail(f, "aborted")

        # file_end — 크기·해시를 확인한 뒤 완성된 PNG를 넘긴다.
        self.files.pop(fid, None)
        png = bytes(f["buf"])
        ok = len(png) == f["size"]
        if ok and f["sha256"]:
            ok = hashlib.sha256(png).hexdigest() == f["sha256"]
        if not ok:
            return self._file_fail(f, "corrupt")
        if f["mode"] == "chat":
            self._chat_image_out(f["room"], f["from"], fid, f["name"], png)
            log(f"[채팅] '{f['room']}' 이미지 수신 {len(png)//1024}KB ({f['from']})")
            return
        b64 = base64.b64encode(png).decode("ascii")
        sent = self._to_waiters(f["pc"], {"t": "shot", "pc": f["pc"], "ok": True,
                                          "name": f["name"], "b64": b64})
        log(f"[사진] {f['pc']}: {len(png)/1048576:.2f}MB 전달 ({sent}명)")

    def _file_begin(self, d):
        room = d.get("room")
        name = d.get("name") or "image.png"
        size = int(d.get("size") or 0)
        pc = self._pc_of_room(room)
        if pc is None:                       # 채팅방 이미지
            st = self.chat_rooms.get(room)
            if st is None or not CONFIG["chat"]:
                return
            if size <= 0 or size > CHAT_IMG_MAX:
                return log(f"[채팅] '{room}' 이미지 크기가 규격 밖({size})이라 버립니다.")
            self.files[d.get("fid")] = {
                "mode": "chat", "room": room, "from": d.get("from"), "pc": None,
                "size": size, "sha256": d.get("sha256"), "name": name,
                "buf": bytearray(), "t0": time.time()}
            return
        if not pc or d.get("from") != pc:
            return
        if size <= 0 or size > SHOT_MAX_BYTES:
            return log(f"[사진] {pc}: 크기가 규격 밖({size})이라 버립니다.")
        self.files[d.get("fid")] = {"mode": "shot", "room": room, "from": pc, "pc": pc,
                                    "size": size, "sha256": d.get("sha256"),
                                    "name": name or "screenshot.png",
                                    "buf": bytearray(), "t0": time.time()}
        log(f"[사진] {pc}: 수신 시작 ({size/1048576:.2f}MB)")

    def _file_fail(self, f, reason):
        if f["mode"] == "chat":
            log(f"[채팅] '{f['room']}' 이미지 실패({reason})")
            self._to_room(f["room"], {"t": "chat_err", "room": f["room"],
                                      "msg": f"이미지를 받지 못했습니다. ({reason})"})
            return
        self._shot_fail(f["pc"], reason)

    def _shot_fail(self, pc, reason):
        log(f"[사진] {pc}: 실패({reason})")
        self._to_waiters(pc, {"t": "shot", "pc": pc, "ok": False, "reason": reason})

    def _to_waiters(self, pc, obj):
        """사진은 **요청한 브라우저에게만** 준다 — 남이 찍은 사진이 갑자기 뜨면
        안 되고, 3MB짜리를 안 기다리는 브라우저에 밀어넣을 이유도 없다."""
        now = time.time()
        with self.lock:
            waiters = [c for c in self.clients
                       if c.pc == pc and 0 < now - c.shot_wait <= SHOT_WAIT_SEC]
        for c in waiters:
            c.shot_wait = 0.0
            c.send(obj)
        return len(waiters)

    # ---------- 유지보수 ----------
    def _maint_loop(self):
        last_retry = 0.0
        while True:
            time.sleep(1.0)
            now = time.time()
            for fid, f in list(self.files.items()):
                if now - f["t0"] > 120:
                    self.files.pop(fid, None)
                    self._file_fail(f, "timeout")
            for fid, u in list(self.uploads.items()):
                if now - u["t0"] > 120:
                    self._img_drop(fid, "이미지 전송이 너무 오래 걸립니다.")
            if now - last_retry >= ROOM_RETRY_SEC:
                last_retry = now
                # 보고 있는 PC 중 아직 못 들어간 방이 있으면 목록을 새로 받아 본다
                # (그 PC가 뒤늦게 켜지면 방이 생긴다). 아무도 안 보고 있으면 조용히 있는다.
                if self.chat.logged_in.is_set() and any(
                        not self._ensure_state(pc)["joined"] for pc in self._watched_pcs()):
                    self.chat.send({"t": "rooms"})

    def start(self):
        host, port = split_server(CONFIG["server"])
        pinned = CONFIG["server_fp"].get(CONFIG["server"])
        for fn, name in ((self._pump_loop, "hub-pump"), (self._sender_loop, "hub-tx"),
                         (self._maint_loop, "hub-maint")):
            threading.Thread(target=fn, daemon=True, name=name).start()
        self.chat.start(host, port, CONFIG["id"], CONFIG["pw"], pinned)
        log(f"[상류] {host}:{port} 에 '{CONFIG['id']}'로 접속을 시작합니다.")


# === [6. 수신 대기 · 실행] ===

HUB = None
SSL_CTX = None
_cert_lock = threading.Lock()
_cert_sig = None                # (인증서 mtime, 키 mtime) — 갱신 감지용


def build_listener_tls():
    """브라우저용 TLS 컨텍스트를 만든다. **1.2로 고정한다** — 연결마다 수신 스레드와
    송신 스레드가 한 소켓을 나눠 쓰므로, 1.3의 핸드셰이크 후 메시지가 record layer를
    깨는 문제를 domichat.md에서 이미 겪었다(같은 구조라 같은 함정이다)."""
    cert, key = CONFIG["certfile"], CONFIG["keyfile"]
    if not cert or not key:
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    except Exception as e:
        log(f"[TLS] 인증서를 읽지 못했습니다: {e}")
        return None
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    if no_reneg:
        ctx.options |= no_reneg
    log(f"[TLS] 인증서 적재: {cert}")
    return ctx


def _cert_sig_now():
    try:
        return (os.path.getmtime(CONFIG["certfile"]),
                os.path.getmtime(CONFIG["keyfile"]))
    except OSError:
        return None


def listener_tls():
    """새 연결마다 인증서 파일의 mtime을 보고 **바뀌었으면 다시 적재한다.**
    Let's Encrypt 인증서는 60~90일마다 갱신되고(Posh-ACME가 같은 경로에 덮어쓴다),
    그때 사람이 domiweb를 재시작해야 하는 구조라면 어느 날 조용히 만료된다.
    적재에 실패하면 **직전 컨텍스트를 그대로 쓴다** — 갱신 도중의 반쪽 파일을
    읽었다고 서비스를 멈출 이유가 없다."""
    global SSL_CTX, _cert_sig
    if not CONFIG["certfile"] or not CONFIG["keyfile"]:
        return None
    with _cert_lock:
        sig = _cert_sig_now()
        if SSL_CTX is None or (sig is not None and sig != _cert_sig):
            ctx = build_listener_tls()
            if ctx is not None:
                SSL_CTX, _cert_sig = ctx, sig
        return SSL_CTX


def serve_web(sock, addr):
    """브라우저 연결 하나. TLS 감싸기를 **여기서** 한다(accept 루프에서 하면
    불량 클라이언트 하나가 새 접속 수락을 막는다 — domiserver와 같은 이유)."""
    conn = None
    try:
        ctx = listener_tls()
        if ctx is not None:
            sock.settimeout(15)
            sock = ctx.wrap_socket(sock, server_side=True)
        sock.settimeout(None)
        reader = SockReader(sock)
        if not ws_handshake(reader, sock):
            # WebSocket 업그레이드가 아닌 평범한 GET — 안내 페이지를 돌려줬다.
            # 사람이 주소를 열어 인증서·도달 여부를 확인하는 경로이므로 로그를
            # 남긴다(안 남기면 "브라우저는 뜨는데 서버 창은 조용하다"가 된다).
            log(f"[웹] 안내 페이지 응답 {addr[0]}:{addr[1]}")
            return
        conn = WebConn(HUB, sock, addr)
        HUB.add_client(conn)
        conn.send({"t": "ready", "my_id": CONFIG["id"], "pcs": list(CONFIG["pcs"]),
                   "connected": HUB.chat.logged_in.is_set(),
                   "chat": bool(CONFIG["chat"]), "version": APP_VERSION})
        conn.serve(reader)
    except (WSClosed, OSError, ssl.SSLError) as e:
        if conn is not None:
            log(f"[웹] {conn.who()} 종료 — {e}")
    except Exception as e:
        log(f"[웹] 처리 중 오류: {e}")
    finally:
        if conn is not None:
            HUB.drop_client(conn)
            conn.close()
        else:
            try:
                sock.close()
            except Exception:
                pass


def accept_loop(srv):
    while True:
        try:
            sock, addr = srv.accept()
        except OSError:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=serve_web, args=(sock, addr), daemon=True,
                         name="web-conn").start()


def repl():
    """콘솔. domiserver와 같은 감각으로 상태를 볼 수 있게 최소한만 둔다."""
    if sys.stdin is None or not sys.stdin.isatty():
        # 콘솔 없이(작업 스케줄러·pythonw) 띄운 경우. **여기서 return하면 서버가
        # 그대로 종료된다** — 입력이 없을 뿐이므로 그냥 잠들어 있어야 한다.
        log("[콘솔] 입력 장치가 없어 명령 입력 없이 계속 실행합니다.")
        while True:
            time.sleep(3600)
    while True:
        try:
            line = input().strip()
        except EOFError:
            log("[콘솔] 입력이 닫혔습니다 — 명령 입력 없이 계속 실행합니다.")
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print()
            return
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("q", "quit", "exit"):
            return
        if cmd == "pcs":
            for pc in CONFIG["pcs"]:
                st = HUB.state.get(pc, {})
                on = {True: "접속중", False: "꺼짐", None: "모름"}[st.get("online")]
                print(f"  {pc:<12} 입장={'O' if st.get('joined') else 'X'}  {on}"
                      f"  {st.get('reason') or ''}")
        elif cmd == "who":
            with HUB.lock:
                for c in HUB.clients:
                    print(f"  {c.who()}  보는 PC={c.pc or '-'}"
                          f"  방={','.join(sorted(c.rooms)) or '-'}")
                print(f"  총 {len(HUB.clients)}명")
        elif cmd == "rooms":
            for room, st in HUB.chat_rooms.items():
                print(f"  {room:<24} 입장={'O' if st['joined'] else 'X'}"
                      f"  보는 중={len(HUB._chat_viewers(room))}명"
                      f"  대화 {len(st['log'])}줄")
            print(f"  서버 목록 {len(HUB.room_list)}개"
                  f"  (채팅 중계 {'켜짐' if CONFIG['chat'] else '꺼짐'})")
        elif cmd == "up":
            print(f"  상류 {'연결됨' if HUB.chat.logged_in.is_set() else '끊김'}"
                  f"  ({CONFIG['server']}, ID={CONFIG['id']})")
        elif cmd == "add" and arg:
            if arg not in CONFIG["pcs"]:
                CONFIG["pcs"].append(arg)
                save_config()
            HUB._ensure_state(arg)      # 입장은 브라우저가 그 PC를 고를 때 한다
            HUB.broadcast({"t": "pcs", "pcs": list(CONFIG["pcs"])})
            print(f"  목록: {', '.join(CONFIG['pcs'])}")
        elif cmd == "help":
            print("  pcs | who | up | rooms | add <ID> | quit")
        else:
            print("  ? help")


def main():
    global HUB, SSL_CTX
    load_config()
    log(f"domiweb {APP_VERSION} 시작")
    if listener_tls() is None:
        log("[TLS] 인증서가 없어 **평문 ws**로 엽니다 — 로컬 개발용이며 "
            "https 페이지(GitHub Pages)에서는 접속되지 않습니다.")
    HUB = Hub()
    HUB.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((CONFIG["listen_host"], CONFIG["listen_port"]))
    srv.listen(16)
    scheme = "wss" if SSL_CTX is not None else "ws"
    log(f"[수신] {scheme}://<호스트>:{CONFIG['listen_port']}/ws 로 브라우저를 받습니다.")
    log(f"[목록] 피제어 PC: {', '.join(CONFIG['pcs'])}   (콘솔: help)")
    threading.Thread(target=accept_loop, args=(srv,), daemon=True,
                     name="accept").start()
    repl()
    log("종료합니다.")
    try:
        srv.close()
    except Exception:
        pass
    HUB.chat.stop()


if __name__ == "__main__":
    main()
