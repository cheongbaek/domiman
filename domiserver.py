# -*- coding: utf-8 -*-
"""domiserver.py — domichat 중계 서버 (BGOD에서 상시 구동)

설계 문서는 domichat.md. 요약:
- TCP 47821, 연결 하나를 끝까지 유지하며 [길이4][종류1][본문] 프레임을 주고받는다.
- **대화 내용은 저장하지 않는다.** 서버는 순수 중계이며, 계정과 채팅방 정보만
  SQLite(domiserver.db)에 남긴다. 그래서 앱이 꺼져 있던 동안의 대화는 복구할
  수단이 없다(설계상 공백으로 남긴다).
- 회원가입은 **사후 승인제**: 클라이언트가 요청하면 대기 목록에 들어가고,
  이 콘솔에서 approve 해야 로그인이 된다. 수락 전에는 '존재하지 않는 ID'로
  응답한다(대기 중이라는 사실조차 알려주지 않는다 — ID 탐색을 막는다).
- 같은 ID 동시 접속 불허. 다만 비정상 종료된 연결이 남아 본인이 재로그인
  못하는 사고를 막기 위해, 같은 ID 로그인이 오면 기존 연결을 즉시 찔러보고
  응답이 없으면 회수한다.

콘솔은 계정·채팅방 관리용이며, 채팅 내용은 출력하지 않는다.
"""

import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.request
from collections import deque

# === [1. 상수 · 설정] ===

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "domiserver.json")
DB_PATH = os.path.join(BASE_DIR, "domiserver.db")

APP_VERSION = "260815d"
PROTO_VER = 1

MAX_FRAME = 1024 * 1024          # 'T' 프레임 상한(1MB). 넘으면 규격 위반으로 끊는다
FRAME_HEAD = struct.Struct(">IB")   # 길이 4바이트(빅엔디안) + 종류 1바이트

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
ROOM_KINDS = ("open", "pw", "allow", "approve")

# 방 이름: 1~30자, 개행·제어문자 금지(앞뒤 공백은 트림 후 검사)
ROOM_NAME_MAX = 30
ROOM_PW_MAX = 19                 # 방 비밀번호 19자 제한(설계 확정값)
USER_PW_MIN, USER_PW_MAX = 4, 64

PBKDF2_ROUNDS = 200_000          # 표준 라이브러리만으로 쓸 수 있는 선에서 충분한 강도

MSG_BURST, MSG_WINDOW = 20, 10.0  # 도배 방지: 10초에 20건까지

DEFAULT_CONFIG = {
    "port": 47821,
    "max_rooms": 100,
    "public_room_ttl_days": 3,   # 공개방 자동 삭제 기준. 0이면 자동 삭제 없음
    "msg_max_len": 4000,
    "ping_sec": 15,
    "pong_timeout_sec": 45,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config():
    """설정 파일 로드(없거나 깨졌으면 기본값). 모르는 키는 무시한다."""
    global CONFIG
    CONFIG = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fp:
            data = json.load(fp)
        for k, v in data.items():
            if k in DEFAULT_CONFIG and isinstance(v, type(DEFAULT_CONFIG[k])):
                CONFIG[k] = v
    except FileNotFoundError:
        save_config()
    except Exception as e:
        log(f"[경고] 설정 로드 실패, 기본값 사용: {e}")


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
            json.dump(CONFIG, fp, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[경고] 설정 저장 실패: {e}")


# === [2. 유틸 — 로그 · 검증 · 비밀번호] ===

_log_lock = threading.Lock()


def log(msg):
    """콘솔 출력. 채팅 본문은 절대 찍지 않는다(서버는 대화를 남기지 않는다)."""
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def now():
    return time.time()


def fmt_ts(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def valid_id(s):
    return bool(isinstance(s, str) and ID_RE.match(s))


def clean_room_name(s):
    """방 이름 정규화. 부적합하면 None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or len(s) > ROOM_NAME_MAX:
        return None
    if any(ord(ch) < 32 for ch in s):     # 개행·제어문자 금지
        return None
    return s


def _ip_kind(ip):
    """주소 성격 판정 — 어느 범위에서 접속할 수 있는 주소인지 안내하기 위한 것."""
    if ip.startswith("127.") or ip == "::1":
        return "loopback"
    if ip.startswith("169.254."):
        return "linklocal"                 # 주소를 못 받은 어댑터 — 안내할 값이 아니다
    if ip.startswith(("10.", "192.168.")):
        return "private"
    if ip.startswith("172."):
        try:
            return "private" if 16 <= int(ip.split(".")[1]) <= 31 else "public"
        except ValueError:
            return "public"
    return "public"


def _primary_ip():
    """기본 경로(밖으로 나가는 경로)에 쓰이는 주소. UDP 소켓의 라우팅만 보며
    실제로 패킷을 보내지는 않는다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def local_ips():
    """이 PC의 IPv4 주소들(기본 경로 주소를 맨 앞에, 링크로컬 제외)."""
    found = []
    p = _primary_ip()
    if p:
        found.append(p)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except Exception:
        pass
    return [ip for ip in found if _ip_kind(ip) != "linklocal"]


def print_addresses(port):
    """시작 시(그리고 콘솔 addr 명령으로) 접속 주소를 안내한다.
    클라이언트는 domichat 로그인 창의 'IP주소' 칸에 이 중 하나를 넣는다.

    **기본 경로 주소를 앞세우는 이유:** 이 PC가 가진 주소를 전부 나열하면 VMware·
    Hyper-V 같은 가상 어댑터 주소(192.168.x.1 등)가 섞여 나오는데, 다른 PC는 그
    주소로 접속할 수 없어 오해를 준다. 밖으로 나가는 경로에 쓰이는 주소만이
    '다른 PC가 쓸 주소'다."""
    ips = local_ips()
    primary = ips[0] if ips else None
    others = [ip for ip in ips[1:] if _ip_kind(ip) != "loopback"]
    log("접속 주소 — domichat 'IP주소' 칸에 넣을 값:")
    log(f"   같은 PC     : 127.0.0.1        (포트가 기본값이 아니면 '주소:{port}')")
    if primary:
        tag = "공인 IP — 외부에서도 이 주소" if _ip_kind(primary) == "public" \
            else "사설 IP — 같은 네트워크 안에서만"
        log(f"   다른 PC에서 : {primary}   ← 기본 경로 주소({tag})")
    if others:
        log(f"   그 밖의 주소: {', '.join(others)}"
            f"   (가상 어댑터 등 — 보통 접속에 쓰이지 않음)")
    threading.Thread(target=_report_external, args=(port,), daemon=True).start()


def _report_external(port):
    """'밖에서 본 내 주소'를 조회해, 포트포워딩이 필요한 환경인지 알려준다."""
    ip = None
    for url in ("https://api64.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                ip = r.read().decode("utf-8", "replace").strip()
                break
        except Exception:
            continue
    if not ip:
        log("   (외부에서 본 주소는 조회하지 못했습니다 — 인터넷 연결 확인)")
        return
    if ip in local_ips():
        log(f"   외부 로그인은 {ip} 로 하면 됩니다 — 공인 IP가 이 PC에 직접"
            f" 할당되어 포트포워딩이 필요 없습니다.")
    else:
        log(f"   외부에서 본 주소는 {ip} 입니다(NAT 안쪽) — 공유기에서 포트"
            f" {port} 를 이 PC로 포워딩해야 외부 로그인이 됩니다.")
    # 방화벽은 '반드시 포트를 열어야 한다'가 아니다(실측): 이 파이썬 실행 파일에
    # 대한 인바운드 허용 규칙이 이미 있으면 포트 규칙 없이도 외부에서 접속된다.
    # 그런 규칙은 프로그램 이름(python.exe)으로 만들어져 'domi'로 검색하면 안 잡힌다.
    log(f"   밖에서 접속이 안 될 때만 방화벽을 보세요 — 포트 {port} 를 열거나,"
        f" 이 파이썬 실행 파일의 인바운드 허용 규칙이 있는지 확인하면 됩니다.")


def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${h.hex()}"


def verify_pw(pw, stored):
    """저장된 해시와 비교. 형식이 깨져 있으면 실패로 본다."""
    try:
        algo, rounds, salt_hex, hash_hex = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                bytes.fromhex(salt_hex), int(rounds))
    except Exception:
        return False
    return hmac.compare_digest(h.hex(), hash_hex)


# === [3. DB — 계정 · 채팅방] ===
# 대화는 저장하지 않는다. rooms.last_msg는 '공개방 자동 삭제' 판정을 위한
# 마지막 대화 시각뿐이며 내용과는 무관하다.

DB_LOCK = threading.RLock()
DB = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         TEXT PRIMARY KEY,
  pw_hash    TEXT NOT NULL,
  created    REAL NOT NULL,
  last_login REAL,
  enabled    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_pending (
  id      TEXT PRIMARY KEY,
  pw_hash TEXT NOT NULL,
  ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
  name     TEXT PRIMARY KEY,
  kind     TEXT NOT NULL,
  owner    TEXT,
  pw_hash  TEXT,
  created  REAL NOT NULL,
  last_msg REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS room_allow   (room TEXT, id TEXT, PRIMARY KEY(room, id));
CREATE TABLE IF NOT EXISTS room_pending (room TEXT, id TEXT, ts REAL, PRIMARY KEY(room, id));
CREATE TABLE IF NOT EXISTS room_block   (room TEXT, id TEXT, PRIMARY KEY(room, id));
"""


def db_init():
    global DB
    DB = sqlite3.connect(DB_PATH, check_same_thread=False)
    DB.row_factory = sqlite3.Row
    with DB_LOCK:
        DB.executescript(SCHEMA)
        DB.commit()


def db_q(sql, args=()):
    with DB_LOCK:
        return DB.execute(sql, args).fetchall()


def db_one(sql, args=()):
    with DB_LOCK:
        return DB.execute(sql, args).fetchone()


def db_x(sql, args=()):
    with DB_LOCK:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur


def room_row(name):
    return db_one("SELECT * FROM rooms WHERE name=?", (name,))


def room_ids(table, name):
    return {r["id"] for r in db_q(f"SELECT id FROM {table} WHERE room=?", (name,))}


def drop_room(name):
    """방과 그에 딸린 명단 전부 삭제(DB만)."""
    for t in ("room_allow", "room_pending", "room_block"):
        db_x(f"DELETE FROM {t} WHERE room=?", (name,))
    db_x("DELETE FROM rooms WHERE name=?", (name,))


def rooms_snapshot(uid):
    """클라이언트에 내려줄 방 목록. 정렬은 클라이언트가 한다.
    allowed = 추가 절차 없이 바로 입장 가능한지(리스트 UI 힌트)."""
    allow, pend, block = {}, {}, {}
    for r in db_q("SELECT room, id FROM room_allow"):
        allow.setdefault(r["room"], set()).add(r["id"])
    for r in db_q("SELECT room, id FROM room_pending"):
        pend.setdefault(r["room"], set()).add(r["id"])
    for r in db_q("SELECT room, id FROM room_block"):
        block.setdefault(r["room"], set()).add(r["id"])

    out = []
    for r in db_q("SELECT * FROM rooms"):
        name, kind, owner = r["name"], r["kind"], r["owner"]
        blocked = uid in block.get(name, ())
        if kind == "open":
            allowed = True
        elif uid == owner:
            allowed = True
        elif kind in ("allow", "approve"):
            allowed = uid in allow.get(name, ())
        else:                                  # pw — 비밀번호를 받아야 판정된다
            allowed = False
        out.append({"name": name, "kind": kind, "owner": owner,
                    "created": r["created"], "allowed": allowed and not blocked,
                    "blocked": blocked, "waiting": uid in pend.get(name, ())})
    return out


# === [4. 프레임 입출력] ===
# [길이 4바이트][종류 1바이트][본문] — 종류 'T'=UTF-8 JSON, 'B'=파일 청크(추후).


class ProtoError(Exception):
    pass


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None                       # 상대가 닫음
        buf += chunk
    return bytes(buf)


def recv_frame(sock):
    """(종류, 본문) 반환. 연결이 닫히면 None."""
    head = recv_exact(sock, FRAME_HEAD.size)
    if head is None:
        return None
    ln, typ = FRAME_HEAD.unpack(head)
    if ln > MAX_FRAME:
        raise ProtoError(f"프레임 과대 ({ln})")
    body = recv_exact(sock, ln) if ln else b""
    if body is None:
        return None
    return chr(typ), body


def pack_frame(typ, payload):
    return FRAME_HEAD.pack(len(payload), ord(typ)) + payload


# === [5. 연결 · 서버 상태] ===

STATE_LOCK = threading.RLock()
CONNS = set()                     # 살아있는 Conn 전부(로그인 전 포함)
ONLINE = {}                       # uid -> Conn (같은 ID 동시 접속 불허)
SEQS = {}                         # room -> 방별 단조 증가 번호
RUN_ID = secrets.token_hex(3)     # 서버 실행 식별자. mid = "{RUN_ID}-{seq}"
STOP = threading.Event()


class Conn:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.uid = None
        self.rooms = set()        # 팬아웃 대상 방(창이 열렸거나 구독 중)
        self.subs = set()         # 구독 표시(서버는 관리 표시용으로만 보관)
        self.send_lock = threading.Lock()
        self.last_rx = now()
        self.msg_times = deque()
        self.alive = True

    def who(self):
        return self.uid or f"{self.addr[0]}:{self.addr[1]}"

    def send(self, obj):
        """JSON 프레임 전송. 실패하면 조용히 연결을 죽인다(정리는 스윕이 한다).
        전송은 짧은 메시지뿐이라 블로킹으로 둔다."""
        if not self.alive:
            return False
        data = pack_frame("T", json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        try:
            with self.send_lock:
                self.sock.sendall(data)
            return True
        except Exception:
            self.alive = False
            try:
                self.sock.close()
            except Exception:
                pass
            return False

    def err(self, code, msg, ref=None):
        o = {"t": "error", "code": code, "msg": msg}
        if ref:
            o["ref"] = ref
        return self.send(o)


def fanout(room, obj, sender=None, cid=None):
    """방에 든 모든 연결에 전달. sender에게만 cid를 되돌려 전송 확인에 쓴다."""
    with STATE_LOCK:
        targets = [c for c in CONNS if c.uid and room in c.rooms]
    for c in targets:
        if c is sender and cid is not None:
            o = dict(obj)
            o["cid"] = cid
            c.send(o)
        else:
            c.send(obj)


def broadcast_all(obj):
    """로그인한 모든 연결에 전달(방 삭제 통지 등)."""
    with STATE_LOCK:
        targets = [c for c in CONNS if c.uid]
    for c in targets:
        c.send(obj)


def conn_of(uid):
    with STATE_LOCK:
        return ONLINE.get(uid)


def close_conn(conn, reason=""):
    """연결 정리. 방에서 빼고 온라인 목록에서 지운다."""
    with STATE_LOCK:
        if conn not in CONNS and not conn.alive:
            return
        conn.alive = False
        CONNS.discard(conn)
        if conn.uid and ONLINE.get(conn.uid) is conn:
            del ONLINE[conn.uid]
        uid, rooms = conn.uid, set(conn.rooms)
        conn.rooms.clear()
    try:
        conn.sock.close()
    except Exception:
        pass
    if uid:
        for room in rooms:
            fanout(room, {"t": "member", "room": room, "id": uid, "in": False})
        log(f"[해제] {uid} 접속 종료{(' — ' + reason) if reason else ''}")


def probe_dead(old):
    """같은 ID 재로그인 시 기존 연결이 살아있는지 확인한다(핑 후 1.5초 관찰).
    비정상 종료(전원 차단·랜 끊김)로 남은 연결 때문에 본인이 못 들어오는
    사고를 막기 위한 장치다. True = 죽었다고 판단."""
    mark = old.last_rx
    if not old.send({"t": "ping"}):
        return True
    deadline = now() + 1.5
    while now() < deadline:
        if old.last_rx > mark:
            return False
        time.sleep(0.1)
    return old.last_rx <= mark


# === [6. 프레임 처리] ===


def handle_register(conn, d):
    """회원가입 = 요청만 접수(사후 승인제). 콘솔에서 approve 해야 유효해진다."""
    uid, pw = d.get("id"), d.get("pw")
    if not valid_id(uid):
        return conn.err("bad_id", "ID는 영문·숫자·_- 1~20자여야 합니다.")
    if not isinstance(pw, str) or not (USER_PW_MIN <= len(pw) <= USER_PW_MAX):
        return conn.err("bad_pw", f"비밀번호는 {USER_PW_MIN}~{USER_PW_MAX}자여야 합니다.")
    if db_one("SELECT 1 FROM users WHERE id=?", (uid,)):
        return conn.err("id_taken", "이미 있는 ID입니다.")
    if db_one("SELECT 1 FROM user_pending WHERE id=?", (uid,)):
        return conn.err("id_pending", "이미 가입이 요청된 ID입니다.")
    db_x("INSERT INTO user_pending (id, pw_hash, ts) VALUES (?,?,?)",
         (uid, hash_pw(pw), now()))
    conn.send({"t": "ok", "of": "register", "status": "pending"})
    log(f"[가입요청] '{uid}' — 수락하려면: approve {uid}")


def handle_login(conn, d):
    uid, pw = d.get("id"), d.get("pw")
    if conn.uid:
        return conn.err("already_login", "이미 로그인된 연결입니다.")
    if not valid_id(uid) or not isinstance(pw, str):
        return conn.err("bad_login", "존재하지 않는 ID입니다.")
    row = db_one("SELECT * FROM users WHERE id=?", (uid,))
    # 승인 대기 중이거나 없는 ID는 똑같이 '존재하지 않는 ID' — 대기 여부를
    # 알려주지 않는 편이 ID 탐색을 막는다.
    if not row or not verify_pw(pw, row["pw_hash"]):
        return conn.err("bad_login", "존재하지 않는 ID입니다.")
    if not row["enabled"]:
        return conn.err("disabled", "사용이 정지된 ID입니다.")

    old = conn_of(uid)
    if old is not None and old is not conn:
        if probe_dead(old):
            close_conn(old, "죽은 연결 회수(같은 ID 재로그인)")
        else:
            return conn.err("already_online",
                            "이미 다른 PC에서 접속 중입니다. 먼저 로그아웃하세요.")

    with STATE_LOCK:
        conn.uid = uid
        ONLINE[uid] = conn
    db_x("UPDATE users SET last_login=? WHERE id=?", (now(), uid))
    conn.send({"t": "welcome", "id": uid, "ver": PROTO_VER,
               "server_time": now(), "rooms": rooms_snapshot(uid)})
    log(f"[로그인] {uid} ({conn.addr[0]})")

    # 방장이 접속했으니 밀린 입장 요청을 알려준다
    for r in db_q("SELECT DISTINCT room FROM room_pending"):
        rr = room_row(r["room"])
        if rr and rr["owner"] == uid:
            ids = sorted(room_ids("room_pending", r["room"]))
            conn.send({"t": "pending", "room": r["room"], "ids": ids})


def handle_rooms(conn, d):
    conn.send({"t": "rooms", "list": rooms_snapshot(conn.uid)})


def handle_room_create(conn, d):
    name = clean_room_name(d.get("name"))
    kind = d.get("kind")
    if not name:
        return conn.err("bad_name", f"방 이름은 1~{ROOM_NAME_MAX}자여야 합니다.")
    if kind not in ROOM_KINDS:
        return conn.err("bad_kind", "방 유형이 잘못되었습니다.")
    if room_row(name):
        return conn.err("room_name_taken", "같은 이름의 채팅방이 이미 있습니다.")
    cnt = db_one("SELECT COUNT(*) AS n FROM rooms")["n"]
    if cnt >= CONFIG["max_rooms"]:
        return conn.err("room_limit",
                        f"채팅방은 최대 {CONFIG['max_rooms']}개까지 만들 수 있습니다.")

    pw_hash, allow = None, []
    if kind == "pw":
        pw = d.get("pw")
        if not isinstance(pw, str) or not (1 <= len(pw) <= ROOM_PW_MAX):
            return conn.err("bad_pw", f"비밀번호는 1~{ROOM_PW_MAX}자여야 합니다.")
        pw_hash = hash_pw(pw)
    elif kind == "allow":
        raw = d.get("allow") or []
        allow = sorted({x for x in raw if valid_id(x)})
        if not allow:
            return conn.err("bad_allow", "사전 승인 ID를 한 명 이상 입력하세요.")

    owner = None if kind == "open" else conn.uid
    db_x("INSERT INTO rooms (name, kind, owner, pw_hash, created, last_msg)"
         " VALUES (?,?,?,?,?,?)", (name, kind, owner, pw_hash, now(), now()))
    for x in allow:
        db_x("INSERT OR IGNORE INTO room_allow (room, id) VALUES (?,?)", (name, x))

    conn.send({"t": "ok", "of": "room_create", "room": name})
    log(f"[방 생성] '{name}' ({kind})"
        + (f" 방장 {owner}" if owner else " 공개(방장 없음)"))
    broadcast_all({"t": "room_new", "room": name, "kind": kind, "owner": owner})
    do_join(conn, name)               # 만든 사람은 곧바로 입장(창이 바로 열린다)


def purge_room(name):
    """방을 지우고 접속자 상태를 정리한 뒤 **전원에게** 삭제를 통지한다.
    클라이언트는 이 통지를 받으면 그 방의 로컬 대화 기록까지 지운다. 접속 중이
    아니었던 손님은 다음 로그인 때 목록에 없는 것으로 감지해 같은 정리를 한다.
    (방장 삭제·콘솔 삭제·공개방 자동 삭제가 모두 이 함수를 쓴다.)"""
    drop_room(name)
    with STATE_LOCK:
        for c in CONNS:
            c.rooms.discard(name)
            c.subs.discard(name)
        SEQS.pop(name, None)
    broadcast_all({"t": "room_deleted", "room": name})


def handle_room_delete(conn, d):
    name = clean_room_name(d.get("room"))
    r = room_row(name) if name else None
    if not r:
        return conn.err("room_missing", "없는 채팅방입니다.")
    if r["kind"] == "open" or r["owner"] != conn.uid:
        return conn.err("not_owner", "방장만 삭제할 수 있습니다.")
    purge_room(name)
    log(f"[방 삭제] '{name}' (방장 {conn.uid})")


def do_join(conn, name):
    """입장 확정 — 팬아웃 대상에 넣고 본인·기존 참여자에게 알린다.
    입장 자격 판정은 호출자(handle_join)가 이미 끝냈다고 본다."""
    r = room_row(name)
    if not r:
        return conn.err("room_missing", "없는 채팅방입니다.")
    with STATE_LOCK:
        conn.rooms.add(name)
    conn.send({"t": "joined", "room": name, "kind": r["kind"], "owner": r["owner"]})
    fanout(name, {"t": "member", "room": name, "id": conn.uid, "in": True})


def handle_join(conn, d):
    name = clean_room_name(d.get("room"))
    r = room_row(name) if name else None
    if not r:
        return conn.err("room_missing", "없는 채팅방입니다.")
    uid, kind = conn.uid, r["kind"]

    if uid in room_ids("room_block", name):
        return conn.send({"t": "denied", "room": name, "reason": "blocked",
                          "msg": "강제 퇴장되어 입장할 수 없습니다."})
    if kind == "open" or uid == r["owner"] or uid in room_ids("room_allow", name):
        return do_join(conn, name)

    if kind == "pw":
        pw = d.get("pw")
        if not isinstance(pw, str) or not verify_pw(pw, r["pw_hash"] or ""):
            return conn.send({"t": "denied", "room": name, "reason": "bad_pw_room",
                              "msg": "비밀번호가 틀렸습니다."})
        return do_join(conn, name)

    if kind == "allow":
        return conn.send({"t": "denied", "room": name, "reason": "not_allowed",
                          "msg": "등록된 ID가 아닙니다."})

    # approve — 요청을 남긴다(방장이 오프라인이어도 보관되며 서버 재시작을 넘긴다)
    db_x("INSERT OR IGNORE INTO room_pending (room, id, ts) VALUES (?,?,?)",
         (name, uid, now()))
    owner_conn = conn_of(r["owner"])
    if owner_conn:
        owner_conn.send({"t": "approve_req", "room": name, "id": uid})
    conn.send({"t": "denied", "room": name, "reason": "await_approval",
               "msg": "방장의 승인을 기다리고 있습니다."})
    log(f"[입장요청] '{name}' <- {uid}")


def handle_leave(conn, d):
    name = d.get("room")
    with STATE_LOCK:
        had = name in conn.rooms
        conn.rooms.discard(name)
        conn.subs.discard(name)
    if had:
        fanout(name, {"t": "member", "room": name, "id": conn.uid, "in": False})


def handle_sub(conn, d):
    """구독은 클라이언트 상태다. 서버는 관리 화면 표시용으로만 기억한다
    (구독 중이면 클라이언트가 창을 닫아도 leave를 보내지 않는 것으로 동작한다)."""
    name, on = d.get("room"), bool(d.get("on"))
    with STATE_LOCK:
        if on:
            if name not in conn.rooms:
                return conn.err("not_joined", "입장하지 않은 방입니다.")
            conn.subs.add(name)
        else:
            conn.subs.discard(name)
    conn.send({"t": "ok", "of": "sub", "room": name, "on": on})


def handle_msg(conn, d):
    name, body = d.get("room"), d.get("body")
    if name not in conn.rooms:
        return conn.err("not_joined", "입장하지 않은 방입니다.")
    if not isinstance(body, str) or not body:
        return
    if len(body) > CONFIG["msg_max_len"]:
        return conn.err("too_long", "메시지가 너무 깁니다.")

    t = now()
    conn.msg_times.append(t)
    while conn.msg_times and t - conn.msg_times[0] > MSG_WINDOW:
        conn.msg_times.popleft()
    if len(conn.msg_times) > MSG_BURST:
        return conn.err("rate_limited", "너무 빠르게 보내고 있습니다.")

    with STATE_LOCK:
        seq = SEQS.get(name, 0) + 1
        SEQS[name] = seq
    db_x("UPDATE rooms SET last_msg=? WHERE name=?", (t, name))
    fanout(name, {"t": "msg", "room": name, "from": conn.uid, "body": body,
                  "mid": f"{RUN_ID}-{seq}", "seq": seq, "ts": t},
           sender=conn, cid=d.get("cid"))


def owner_or_err(conn, name):
    r = room_row(name) if name else None
    if not r:
        conn.err("room_missing", "없는 채팅방입니다.")
        return None
    if r["kind"] == "open" or r["owner"] != conn.uid:
        conn.err("not_owner", "방장만 할 수 있습니다.")
        return None
    return r


def handle_pending(conn, d):
    name = clean_room_name(d.get("room"))
    if not owner_or_err(conn, name):
        return
    conn.send({"t": "pending", "room": name,
               "ids": sorted(room_ids("room_pending", name))})


def handle_approve(conn, d):
    name, uid, ok = clean_room_name(d.get("room")), d.get("id"), bool(d.get("ok"))
    if not owner_or_err(conn, name):
        return
    if not valid_id(uid):
        return
    db_x("DELETE FROM room_pending WHERE room=? AND id=?", (name, uid))
    if ok:
        # 수락은 영구 — 허용 명단에 넣어 다음부터 바로 입장된다
        db_x("INSERT OR IGNORE INTO room_allow (room, id) VALUES (?,?)", (name, uid))
    target = conn_of(uid)
    if target:
        target.send({"t": "approve_res", "room": name, "ok": ok,
                     "msg": "입장이 승인되었습니다." if ok else "입장이 거절되었습니다."})
    conn.send({"t": "ok", "of": "approve", "room": name, "id": uid, "ok": ok})
    log(f"[{'승인' if ok else '거절'}] '{name}' {uid} (방장 {conn.uid})")


def handle_kick(conn, d):
    """강제 퇴장 — 자동으로 그 방 블랙리스트에 등재된다.
    사전 승인 방이면 허용 명단에서도 함께 지운다(그래야 다시 못 들어온다)."""
    name, uid = clean_room_name(d.get("room")), d.get("id")
    if not owner_or_err(conn, name):
        return
    if not valid_id(uid):
        return
    if uid == conn.uid:
        return conn.err("bad_target", "방장 자신은 퇴장시킬 수 없습니다.")
    db_x("INSERT OR IGNORE INTO room_block (room, id) VALUES (?,?)", (name, uid))
    db_x("DELETE FROM room_allow   WHERE room=? AND id=?", (name, uid))
    db_x("DELETE FROM room_pending WHERE room=? AND id=?", (name, uid))

    target = conn_of(uid)
    if target:
        with STATE_LOCK:
            was_in = name in target.rooms
            target.rooms.discard(name)
            target.subs.discard(name)
        target.send({"t": "kicked", "room": name,
                     "msg": "방장에 의해 강제 퇴장되었습니다."})
        if was_in:
            fanout(name, {"t": "member", "room": name, "id": uid, "in": False})
    conn.send({"t": "ok", "of": "kick", "room": name, "id": uid})
    log(f"[강제퇴장] '{name}' {uid} (방장 {conn.uid}) — 블랙리스트 등재")


def handle_room_pw(conn, d):
    """비밀번호 방의 비밀번호를 방장이 바꾼다.
    바꾼 뒤에도 이미 들어와 있는 사람을 내보내지는 않는다(내보내려면 kick).
    다른 사람이 보관해둔 옛 비번은 다음 입장에서 거절되고, 클라이언트가 그때
    보관값을 지우고 입력창을 띄운다."""
    name, pw = clean_room_name(d.get("room")), d.get("pw")
    r = owner_or_err(conn, name)
    if not r:
        return
    if r["kind"] != "pw":
        return conn.err("bad_kind", "비밀번호 방이 아닙니다.")
    if not isinstance(pw, str) or not (1 <= len(pw) <= ROOM_PW_MAX):
        return conn.err("bad_pw", f"비밀번호는 1~{ROOM_PW_MAX}자여야 합니다.")
    db_x("UPDATE rooms SET pw_hash=? WHERE name=?", (hash_pw(pw), name))
    conn.send({"t": "ok", "of": "room_pw", "room": name})
    log(f"[비번 변경] '{name}' (방장 {conn.uid})")


def handle_blocklist(conn, d):
    name = clean_room_name(d.get("room"))
    if not owner_or_err(conn, name):
        return
    conn.send({"t": "blocklist", "room": name,
               "ids": sorted(room_ids("room_block", name))})


def handle_unblock(conn, d):
    name, uid = clean_room_name(d.get("room")), d.get("id")
    if not owner_or_err(conn, name):
        return
    db_x("DELETE FROM room_block WHERE room=? AND id=?", (name, uid))
    conn.send({"t": "ok", "of": "unblock", "room": name, "id": uid})
    log(f"[블랙 해제] '{name}' {uid} (방장 {conn.uid})")


def handle_pong(conn, d):
    pass                                 # last_rx는 프레임 수신 자체로 갱신된다


# 로그인 없이 받아주는 프레임
PUBLIC_HANDLERS = {"register": handle_register, "login": handle_login,
                   "pong": handle_pong}
HANDLERS = {
    "rooms": handle_rooms, "room_create": handle_room_create,
    "room_delete": handle_room_delete, "join": handle_join,
    "leave": handle_leave, "sub": handle_sub, "msg": handle_msg,
    "pending": handle_pending, "approve": handle_approve, "kick": handle_kick,
    "blocklist": handle_blocklist, "unblock": handle_unblock,
    "room_pw": handle_room_pw, "pong": handle_pong,
}


def serve_conn(conn):
    """연결 하나를 담당하는 스레드."""
    while not STOP.is_set() and conn.alive:
        try:
            got = recv_frame(conn.sock)
        except ProtoError as e:
            conn.err("bad_frame", str(e))
            break
        except OSError:
            break
        if got is None:
            break
        typ, body = got
        conn.last_rx = now()
        if typ != "T":
            conn.err("bad_frame", "지원하지 않는 프레임 종류입니다.")
            break
        try:
            d = json.loads(body.decode("utf-8"))
            t = d.get("t")
        except Exception:
            conn.err("bad_frame", "JSON을 해석할 수 없습니다.")
            break
        if not isinstance(d, dict) or not isinstance(t, str):
            conn.err("bad_frame", "규격에 맞지 않는 메시지입니다.")
            break

        if t == "logout":
            break
        try:
            if t in PUBLIC_HANDLERS:
                PUBLIC_HANDLERS[t](conn, d)
            elif not conn.uid:
                conn.err("unauth", "로그인이 필요합니다.")
            elif t in HANDLERS:
                HANDLERS[t](conn, d)
            else:
                conn.err("bad_frame", f"알 수 없는 요청: {t}")
        except Exception as e:
            log(f"[경고] {conn.who()} '{t}' 처리 실패: {e}")
            conn.err("server_error", "서버에서 처리 중 오류가 났습니다.")
    close_conn(conn)


def accept_loop(srv):
    while not STOP.is_set():
        try:
            sock, addr = srv.accept()
        except OSError:
            break
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            # 서버는 절전할 이유가 없다 — 연결은 계속 열어두고, 죽은 링크는
            # OS 수준 keepalive와 아래 ping/타임아웃 두 겹으로 걸러낸다.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        conn = Conn(sock, addr)
        with STATE_LOCK:
            CONNS.add(conn)
        threading.Thread(target=serve_conn, args=(conn,), daemon=True).start()


# === [7. 유지보수 — 생존 확인 · 공개방 자동 삭제] ===


def maintenance_loop():
    """ping 주기로 생존을 확인하고, 60초마다 공개방 TTL을 청소한다."""
    last_sweep = 0.0
    while not STOP.wait(CONFIG["ping_sec"]):
        limit = now() - CONFIG["pong_timeout_sec"]
        with STATE_LOCK:
            conns = list(CONNS)
        for c in conns:
            if c.last_rx < limit:
                close_conn(c, "응답 없음(타임아웃)")
            else:
                c.send({"t": "ping"})

        if now() - last_sweep >= 60:
            last_sweep = now()
            sweep_public_rooms()


def sweep_public_rooms():
    days = CONFIG["public_room_ttl_days"]
    if not days:
        return                            # 0이면 자동 삭제 없음(수동 삭제만)
    limit = now() - days * 86400
    for r in db_q("SELECT name FROM rooms WHERE kind='open' AND last_msg < ?",
                  (limit,)):
        purge_room(r["name"])
        log(f"[자동 삭제] 공개방 '{r['name']}' — {days}일 이상 대화 없음")


# === [8. 콘솔 — 계정 · 채팅방 관리] ===

HELP = """\
계정   users | pending | approve <ID> | reject <ID> | deluser <ID>
       disable <ID> | enable <ID> | online | kick <ID>
채팅방 rooms | room <이름> | delroom <이름>
       delrooms all|open|limited|owner <ID>   (일괄 삭제, 확인 후 진행)
기타   addr | set <키> <값> | config | help | quit"""


def cmd_users(_):
    rows = db_q("SELECT * FROM users ORDER BY id")
    if not rows:
        print("승인된 계정이 없습니다. (pending 으로 가입 요청을 확인하세요)")
        return
    with STATE_LOCK:
        on = set(ONLINE)
    print(f"{'ID':<20} {'상태':<6} {'가입':<12} {'최근 로그인':<12}")
    for r in rows:
        state = "접속중" if r["id"] in on else ("정지" if not r["enabled"] else "-")
        print(f"{r['id']:<20} {state:<6} {fmt_ts(r['created']):<12} "
              f"{fmt_ts(r['last_login']):<12}")
    print(f"총 {len(rows)}명, 접속 중 {len(on)}명")


def cmd_pending(_):
    rows = db_q("SELECT * FROM user_pending ORDER BY ts")
    if not rows:
        print("가입 대기 없음")
        return
    for r in rows:
        print(f"  {r['id']:<20} 요청 {fmt_ts(r['ts'])}")
    print(f"수락: approve <ID> / 거절: reject <ID>")


def cmd_approve(args):
    if not args:
        return print("사용법: approve <ID>")
    uid = args[0]
    row = db_one("SELECT * FROM user_pending WHERE id=?", (uid,))
    if not row:
        return print(f"'{uid}' 가입 요청이 없습니다.")
    db_x("INSERT INTO users (id, pw_hash, created, enabled) VALUES (?,?,?,1)",
         (uid, row["pw_hash"], now()))
    db_x("DELETE FROM user_pending WHERE id=?", (uid,))
    print(f"'{uid}' 가입을 수락했습니다. 이제 로그인할 수 있습니다.")


def cmd_reject(args):
    if not args:
        return print("사용법: reject <ID>")
    cur = db_x("DELETE FROM user_pending WHERE id=?", (args[0],))
    print(f"'{args[0]}' 가입 요청을 거절했습니다." if cur.rowcount
          else f"'{args[0]}' 가입 요청이 없습니다.")


def cmd_deluser(args):
    if not args:
        return print("사용법: deluser <ID>")
    uid = args[0]
    if not db_one("SELECT 1 FROM users WHERE id=?", (uid,)):
        return print(f"'{uid}' 계정이 없습니다.")
    db_x("DELETE FROM users WHERE id=?", (uid,))
    c = conn_of(uid)
    if c:
        c.send({"t": "error", "code": "deleted", "msg": "계정이 삭제되었습니다."})
        close_conn(c, "계정 삭제")
    print(f"'{uid}' 계정을 삭제했습니다."
          " (이 계정이 방장인 제한방은 그대로 남습니다 — delroom 으로 정리)")


def _set_enabled(uid, on):
    if not db_one("SELECT 1 FROM users WHERE id=?", (uid,)):
        return print(f"'{uid}' 계정이 없습니다.")
    db_x("UPDATE users SET enabled=? WHERE id=?", (1 if on else 0, uid))
    if not on:
        c = conn_of(uid)
        if c:
            close_conn(c, "계정 정지")
    print(f"'{uid}' 계정을 {'해제' if on else '정지'}했습니다.")


def cmd_disable(args):
    return _set_enabled(args[0], False) if args else print("사용법: disable <ID>")


def cmd_enable(args):
    return _set_enabled(args[0], True) if args else print("사용법: enable <ID>")


def cmd_online(_):
    with STATE_LOCK:
        items = [(uid, c) for uid, c in ONLINE.items()]
        rooms = {uid: sorted(c.rooms) for uid, c in items}
    if not items:
        return print("접속 중인 사용자가 없습니다.")
    for uid, c in sorted(items):
        r = ", ".join(rooms[uid]) or "-"
        print(f"  {uid:<20} {c.addr[0]:<16} 방: {r}")


def cmd_kick(args):
    if not args:
        return print("사용법: kick <ID>   (연결만 끊습니다. 계정은 그대로)")
    c = conn_of(args[0])
    if not c:
        return print(f"'{args[0]}' 는 접속 중이 아닙니다.")
    c.send({"t": "error", "code": "kicked_conn", "msg": "서버에서 연결을 끊었습니다."})
    close_conn(c, "관리자 kick")
    print(f"'{args[0]}' 연결을 끊었습니다.")


KIND_KO = {"open": "공개", "pw": "비밀번호", "allow": "사전승인", "approve": "사후승인"}


def cmd_rooms(_):
    rows = db_q("SELECT * FROM rooms ORDER BY created")
    if not rows:
        return print("채팅방이 없습니다.")
    with STATE_LOCK:
        joined = {}
        for c in CONNS:
            for r in c.rooms:
                joined[r] = joined.get(r, 0) + 1
    print(f"{'이름':<24} {'유형':<8} {'방장':<12} {'생성':<12} {'마지막대화':<12} 접속")
    for r in rows:
        print(f"{r['name']:<24} {KIND_KO.get(r['kind'], r['kind']):<8} "
              f"{(r['owner'] or '-'):<12} {fmt_ts(r['created']):<12} "
              f"{fmt_ts(r['last_msg']):<12} {joined.get(r['name'], 0)}")
    print(f"총 {len(rows)}개 / 상한 {CONFIG['max_rooms']}개")


def cmd_room(args):
    if not args:
        return print("사용법: room <이름>")
    name = " ".join(args)
    r = room_row(name)
    if not r:
        return print(f"'{name}' 채팅방이 없습니다.")
    print(f"이름   : {r['name']}")
    print(f"유형   : {KIND_KO.get(r['kind'], r['kind'])}")
    print(f"방장   : {r['owner'] or '없음(공개방)'}")
    print(f"생성   : {fmt_ts(r['created'])}   마지막 대화: {fmt_ts(r['last_msg'])}")
    # 한글은 표시 폭이 2칸이라 f-string 폭 지정으로는 정렬이 맞지 않는다 → 그냥 붙인다
    for label, table in (("허용 ID", "room_allow"), ("승인 대기", "room_pending"),
                         ("블랙리스트", "room_block")):
        ids = sorted(room_ids(table, name))
        if ids:
            print(f"{label}: {', '.join(ids)}")
    with STATE_LOCK:
        here = sorted(c.uid for c in CONNS if c.uid and name in c.rooms)
        subs = sorted(c.uid for c in CONNS if c.uid and name in c.subs)
    print(f"접속 중: {', '.join(here) or '-'}")
    print(f"구독 중: {', '.join(subs) or '-'}")


def cmd_delroom(args):
    if not args:
        return print("사용법: delroom <이름>")
    name = " ".join(args)
    if not room_row(name):
        return print(f"'{name}' 채팅방이 없습니다.")
    purge_room(name)
    print(f"'{name}' 채팅방을 삭제했습니다. (참여자들의 대화 기록도 삭제됩니다)")


def cmd_delrooms(args):
    """채팅방 일괄 삭제. 되돌릴 수 없으므로 대상을 보여준 뒤 확인을 받는다."""
    usage = "사용법: delrooms all | open | limited | owner <ID>"
    what = args[0].lower() if args else ""
    if what == "all":
        rows, desc = db_q("SELECT name FROM rooms"), "모든 채팅방"
    elif what == "open":
        rows = db_q("SELECT name FROM rooms WHERE kind='open'")
        desc = "공개 채팅방"
    elif what == "limited":
        rows = db_q("SELECT name FROM rooms WHERE kind!='open'")
        desc = "제한 채팅방"
    elif what == "owner" and len(args) >= 2:
        rows = db_q("SELECT name FROM rooms WHERE owner=?", (args[1],))
        desc = f"'{args[1]}' 이(가) 만든 채팅방"
    else:
        return print(usage)

    names = [r["name"] for r in rows]
    if not names:
        return print(f"{desc}이 없습니다.")
    shown = ", ".join(names[:20]) + (" ..." if len(names) > 20 else "")
    print(f"{desc} {len(names)}개: {shown}")
    # 질문을 input() 프롬프트로 주면 개행이 없어 로그·파이프에서 잘 안 보인다 → 따로 출력
    print("정말 삭제할까요? 참여자들의 대화 기록도 함께 삭제됩니다. (y = 삭제)")
    try:
        ans = input("> ")
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans.strip().lower() != "y":
        return print("취소했습니다.")
    for n in names:
        purge_room(n)
    log(f"[일괄 삭제] {desc} {len(names)}개 삭제")


def cmd_config(_):
    for k in DEFAULT_CONFIG:
        print(f"  {k} = {CONFIG[k]}")


def cmd_set(args):
    if len(args) < 2:
        return print("사용법: set <키> <값>   (config 로 키 목록 확인)")
    k, v = args[0], args[1]
    if k not in DEFAULT_CONFIG:
        return print(f"모르는 키: {k}")
    try:
        CONFIG[k] = type(DEFAULT_CONFIG[k])(v)
    except ValueError:
        return print(f"'{v}' 는 {k} 에 넣을 수 없습니다.")
    save_config()
    print(f"{k} = {CONFIG[k]}"
          + ("  (포트는 재시작 후 적용)" if k == "port" else ""))


def cmd_addr(_):
    print_addresses(CONFIG["port"])


COMMANDS = {
    "addr": cmd_addr,
    "users": cmd_users, "pending": cmd_pending, "approve": cmd_approve,
    "reject": cmd_reject, "deluser": cmd_deluser, "disable": cmd_disable,
    "enable": cmd_enable, "online": cmd_online, "kick": cmd_kick,
    "rooms": cmd_rooms, "room": cmd_room, "delroom": cmd_delroom,
    "delrooms": cmd_delrooms,
    "config": cmd_config, "set": cmd_set,
}


def repl():
    print(HELP)
    while not STOP.is_set():
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("quit", "exit"):
            break
        if cmd == "help":
            print(HELP)
            continue
        fn = COMMANDS.get(cmd)
        if not fn:
            print(f"모르는 명령: {cmd}  (help)")
            continue
        try:
            fn(args)
        except Exception as e:
            print(f"명령 실패: {e}")


# === [9. 진입점] ===


def main():
    load_config()
    db_init()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", CONFIG["port"]))
    except OSError as e:
        print(f"포트 {CONFIG['port']} 를 열 수 없습니다: {e}")
        return 1
    srv.listen(32)

    log(f"domiserver {APP_VERSION} 시작 — 포트 {CONFIG['port']}, run={RUN_ID}")
    print_addresses(CONFIG["port"])
    users = db_one("SELECT COUNT(*) AS n FROM users")["n"]
    waiting = db_one("SELECT COUNT(*) AS n FROM user_pending")["n"]
    rooms = db_one("SELECT COUNT(*) AS n FROM rooms")["n"]
    log(f"계정 {users}명(가입 대기 {waiting}명), 채팅방 {rooms}개")
    if waiting:
        log("가입 대기가 있습니다 — pending 으로 확인하세요.")

    threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()

    try:
        repl()
    finally:
        STOP.set()
        try:
            srv.close()
        except Exception:
            pass
        with STATE_LOCK:
            conns = list(CONNS)
        for c in conns:
            close_conn(c, "서버 종료")
        with DB_LOCK:
            DB.commit()
            DB.close()
        log("domiserver 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
