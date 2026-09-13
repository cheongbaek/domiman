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

**브라우저용 웹 중계가 이 안에 들어 있다(옛 domiweb.py).** 예전에는 옆에서 따로
돌던 프로세스가 도로 이 서버에 127.0.0.1 로 붙어 'web' 계정으로 로그인했는데,
한 프로그램이 되면서 그 왕복이 사라졌다 — 중계는 소켓 대신 `HubConn`(가상 연결)로
서버 안에 자리를 잡고, 입장·비밀번호·팬아웃·도배 제한·이미지 중계를 기존 handle_*
그대로 통과한다. 자세한 것은 아래 [8. 웹 중계] 절 머리말.

**관리 창(tkinter)도 이 안에 들어 있다(옛 domiserver_gui.py).** 그냥 실행하면
관리 창이 뜨고 서버·웹 중계가 그 뒤에서 돈다. `--console` 을 주면 예전처럼
콘솔(repl)로 뜬다(창을 못 쓰는 환경·원격 세션용).

콘솔·창 모두 계정·채팅방·웹 중계 관리용이며, 채팅 내용은 출력하지 않는다.
"""

import base64
import contextlib
import ctypes
import hashlib
import hmac
import io
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

# === [1. 상수 · 설정] ===

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "domiserver.json")
DB_PATH = os.path.join(BASE_DIR, "domiserver.db")
DOMIWEB_CONFIG_PATH = os.path.join(BASE_DIR, "domiweb.json")   # 옛 설정 승계용

APP_VERSION = "260914a"
# 프로토콜 버전 — welcome 으로 알려준다. 클라이언트는 이 값으로 기능 유무를 판단한다.
#   1 = 텍스트 채팅  /  2 = 이미지 첨부('B' 프레임) 지원
# 옛 서버는 'B' 프레임을 '지원하지 않는 프레임'으로 보고 **연결을 끊으므로**,
# 클라이언트가 버전을 보고 미리 막지 않으면 재접속 고리에 빠진다(실측).
PROTO_VER = 2

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
    "tls": True,                # 자체 서명 인증서로 TLS 제공(없으면 첫 실행에 생성)
    "require_tls": False,       # True면 평문 접속을 거부한다(전환이 끝난 뒤 켤 것)
    "file_max_mb": 32,          # 한 이미지 최대 크기(변환 후 PNG 기준)
    "file_max_concurrent": 3,   # 한 연결이 동시에 보낼 수 있는 전송 수

    # --- 웹 중계(옛 domiweb.json). 아래 [8. 웹 중계] 절 참고 ---
    "web": True,                # 브라우저 중계를 열지 여부
    "web_host": "0.0.0.0",
    "web_port": 47822,          # 브라우저(WSS)용. 채팅 포트 47821 옆자리
    "web_id": "web",            # 중계가 방에서 쓰는 이름(계정은 필요 없다)
    "web_pcs": ["seoul", "chungju", "domi"],   # 피제어 PC 목록(웹에서 추가 가능)
    "web_certfile": "",         # 브라우저가 신뢰하는 인증서(fullchain PEM)
    "web_keyfile": "",          # 그 개인키(PEM). 비우면 평문 ws(개발용)
    "web_origins": [],          # 빈 배열이면 Origin 검사 없음(오픈 방침)
}

# domiweb.json -> domiserver.json 키 이름 대응(따로 돌던 시절의 설정 승계용).
# server/pw/server_fp 는 상류 접속용이던 것이라 한 프로그램이 되면서 없어졌다.
DOMIWEB_KEYMAP = {
    "listen_host": "web_host", "listen_port": "web_port", "id": "web_id",
    "pcs": "web_pcs", "certfile": "web_certfile", "keyfile": "web_keyfile",
    "allow_origins": "web_origins",
}

FILE_CHUNK_MAX = 65536          # 'B' 프레임 한 개의 데이터 상한
FILE_HEAD = struct.Struct(">16sI")   # 'B' 프레임 머리: fid 16바이트 + seq 4바이트

CERT_PATH = os.path.join(BASE_DIR, "domiserver.crt")
KEY_PATH = os.path.join(BASE_DIR, "domiserver.key")
SSL_CTX = None                  # TLS 사용 가능하면 SSLContext, 아니면 None
CERT_FP = None                  # 인증서 SHA-256 지문(클라이언트가 고정하는 값)

CONFIG = dict(DEFAULT_CONFIG)


CONFIG_HAD_WEB_KEYS = False       # 설정 파일에 web_* 가 이미 있었는가(승계 판정용)


def load_config():
    """설정 파일 로드(없거나 깨졌으면 기본값). 모르는 키는 무시한다."""
    global CONFIG, CONFIG_HAD_WEB_KEYS
    CONFIG = dict(DEFAULT_CONFIG)
    CONFIG_HAD_WEB_KEYS = False
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fp:
            data = json.load(fp)
        CONFIG_HAD_WEB_KEYS = any(k.startswith("web") for k in data)
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


def import_domiweb_config():
    """옆에 옛 `domiweb.json` 이 있고 이 설정에 web_* 가 아직 없으면 한 번 옮겨 온다.

    **인증서 경로를 잃지 않으려는 것이 요점이다** — 공인 인증서·키 경로는 사람이
    손으로 적어 넣은 값이고, 비어 있으면 평문 ws 로 열려 **브라우저가 조용히 못
    붙는다**(오류도 안 난다). 피제어 PC 목록도 마찬가지로 손으로 쌓인 값이다.
    한 번 옮기고 나면 domiserver.json 에 web_* 가 생겨 다시 보지 않는다."""
    if CONFIG_HAD_WEB_KEYS:
        return
    try:
        # utf-8-sig: PowerShell 의 `Set-Content -Encoding UTF8` 은 **BOM 을 붙인다.**
        # 그냥 utf-8 로 읽으면 json 이 BOM 에서 깨져 조용히 건너뛰게 된다.
        with open(DOMIWEB_CONFIG_PATH, encoding="utf-8-sig") as fp:
            old = json.load(fp)
    except FileNotFoundError:
        return
    except Exception as e:
        return log(f"[웹] 옛 domiweb.json 을 읽지 못했습니다: {e}")
    moved = []
    for src, dst in DOMIWEB_KEYMAP.items():
        v = old.get(src)
        if v is not None and isinstance(v, type(DEFAULT_CONFIG[dst])):
            CONFIG[dst] = v
            moved.append(dst)
    if moved:
        save_config()
        log(f"[웹] 옛 domiweb.json 설정을 옮겨 왔습니다: {', '.join(moved)}")


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


def external_ip():
    """'밖에서 본 내 주소'(공인 IP). 조회 실패면 None.
    **조회처를 한 곳에만 둔다** — 콘솔 안내(_report_external)와 관리 창
    (domiserver_gui.py 의 주소 표시)이 이 함수를 같이 쓴다."""
    for url in ("https://api64.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.read().decode("utf-8", "replace").strip()
        except Exception:
            continue
    return None


def _report_external(port):
    """'밖에서 본 내 주소'를 조회해, 포트포워딩이 필요한 환경인지 알려준다."""
    ip = external_ip()
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


# === [3-1. TLS — 자체 서명 인증서 + 지문 고정] ===
# 브라우저가 아니라 우리 클라이언트만 붙으므로 도메인·Let's Encrypt가 필요 없다.
# 클라이언트는 인증서를 검증하지 않고 **지문(SHA-256)을 처음 접속에서 기억해 고정**
# 한다(SSH와 같은 방식). 첫 접속만 신뢰하면 그 뒤로는 중간자 개입을 막는다.


def _find_openssl():
    """인증서 생성용 openssl. 파이썬 표준 라이브러리로는 X.509를 만들 수 없어
    외부 도구가 필요하다(Windows에는 Git 설치본에 들어 있다)."""
    p = shutil.which("openssl")
    if p:
        return p
    for cand in (r"C:\Program Files\Git\usr\bin\openssl.exe",
                 r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\openssl.exe")):
        if os.path.isfile(cand):
            return cand
    return None


def ensure_cert():
    """인증서·키가 없으면 만든다. 만들 수 없으면 None(평문으로 계속 운영)."""
    if os.path.isfile(CERT_PATH) and os.path.isfile(KEY_PATH):
        return True
    ossl = _find_openssl()
    if not ossl:
        log("[TLS] openssl 을 찾지 못해 인증서를 만들 수 없습니다 — 평문으로 운영합니다.")
        return False
    try:
        subprocess.run(
            [ossl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
             "-days", "7300", "-subj", "/CN=domiserver",
             "-keyout", KEY_PATH, "-out", CERT_PATH],
            check=True, capture_output=True, timeout=120)
    except Exception as e:
        log(f"[TLS] 인증서 생성 실패: {e} — 평문으로 운영합니다.")
        return False
    log("[TLS] 자체 서명 인증서를 새로 만들었습니다(domiserver.crt/.key)."
        " 키 파일은 절대 공유하지 마세요.")
    return True


def cert_fingerprint():
    """인증서 SHA-256 지문(소문자 hex). 클라이언트가 고정하는 값과 같다."""
    try:
        with open(CERT_PATH, encoding="ascii") as fp:
            der = ssl.PEM_cert_to_DER_cert(fp.read())
        return hashlib.sha256(der).hexdigest()
    except Exception:
        return None


def _pin_tls12(ctx):
    """**TLS 1.2로 고정하고 재협상을 금지한다(중요한 이유가 있다).**

    이 서버는 연결마다 스레드가 하나 붙어 그 소켓을 읽고, 다른 연결의 스레드가
    같은 소켓에 팬아웃을 쓴다(클라이언트도 수신 스레드와 송신 스레드가 나뉘어
    있다). 즉 **하나의 SSL 소켓을 서로 다른 스레드가 읽고 쓴다.**

    TLS 1.3은 핸드셰이크가 끝난 뒤에도 서버가 NewSessionTicket을 보내고
    KeyUpdate가 오갈 수 있어, 읽기 경로가 내부적으로 쓰기 상태를 건드린다.
    그래서 읽기와 쓰기가 겹치면 record layer가 깨진다 — 실측 증상은 서버의
    `[SSL: RECORD_LAYER_FAILURE]`와 클라이언트의 갑작스러운 EOF였고, 접속 직후
    8초쯤에 재현됐다.

    TLS 1.2에서는 핸드셰이크 이후 방향별 record layer가 분리돼 '한 스레드가 읽고
    한 스레드가 쓰는' 구조가 안전하다(재협상만이 예외이므로 그것도 막는다).
    암호화 강도는 이 용도에 충분하다. **1.3으로 올리려면 먼저 양쪽 I/O를 단일
    스레드(selectors/asyncio)로 바꿔야 한다.**"""
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    no_reneg = getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    if no_reneg:
        ctx.options |= no_reneg


def setup_tls():
    global SSL_CTX, CERT_FP
    if not CONFIG["tls"]:
        log("[TLS] 설정에서 꺼져 있습니다 — 평문으로 운영합니다.")
        return
    if not ensure_cert():
        return
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_PATH, KEY_PATH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        _pin_tls12(ctx)
    except Exception as e:
        log(f"[TLS] 인증서를 읽지 못했습니다: {e} — 평문으로 운영합니다.")
        return
    SSL_CTX, CERT_FP = ctx, cert_fingerprint()
    log(f"[TLS] 사용 중 (지문 {CERT_FP[:16]}…)"
        + ("  ※ 평문 접속은 거부합니다." if CONFIG["require_tls"]
           else "  평문 접속도 함께 받습니다."))


def wrap_if_tls(sock):
    """접속 직후 첫 바이트를 **엿봐서**(MSG_PEEK) TLS 핸드셰이크(0x16)면 감싼다.
    평문도 계속 받아주므로 **서버를 먼저 올려도 옛 클라이언트가 죽지 않는다**
    (require_tls=True면 평문을 거부한다). 반환 (소켓|None, TLS여부)."""
    try:
        sock.settimeout(10)
        head = sock.recv(1, socket.MSG_PEEK)
    except OSError:
        return None, False
    if not head:
        return None, False

    is_tls_hello = head[0] == 0x16
    if not is_tls_hello:
        if CONFIG["require_tls"]:
            log("[TLS] 평문 접속을 거부했습니다(require_tls).")
            return None, False
        try:
            sock.settimeout(None)
        except OSError:
            pass
        return sock, False

    if SSL_CTX is None:
        log("[TLS] 클라이언트가 TLS로 접속했지만 서버에 인증서가 없습니다.")
        return None, False
    try:
        secure = SSL_CTX.wrap_socket(sock, server_side=True)
        secure.settimeout(None)
        return secure, True
    except (ssl.SSLError, OSError) as e:
        log(f"[TLS] 핸드셰이크 실패: {e}")
        return None, False


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

# 관리 창(domiserver_gui.py)이 방 안을 들여다보는 **유일한 통로**. 콘솔로 돌리면
# None 이라 아무 일도 하지 않는다(기존 동작 그대로).
#   서버가 대화를 저장하지 않는다는 원칙은 그대로다 — 지나가는 프레임을 그때
#   넘겨줄 뿐이고, 남는 곳은 관리 창을 열어 둔 동안의 화면뿐이다.
ROOM_OBSERVER = None


class Conn:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.uid = None
        self.tls = False          # 이 연결이 TLS인지(관리 화면 표시용)
        self.ready = False        # TLS 감싸기까지 끝났는지 — 끝나기 전엔 아무것도 보내면 안 된다
        self.rooms = set()        # 팬아웃 대상 방(창이 열렸거나 구독 중)
        self.subs = set()         # 구독 표시(서버는 관리 표시용으로만 보관)
        self.tx_files = {}        # 이 연결이 지금 보내는 중인 이미지: fid -> {room,size,got}
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

    def send_bytes(self, data):
        """이미 프레임으로 만들어진 바이트를 그대로 보낸다(이미지 청크 중계용)."""
        if not self.alive:
            return False
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


def fanout(room, obj, sender=None, cid=None, exclude=None):
    """방에 든 모든 연결에 전달. sender에게만 cid를 되돌려 전송 확인에 쓴다.
    exclude 로 지정한 연결은 건너뛴다(이미지는 보낸 쪽이 이미 화면에 그려뒀다)."""
    with STATE_LOCK:
        targets = [c for c in CONNS if c.uid and room in c.rooms and c is not exclude]
    if ROOM_OBSERVER is not None:
        try:
            ROOM_OBSERVER(room, obj)
        except Exception:
            pass                  # 보는 쪽 사고가 중계를 막아서는 안 된다
    for c in targets:
        if c is sender and cid is not None:
            o = dict(obj)
            o["cid"] = cid
            c.send(o)
        else:
            c.send(obj)


def fanout_bytes(room, data, exclude=None):
    """이미지 청크 프레임을 방의 다른 연결들에 그대로 흘려보낸다(서버는 저장하지 않음)."""
    with STATE_LOCK:
        targets = [c for c in CONNS if c.uid and room in c.rooms and c is not exclude]
    for c in targets:
        c.send_bytes(data)


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
        pending_files = list(conn.tx_files.items())
        conn.tx_files.clear()
        conn.rooms.clear()
    try:
        conn.sock.close()
    except Exception:
        pass
    if uid:
        # 보내던 이미지가 있으면 받는 쪽이 반쪽 데이터를 붙들고 있지 않게 알려준다
        for fid, tr in pending_files:
            fanout(tr["room"], {"t": "file_abort", "fid": fid}, exclude=conn)
        for room in rooms:
            fanout(room, {"t": "member", "room": room, "id": uid, "in": False})
        log(f"[해제] {uid} 접속 종료{(' — ' + reason) if reason else ''}")
    elif reason:
        # 로그인 전에 끊긴 연결도 사유가 있으면 남긴다 — 안 남기면 TLS·규격 문제로
        # 조용히 끊겼을 때 원인을 볼 수가 없다.
        log(f"[해제] {conn.addr[0]}:{conn.addr[1]} (로그인 전) — {reason}")


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
    log(f"[로그인] {uid} ({conn.addr[0]}, {'TLS' if conn.tls else '평문'})")

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


# ---------- 이미지(첨부) 중계 ----------
# **서버는 이미지를 저장하지 않는다.** 청크를 받는 즉시 방의 다른 접속자에게 흘려보내고
# 아무것도 남기지 않는다(대화 내용을 저장하지 않는 원칙과 같다). 그래서 받는 쪽이 접속해
# 있지 않으면 그 이미지는 못 받으며, 받은 쪽 로컬에는 그 방의 기록으로 남는다.


def _safe_name(name):
    """받는 쪽에서 파일로 저장하므로 경로 탈출·제어문자를 서버에서 먼저 막는다."""
    if not isinstance(name, str):
        return None
    name = os.path.basename(name.replace("\\", "/")).strip()
    if not name or name in (".", "..") or any(ord(c) < 32 for c in name):
        return None
    for ch in '<>:"|?*':
        name = name.replace(ch, "_")
    return name[:100]


def handle_file_begin(conn, d):
    room = clean_room_name(d.get("room"))
    name = _safe_name(d.get("name"))
    fid = d.get("fid")
    size = d.get("size")
    if room not in conn.rooms:
        return conn.err("not_joined", "입장하지 않은 방입니다.")
    if not name or not isinstance(fid, str) or not re.fullmatch(r"[0-9a-f]{32}", fid):
        return conn.err("bad_frame", "이미지 정보가 규격에 맞지 않습니다.")
    if not isinstance(size, int) or not (0 < size <= CONFIG["file_max_mb"] * 1024 * 1024):
        return conn.err("file_too_big",
                        f"이미지는 최대 {CONFIG['file_max_mb']}MB 까지 보낼 수 있습니다.")
    if len(conn.tx_files) >= CONFIG["file_max_concurrent"]:
        return conn.err("file_busy", "동시에 보낼 수 있는 이미지 수를 넘었습니다.")

    conn.tx_files[fid] = {"room": room, "size": size, "got": 0}
    with STATE_LOCK:
        seq = SEQS.get(room, 0) + 1
        SEQS[room] = seq
    db_x("UPDATE rooms SET last_msg=? WHERE name=?", (now(), room))
    fanout(room, {"t": "file_begin", "room": room, "from": conn.uid, "fid": fid,
                  "name": name, "size": size, "sha256": d.get("sha256"),
                  "w": d.get("w"), "h": d.get("h"),
                  "mid": f"{RUN_ID}-{seq}", "ts": now()}, exclude=conn)


def handle_file_end(conn, d):
    fid = d.get("fid")
    tr = conn.tx_files.pop(fid, None)
    if not tr:
        return
    fanout(tr["room"], {"t": "file_end", "room": tr["room"], "fid": fid,
                        "ok": tr["got"] == tr["size"]}, exclude=conn)
    # 보낸 쪽에는 도달 확인만 돌려준다(이미 자기 화면에 그려뒀으므로 다시 안 보낸다)
    conn.send({"t": "ok", "of": "file_end", "fid": fid,
               "sent": tr["got"], "size": tr["size"]})


def relay_file_chunk(conn, body):
    """'B' 프레임: [fid 16바이트][seq 4바이트][데이터]. 그대로 중계한다."""
    if len(body) < FILE_HEAD.size:
        return conn.err("bad_frame", "이미지 청크가 너무 짧습니다.")
    raw_fid, _seq = FILE_HEAD.unpack(body[:FILE_HEAD.size])
    fid = raw_fid.hex()
    tr = conn.tx_files.get(fid)
    if not tr:
        return                      # file_begin 없이 온 청크 — 조용히 버린다
    data_len = len(body) - FILE_HEAD.size
    if data_len > FILE_CHUNK_MAX:
        conn.tx_files.pop(fid, None)
        return conn.err("bad_frame", "이미지 청크가 너무 큽니다.")
    tr["got"] += data_len
    if tr["got"] > tr["size"]:
        conn.tx_files.pop(fid, None)
        fanout(tr["room"], {"t": "file_abort", "fid": fid}, exclude=conn)
        return conn.err("file_too_big", "선언한 크기보다 많이 보냈습니다.")
    fanout_bytes(tr["room"], pack_frame("B", body), exclude=conn)


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
    "file_begin": handle_file_begin, "file_end": handle_file_end,
}


def serve_conn(conn):
    """연결 하나를 담당하는 스레드.
    TLS 감싸기를 **여기서** 한다 — accept 루프에서 하면 느린/불량 클라이언트 하나가
    새 접속 수락을 막는다."""
    sock, is_tls = wrap_if_tls(conn.sock)
    if sock is None:
        close_conn(conn, "TLS 처리 실패 또는 거부")
        return
    conn.sock, conn.tls = sock, is_tls
    conn.ready = True

    reason = ""
    while not STOP.is_set() and conn.alive:
        try:
            got = recv_frame(conn.sock)
        except ProtoError as e:
            conn.err("bad_frame", str(e))
            reason = f"규격 위반({e})"
            break
        except OSError as e:
            reason = f"소켓 오류({e})"
            break
        if got is None:
            reason = "상대가 연결을 닫음"
            break
        typ, body = got
        conn.last_rx = now()
        if typ == "B":
            if not conn.uid:
                conn.err("unauth", "로그인이 필요합니다.")
                break
            try:
                relay_file_chunk(conn, body)
            except Exception as e:
                log(f"[경고] {conn.who()} 이미지 청크 중계 실패: {e}")
            continue
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
            reason = "로그아웃"
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
    close_conn(conn, reason or "연결 종료")


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
            elif c.ready:
                # **핸드셰이크가 끝나기 전에는 절대 쓰지 않는다(함정):** TLS 감싸기
                # 도중에 평문 ping 프레임을 끼워 넣으면 그 연결의 record layer가
                # 깨져 접속이 실패한다. ping 주기가 15초라 접속 타이밍에 따라
                # 간헐적으로만 터져서 원인을 찾기 어려웠다
                # (증상: 서버에 [SSL: RECORD_LAYER_FAILURE], 클라이언트엔 EOF).
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


# === [8. 웹 중계 — 브라우저(WSS) ↔ 이 서버] ===
# `https://cheongbaek.github.io/domiman/` 에서 받은 정적 웹앱을 받아주는 부분.
# 예전에는 옆에서 따로 돌던 프로세스(domiweb.py)였고, 그 프로세스는 도로 이 서버에
# 127.0.0.1 로 **TLS 소켓을 열어** 'web' 계정으로 로그인했다. 한 프로그램이 되었으니
# 그 왕복이 통째로 사라진다 — 접속·재접속·지문 고정·계정·비밀번호가 전부 필요 없다.
#
# ■ 왜 '바이트 파이프'가 아니라 '중계'인가 (설계 결정, 그대로 유효)
#   이 서버는 **같은 ID 동시 접속을 불허**한다. 브라우저마다 domichat 로그인을 시키면
#   기기 하나만 쓸 수 있다. 그래서 중계가 연결 **하나**로 자리를 잡고 브라우저 여러
#   대를 그 하나에 다중화한다. 덕분에 **브라우저에는 계정·비밀번호가 아예 실리지
#   않는다**(공개 정적 사이트에 자격을 박는 문제가 구조적으로 없어진다).
#
# ■ 흐름
#   브라우저 ──wss://<호스트>:47822/ws── [웹 중계] ── HubConn ── 이 서버의 방
#                                                                    ▲
#                                              피제어 PC(seoul 등) ──┘
#   - **브라우저가 지목한 PC의 방에만 들어간다**(마지막 사람이 그 PC를 떠나면 방에서
#     나온다). 동시에 여러 PC를 돌릴 일이 없고, 보지도 않는 방의 수량 방송을 계속
#     받을 이유도 없다.
#   - 방 이름·비번·명령 문자열은 domichat.md / domiman.py 규격 그대로다. 중계는
#     `web,Z,...` 응답과 `,Z,F,*`·`,Z,N,*` 방송을 **해석하지 않고 그대로 넘긴다**
#     (파싱은 브라우저가 한다 — 규격의 단일 소유자를 늘리지 않기 위해서).
#   - 예외는 스크린샷뿐이다: 'B' 프레임(이미지 청크)은 여기서 조립해 완성된 PNG를
#     base64로 넘긴다(브라우저에 이진 프레임 조립 로직을 또 두지 않는다).
#
# ■ 브라우저 ↔ 중계 프레임 (WebSocket, JSON 텍스트) — **웹앱과 맞춰진 규격이라
#   한 글자도 바꾸지 않는다.** 웹앱은 GitHub Pages에 이미 배포돼 있어 서버만 고칠 수
#   있는 처지이므로, 여기를 손대면 옛 페이지가 조용히 깨진다.
#   받는 것: {"t":"hello"} / {"t":"select","pc":..} / {"t":"cmd","pc":..,"body":"S"}
#            {"t":"add_pc","pc":..} / {"t":"del_pc","pc":..} / {"t":"pong"}
#   주는 것: {"t":"ready","my_id":..,"pcs":[..],"connected":bool,"version":..}
#            {"t":"snap","pc":..,...}              — 그 PC의 마지막 상태
#            {"t":"msg","pc":..,"body":..}         — 방에서 온 원문 그대로
#            {"t":"pcs","pcs":[..]}                — 목록 변경
#            {"t":"pc","pc":..,"online":bool|null,"joined":bool,"reason":..}
#            {"t":"up","connected":bool,"msg":..}  — 중계 가동 상태
#            {"t":"shot","pc":..,"ok":bool,"name":..,"b64":..,"reason":..}
#            {"t":"err","msg":..}

FISHING_ROOM_PREFIX = "domi_fishing_"     # domichat.md / domiman.py 규격
FISHING_ROOM_PW = "domi_fishing_9714"

# 발신 스로틀 — 서버의 도배 제한(MSG_BURST=20 / MSG_WINDOW=10초)은 **연결 하나당**
# 걸리는데, 브라우저 여러 대의 명령이 이 중계 연결 하나로 합쳐진다. 한 프로그램이
# 되었어도 handle_msg 를 그대로 통과하므로 이 여유는 그대로 필요하다.
WEB_SEND_BURST, WEB_SEND_WINDOW = 12, 10.0

WEB_SHOT_MAX_BYTES = 16 * 1024 * 1024   # 스크린샷 상한(실제 2MB 남짓). 메모리 보호
WEB_SHOT_WAIT_SEC = 40.0                # 브라우저의 사진 대기 유효시간
WEB_ROOM_RETRY_SEC = 60.0               # 방이 없던 PC를 다시 찾아보는 주기
WEB_PING_SEC = 20.0
WEB_MAX_RX = 64 * 1024                  # 브라우저가 보내는 프레임 상한(명령뿐이다)
WEB_LOG_BACKLOG = 30                    # PC별로 보관하는 최근 원문 수(새 브라우저용)
WEB_CONN_ADDR = "내장 웹중계"           # 관리 화면의 주소 칸에 뜨는 이름

# 명령 화이트리스트. 누구나 붙을 수 있는 공개 중계이므로, 방에 흘려보낼 수 있는
# 문자열을 **domiman 명령 규격으로만** 제한한다(채팅방 스팸 통로가 되지 않게).
WEB_CMD_RE = re.compile(r"[SGPYWQVTCNI](,[A-Za-z0-9.\-]{1,12}){0,3}")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

WEB_HUB = None                  # 가동 중이면 WebHub
WEB_SSL_CTX = None              # 브라우저용 SSLContext(공인 인증서). 없으면 평문 ws
_web_cert_lock = threading.Lock()
_web_cert_sig_seen = None       # (인증서 mtime, 키 mtime) — 갱신 감지용


def fishing_room_of(uid):
    return f"{FISHING_ROOM_PREFIX}{uid}"


def pc_of_fishing_room(room):
    if isinstance(room, str) and room.startswith(FISHING_ROOM_PREFIX):
        return room[len(FISHING_ROOM_PREFIX):] or None
    return None


# --- [8-1. 내부 연결 — 중계가 서버 안에서 쓰는 가상 Conn] ---


class HubConn(Conn):
    """웹 중계의 **가상 연결**. 소켓 대신 큐 하나를 달고 있을 뿐, 서버가 보기에는
    평범한 로그인된 연결이다.

    **웹 전용 경로를 만들지 않으려고 이렇게 했다.** 입장 자격·방 비밀번호·팬아웃·
    도배 제한·이미지 중계를 handle_* 가 그대로 처리하므로, 방 규칙이 바뀌어도 중계가
    따라 바뀐다(규칙이 두 곳에 생기지 않는다).

    `handle_login` 을 거치지 않는 이유는 계정이 필요 없기 때문이다 — 이 연결은
    비밀번호로 자신을 증명할 상대가 아니라 서버 자신의 일부다. 대신 ONLINE 에 이름을
    올려 **바깥에서 같은 ID로 로그인하는 것을 막는다**(중계를 사칭할 통로를 남기지
    않는다)."""

    def __init__(self, uid, q):
        super().__init__(None, (WEB_CONN_ADDR, 0))
        self.uid = uid
        self.q = q
        self.ready = True

    def who(self):
        return f"{self.uid}(웹 중계)"

    def send(self, obj):
        if not self.alive:
            return False
        self.q.put(obj)
        return True

    def send_bytes(self, data):
        """팬아웃이 흘려보내는 'B' 프레임(이미지 청크)을 풀어서 큐에 넣는다 —
        바깥 클라이언트가 소켓에서 하던 일을 그대로 여기서 한다."""
        if not self.alive:
            return False
        if len(data) < FRAME_HEAD.size + FILE_HEAD.size:
            return False
        body = data[FRAME_HEAD.size:]
        raw_fid, seq = FILE_HEAD.unpack(body[:FILE_HEAD.size])
        self.q.put({"t": "bin", "fid": raw_fid.hex(), "seq": seq,
                    "data": body[FILE_HEAD.size:]})
        return True


# --- [8-2. WebSocket — 핸드셰이크 · 프레임 코덱 (RFC 6455, 표준 라이브러리만)] ---


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
    if ln > WEB_MAX_RX:
        raise WSClosed(f"프레임 과대({ln})")
    if not masked:
        raise WSClosed("마스킹되지 않은 클라이언트 프레임")
    key = reader.take(4)
    data = bytearray(reader.take(ln))
    for i in range(ln):
        data[i] ^= key[i & 3]
    return opcode, bytes(data)


WEB_HTTP_INFO = (
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
        body = (f"domiserver {APP_VERSION} 웹 중계 — 살아 있습니다."
                f" 웹앱에서 /ws 로 접속하세요.\n")
        sock.sendall(WEB_HTTP_INFO.format(
            n=len(body.encode()), body=body).encode("utf-8"))
        return False

    origins = CONFIG["web_origins"]
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


# --- [8-3. 브라우저 연결] ---


class BrowserConn:
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
        threading.Thread(target=self._tx_loop, daemon=True, name="web-tx").start()

    def who(self):
        return f"{self.addr[0]}:{self.addr[1]}"

    def send(self, obj):
        if self.alive:
            self.txq.put(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _tx_loop(self):
        last_ping = now()
        while self.alive:
            try:
                data = self.txq.get(timeout=0.5)
            except queue.Empty:
                data = None
            try:
                if data is not None:
                    self.sock.sendall(ws_frame(0x1, data))
                if now() - last_ping >= WEB_PING_SEC:
                    self.sock.sendall(ws_frame(0x9))    # ping — 브라우저가 자동 pong
                    last_ping = now()
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


# --- [8-4. 중계 허브] ---


class WebHub:
    """가상 연결 하나(HubConn) + 브라우저 여러 대. 모든 상태 변경은 이 객체를 거친다.

    서버에서 올라온 메시지는 **해석하지 않고 그대로** 브라우저에 넘기는 것이
    원칙이다. 다만 나중에 접속한 브라우저에게 '지금 상태'를 즉시 그려주려면 마지막
    값이 필요하므로, 접두어 세 가지(상태 응답 / 수량 방송 / 보고)만 구분해 캐시한다.
    파싱은 브라우저가 한다 — 규격의 소유자를 늘리지 않는다."""

    def __init__(self, uid):
        self.uid = uid
        self.q = queue.Queue()          # HubConn 이 받은 프레임
        self.conn = HubConn(uid, self.q)
        self.lock = threading.RLock()
        self.clients = set()
        self.state = {}          # pc -> dict
        self.files = {}          # fid -> 조립 중인 이미지
        self.txq = queue.Queue()  # (프레임, 로그라벨|None) — 스로틀 통과 대기
        self.last_query = {}     # pc -> 마지막 S 질의 시각(중복 억제)
        for pc in CONFIG["web_pcs"]:
            self._ensure_state(pc)

    # ---------- 상태 ----------
    def _ensure_state(self, pc):
        return self.state.setdefault(pc, {
            "joined": False, "online": None, "reason": "",
            "status": None, "tank": None,
            "reports": deque(maxlen=WEB_LOG_BACKLOG),
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
            targets = [c for c in self.clients if pc is None or c.pc == pc]
        for c in targets:
            c.send(obj)

    def add_client(self, conn):
        with self.lock:
            self.clients.add(conn)
            n = len(self.clients)
        log(f"[웹] 접속 {conn.who()} (총 {n}명)")

    def drop_client(self, conn):
        with self.lock:
            self.clients.discard(conn)
            n = len(self.clients)
        log(f"[웹] 해제 {conn.who()} (총 {n}명)")
        self._maybe_leave(conn.pc)      # 마지막 사람이 나가면 그 방도 뜬다

    # ---------- 브라우저 → 서버 ----------
    def submit(self, obj, label=None):
        """서버 프레임을 중계 연결로 내보낸다. label 이 있으면 도배 제한을 거치고
        그 문구를 로그에 남긴다(= 사람이 누른 명령)."""
        self.txq.put((obj, label))

    def queue_cmd(self, pc, body):
        self.submit({"t": "msg", "room": fishing_room_of(pc), "body": f"{pc},{body}"},
                    label=f"{pc},{body}")

    def _uplink_loop(self):
        """중계 연결로 나가는 프레임을 **한 줄로 세워** 처리한다.

        스레드를 하나로 묶는 이유는 handle_msg 의 도배 카운터(conn.msg_times)가
        연결 하나를 자기 스레드만 건드린다고 보고 짜여 있기 때문이다 — 실제 연결이
        그렇듯 여기도 한 스레드만 그 연결을 쓴다."""
        times = deque()
        while not STOP.is_set():
            try:
                obj, label = self.txq.get(timeout=0.5)
            except queue.Empty:
                continue
            if label is not None:
                while not STOP.is_set():
                    t = now()
                    while times and t - times[0] > WEB_SEND_WINDOW:
                        times.popleft()
                    if len(times) < WEB_SEND_BURST:
                        break
                    STOP.wait(min(1.0, WEB_SEND_WINDOW - (t - times[0]) + 0.05))
                times.append(now())
            if not self.conn.alive:
                self.broadcast({"t": "err", "msg": "서버가 종료되는 중입니다."})
                continue
            t = obj.get("t")
            try:
                HANDLERS[t](self.conn, obj)
            except Exception as e:
                log(f"[웹] '{t}' 처리 실패: {e}")
                continue
            if label is not None:
                log(f"[발신] {label}")

    def on_web(self, conn, d):
        t = d.get("t")
        if t == "hello":
            conn.send(self.ready_frame())
            return
        if t == "select":
            pc = (d.get("pc") or "").strip()
            if pc and pc not in CONFIG["web_pcs"]:
                return conn.send({"t": "err", "msg": "목록에 없는 PC입니다."})
            old_pc, conn.pc = conn.pc, pc
            if old_pc and old_pc != pc:
                self._maybe_leave(old_pc)
            if not pc:
                return
            conn.send(self.snapshot(pc))
            if not self._ensure_state(pc)["joined"]:
                self._ensure_join(pc)
            elif now() - self.last_query.get(pc, 0) > 5.0:
                # 이미 들어가 있는 방이면 상태만 한 번 맞춘다(5초 내 중복은 생략).
                self.last_query[pc] = now()
                self.queue_cmd(pc, "S")
            return
        if t == "cmd":
            pc = (d.get("pc") or "").strip()
            body = (d.get("body") or "").strip()
            if pc not in CONFIG["web_pcs"]:
                return conn.send({"t": "err", "msg": "목록에 없는 PC입니다."})
            if not WEB_CMD_RE.fullmatch(body):
                return conn.send({"t": "err", "msg": f"규격 밖 명령입니다: {body}"})
            if not self._ensure_state(pc)["joined"]:
                # 방에 못 들어간 상태로 보내면 서버가 not_joined로 되돌려준다.
                self._ensure_join(pc)
                return conn.send(
                    {"t": "err", "msg": f"'{pc}'의 방에 아직 들어가지 못했습니다."})
            if body == "I":
                conn.shot_wait = now()
            self.queue_cmd(pc, body)
            return
        if t in ("add_pc", "del_pc"):
            return self._edit_pcs(conn, t, (d.get("pc") or "").strip())
        if t == "pong":
            return

    def ready_frame(self):
        return {"t": "ready", "my_id": self.uid, "pcs": list(CONFIG["web_pcs"]),
                "connected": self.conn.alive, "version": APP_VERSION}

    # 목록 편집은 브라우저(add_pc/del_pc 프레임)와 콘솔(web add/del)이 **같은 두
    # 메서드**를 쓴다 — 목록을 고치는 규칙의 주인을 둘로 늘리지 않는다.
    def add_pc(self, pc):
        """(성공 여부, 안내문). 실패해도 목록은 손대지 않는다."""
        if not valid_id(pc):
            return False, "PC 이름 형식이 아닙니다."
        if pc in CONFIG["web_pcs"]:
            return False, "이미 목록에 있습니다."
        CONFIG["web_pcs"] = list(CONFIG["web_pcs"]) + [pc]
        save_config()
        self._ensure_state(pc)     # 입장은 브라우저가 그 PC를 고를 때 한다
        self.broadcast({"t": "pcs", "pcs": list(CONFIG["web_pcs"])})
        return True, f"'{pc}' 추가"

    def del_pc(self, pc):
        if pc not in CONFIG["web_pcs"]:
            return False, "목록에 없습니다."
        CONFIG["web_pcs"] = [x for x in CONFIG["web_pcs"] if x != pc]
        save_config()
        with self.lock:
            for c in self.clients:
                if c.pc == pc:
                    c.pc = ""
        st = self.state.pop(pc, None)
        if st and st["joined"]:
            self.submit({"t": "sub", "room": fishing_room_of(pc), "on": False})
            self.submit({"t": "leave", "room": fishing_room_of(pc)})
        self.broadcast({"t": "pcs", "pcs": list(CONFIG["web_pcs"])})
        return True, f"'{pc}' 삭제"

    def _edit_pcs(self, conn, t, pc):
        ok, msg = (self.add_pc if t == "add_pc" else self.del_pc)(pc)
        if not ok:
            return conn.send({"t": "err", "msg": msg})
        log(f"[웹 목록] {msg} ({conn.who()})")

    # ---------- 방 입장 · 퇴장 ----------
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
        수량 방송(사이클마다)을 계속 받고, 그 방 참가자 목록에도 중계가 늘 떠
        있게 된다. 동시에 여러 PC를 돌릴 일이 없으므로 필요할 때만 붙는다."""
        st = self._ensure_state(pc)
        if st["joined"]:
            return
        room = fishing_room_of(pc)
        if not room_row(room):
            # 그 PC가 아직 domichat 에 붙은 적이 없어 방이 없다. 방이 생기면
            # room_new 통지가 이 연결에도 오므로 그때 다시 시도한다(아래 _handle_up).
            if st["reason"] != "no_room":
                st["reason"] = "no_room"
                self._notify_pc(pc)
            return
        self.submit({"t": "join", "room": room, "pw": FISHING_ROOM_PW})

    def _maybe_leave(self, pc):
        """보는 사람이 아무도 없으면 그 방에서 나온다. 캐시도 버린다 — 다시 들어갈
        때 옛 상태를 잠깐 보여주면 '지금 값'으로 오해하게 된다(입장 직후 S 질의로
        새로 받는다)."""
        st = self.state.get(pc) if pc else None
        if st is None or not st["joined"] or self._watchers(pc):
            return
        self.submit({"t": "sub", "room": fishing_room_of(pc), "on": False})
        self.submit({"t": "leave", "room": fishing_room_of(pc)})
        st.update({"joined": False, "online": None, "reason": "",
                   "status": None, "tank": None})
        st["reports"].clear()
        self.last_query.pop(pc, None)
        log(f"[웹] '{pc}' 방에서 나왔습니다(보는 사람 없음).")

    def _rejoin_watched(self):
        """지금 누군가 보고 있는 PC 중 아직 못 들어간 방에 다시 붙어 본다."""
        for pc in self._watched_pcs():
            self._ensure_join(pc)

    # ---------- 서버 → 브라우저 ----------
    def _pump_loop(self):
        while not STOP.is_set():
            try:
                d = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            # 서버의 생존 확인(maintenance_loop)에 답하는 자리. 실제 연결이
            # serve_conn 에서 하는 일과 같다 — 안 하면 45초 뒤 회수당한다.
            self.conn.last_rx = now()
            try:
                self._handle_up(d)
            except Exception as e:
                log(f"[웹] 서버 프레임 처리 실패: {e}")

    def _handle_up(self, d):
        t = d.get("t")
        if t == "joined":
            pc = pc_of_fishing_room(d.get("room"))
            if pc:
                st = self._ensure_state(pc)
                st["joined"], st["reason"] = True, ""
                self.submit({"t": "sub", "room": d.get("room"), "on": True})
                log(f"[웹] '{pc}' 방에 입장했습니다.")
                self.broadcast({"t": "pc", "pc": pc, "joined": True,
                                "online": st["online"], "reason": ""})
                self.queue_cmd(pc, "S")
                self.last_query[pc] = now()
            return
        if t == "denied":
            pc = pc_of_fishing_room(d.get("room"))
            if pc:
                st = self._ensure_state(pc)
                st["joined"], st["reason"] = False, d.get("reason") or "denied"
                log(f"[웹] '{pc}' 방 입장 거절: {d.get('reason')} {d.get('msg') or ''}")
                self.broadcast({"t": "pc", "pc": pc, "joined": False,
                                "online": None, "reason": st["reason"]})
            return
        if t == "member":
            pc = pc_of_fishing_room(d.get("room"))
            if pc and d.get("id") == pc:
                st = self._ensure_state(pc)
                st["online"] = bool(d.get("in"))
                log(f"[웹] '{pc}' {'접속' if st['online'] else '접속 종료'}")
                self.broadcast({"t": "pc", "pc": pc, "joined": st["joined"],
                                "online": st["online"], "reason": st["reason"]})
            return
        if t == "room_new":
            # 피제어 PC가 이제 막 켜져 자기 방을 만들었다. 예전에는 목록을 주기적으로
            # 다시 받아 봐야 알 수 있었는데, 한 프로그램이 되면서 이 통지가 그대로
            # 들어온다 — 기다리지 않고 바로 붙는다(주기 재시도는 안전망으로 남긴다).
            if pc_of_fishing_room(d.get("room")):
                self._rejoin_watched()
            return
        if t == "room_deleted":
            pc = pc_of_fishing_room(d.get("room"))
            if pc:
                st = self._ensure_state(pc)
                st["joined"], st["online"] = False, None
                st["reason"] = "room_deleted"
                self.broadcast({"t": "pc", "pc": pc, "joined": False, "online": None,
                                "reason": "room_deleted"})
            return
        if t == "msg":
            return self._on_room_msg(d)
        if t in ("file_begin", "bin", "file_end", "file_abort"):
            return self._on_file(t, d)
        if t == "error":
            code = d.get("code")
            log(f"[웹 오류] {code}: {d.get('msg')}")
            if code in ("room_missing", "not_joined"):
                # 방이 사라졌거나 입장이 풀렸다. 다음 select/재시도에서 다시 붙는다.
                for pc in list(self.state):
                    st = self.state[pc]
                    if st["joined"]:
                        st["joined"] = False
                        self._notify_pc(pc)
            return

    def _on_room_msg(self, d):
        frm, body = d.get("from"), (d.get("body") or "").strip()
        pc = pc_of_fishing_room(d.get("room"))
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
            if parts[0] == self.uid and parts[2] not in (
                    "N", "F", "G", "P", "W", "Q", "Y", "I"):
                st["status"] = body
            elif parts[0] == "" and parts[2] == "N":
                st["tank"] = body
            elif parts[0] == "" and parts[2] == "F":
                st["reports"].append([round(now(), 3), body])
                log(f"[웹 보고] {pc}: {body}")
        self.broadcast({"t": "msg", "pc": pc, "body": body}, pc=pc)

    # ---------- 스크린샷 조립 ----------
    def _on_file(self, t, d):
        fid = d.get("fid")
        if t == "file_begin":
            pc = pc_of_fishing_room(d.get("room"))
            if not pc or d.get("from") != pc:
                return
            size = int(d.get("size") or 0)
            if size <= 0 or size > WEB_SHOT_MAX_BYTES:
                return log(f"[사진] {pc}: 크기가 규격 밖({size})이라 버립니다.")
            self.files[fid] = {"pc": pc, "size": size, "sha256": d.get("sha256"),
                               "name": d.get("name") or "screenshot.png",
                               "buf": bytearray(), "t0": now()}
            log(f"[사진] {pc}: 수신 시작 ({size/1048576:.2f}MB)")
            return
        f = self.files.get(fid)
        if f is None:
            return
        if t == "bin":
            f["buf"] += d.get("data", b"")
            if len(f["buf"]) > f["size"]:
                self.files.pop(fid, None)
                self._shot_fail(f["pc"], "too_big")
            return
        if t == "file_abort":
            self.files.pop(fid, None)
            return self._shot_fail(f["pc"], "aborted")

        # file_end — 크기·해시를 확인한 뒤 완성된 PNG를 base64로 넘긴다.
        self.files.pop(fid, None)
        png = bytes(f["buf"])
        ok = len(png) == f["size"]
        if ok and f["sha256"]:
            ok = hashlib.sha256(png).hexdigest() == f["sha256"]
        if not ok:
            return self._shot_fail(f["pc"], "corrupt")
        b64 = base64.b64encode(png).decode("ascii")
        sent = self._to_waiters(f["pc"], {"t": "shot", "pc": f["pc"], "ok": True,
                                          "name": f["name"], "b64": b64})
        log(f"[사진] {f['pc']}: {len(png)/1048576:.2f}MB 전달 ({sent}명)")

    def _shot_fail(self, pc, reason):
        log(f"[사진] {pc}: 실패({reason})")
        self._to_waiters(pc, {"t": "shot", "pc": pc, "ok": False, "reason": reason})

    def _to_waiters(self, pc, obj):
        """사진은 **요청한 브라우저에게만** 준다 — 남이 찍은 사진이 갑자기 뜨면
        안 되고, 3MB짜리를 안 기다리는 브라우저에 밀어넣을 이유도 없다."""
        t = now()
        with self.lock:
            waiters = [c for c in self.clients
                       if c.pc == pc and 0 < t - c.shot_wait <= WEB_SHOT_WAIT_SEC]
        for c in waiters:
            c.shot_wait = 0.0
            c.send(obj)
        return len(waiters)

    # ---------- 유지보수 ----------
    def _maint_loop(self):
        last_retry = 0.0
        while not STOP.wait(1.0):
            t = now()
            for fid, f in list(self.files.items()):
                if t - f["t0"] > 120:
                    self.files.pop(fid, None)
                    self._shot_fail(f["pc"], "timeout")
            if t - last_retry >= WEB_ROOM_RETRY_SEC:
                last_retry = t
                # 보고 있는 PC 중 아직 못 들어간 방이 있으면 다시 붙어 본다(그 PC가
                # 뒤늦게 켜지면 방이 생긴다). room_new 통지를 놓쳤을 때의 안전망.
                self._rejoin_watched()

    # ---------- 가동 ----------
    def start(self):
        with STATE_LOCK:
            CONNS.add(self.conn)
            ONLINE[self.uid] = self.conn
        for fn, name in ((self._pump_loop, "web-pump"),
                         (self._uplink_loop, "web-uplink"),
                         (self._maint_loop, "web-maint")):
            threading.Thread(target=fn, daemon=True, name=name).start()
        self.broadcast({"t": "up", "connected": True, "msg": "연결됨"})


# --- [8-5. 브라우저용 TLS · 수신 대기] ---


def web_build_tls():
    """브라우저용 TLS 컨텍스트를 만든다(공인 인증서 — 자체 서명으로는 브라우저가
    붙지 않는다). **1.2로 고정한다** — 연결마다 수신 스레드와 송신 스레드가 한
    소켓을 나눠 쓰므로, 1.3의 핸드셰이크 후 메시지가 record layer를 깨는 문제를
    이미 겪었다(_pin_tls12 의 설명과 같은 이유, 같은 구조)."""
    cert, key = CONFIG["web_certfile"], CONFIG["web_keyfile"]
    if not cert or not key:
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
    except Exception as e:
        log(f"[웹 TLS] 인증서를 읽지 못했습니다: {e}")
        return None
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    _pin_tls12(ctx)
    log(f"[웹 TLS] 인증서 적재: {cert}")
    return ctx


def _web_cert_sig_now():
    try:
        return (os.path.getmtime(CONFIG["web_certfile"]),
                os.path.getmtime(CONFIG["web_keyfile"]))
    except OSError:
        return None


def web_tls_ctx():
    """새 연결마다 인증서 파일의 mtime을 보고 **바뀌었으면 다시 적재한다.**
    Let's Encrypt 인증서는 60~90일마다 갱신되고(Posh-ACME가 같은 경로에 덮어쓴다),
    그때 사람이 서버를 재시작해야 하는 구조라면 어느 날 조용히 만료된다.
    적재에 실패하면 **직전 컨텍스트를 그대로 쓴다** — 갱신 도중의 반쪽 파일을
    읽었다고 서비스를 멈출 이유가 없다."""
    global WEB_SSL_CTX, _web_cert_sig_seen
    if not CONFIG["web_certfile"] or not CONFIG["web_keyfile"]:
        return None
    with _web_cert_lock:
        sig = _web_cert_sig_now()
        if WEB_SSL_CTX is None or (sig is not None and sig != _web_cert_sig_seen):
            ctx = web_build_tls()
            if ctx is not None:
                WEB_SSL_CTX, _web_cert_sig_seen = ctx, sig
        return WEB_SSL_CTX


def serve_web(sock, addr):
    """브라우저 연결 하나. TLS 감싸기를 **여기서** 한다(accept 루프에서 하면
    불량 클라이언트 하나가 새 접속 수락을 막는다 — serve_conn 과 같은 이유)."""
    conn = None
    try:
        ctx = web_tls_ctx()
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
        conn = BrowserConn(WEB_HUB, sock, addr)
        WEB_HUB.add_client(conn)
        conn.send(WEB_HUB.ready_frame())
        conn.serve(reader)
    except (WSClosed, OSError, ssl.SSLError) as e:
        if conn is not None:
            log(f"[웹] {conn.who()} 종료 — {e}")
    except Exception as e:
        log(f"[웹] 처리 중 오류: {e}")
    finally:
        if conn is not None:
            WEB_HUB.drop_client(conn)
            conn.close()
        else:
            try:
                sock.close()
            except Exception:
                pass


def web_accept_loop(srv):
    while not STOP.is_set():
        try:
            sock, addr = srv.accept()
        except OSError:
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=serve_web, args=(sock, addr), daemon=True,
                         name="web-conn").start()


def start_web_relay():
    """웹 중계를 켠다. 켜지 않기로 돼 있거나 포트를 못 열면 None을 돌려주고
    **서버 본체는 그대로 계속 돈다** — 채팅 중계가 웹 때문에 죽어서는 안 된다.
    (관리 창 domiserver_gui.py 도 이 함수 하나만 부르면 된다.)"""
    global WEB_HUB
    if not CONFIG["web"]:
        log("[웹] 설정에서 꺼져 있습니다 — 브라우저 중계를 열지 않습니다.")
        return None
    uid = CONFIG["web_id"] if valid_id(CONFIG["web_id"]) else "web"
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((CONFIG["web_host"], CONFIG["web_port"]))
        srv.listen(16)
    except OSError as e:
        log(f"[웹] 포트 {CONFIG['web_port']} 를 열 수 없어 중계를 건너뜁니다: {e}")
        return None

    WEB_HUB = WebHub(uid)
    WEB_HUB.start()
    threading.Thread(target=web_accept_loop, args=(srv,), daemon=True,
                     name="web-accept").start()
    scheme = "wss" if web_tls_ctx() is not None else "ws"
    if scheme == "ws":
        log("[웹 TLS] 인증서가 없어 **평문 ws**로 엽니다 — 로컬 개발용이며 "
            "https 페이지(GitHub Pages)에서는 접속되지 않습니다.")
    log(f"[웹] {scheme}://<호스트>:{CONFIG['web_port']}/ws 로 브라우저를 받습니다"
        f" (중계 ID '{uid}').")
    log(f"[웹] 피제어 PC: {', '.join(CONFIG['web_pcs']) or '(없음)'}   (콘솔: web)")
    return srv


# === [9. 콘솔 — 계정 · 채팅방 · 웹 중계 관리] ===

HELP = """\
계정   users | pending | approve <ID> | reject <ID> | deluser <ID>
       disable <ID> | enable <ID> | online | kick <ID>
채팅방 rooms | room <이름> | delroom <이름>
       delrooms all|open|limited|owner <ID>   (일괄 삭제, 확인 후 진행)
웹중계 web | web add <PC ID> | web del <PC ID>
기타   addr | cert | set <키> <값> | config | help | quit"""


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
        tls = {uid: c.tls for uid, c in items}
    if not items:
        return print("접속 중인 사용자가 없습니다.")
    for uid, c in sorted(items):
        r = ", ".join(rooms[uid]) or "-"
        print(f"  {uid:<20} {c.addr[0]:<16} {'TLS' if tls[uid] else '평문':<4} 방: {r}")


def cmd_cert(_):
    """클라이언트가 고정한 지문과 맞춰볼 때 쓴다."""
    if SSL_CTX is None:
        return print("TLS를 쓰지 않습니다(설정 tls=false 또는 인증서 없음).")
    print(f"인증서 : {CERT_PATH}")
    print(f"지문   : {CERT_FP}")
    print("클라이언트가 처음 접속할 때 이 지문을 기억하며, 이후 바뀌면 접속을 거부합니다."
          " 서버를 재설치해 인증서가 바뀌면 클라이언트에서 재신뢰가 필요합니다.")


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


def cmd_web(args):
    """웹 중계 상태 보기와 피제어 PC 목록 편집.
    목록 편집은 브라우저가 쓰는 것과 **같은 메서드**(add_pc/del_pc)를 부른다."""
    if WEB_HUB is None:
        return print("  웹 중계가 꺼져 있습니다 (set web 1 후 재시작).")
    if not args:
        scheme = "wss" if WEB_SSL_CTX is not None else "ws"
        with WEB_HUB.lock:
            clients = sorted((c.who(), c.pc) for c in WEB_HUB.clients)
        print(f"  수신     : {scheme}://<호스트>:{CONFIG['web_port']}/ws"
              f"   중계 이름 '{WEB_HUB.uid}'")
        print(f"  브라우저 : {len(clients)}명")
        for who, pc in clients:
            print(f"     {who}  보는 PC={pc or '-'}")
        print("  피제어 PC:")
        for pc in CONFIG["web_pcs"]:
            st = WEB_HUB.state.get(pc, {})
            on = {True: "접속중", False: "꺼짐", None: "모름"}[st.get("online")]
            print(f"     {pc:<14} 입장={'O' if st.get('joined') else 'X'}  {on}"
                  f"  {st.get('reason') or ''}")
        return
    op, pc = args[0].lower(), (args[1] if len(args) > 1 else "")
    if op not in ("add", "del") or not pc:
        return print("사용법: web | web add <PC ID> | web del <PC ID>")
    ok, msg = (WEB_HUB.add_pc if op == "add" else WEB_HUB.del_pc)(pc)
    if not ok:
        return print(f"  {msg}")
    log(f"[웹 목록] {msg} (콘솔)")
    print(f"  목록: {', '.join(CONFIG['web_pcs']) or '(없음)'}")


COMMANDS = {
    "addr": cmd_addr, "cert": cmd_cert,
    "users": cmd_users, "pending": cmd_pending, "approve": cmd_approve,
    "reject": cmd_reject, "deluser": cmd_deluser, "disable": cmd_disable,
    "enable": cmd_enable, "online": cmd_online, "kick": cmd_kick,
    "rooms": cmd_rooms, "room": cmd_room, "delroom": cmd_delroom,
    "delrooms": cmd_delrooms, "web": cmd_web,
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


# === [10. 관리 창 (tkinter, Windows) — 옛 domiserver_gui.py] ===
# 콘솔(repl)이 하던 관리 작업을 창으로 옮긴 것. **서버 로직을 다시 구현하지 않는다**
# — 관리 동작은 되도록 위 [9. 콘솔] 의 COMMANDS 를 `run_console` 로 **그대로 부르고**
# 출력만 로그 칸으로 옮긴다(같은 일을 두 벌 구현하면 언젠가 어긋난다).
#
# 지켜야 할 것들(고치기 전에 읽을 것):
# - **서버는 이 창이 떠 있는 동안 상시 가동**한다 — 켜고 끄는 버튼을 두지 않는다.
#   창을 닫으면 서버도 함께 멈춘다.
# - 콘솔이 `input()` 으로 되묻는 `delrooms` 만 창으로 다시 만들었다(그대로 부르면
#   GUI 스레드가 입력을 기다리며 멈춘다).
# - 로그는 `log` 를 `gui_log` 로 갈아 끼워 가로챈다. **`install_gui_log()` 는 창을
#   띄울 때만 부른다** — 모듈을 읽자마자 갈아 끼우면 `--console` 로 띄웠을 때도
#   창용 큐에만 쌓여 화면에 아무것도 안 나온다.
# - `pythonw` 로 띄우면 표준 출력이 없어 `print` 가 그대로 터지므로 원래 stdout 이
#   있을 때만 쓴다. 그리고 `sys.__stdout__` 에 직접 쓴다 — run_console 의 출력
#   가로채기(redirect_stdout)에 다른 스레드의 로그가 휩쓸려 들어가지 않게.
# - 방 보기는 `ROOM_OBSERVER` 훅으로 받는다. 서버는 여전히 대화를 저장하지 않으므로
#   **창을 연 뒤에 오간 대화만** 보인다(설계상 공백).
# - 서버 스레드에서 tkinter 를 직접 건드리면 안 된다. 로그·방 이벤트는 큐에 넣고
#   GUI 스레드의 `after` 가 꺼내 그린다.
# - 웹 중계('웹 중계' 칸)는 WEB_HUB 를 **읽기만** 하고, 목록 편집은 브라우저·콘솔과
#   같은 `add_pc`/`del_pc` 를 부른다(목록 규칙의 주인을 늘리지 않는다).


# === [10-1. 창 상수] ===

LOG_MAX_LINES = 3000      # 로그 창 보관 줄 수(넘으면 위에서부터 버린다)
CHAT_MAX_LINES = 2000     # 방 보기 창 보관 줄 수
REFRESH_MS = 1000         # 목록 갱신 주기 — 상태는 그때그때 DB·메모리에서 읽는다
DRAIN_MS = 200            # 로그·방 이벤트 큐를 비우는 주기

MONO = ("Consolas", 9)

# 설정 창에 띄울 설명. '재시작' 표시가 붙은 값은 기동할 때 한 번만 쓰이는 것들이다
# (port=bind, tls/require_tls=setup_tls). 나머지는 서버가 매번 CONFIG 를 다시
# 보므로 저장 즉시 반영된다.
CONFIG_HINT = {
    "port": "서버 포트 · 바꾸면 재시작해야 적용",
    "max_rooms": "채팅방 최대 개수",
    "public_room_ttl_days": "공개방 자동 삭제 기준(일) · 0이면 자동 삭제 없음",
    "msg_max_len": "메시지 최대 길이(글자)",
    "ping_sec": "생존 확인 주기(초)",
    "pong_timeout_sec": "무응답 판정 시간(초)",
    "tls": "TLS 사용 · 바꾸면 재시작해야 적용",
    "require_tls": "평문 접속 거부 · 바꾸면 재시작해야 적용",
    "file_max_mb": "이미지 한 장 최대 크기(MB)",
    "file_max_concurrent": "한 연결이 동시에 보낼 수 있는 이미지 수",
    "web": "브라우저 웹 중계 사용 · 바꾸면 재시작해야 적용",
    "web_host": "웹 중계가 들을 주소 · 재시작",
    "web_port": "웹 중계 포트(브라우저 wss) · 재시작",
    "web_id": "웹 중계가 방에서 쓰는 이름 · 재시작",
    "web_certfile": "브라우저가 신뢰하는 인증서(fullchain) · 비면 평문 ws",
    "web_keyfile": "그 개인키 · 비면 평문 ws",
}


# === [10-2. 로그 다리 — domiserver.log 가로채기] ===

LOG_Q = queue.Queue()


def gui_log(msg):
    """`domiserver.log` 대체. 서버 스레드에서 불리므로 큐에만 넣고 돌아온다."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG_Q.put(line)
    out = sys.__stdout__          # redirect_stdout 에 휩쓸리지 않도록 원본에 직접
    if out is None:               # pythonw 로 띄우면 표준 출력이 아예 없다
        return
    try:
        out.write(line + "\n")
        out.flush()
    except Exception:
        pass


def install_gui_log():
    """서버 로그를 창으로 돌린다. **창을 띄울 때만** 부른다 —
    모듈을 읽자마자 갈아 끼우면 콘솔(`--console`)로 띄웠을 때도 창용 큐에만
    쌓여 화면에 아무것도 안 나온다."""
    global log
    log = gui_log


def put_log(msg):
    """창에서 만든 안내를 서버 로그와 같은 모양으로 남긴다."""
    LOG_Q.put(f"[{time.strftime('%H:%M:%S')}] {msg}")


def run_console(name, *args):
    """콘솔 명령을 **그대로** 실행하고 그 출력(print)을 로그로 옮긴다.
    관리 동작을 창에서 다시 구현하지 않기 위한 통로다 — 동작도 문구도 콘솔과 같다."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            COMMANDS[name]([str(a) for a in args])
    except Exception as e:
        put_log(f"명령 실패: {name} — {e}")
    for line in buf.getvalue().splitlines():
        if line.strip():
            put_log(line)
    return buf.getvalue()


# === [10-3. 서버 기동 · 정지] ===


def port_busy(port):
    """그 포트에서 이미 누가 응답하는지 본다(붙어 보고 바로 끊는다).

    **bind 만으로는 못 걸러낸다(실측 함정):** Windows 에서는 `SO_REUSEADDR` 를
    켠 소켓이 **이미 쓰이는 포트에도 그대로 bind 된다.** 그래서 콘솔판 서버가
    떠 있는데 이 창을 또 띄우면 오류 없이 둘 다 올라가고, 들어오는 접속이
    갈라져 "로그인은 되는데 관리 창에는 안 보이는" 상태가 된다(테스트 중 실제로
    겪었다). 붙어 보는 것이 이 상황을 확실히 잡아내는 방법이다."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def start_server():
    """domiserver 부팅. `main()` 에서 콘솔(repl)만 뺀 것과 같다.
    포트를 못 열면 OSError 를 그대로 올린다(호출자가 창으로 알린다)."""
    load_config()
    import_domiweb_config()
    db_init()
    if port_busy(CONFIG["port"]):
        raise OSError(f"이미 무언가가 포트 {CONFIG['port']} 에서 응답하고 있습니다"
                      " (콘솔판 domiserver.py 나 이 관리 창이 이미 떠 있지 않은지"
                      " 확인하세요).")
    setup_tls()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", CONFIG["port"]))
    srv.listen(32)

    log(f"domiserver {APP_VERSION} 시작 — 포트 {CONFIG['port']}, run={RUN_ID}")
    users = db_one("SELECT COUNT(*) AS n FROM users")["n"]
    waiting = db_one("SELECT COUNT(*) AS n FROM user_pending")["n"]
    rooms = db_one("SELECT COUNT(*) AS n FROM rooms")["n"]
    log(f"계정 {users}명(가입 대기 {waiting}명), 채팅방 {rooms}개")
    if waiting:
        log("가입 대기가 있습니다 — '가입 대기' 목록에서 수락/거절하세요.")

    threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    # 웹 중계도 같이 띄운다. 포트를 못 열면 None 을 돌려주고 **서버 본체는 그대로
    # 돈다** — 채팅 중계가 웹 때문에 죽어서는 안 된다.
    return srv, start_web_relay()


def stop_server(srv):
    """`main()` 의 finally 와 같은 순서로 정리한다.
    srv 는 `start_server()` 가 돌려준 (채팅 소켓, 웹 소켓|None)."""
    STOP.set()
    for s in (srv if isinstance(srv, tuple) else (srv,)):
        if s is None:
            continue
        try:
            s.close()
        except Exception:
            pass
    with STATE_LOCK:
        conns = list(CONNS)
    for c in conns:
        close_conn(c, "서버 종료")
    try:
        with DB_LOCK:
            DB.commit()
            DB.close()
    except Exception:
        pass
    log("domiserver 종료")


def enable_dpi_awareness():
    """고배율 모니터에서 글자가 뭉개지지 않게 한다(Windows 전용)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# === [10-4. 공용 위젯 도우미] ===


def _sort_key(v):
    """숫자로 보이면 숫자로, 아니면 글자로 정렬한다('10' < '9' 를 막는다)."""
    s = "" if v is None else str(v)
    try:
        return (0, float(s), "")
    except ValueError:
        return (1, 0.0, s)


def make_tree(parent, columns, height=8, on_sort=None):
    """columns = [(키, 제목, 폭, 정렬)] · (프레임, 트리) 반환.
    제목을 누르면 그 열로 정렬한다(정렬 상태는 트리에 달아 두고 갱신 때 적용)."""
    keys = [c[0] for c in columns]
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=keys, show="headings", height=height,
                        selectmode="browse")
    vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vs.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    def sorter(idx):
        col, rev = tree.sort_by
        tree.sort_by = (idx, (not rev) if col == idx else False)
        if on_sort:
            on_sort()

    tree.sort_by = (0, False)
    for i, (key, title, width, anchor) in enumerate(columns):
        tree.heading(key, text=title, command=lambda i=i: sorter(i))
        tree.column(key, width=width, anchor=anchor, stretch=(i == 0))
    return frame, tree


def sync_tree(tree, rows):
    """rows = [(iid, (값,...))] 을 트리에 반영한다.
    통째로 지웠다 다시 넣지 않는 이유: 1초마다 갱신하므로 그러면 선택과 스크롤이
    매번 풀려 목록을 쓸 수가 없다. 그래서 없어진 것만 지우고 바뀐 것만 고친다."""
    col, rev = tree.sort_by
    rows = sorted(rows, key=lambda it: _sort_key(it[1][col]), reverse=rev)
    want = {iid: tuple(str(v) for v in vals) for iid, vals in rows}
    for iid in set(tree.get_children("")) - set(want):
        tree.delete(iid)
    for pos, (iid, _) in enumerate(rows):
        vals = want[iid]
        if tree.exists(iid):
            if tuple(tree.item(iid, "values")) != vals:
                tree.item(iid, values=vals)
            if tree.index(iid) != pos:
                tree.move(iid, "", pos)
        else:
            tree.insert("", pos, iid=iid, values=vals)


def selected(tree):
    sel = tree.selection()
    return sel[0] if sel else None


def append_text(widget, chunks, max_lines, follow=True):
    """읽기 전용 Text 에 줄을 덧붙이고 오래된 줄을 버린다.
    chunks = [(글자, 태그)] — 한 줄 안에서 색을 나누기 위해 조각으로 받는다."""
    widget.configure(state="normal")
    for text, tag in chunks:
        widget.insert("end", text, (tag,) if tag else ())
    widget.insert("end", "\n")
    lines = int(widget.index("end-1c").split(".")[0])
    if lines > max_lines:
        widget.delete("1.0", f"{lines - max_lines + 1}.0")
    widget.configure(state="disabled")
    if follow:
        widget.see("end")


# === [10-5. 메인 창] ===


class AdminApp:
    """관리 창 하나. 주소·접속자·계정·채팅방을 **한 창에 상시** 띄운다."""

    def __init__(self, root, srv):
        self.root = root
        self.srv = srv
        self.ui_q = queue.Queue()        # 다른 스레드가 GUI 에 시킬 일(주소 조회 결과 등)
        self.evt_q = queue.Queue()       # 방 관찰 이벤트 (room, obj)
        self.room_windows = {}           # 방 이름 -> RoomWindow
        self.ext_ip = None

        root.title(f"domiserver 관리 — {APP_VERSION}")
        root.geometry("1360x820")
        root.minsize(1080, 620)
        try:
            root.iconbitmap(os.path.join(BASE_DIR, "domichat.ico"))
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build()
        # 서버의 팬아웃 훅을 잡는다. **global 이 없으면 지역 변수가 되어** 방 보기
        # 창에 대화가 한 줄도 안 들어온다(ds. 접두어를 떼면서 실제로 겪었다).
        global ROOM_OBSERVER
        ROOM_OBSERVER = self._observe
        self.refresh_addresses()
        self.refresh_lists()
        self._tick_fast()
        self._tick_slow()

    # ---------- 화면 구성 ----------

    def _build(self):
        r = self.root
        r.rowconfigure(1, weight=1)
        r.columnconfigure(0, weight=1)

        self._build_header(r)

        outer = ttk.PanedWindow(r, orient="vertical")
        outer.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 4))
        lists = ttk.PanedWindow(outer, orient="horizontal")
        outer.add(lists, weight=3)
        self._build_online(lists)
        self._build_users(lists)
        self._build_rooms(lists)
        self._build_web(lists)
        logf = ttk.Frame(outer)
        outer.add(logf, weight=2)
        self._build_log(logf)

        self.var_status = tk.StringVar(value="시작하는 중…")
        ttk.Label(r, textvariable=self.var_status, anchor="w",
                  relief="sunken", padding=(6, 2)).grid(row=2, column=0,
                                                        sticky="ew", padx=8, pady=(0, 6))

    def _build_header(self, parent):
        box = ttk.LabelFrame(parent, text="서버", padding=(8, 4))
        box.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))
        box.columnconfigure(1, weight=1)

        self.var_server = tk.StringVar()
        self.var_addr = tk.StringVar()
        self.var_ext = tk.StringVar()
        self.var_other = tk.StringVar()

        ttk.Label(box, text="상태 :").grid(row=0, column=0, sticky="w")
        ttk.Label(box, textvariable=self.var_server).grid(row=0, column=1, sticky="w")
        ttk.Label(box, text="접속 주소 :").grid(row=1, column=0, sticky="w")
        ttk.Label(box, textvariable=self.var_addr).grid(row=1, column=1, sticky="w")
        ttk.Label(box, text="외부에서 :").grid(row=2, column=0, sticky="w")
        ttk.Label(box, textvariable=self.var_ext).grid(row=2, column=1, sticky="w")
        ttk.Label(box, text="그 밖의 :").grid(row=3, column=0, sticky="w")
        ttk.Label(box, textvariable=self.var_other, foreground="#666").grid(
            row=3, column=1, sticky="w")

        btns = ttk.Frame(box)
        btns.grid(row=0, column=2, rowspan=4, sticky="e", padx=(10, 0))
        ttk.Button(btns, text="주소 새로고침", width=14,
                   command=self.refresh_addresses).pack(fill="x", pady=1)
        self.btn_cert = ttk.Button(btns, text="인증서 지문 복사", width=14,
                                   command=self.act_copy_cert)
        self.btn_cert.pack(fill="x", pady=1)
        ttk.Button(btns, text="설정…", width=14,
                   command=self.act_config).pack(fill="x", pady=1)

    def _build_online(self, parent):
        self.lf_online = ttk.LabelFrame(parent, text="접속 중", padding=(6, 4))
        parent.add(self.lf_online, weight=2)
        self.lf_online.rowconfigure(0, weight=1)
        self.lf_online.columnconfigure(0, weight=1)
        frame, self.tr_online = make_tree(self.lf_online, [
            ("id", "ID", 105, "w"), ("ip", "주소", 95, "w"),
            ("sec", "보안", 45, "center"), ("rooms", "들어가 있는 방", 130, "w"),
        ], on_sort=self.refresh_lists)
        frame.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(self.lf_online)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="연결 끊기", command=self.act_kick).pack(side="left")
        ttk.Label(bar, text="계정은 그대로 남습니다", foreground="#666").pack(
            side="left", padx=6)

    def _build_users(self, parent):
        box = ttk.Frame(parent)
        parent.add(box, weight=2)
        box.rowconfigure(0, weight=3)
        box.rowconfigure(1, weight=2)
        box.columnconfigure(0, weight=1)

        self.lf_users = ttk.LabelFrame(box, text="계정", padding=(6, 4))
        self.lf_users.grid(row=0, column=0, sticky="nsew")
        self.lf_users.rowconfigure(0, weight=1)
        self.lf_users.columnconfigure(0, weight=1)
        frame, self.tr_users = make_tree(self.lf_users, [
            ("id", "ID", 105, "w"), ("state", "상태", 50, "center"),
            ("created", "가입", 80, "center"), ("last", "최근 로그인", 85, "center"),
        ], on_sort=self.refresh_lists)
        frame.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(self.lf_users)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="계정 삭제", command=self.act_deluser).pack(side="left")
        ttk.Button(bar, text="정지", width=6,
                   command=lambda: self.act_enable(False)).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="해제", width=6,
                   command=lambda: self.act_enable(True)).pack(side="left", padx=(4, 0))

        self.lf_pending = ttk.LabelFrame(box, text="가입 대기", padding=(6, 4))
        self.lf_pending.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.lf_pending.rowconfigure(0, weight=1)
        self.lf_pending.columnconfigure(0, weight=1)
        frame, self.tr_pending = make_tree(self.lf_pending, [
            ("id", "ID", 105, "w"), ("ts", "요청 시각", 100, "center"),
        ], height=4, on_sort=self.refresh_lists)
        frame.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(self.lf_pending)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="수락", width=8, command=self.act_approve).pack(side="left")
        ttk.Button(bar, text="거절", width=8,
                   command=self.act_reject).pack(side="left", padx=(4, 0))
        ttk.Label(bar, text="수락해야 로그인됩니다", foreground="#666").pack(
            side="left", padx=6)

    def _build_rooms(self, parent):
        self.lf_rooms = ttk.LabelFrame(parent, text="채팅방", padding=(6, 4))
        parent.add(self.lf_rooms, weight=3)
        self.lf_rooms.rowconfigure(0, weight=1)
        self.lf_rooms.columnconfigure(0, weight=1)
        frame, self.tr_rooms = make_tree(self.lf_rooms, [
            ("name", "이름", 155, "w"), ("kind", "유형", 65, "center"),
            ("owner", "방장", 90, "w"), ("n", "인원", 40, "center"),
            ("created", "생성", 80, "center"), ("last", "마지막 대화", 88, "center"),
        ], on_sort=self.refresh_lists)
        frame.grid(row=0, column=0, sticky="nsew")
        self.tr_rooms.bind("<Double-1>", lambda _e: self.act_open_room())
        bar = ttk.Frame(self.lf_rooms)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="입장(보기)", command=self.act_open_room).pack(side="left")
        ttk.Button(bar, text="방 정보", command=self.act_room_info).pack(
            side="left", padx=(4, 0))
        ttk.Button(bar, text="삭제", command=self.act_delroom).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="일괄 삭제…", command=self.act_bulk_delete).pack(
            side="left", padx=(4, 0))
        ttk.Label(bar, text="제한방도 열어 볼 수 있습니다", foreground="#666").pack(
            side="left", padx=6)

    def _build_web(self, parent):
        """웹 중계 칸 — 브라우저가 보는 피제어 PC 목록과 그 상태.
        콘솔 `web` / `web add` / `web del` 과 **같은 것을 본다**(WEB_HUB 한 곳)."""
        self.lf_web = ttk.LabelFrame(parent, text="웹 중계", padding=(6, 4))
        parent.add(self.lf_web, weight=2)
        self.lf_web.rowconfigure(1, weight=1)
        self.lf_web.columnconfigure(0, weight=1)

        self.var_web = tk.StringVar()
        ttk.Label(self.lf_web, textvariable=self.var_web, foreground="#666",
                  wraplength=260, justify="left").grid(row=0, column=0, sticky="ew")

        frame, self.tr_web = make_tree(self.lf_web, [
            ("pc", "피제어 PC", 105, "w"), ("joined", "입장", 40, "center"),
            ("online", "상태", 55, "center"), ("watch", "보는 사람", 60, "center"),
        ], on_sort=self.refresh_lists)
        frame.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

        bar = ttk.Frame(self.lf_web)
        bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(bar, text="PC 추가…", command=self.act_web_add).pack(side="left")
        ttk.Button(bar, text="삭제", width=7,
                   command=self.act_web_del).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="브라우저 보기", command=self.act_web_clients).pack(
            side="left", padx=(4, 0))

    def act_web_add(self):
        if WEB_HUB is None:
            return messagebox.showinfo("웹 중계", "웹 중계가 꺼져 있습니다"
                                       " (설정에서 web 을 켜고 재시작하세요).",
                                       parent=self.root)
        pc = simpledialog.askstring("PC 추가", "피제어 PC 이름(domichat ID)",
                                    parent=self.root)
        if not pc:
            return
        ok, msg = WEB_HUB.add_pc(pc.strip())
        if not ok:
            return messagebox.showerror("PC 추가", msg, parent=self.root)
        log(f"[웹 목록] {msg} (관리 창)")
        self.refresh_lists()

    def act_web_del(self):
        if WEB_HUB is None:
            return
        pc = self._pick(self.tr_web, "피제어 PC를")
        if not pc:
            return
        if not messagebox.askyesno("PC 삭제", f"'{pc}' 를 웹 목록에서 뺄까요?\n\n"
                                   "브라우저에서 그 PC가 사라집니다"
                                   " (계정·채팅방은 그대로입니다).", parent=self.root):
            return
        ok, msg = WEB_HUB.del_pc(pc)
        if ok:
            log(f"[웹 목록] {msg} (관리 창)")
        self.refresh_lists()

    def act_web_clients(self):
        """지금 붙어 있는 브라우저를 로그에 찍는다(콘솔 `web` 과 같은 내용)."""
        run_console("web")

    def _build_log(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        head = ttk.Frame(parent)
        head.grid(row=0, column=0, sticky="ew", pady=(4, 2))
        ttk.Label(head, text="서버 로그").pack(side="left")
        ttk.Label(head, text="(대화 내용은 남기지 않습니다)",
                  foreground="#666").pack(side="left", padx=6)
        ttk.Button(head, text="지우기", width=8,
                   command=self.act_clear_log).pack(side="right")
        self.var_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(head, text="자동 스크롤",
                        variable=self.var_follow).pack(side="right", padx=6)

        wrap = ttk.Frame(parent)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.txt_log = tk.Text(wrap, height=8, font=MONO, wrap="none",
                               state="disabled", background="#fbfbfb")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=vs.set)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")

        # 콘솔 명령줄 — 버튼으로 옮기지 않은 명령까지 그대로 쓸 수 있게 남겨 둔다
        cmd = ttk.Frame(parent)
        cmd.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        cmd.columnconfigure(1, weight=1)
        ttk.Label(cmd, text="명령 :").grid(row=0, column=0)
        self.ent_cmd = ttk.Entry(cmd, font=MONO)
        self.ent_cmd.grid(row=0, column=1, sticky="ew", padx=4)
        self.ent_cmd.bind("<Return>", lambda _e: self.act_run_cmd())
        ttk.Button(cmd, text="실행", width=8,
                   command=self.act_run_cmd).grid(row=0, column=2)
        ttk.Button(cmd, text="도움말", width=8,
                   command=self.act_help).grid(row=0, column=3, padx=(4, 0))

    # ---------- 주기 작업 ----------

    def _tick_fast(self):
        """로그·방 이벤트·다른 스레드가 맡긴 일을 비운다."""
        for _ in range(400):
            try:
                line = LOG_Q.get_nowait()
            except queue.Empty:
                break
            append_text(self.txt_log, [(line, None)], LOG_MAX_LINES,
                        follow=self.var_follow.get())
        for _ in range(400):
            try:
                room, obj = self.evt_q.get_nowait()
            except queue.Empty:
                break
            win = self.room_windows.get(room)
            if win:
                win.on_event(obj)
        while True:
            try:
                fn = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                put_log(f"[경고] 화면 갱신 실패: {e}")
        self.root.after(DRAIN_MS, self._tick_fast)

    def _tick_slow(self):
        self.refresh_lists()
        for win in list(self.room_windows.values()):
            win.refresh_info()
        self.root.after(REFRESH_MS, self._tick_slow)

    # ---------- 목록 갱신 ----------

    def refresh_lists(self):
        with STATE_LOCK:
            online = [(c.uid, c.addr[0], "TLS" if c.tls else "평문",
                       ", ".join(sorted(c.rooms)) or "-")
                      for c in CONNS if c.uid]
            members = {}
            for c in CONNS:
                if c.uid:
                    for name in c.rooms:
                        members[name] = members.get(name, 0) + 1
        on_ids = {row[0] for row in online}

        sync_tree(self.tr_online, [(u[0], u) for u in online])
        self.lf_online.configure(text=f"접속 중 ({len(online)}명)")

        urows = []
        for r in db_q("SELECT * FROM users"):
            state = "접속중" if r["id"] in on_ids else ("정지" if not r["enabled"] else "-")
            urows.append((r["id"], (r["id"], state, fmt_ts(r["created"]),
                                    fmt_ts(r["last_login"]))))
        sync_tree(self.tr_users, urows)
        self.lf_users.configure(text=f"계정 ({len(urows)}명)")

        prows = [(r["id"], (r["id"], fmt_ts(r["ts"])))
                 for r in db_q("SELECT * FROM user_pending")]
        sync_tree(self.tr_pending, prows)
        self.lf_pending.configure(text=f"가입 대기 ({len(prows)}명)")

        rrows = []
        for r in db_q("SELECT * FROM rooms"):
            rrows.append((r["name"], (
                r["name"], KIND_KO.get(r["kind"], r["kind"]), r["owner"] or "-",
                members.get(r["name"], 0), fmt_ts(r["created"]),
                fmt_ts(r["last_msg"]))))
        sync_tree(self.tr_rooms, rrows)
        self.lf_rooms.configure(text=f"채팅방 ({len(rrows)}/{CONFIG['max_rooms']})")

        self.refresh_web()

        tls = "TLS" if SSL_CTX is not None else "평문"
        self.var_status.set(
            f"계정 {len(urows)}명 · 가입 대기 {len(prows)}명 · 접속 중 {len(online)}명"
            f" · 채팅방 {len(rrows)}/{CONFIG['max_rooms']}"
            f" · 포트 {CONFIG['port']} · {tls}"
            f" · 방 보기 {len(self.room_windows)}개 열림")

    def refresh_web(self):
        """'웹 중계' 칸 갱신. 중계가 꺼져 있으면 목록을 비우고 그 사실만 적는다."""
        if WEB_HUB is None:
            self.var_web.set("꺼져 있음 — 설정에서 web 을 켜고 재시작하세요.")
            sync_tree(self.tr_web, [])
            self.lf_web.configure(text="웹 중계 (꺼짐)")
            return
        with WEB_HUB.lock:
            watch = {}
            for c in WEB_HUB.clients:
                if c.pc:
                    watch[c.pc] = watch.get(c.pc, 0) + 1
            n_cli = len(WEB_HUB.clients)
        scheme = "wss" if WEB_SSL_CTX is not None else "ws(평문)"
        self.var_web.set(f"{scheme} · 포트 {CONFIG['web_port']} · 중계 이름"
                         f" '{WEB_HUB.uid}' · 브라우저 {n_cli}명")
        rows = []
        for pc in CONFIG["web_pcs"]:
            st = WEB_HUB.state.get(pc, {})
            on = {True: "접속중", False: "꺼짐", None: "모름"}[st.get("online")]
            rows.append((pc, (pc, "O" if st.get("joined") else "X", on,
                              watch.get(pc, 0))))
        sync_tree(self.tr_web, rows)
        self.lf_web.configure(text=f"웹 중계 (브라우저 {n_cli}명)")

    def refresh_addresses(self):
        """주소 표시 갱신. 공인 IP 조회는 느릴 수 있어 스레드로 돌린다."""
        port = CONFIG["port"]
        if SSL_CTX is None:
            sec = "TLS 미사용(평문)"
        else:
            sec = f"TLS 사용 · 지문 {(CERT_FP or '?')[:16]}…"
            sec += " · 평문 거부" if CONFIG["require_tls"] else " · 평문도 허용"
        web = (f"웹 중계 {CONFIG['web_port']}"
               f"({'wss' if WEB_SSL_CTX is not None else 'ws 평문'})"
               if WEB_HUB is not None else "웹 중계 꺼짐")
        self.var_server.set(f"domiserver {APP_VERSION} 가동 중 · 포트 {port} · {sec}"
                            f" · {web} · run={RUN_ID}")
        self.btn_cert.configure(state=("normal" if CERT_FP else "disabled"))
        self.var_ext.set("조회 중…")

        def work():
            ips = local_ips()
            primary = ips[0] if ips else None
            others = [ip for ip in ips[1:] if _ip_kind(ip) != "loopback"]
            ext = external_ip()

            def apply():
                self.ext_ip = ext
                tag = ""
                if primary:
                    tag = ("공인 IP — 밖에서도 이 주소"
                           if _ip_kind(primary) == "public"
                           else "사설 IP — 같은 네트워크 안에서만")
                self.var_addr.set(f"같은 PC 127.0.0.1      |      다른 PC "
                                  f"{primary or '?'}  ({tag})      |      포트 {port}")
                if not ext:
                    self.var_ext.set("조회하지 못했습니다 — 인터넷 연결을 확인하세요")
                elif ext in ips:
                    self.var_ext.set(f"{ext}   ← 밖에서도 이 주소로 바로 접속됩니다"
                                     f" (공인 IP가 이 PC에 직접 할당 · 포워딩 불필요)")
                else:
                    self.var_ext.set(f"{ext}   ← NAT 안쪽입니다 · 공유기에서 포트 "
                                     f"{port} 를 이 PC로 포워딩해야 밖에서 접속됩니다")
                self.var_other.set((", ".join(others) + "   (가상 어댑터 등 — 보통 접속에"
                                    " 쓰이지 않음)") if others else "없음")
            self.ui_q.put(apply)

        threading.Thread(target=work, daemon=True).start()

    # ---------- 방 관찰 ----------

    def _observe(self, room, obj):
        """`domiserver.ROOM_OBSERVER` — **서버 스레드에서 불린다.**
        tkinter 는 건드리지 않고 큐에만 넣는다. 열어 둔 창이 없는 방은 그냥 버려
        큐가 쌓이지 않게 한다."""
        if room in self.room_windows:
            self.evt_q.put((room, obj))

    def open_room(self, name):
        win = self.room_windows.get(name)
        if win is not None:
            win.lift()
            win.focus_force()
            return
        self.room_windows[name] = RoomWindow(self, name)
        self.refresh_lists()

    def close_room(self, name):
        self.room_windows.pop(name, None)

    # ---------- 동작 (콘솔 명령과 1:1) ----------

    def _pick(self, tree, what):
        iid = selected(tree)
        if iid is None:
            messagebox.showinfo("선택 필요", f"먼저 목록에서 {what} 고르세요.",
                                parent=self.root)
        return iid

    def act_kick(self):
        """kick <ID> — 연결만 끊는다(계정은 그대로)."""
        uid = self._pick(self.tr_online, "접속 중인 사용자를")
        if not uid:
            return
        if messagebox.askyesno("연결 끊기", f"'{uid}' 의 연결을 끊을까요?\n\n"
                               "계정은 그대로 남고, 상대는 다시 로그인할 수 있습니다.",
                               parent=self.root):
            run_console("kick", uid)
            self.refresh_lists()

    def act_deluser(self):
        """deluser <ID> — ID와 비밀번호를 서버에서 지운다(수정 기능은 두지 않는다)."""
        uid = self._pick(self.tr_users, "계정을")
        if not uid:
            return
        if messagebox.askyesno(
                "계정 삭제",
                f"'{uid}' 계정을 삭제할까요?\n\n"
                "· ID와 비밀번호가 서버에서 지워지며 되돌릴 수 없습니다.\n"
                "· 접속 중이면 연결이 끊깁니다.\n"
                "· 이 계정이 방장인 제한방은 그대로 남습니다(따로 삭제하세요).",
                icon="warning", parent=self.root):
            run_console("deluser", uid)
            self.refresh_lists()

    def act_enable(self, on):
        """enable / disable <ID> — 계정을 살리거나 정지한다."""
        uid = self._pick(self.tr_users, "계정을")
        if not uid:
            return
        if not on and not messagebox.askyesno(
                "계정 정지", f"'{uid}' 계정을 정지할까요?\n\n"
                "접속 중이면 연결이 끊기고, 해제할 때까지 로그인할 수 없습니다.",
                parent=self.root):
            return
        run_console("enable" if on else "disable", uid)
        self.refresh_lists()

    def act_approve(self):
        """approve <ID> — 가입 수락(이때부터 로그인된다)."""
        uid = self._pick(self.tr_pending, "가입 요청을")
        if not uid:
            return
        run_console("approve", uid)
        self.refresh_lists()

    def act_reject(self):
        """reject <ID> — 가입 요청 거절."""
        uid = self._pick(self.tr_pending, "가입 요청을")
        if not uid:
            return
        if messagebox.askyesno("가입 거절", f"'{uid}' 의 가입 요청을 거절할까요?",
                               parent=self.root):
            run_console("reject", uid)
            self.refresh_lists()

    def act_open_room(self):
        name = self._pick(self.tr_rooms, "채팅방을")
        if name:
            self.open_room(name)

    def act_room_info(self):
        """room <이름> — 콘솔과 같은 상세 정보를 로그에 찍는다."""
        name = self._pick(self.tr_rooms, "채팅방을")
        if name:
            run_console("room", name)

    def act_delroom(self):
        name = self._pick(self.tr_rooms, "채팅방을")
        if not name:
            return
        if messagebox.askyesno(
                "채팅방 삭제", f"'{name}' 채팅방을 삭제할까요?\n\n"
                "참여자들의 대화 기록도 함께 삭제됩니다. 되돌릴 수 없습니다.",
                icon="warning", parent=self.root):
            run_console("delroom", name)
            win = self.room_windows.get(name)
            if win is not None:
                win.refresh_info()
            self.refresh_lists()

    def act_bulk_delete(self):
        BulkDeleteWindow(self)

    def act_config(self):
        ConfigWindow(self)

    def act_copy_cert(self):
        """cert — 클라이언트가 고정한 지문과 맞춰 볼 때 쓴다."""
        if not CERT_FP:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(CERT_FP)
        run_console("cert")
        put_log("인증서 지문을 클립보드에 복사했습니다.")

    def act_clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def act_help(self):
        for line in HELP.splitlines():
            put_log(line)
        put_log("창에서는 delrooms 를 치면 '일괄 삭제' 창이 대신 열립니다"
                " (콘솔판은 되묻기에 입력이 필요해 창을 멈춥니다).")

    def act_run_cmd(self):
        """콘솔 명령줄 — 버튼으로 옮기지 않은 것까지 그대로 쓸 수 있게 남겨 둔 통로."""
        line = self.ent_cmd.get().strip()
        if not line:
            return
        self.ent_cmd.delete(0, "end")
        put_log(f"> {line}")
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("quit", "exit"):
            self.on_close()
        elif cmd == "help":
            self.act_help()
        elif cmd == "delrooms":
            # 콘솔판은 input() 으로 되물어 GUI 스레드를 멈춘다 → 창으로 대신한다
            self.act_bulk_delete()
        elif cmd in COMMANDS:
            run_console(cmd, *args)
            self.refresh_lists()
            if cmd in ("set", "config"):
                self.refresh_addresses()
        else:
            put_log(f"모르는 명령: {cmd}  (도움말 버튼을 눌러 보세요)")

    # ---------- 종료 ----------

    def on_close(self):
        with STATE_LOCK:
            n = len([c for c in CONNS if c.uid])
        msg = "관리 창을 닫으면 서버도 함께 멈춥니다."
        if n:
            msg += f"\n지금 접속 중인 {n}명의 연결이 끊깁니다."
        if not messagebox.askokcancel("종료", msg + "\n\n종료할까요?", parent=self.root):
            return
        global ROOM_OBSERVER
        ROOM_OBSERVER = None
        stop_server(self.srv)
        try:
            self.root.destroy()
        except Exception:
            pass
        # DB는 위에서 이미 닫았다. 남은 스레드는 전부 데몬이지만 SSL 소켓에서
        # 블로킹 중일 수 있어, 창이 사라진 뒤에도 프로세스가 남지 않게 끊는다.
        os._exit(0)


# === [10-6. 방 보기 창 — 제한방도 그대로 들여다본다] ===


class RoomWindow(tk.Toplevel):
    """방 하나를 들여다보는 창.

    **입장 절차를 밟지 않는다** — 서버가 중계하는 프레임을 옆에서 받아 볼 뿐이라
    비밀번호방·사전승인방·사후승인방도 그대로 열린다. 방 사람들에게는 아무도
    들어온 것으로 보이지 않는다(member 알림이 나가지 않는다).

    **서버는 대화를 저장하지 않으므로 창을 연 뒤의 대화만 보인다.** 이건 고장이
    아니라 설계다(domichat.md — 서버는 순수 중계).
    """

    def __init__(self, app, room):
        super().__init__(app.root)
        self.app = app
        self.room = room
        self.gone = False               # 방이 삭제됐는지(한 번만 알린다)
        self.title(f"방 보기 — {room}")
        self.geometry("860x560")
        self.minsize(640, 400)
        try:
            self.iconbitmap(os.path.join(BASE_DIR, "domichat.ico"))
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.refresh_info()
        self._sys(f"'{room}' 방을 열었습니다 — 지금부터 오가는 대화가 여기 보입니다.")

    def _build(self):
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        top = ttk.Frame(self, padding=(8, 6, 8, 2))
        top.grid(row=0, column=0, sticky="ew")
        self.var_head = tk.StringVar()
        ttk.Label(top, textvariable=self.var_head, font=("", 10, "bold")).pack(side="left")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.grid(row=1, column=0, sticky="nsew", padx=8)

        left = ttk.LabelFrame(pane, text="대화 (실시간 중계)", padding=(6, 4))
        pane.add(left, weight=3)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.txt = tk.Text(left, font=MONO, wrap="word", state="disabled",
                           background="#ffffff")
        vs = ttk.Scrollbar(left, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=vs.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        self.txt.tag_configure("ts", foreground="#999")
        self.txt.tag_configure("who", foreground="#0b5", font=(MONO[0], MONO[1], "bold"))
        self.txt.tag_configure("body", foreground="#111")
        self.txt.tag_configure("sys", foreground="#888")
        self.txt.tag_configure("img", foreground="#06c")

        right = ttk.LabelFrame(pane, text="방 정보", padding=(6, 4))
        pane.add(right, weight=1)
        self.info_vars = {}
        for i, label in enumerate(("유형", "방장", "생성", "마지막 대화", "접속 중",
                                   "구독 중", "허용 ID", "승인 대기", "블랙리스트")):
            ttk.Label(right, text=label, foreground="#666").grid(
                row=i * 2, column=0, sticky="w", pady=(4, 0))
            var = tk.StringVar(value="-")
            ttk.Label(right, textvariable=var, wraplength=210, justify="left").grid(
                row=i * 2 + 1, column=0, sticky="w")
            self.info_vars[label] = var

        bottom = ttk.Frame(self, padding=(8, 4))
        bottom.grid(row=2, column=0, sticky="ew")
        ttk.Label(bottom, foreground="#666",
                  text="서버는 대화를 저장하지 않습니다 — 창을 연 뒤의 대화만 보입니다."
                  ).pack(side="left")
        ttk.Button(bottom, text="닫기", width=8, command=self.on_close).pack(side="right")
        ttk.Button(bottom, text="기록 지우기", width=11,
                   command=self.clear).pack(side="right", padx=4)
        self.var_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="자동 스크롤",
                        variable=self.var_follow).pack(side="right", padx=6)

    # ---------- 표시 ----------

    def _line(self, chunks):
        append_text(self.txt, chunks, CHAT_MAX_LINES, follow=self.var_follow.get())

    def _stamp(self, ts=None):
        return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))

    def _sys(self, text, ts=None):
        self._line([(f"[{self._stamp(ts)}] ", "ts"), (f"— {text} —", "sys")])

    def on_event(self, obj):
        """`fanout` 이 방에 흘려보낸 프레임 하나. GUI 스레드에서 불린다."""
        t = obj.get("t")
        if t == "msg":
            self._line([(f"[{self._stamp(obj.get('ts'))}] ", "ts"),
                        (f"{obj.get('from')}", "who"),
                        (f" : {obj.get('body', '')}", "body")])
        elif t == "member":
            self._sys(f"{obj.get('id')} {'입장' if obj.get('in') else '나감'}")
        elif t == "file_begin":
            kb = (obj.get("size") or 0) / 1024.0
            self._line([(f"[{self._stamp(obj.get('ts'))}] ", "ts"),
                        (f"{obj.get('from')}", "who"),
                        (f" : [이미지] {obj.get('name')} ({kb:,.0f} KB)", "img")])
        elif t == "file_end" and not obj.get("ok"):
            self._sys("이미지가 온전히 전달되지 않았습니다")
        elif t == "file_abort":
            self._sys("이미지 전송이 중단되었습니다")

    def clear(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def refresh_info(self):
        """오른쪽 정보 칸 갱신 — 콘솔 `room <이름>` 이 보여주던 것과 같은 내용."""
        r = room_row(self.room)
        if not r:
            if not self.gone:
                self.gone = True
                self._sys("이 채팅방은 삭제되었습니다")
                self.var_head.set(f"{self.room}   (삭제됨)")
                for var in self.info_vars.values():
                    var.set("-")
            return
        with STATE_LOCK:
            here = sorted(c.uid for c in CONNS if c.uid and self.room in c.rooms)
            subs = sorted(c.uid for c in CONNS if c.uid and self.room in c.subs)
        kind = KIND_KO.get(r["kind"], r["kind"])
        self.var_head.set(f"{self.room}   ·   {kind}방   ·   접속 {len(here)}명")
        info = {
            "유형": kind,
            "방장": r["owner"] or "없음(공개방)",
            "생성": fmt_ts(r["created"]),
            "마지막 대화": fmt_ts(r["last_msg"]),
            "접속 중": ", ".join(here) or "-",
            "구독 중": ", ".join(subs) or "-",
            "허용 ID": ", ".join(sorted(room_ids("room_allow", self.room))) or "-",
            "승인 대기": ", ".join(sorted(room_ids("room_pending", self.room))) or "-",
            "블랙리스트": ", ".join(sorted(room_ids("room_block", self.room))) or "-",
        }
        for k, v in info.items():
            if self.info_vars[k].get() != v:
                self.info_vars[k].set(v)

    def on_close(self):
        self.app.close_room(self.room)
        self.destroy()


# === [10-7. 채팅방 일괄 삭제 창 (콘솔 delrooms)] ===


class BulkDeleteWindow(tk.Toplevel):
    """콘솔 `delrooms all|open|limited|owner <ID>` 를 창으로 옮긴 것.
    콘솔판은 `input()` 으로 되묻는데 GUI 스레드에서 그걸 부르면 창이 멈추므로
    **여기서만** 같은 일을 다시 구현했다(대상 선정 SQL·경고 문구는 콘솔과 같다)."""

    MODES = (("all", "모든 채팅방"), ("open", "공개 채팅방만"),
             ("limited", "제한 채팅방만"), ("owner", "특정 방장이 만든 방"))

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("채팅방 일괄 삭제")
        self.geometry("460x420")
        self.transient(app.root)
        self.var_mode = tk.StringVar(value="all")
        self.var_owner = tk.StringVar()
        self.var_count = tk.StringVar()
        self._build()
        self.refresh()

    def _build(self):
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        box = ttk.LabelFrame(self, text="지울 대상", padding=(8, 6))
        box.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        for key, label in self.MODES:
            ttk.Radiobutton(box, text=label, value=key, variable=self.var_mode,
                            command=self.refresh).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(anchor="w", pady=(4, 0))
        ttk.Label(row, text="방장 ID :").pack(side="left")
        self.ent_owner = ttk.Entry(row, textvariable=self.var_owner, width=22)
        self.ent_owner.pack(side="left", padx=4)
        self.var_owner.trace_add("write", lambda *_a: self.refresh())

        ttk.Label(self, textvariable=self.var_count).grid(row=1, column=0,
                                                          sticky="w", padx=10)
        wrap = ttk.Frame(self)
        wrap.grid(row=2, column=0, sticky="nsew", padx=8)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.lst = tk.Listbox(wrap)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.lst.yview)
        self.lst.configure(yscrollcommand=vs.set)
        self.lst.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")

        bar = ttk.Frame(self, padding=8)
        bar.grid(row=3, column=0, sticky="ew")
        ttk.Button(bar, text="닫기", width=10, command=self.destroy).pack(side="right")
        ttk.Button(bar, text="삭제", width=10,
                   command=self.do_delete).pack(side="right", padx=4)

    def _match(self):
        """콘솔 cmd_delrooms 와 같은 조건으로 대상을 고른다."""
        mode = self.var_mode.get()
        if mode == "all":
            rows = db_q("SELECT name FROM rooms")
            return [r["name"] for r in rows], "모든 채팅방"
        if mode == "open":
            rows = db_q("SELECT name FROM rooms WHERE kind='open'")
            return [r["name"] for r in rows], "공개 채팅방"
        if mode == "limited":
            rows = db_q("SELECT name FROM rooms WHERE kind!='open'")
            return [r["name"] for r in rows], "제한 채팅방"
        owner = self.var_owner.get().strip()
        if not owner:
            return [], "방장 ID를 입력하세요"
        rows = db_q("SELECT name FROM rooms WHERE owner=?", (owner,))
        return [r["name"] for r in rows], f"'{owner}' 이(가) 만든 채팅방"

    def refresh(self):
        self.ent_owner.configure(
            state="normal" if self.var_mode.get() == "owner" else "disabled")
        names, desc = self._match()
        self.var_count.set(f"{desc} — {len(names)}개")
        self.lst.delete(0, "end")
        for n in names:
            self.lst.insert("end", n)

    def do_delete(self):
        names, desc = self._match()
        if not names:
            messagebox.showinfo("일괄 삭제", f"{desc}이 없습니다.", parent=self)
            return
        shown = "\n".join(names[:20]) + ("\n…" if len(names) > 20 else "")
        if not messagebox.askyesno(
                "일괄 삭제", f"{desc} {len(names)}개를 삭제할까요?\n\n{shown}\n\n"
                "참여자들의 대화 기록도 함께 삭제됩니다. 되돌릴 수 없습니다.",
                icon="warning", parent=self):
            return
        for n in names:
            purge_room(n)
            win = self.app.room_windows.get(n)
            if win is not None:
                win.refresh_info()
        log(f"[일괄 삭제] {desc} {len(names)}개 삭제")
        self.app.refresh_lists()
        self.refresh()


# === [10-8. 설정 창 (콘솔 config / set)] ===


class ConfigWindow(tk.Toplevel):
    """`domiserver.json` 편집.

    참·거짓 값은 **체크박스**로 받는다(함정): 콘솔 `set` 은 `type(기본값)(입력)`
    으로 바꾸는데 `bool("false")` 는 True 라서, 콘솔에서 `set tls false` 를 치면
    오히려 켜진다. 창에서는 그 길을 아예 막는다."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("서버 설정")
        self.transient(app.root)
        self.resizable(False, False)
        self.vars = {}
        self._build()

    def _build(self):
        box = ttk.Frame(self, padding=10)
        box.pack(fill="both", expand=True)
        # 목록형(web_pcs·web_origins)은 여기서 다루지 않는다 — `type(기본값)(문자열)`
        # 이 `list("abc") -> ['a','b','c']` 로 망가진다. 피제어 PC 목록은 '웹 중계'
        # 칸의 추가/삭제 버튼이 주인이다.
        for i, key in enumerate(k for k in DEFAULT_CONFIG
                                if not isinstance(DEFAULT_CONFIG[k], list)):
            ttk.Label(box, text=key).grid(row=i, column=0, sticky="w", pady=2)
            cur = CONFIG[key]
            if isinstance(DEFAULT_CONFIG[key], bool):
                var = tk.BooleanVar(value=bool(cur))
                ttk.Checkbutton(box, variable=var).grid(row=i, column=1,
                                                        sticky="w", padx=8)
            else:
                var = tk.StringVar(value=str(cur))
                ttk.Entry(box, textvariable=var, width=12).grid(row=i, column=1,
                                                                sticky="w", padx=8)
            self.vars[key] = var
            ttk.Label(box, text=CONFIG_HINT.get(key, ""), foreground="#666").grid(
                row=i, column=2, sticky="w")

        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        ttk.Label(bar, foreground="#666",
                  text="'재시작해야 적용' 항목은 저장만 되고 다음 실행부터 반영됩니다."
                  ).pack(side="left")
        ttk.Button(bar, text="닫기", width=10, command=self.destroy).pack(side="right")
        ttk.Button(bar, text="저장", width=10,
                   command=self.save).pack(side="right", padx=4)

    def save(self):
        changed = []
        for key, var in self.vars.items():
            default = DEFAULT_CONFIG[key]
            if isinstance(default, bool):
                value = bool(var.get())
            else:
                try:
                    value = type(default)(var.get().strip())
                except ValueError:
                    messagebox.showerror(
                        "설정", f"'{var.get()}' 는 {key} 에 넣을 수 없습니다.",
                        parent=self)
                    return
            if CONFIG[key] != value:
                CONFIG[key] = value
                changed.append(f"{key} = {value}")
        if not changed:
            put_log("설정: 바뀐 값이 없습니다.")
            self.destroy()
            return
        save_config()
        for line in changed:
            put_log(f"설정 저장: {line}")
        self.app.refresh_addresses()
        self.app.refresh_lists()
        self.destroy()


def gui_main():
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    try:
        srv = start_server()
    except OSError as e:
        messagebox.showerror(
            "domiserver 시작 실패",
            f"포트 {CONFIG['port']} 를 열 수 없습니다.\n\n{e}\n\n"
            "이미 다른 domiserver 가 떠 있거나, 다른 프로그램이 그 포트를 쓰고"
            " 있습니다.\ndomiserver.json 의 port 를 바꾼 뒤 다시 실행하세요.")
        return 1
    except Exception as e:
        messagebox.showerror("domiserver 시작 실패",
                             f"서버를 시작하지 못했습니다.\n\n{e}")
        return 1
    root.deiconify()
    AdminApp(root, srv)
    root.mainloop()
    return 0


# === [11. 진입점] ===


def console_main():
    load_config()
    import_domiweb_config()
    db_init()
    setup_tls()

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
    web_srv = start_web_relay()

    try:
        repl()
    finally:
        STOP.set()
        for s in (srv, web_srv):
            if s is None:
                continue
            try:
                s.close()
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


def main():
    """기본은 **관리 창**이다. 창을 띄울 수 없는 환경(원격 세션·헤드리스)이나
    예전처럼 콘솔로 쓰고 싶을 때 `--console` 을 준다."""
    if "--console" in sys.argv[1:]:
        return console_main()
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
