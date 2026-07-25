# -*- coding: utf-8 -*-
"""
domiman_m.py — DOMIMAN 모바일(안드로이드) 원격제어 앱 Kotlin 포팅 레퍼런스
====================================================================
`앱UI설명.xlsx`(Sheet1=로그인 화면, Sheet2=메인 제어 화면, A-E열이 대략적
레이아웃/G열이 상세 설명)를 기준으로 전면 재설계됨. domiman.py에서
데스크톱 전용 코드(tkinter GUI, pyautogui/win32 클릭, WGC 캡처, OCR, 낚시
자동화 루틴)를 전부 제외하고, ① ntfy 통신 프로토콜 ② 로그인/최근 로그인
저장·자동로그인 규칙 ③ 화면 상태·전환 로직만 다룬다. "제어(dB) 역할"만
맡는다 — 자기 앞으로 온 '명령'을 받아 실행하는 '피제어(d3)' 쪽 로직
(domiman.py의 _handle_command)은 필요 없다.

실제 위젯 렌더링, 화면 전환 애니메이션, 안드로이드 뒤로가기 버튼 인터셉트,
Toast, 알림, 다크모드 감지, 실제 영속 저장소(SharedPreferences/DataStore
등)는 전부 Kotlin 쪽 책임이다 — 이 모듈은 "무엇을 언제 어떻게 바꿔야
하는가"라는 규칙만 담은 참고 구현이며, Android 포팅 시 Chaquopy로 앱에
내장해 Kotlin에서 호출하는 것을 염두에 두고 플랫폼 의존성 없이
requests/json/re/time/threading/queue/dataclasses/enum/collections만 쓴다.

■ 화면 구성 (앱UI설명.xlsx 기준)
  Sheet1 A1:E18  로그인 화면  — ID/피제어PC/ntfy채널명 입력 + 자동로그인
                                체크 + [로그인]/[…] 버튼. 뒤로가기=앱 종료.
  Sheet1 A21:E39 최근 로그인  — […] 버튼으로 진입. 짧게 탭=즉시 로그인,
                                길게 눌러 수정/삭제. '<'=로그인 화면으로.
  Sheet2 A1:E19  메인 제어    — PC GUI와 거의 동일(해상도/타이머/체크박스/
                                시작-중지/상태문구/예약종료/즉시회수/
                                실시간수량확인/다크모드/로그). 이 부분의
                                버튼들은 기존 DomimanClient.cmd_* 를 그대로
                                호출하면 되므로 이 파일에서 별도로 감싸지
                                않는다(중복 방지).

■ '업데이트' 버튼 관련 결정: Android는 PC(os.replace 무음 교체)처럼 완전
  자동 업데이트가 불가능하고 새 APK 설치 시 OS 확인 탭이 항상 필요하다.
  사용자 확정: 이 구조는 채택하지 않음 — Sheet2 G13 자리는 항상
  '실시간 수량확인' 버튼이며, G18의 풀투리프레시 제스처도 구현하지 않는다.
  (수량 확인은 DomimanClient.cmd_tank_query()를 그 버튼에 연결하면 끝.)

■ 수신 방식(중요): stream()이 권장 방식이다. poll()의 반복 GET은 무료 ntfy
  한도(GET/POST가 같은 IP 버킷을 공유, 5~10초당 1개 충전)를 빠르게 소모해,
  같은 Wi-Fi(같은 공인 IP)를 쓰는 PC/휴대폰의 발신이 429로 실패한다. stream()은
  연결 하나를 열어두고 도착 메시지를 실시간으로 받아 요청 토큰을 '연결당 1회'만
  쓴다(수신 지연도 사실상 0). poll()은 스트리밍이 어려운 환경의 폴백으로만 남긴다.
  Kotlin에서는 OkHttp 스트리밍 응답(response.body().source() 줄 읽기) 또는 /sse
  엔드포인트 + EventSource로 stream()과 동일하게 구현하고, dispatch()/parse_status()
  /report_text()는 그대로 재사용한다.

단독 실행하면 로그인 시도(자동로그인 실패 시 재시도) + 최근 로그인 저장까지
포함한 스모크 테스트가 된다:
    python domiman_m.py <내 ID> <대상PC 이름> <ntfy 채널 이름>
"""
import json
import re
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import requests

NTFY_SERVER = "https://ntfy.sh"
PENDING_TIMEOUT_SEC = 15.0   # domiman.py _check_pending_timeout과 동일

NAME_RE = re.compile(r"[A-Za-z0-9]+")
CHANNEL_RE = re.compile(r"[A-Za-z0-9_\-]+")


def is_valid_id(name):
    """대상 PC ID 형식 검사 (domiman.py의 PC_NAME 규칙과 동일: 영문+숫자)."""
    return bool(NAME_RE.fullmatch(name or ""))


def is_valid_channel(channel):
    """ntfy 채널 이름 형식 검사 (domiman.py의 NTFY_TOPIC 규칙과 동일)."""
    return bool(CHANNEL_RE.fullmatch(channel or ""))


# 상태 문구 (domiman.py STATUS_TEXT 그대로 포팅 — 엑셀 J8:K18 원본).
# Sheet2 A11 '[상태 메시지]' 자리에 띄울 텍스트.
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
    ("y", "r"):  "{name}의 낚싯대 교체가 성공하였습니다.",
    ("y", "b"):  "{name}의 미끼 교체가 성공하였습니다.",
    ("x", "d"):  "{name}의 게임 연결이 끊겼습니다.",
    ("x", "r"):  "{name}의 낚싯대 교체가 실패하였습니다.",
    ("x", "b"):  "{name}의 미끼 교체가 실패하였습니다.",
}
REPORT_STATUS = {
    ("s",): "collect", ("g",): "fishing", ("f",): "fishing",
    ("y", "r"): "fishing", ("y", "b"): "fishing",
    ("x", "d"): "disconnect", ("x", "r"): "fishing", ("x", "b"): "fishing",
}

# 알림 설정 체크박스 <-> 보고 코드 매핑 (domiman.py엔 없는 안드로이드 전용 기능).
NOTIFY_KEYS = {
    "routine_start": ("s",),
    "routine_fail":  ("f",),
    "rod_success":   ("y", "r"),
    "rod_fail":      ("x", "r"),
    "bait_success":  ("y", "b"),
    "bait_fail":     ("x", "b"),
    "crash":         ("x", "d"),
}


# ============================================================
# [1. 프로토콜: ntfy 통신 (domiman.py 규격 그대로)]
# ============================================================
class DomimanClient:
    """ntfy 채널 하나 + 대상 PC 하나에 대한 '이름 있는(my_id) 컨트롤러' 세션.
    스레드 안전성은 호출부(Kotlin Foreground Service)가 책임진다 — 이 클래스는
    자체 잠금을 걸지 않으며, 한 백그라운드 스레드에서 순차 호출되는 것을 가정한다."""

    def __init__(self, my_id, target_id, channel, server=NTFY_SERVER):
        if not is_valid_id(my_id):
            raise ValueError(f"잘못된 ID: {my_id!r} (영문+숫자만 가능)")
        if not is_valid_id(target_id):
            raise ValueError(f"잘못된 대상 PC 이름: {target_id!r} (영문+숫자만 가능)")
        if not is_valid_channel(channel):
            raise ValueError(f"잘못된 채널 이름: {channel!r}")
        self.my_id = my_id
        self.target_id = target_id
        self.channel = channel
        self.server = server
        self.url = f"{server}/{channel}"
        self._last_poll_time = 0
        self.pending = None   # {"kind": str, "sent": epoch}

    # ---------- 발신 ----------
    def _send(self, body, timeout=10):
        """domiman.py의 PC_NAME과 동일하게 내 이름(my_id)을 Title에 실어 보낸다."""
        requests.post(self.url, data=body.encode("utf-8"),
                      headers={"Title": self.my_id, "Priority": "3"}, timeout=timeout)

    def send_command(self, cmdbody, kind):
        """`{target},{cmdbody}` 발신 + pending 진입. kind는 응답 대응용 태그."""
        self._send(f"{self.target_id},{cmdbody}")
        self.pending = {"kind": kind, "sent": time.time()}

    def resolve_pending(self):
        p, self.pending = self.pending, None
        return p

    def check_pending_timeout(self):
        """15초 무응답이면 pending을 비우고 반환(호출부가 '실패/응답없음' 처리)."""
        if self.pending is None:
            return None
        if time.time() - self.pending["sent"] <= PENDING_TIMEOUT_SEC:
            return None
        return self.resolve_pending()

    # ---------- 명령 빌더 (domiman.py 프로토콜 규격 그대로) ----------
    def cmd_login(self):
        """로그인 = 상태 질의(S). 15초 안에 응답 오면 성공(Sheet1 G6)."""
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
        """mode: 'a'(자동감지) | '1080' | '1440'. Sheet2 E2/G2."""
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
        """Sheet2 G13 '실시간 수량확인' 버튼(N). 응답: ',Z,N,<cur>,<mx>' 또는
        ',Z,N,fail'. 피제어 PC는 마지막 파싱값이 있으면 즉시, 없으면 창을
        띄우고 3초 뒤 1회 파싱해 응답하므로 최대 5초 안팎 걸릴 수 있다
        (15초 타임아웃 내)."""
        self.send_command("N", "N")

    # ---------- 수신: 스트리밍(권장) ----------
    def stream(self, on_message, should_stop, connect_timeout=10, read_timeout=90):
        """지속 연결(스트리밍) 구독 — poll()의 반복 GET을 대체하는 권장 방식.
        `{url}/json` 연결 하나를 열어두고 도착하는 메시지마다
        on_message(title, body)를 호출한다. 요청 토큰을 '연결당 1회'만
        소비하므로 같은 IP를 공유하는 PC/휴대폰이 무료 한도에 밀리지 않는다.

        should_stop(): True면 연결을 끊고 반환(메시지 도착 사이사이 확인).
        네트워크 오류/연결 종료 시 조용히 반환하므로, 호출부가 should_stop을
        보며 재호출(재연결)한다. since는 쓰지 않는다 — 살아있는 연결은 메시지를
        정확히 한 번만 전달하므로 재연결 시 과거 명령을 중복 수신할 위험이 없다.
        (내가 보낸 것/대상 외 발신자 필터는 dispatch()가 담당하므로 여기선
        원문 그대로 넘긴다 — poll()과 동일.)

        ※ read_timeout: 정상 연결은 ntfy keepalive(기본 45s)가 계속 도착해
          유지된다. Kotlin에서는 프롬프트한 취소를 위해 별도 스레드에서
          연결(OkHttp Call)을 cancel()하는 방식을 쓴다(domiman.py와 동일)."""
        resp = requests.get(f"{self.url}/json", stream=True,
                            timeout=(connect_timeout, read_timeout))
        try:
            if resp.status_code != 200:
                return
            for line in resp.iter_lines(decode_unicode=True):
                if should_stop():
                    break
                if not line:
                    continue                         # keepalive 사이 빈 줄
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("event") != "message":
                    continue                         # open/keepalive 무시
                on_message(data.get("title", ""), data.get("message", "").strip())
        finally:
            try:
                resp.close()
            except Exception:
                pass

    # ---------- 수신: 반복 폴링(폴백) ----------
    def poll(self, timeout=3):
        """[폴백] ntfy 토픽의 새 메시지 전부를 시간순으로 반환: [(title, body), ...].
        무료 한도를 빠르게 소모하므로 stream()을 쓸 수 없을 때만 사용한다.
        내가 보낸 메시지도 Title=my_id로 그대로 돌아오지만, dispatch()가
        title==target_id인 것만 받아들이므로(내 my_id로 온 것은 걸러짐)
        따로 '내가 보낸 것 제외' 필터가 필요 없다."""
        since = str(self._last_poll_time) if self._last_poll_time > 0 else "10s"
        resp = requests.get(f"{self.url}/json?poll=1&since={since}", timeout=timeout)
        if resp.status_code != 200:
            return []
        events = []
        latest = self._last_poll_time
        for line in resp.text.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("event") != "message":
                continue
            t = data.get("time", 0)
            if t > latest:
                latest = t
            if t > self._last_poll_time:
                events.append((t, data.get("title", ""), data.get("message", "").strip()))
        if latest > self._last_poll_time:
            self._last_poll_time = latest
        events.sort(key=lambda e: e[0])
        return [(title, msg) for _, title, msg in events]

    # ---------- 수신 분배 (domiman.py _dispatch_ntfy의 'dB 원격 모드' 분기만 포팅) ----------
    def dispatch(self, title, body):
        """메시지 하나를 분류해 반환. domiman.py _dispatch_ntfy의 dB 분기와
        완전히 동일한 두 갈래(응답은 내 my_id로 echo, 보고는 항상 무명 broadcast):
          ("reply", [필드...])   -- 내 질의/명령에 대한 응답 ({my_id},Z,...)
          ("report", [필드...])  -- 대상 PC의 상황 보고 (,Z,F,... 브로드캐스트)
          (None, None)           -- 무시 대상(대상 PC 것이 아니거나 규격 밖)
        실제 반영(버튼 상태 변경/다이얼로그/알림 표시)은 호출부 책임 — 이 함수는
        파싱·분류만 한다."""
        if title != self.target_id:
            return None, None
        parts = [p.strip() for p in body.split(",")]
        if len(parts) < 2:
            return None, None
        if parts[0] == self.my_id and parts[1] == "Z":
            return "reply", parts[2:]
        if parts[0] == "" and parts[1] == "Z" and len(parts) >= 3 and parts[2] == "F":
            return "report", parts[3:]
        return None, None

    @staticmethod
    def parse_status(rest):
        """S/V/T/C 응답 뒤 필드 파싱: 타이머,해상도,a|m,로그,실행중[,낚싯대,미끼].
        '실행중' 필드(domiman.py 260725d에서 추가, CLAUDE.md ntfy 프로토콜 절
        참고)는 감시모드 여부와 무관하게 항상 5번째 고정 위치 — 낚싯대/미끼처럼
        있다 없다 하면 자리가 밀려 파싱이 꼬이기 때문."""
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
        if len(rest) >= 3:
            try:
                return int(rest[1]), int(rest[2])
            except ValueError:
                return None
        return None


def attempt_login(client, timeout=PENDING_TIMEOUT_SEC):
    """로그인(자동/수동 공통) 시도: cmd_login() 발신 후 timeout초 안에 상태
    응답이 오면 parse_status() 결과를, 응답이 없으면 None을 반환한다.
    (Sheet1 G6 '로그인' 버튼, G5 자동로그인 실패 시 처리에 공통으로 쓰인다.)

    실제 안드로이드 앱에서는 Foreground Service가 앱 실행 내내 이미
    stream()을 돌리고 있을 것이므로 그 큐를 그대로 재사용하면 되고, 이
    함수처럼 매번 새로 스트림 스레드를 여닫을 필요는 없다 — 이 함수는
    '보내고 15초 기다린다'는 규칙 자체를 보여주는 참고 구현이자, 서비스가
    아직 없는 상태(예: 콜드 스타트 자동로그인)에서 쓸 수 있는 독립 버전이다."""
    inbox = queue.Queue()
    stop_event = threading.Event()

    def _reader():
        try:
            client.stream(lambda t, b: inbox.put((t, b)), stop_event.is_set)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    time.sleep(0.3)   # 스트림 연결이 열릴 시간
    client.cmd_login()

    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        try:
            title, body = inbox.get(timeout=0.5)
        except queue.Empty:
            continue
        kind, rest = client.dispatch(title, body)
        if kind == "reply":
            client.resolve_pending()
            status = client.parse_status(rest)
            break
    stop_event.set()
    return status


def attempt_login_json(client, timeout=PENDING_TIMEOUT_SEC):
    """attempt_login()의 Kotlin/Chaquopy 친화 버전 — PyObject dict 대신 JSON
    문자열로 반환한다(Map<PyObject,PyObject> 변환을 Kotlin 쪽에서 다루지 않아도
    되게). 성공 시 상태 dict의 JSON, 실패 시 문자열 "null"."""
    return json.dumps(attempt_login(client, timeout))


def dispatch_json(client, title, body):
    """dispatch()의 Kotlin/Chaquopy 친화 버전. 반환 JSON 스키마:
      {"kind": "reply"|"report"|null,
       "status": {...}|null,           # kind=="reply"이고 상태 응답일 때
       "tank": [cur,mx]|null,           # kind=="reply"이고 N(수량) 응답일 때
       "tank_fail": bool,               # 위와 같되 파싱 실패(",Z,N,fail")
       "report_text": str|null,         # kind=="report"일 때 로그에 띄울 문장
       "report_status_key": str|null}   # 위와 같이 온 상태문구 키(STATUS_TEXT)
    kind에 따라 관련 없는 필드는 그냥 없거나 null이다."""
    kind, rest = client.dispatch(title, body)
    out = {"kind": kind}
    if kind == "reply":
        if rest and rest[0] == "N":
            tank = DomimanClient.parse_tank_reply(rest)
            out["tank"] = list(tank) if tank else None
            out["tank_fail"] = tank is None
        else:
            out["status"] = DomimanClient.parse_status(rest)
    elif kind == "report":
        text, status_key = DomimanClient.report_text(rest, client.target_id)
        out["report_text"] = text
        out["report_status_key"] = status_key
    return json.dumps(out)


# ============================================================
# [2. 로그인 정보 저장 (Sheet1 전체 — 최근 로그인 목록 + 자동로그인 무장상태)]
# ============================================================
@dataclass
class SavedLogin:
    """'최근 로그인' 한 행 (Sheet1 A24:C25 열: ID/피제어PC/ntfy채널명)."""
    my_id: str
    target_pc: str
    channel: str

    def key(self):
        return (self.my_id, self.target_pc, self.channel)

    def to_dict(self):
        return {"id": self.my_id, "target_pc": self.target_pc, "channel": self.channel}

    @staticmethod
    def from_dict(d):
        return SavedLogin(d["id"], d["target_pc"], d["channel"])


class LoginStore:
    """'최근 로그인' 목록 + 자동로그인 '무장(armed)' 상태(Sheet1 전체 규칙).
    실제 영속화(SharedPreferences/DataStore 등)는 Kotlin 쪽 책임이며, **캐시/
    데이터 삭제 시 함께 지워지는 저장소**를 쓸 것(Sheet1 G7 — 계정 백업처럼
    앱 데이터보다 오래 남는 저장소는 금지). 이 클래스는 데이터/규칙만 담당한다.

    사용자 확정 정책:
      - 같은 (id, 피제어PC, 채널) 조합으로 다시 로그인하면 기존 행을 갱신하고
        맨 위(최신)로 옮긴다 — 중복 행을 만들지 않는다.
      - 목록 개수 상한 없음(무제한 보관)."""

    def __init__(self, recent=None, auto_login_enabled=False):
        self.recent = list(recent) if recent else []   # recent[0] = 최신
        self.auto_login_enabled = auto_login_enabled

    def add_or_bump(self, entry):
        """로그인 성공 시 호출(Sheet1 G8 — 자동로그인 체크 유무와 무관하게
        모든 로그인을 기록). 동일 항목이 있으면 갱신 후 맨 앞으로."""
        self.recent = [e for e in self.recent if e.key() != entry.key()]
        self.recent.insert(0, entry)

    def remove(self, entry):
        """길게 눌러 '삭제'(Sheet1 G10)."""
        self.recent = [e for e in self.recent if e.key() != entry.key()]

    def update(self, old_entry, new_entry):
        """길게 눌러 '수정' 확정(Sheet1 G10). 기존 자리를 새 값으로 교체만
        하고 맨 앞으로 옮기지는 않는다 — 재로그인이 아니라 단순 정보 수정."""
        for i, e in enumerate(self.recent):
            if e.key() == old_entry.key():
                self.recent[i] = new_entry
                return True
        return False

    def last(self):
        """가장 최근 로그인(자동로그인 대상). 없으면 None."""
        return self.recent[0] if self.recent else None

    def to_dict(self):
        return {"recent": [e.to_dict() for e in self.recent],
                "auto_login_enabled": self.auto_login_enabled}

    @staticmethod
    def from_dict(d):
        return LoginStore(
            recent=[SavedLogin.from_dict(x) for x in d.get("recent", [])],
            auto_login_enabled=bool(d.get("auto_login_enabled", False)))

    def to_json(self):
        """SharedPreferences처럼 문자열 하나만 다루는 저장소에 그대로 넣을 수
        있는 형태(Kotlin에서 dict/list 변환을 직접 다루지 않아도 되게)."""
        return json.dumps(self.to_dict())

    @staticmethod
    def from_json(s):
        """비어있거나 손상된 값이면 빈 LoginStore(첫 실행과 동일하게 안전 처리)."""
        if not s:
            return LoginStore()
        try:
            return LoginStore.from_dict(json.loads(s))
        except (json.JSONDecodeError, TypeError, KeyError):
            return LoginStore()


# ============================================================
# [3. 화면 상태 모델 (Kotlin ViewModel 상태 필드 대응 참고용)]
# ============================================================
class Screen(Enum):
    LOGIN = "login"              # Sheet1 A1:E18
    RECENT_LOGINS = "recent"     # Sheet1 A21:E39
    MAIN = "main"                # Sheet2 A1:E19


@dataclass
class LoginFormState:
    """Sheet1 A1:E18 입력 폼 상태. mode='edit'이면(Sheet1 G9) 자동로그인
    체크박스가 숨겨지고 버튼이 로그인/…→수정/취소로 바뀐다."""
    my_id: str = ""
    target_pc: str = ""
    channel: str = ""
    auto_login_checked: bool = False
    mode: str = "login"            # 'login' | 'edit'
    editing: SavedLogin = field(default=None)   # mode='edit'일 때 수정 대상 원본


# ============================================================
# [4. 화면 전환/이벤트 규칙 (Kotlin ViewModel 로직 대응 참고용)]
# ============================================================
class LoginFlow:
    """Sheet1(로그인+최근 로그인) 전체의 화면전환·데이터 규칙. 통신은
    attempt_login()/DomimanClient가, 저장은 LoginStore가 맡고 이 클래스는
    그 둘을 화면 이벤트에 연결하는 규칙만 표현한다.

    Sheet2(메인 제어 화면)의 버튼들은 기존 DomimanClient.cmd_* 를 그대로
    호출하면 되므로 여기서 다시 감싸지 않는다 — 새로 규칙이 필요한 것은
    로그인/로그아웃/최근목록 흐름뿐이다."""

    def __init__(self, store):
        self.store = store
        self.screen = Screen.LOGIN
        self.form = LoginFormState()

    def try_auto_login_on_launch(self, make_client, timeout=PENDING_TIMEOUT_SEC):
        """앱 시작 시 1회 호출(Sheet1 G4/G6). 무장 상태 + 최근 로그인이 있으면
        시도한다. 반환: 성공 시 (client, status), 실패/미무장이면 None
        (실패 시 G5대로 폼을 비워 로그인 화면에 남는다)."""
        if not self.store.auto_login_enabled or self.store.last() is None:
            return None
        entry = self.store.last()
        client = make_client(entry.my_id, entry.target_pc, entry.channel)
        status = attempt_login(client, timeout)
        if status is None:
            self.form = LoginFormState()          # G5: 입력값 초기화 + 재입력 메시지
            return None
        self.screen = Screen.MAIN
        return client, status

    def submit_login(self, make_client, my_id, target_pc, channel,
                      auto_login_checked, timeout=PENDING_TIMEOUT_SEC):
        """'로그인' 버튼(Sheet1 B14). 성공하면 최근 로그인 갱신 + 무장상태
        반영 후 Sheet2로 전환(G6). 실패하면 None(화면 전환 없음)."""
        client = make_client(my_id, target_pc, channel)
        status = attempt_login(client, timeout)
        if status is None:
            return None
        self.store.add_or_bump(SavedLogin(my_id, target_pc, channel))
        self.store.auto_login_enabled = auto_login_checked
        self.screen = Screen.MAIN
        return client, status

    def open_recent_logins(self):
        """'…' 버튼(Sheet1 D14/G8) — 최근 로그인 화면으로."""
        self.screen = Screen.RECENT_LOGINS

    def tap_recent_login(self, make_client, entry, timeout=PENDING_TIMEOUT_SEC):
        """최근 로그인 행을 짧게 탭(Sheet1 G8/G9) — 그 정보로 즉시 로그인
        시도. 무장 상태는 건드리지 않는다(단순 재접속이지 로그인 설정 변경이
        아니므로). 성공하면 그 항목을 맨 위로 갱신."""
        client = make_client(entry.my_id, entry.target_pc, entry.channel)
        status = attempt_login(client, timeout)
        if status is None:
            return None
        self.store.add_or_bump(entry)
        self.screen = Screen.MAIN
        return client, status

    def start_edit(self, entry):
        """최근 로그인 행을 길게 눌러 '수정' 선택(Sheet1 G9) — 로그인 화면을
        edit 모드로 전환, 입력란에 기존 값을 채운다."""
        self.form = LoginFormState(my_id=entry.my_id, target_pc=entry.target_pc,
                                   channel=entry.channel, mode="edit", editing=entry)
        self.screen = Screen.LOGIN

    def confirm_edit(self, new_id, new_target_pc, new_channel):
        """edit 모드의 '수정' 버튼(Sheet1 G10) — 목록의 해당 항목 값만 교체
        (맨 위로 옮기지 않음). 완료 후 최근 로그인 화면으로 복귀."""
        old = self.form.editing
        self.store.update(old, SavedLogin(new_id, new_target_pc, new_channel))
        self.form = LoginFormState()
        self.screen = Screen.RECENT_LOGINS

    def cancel_edit(self):
        """edit 모드의 '취소' 버튼(Sheet1 G9)."""
        self.form = LoginFormState()
        self.screen = Screen.RECENT_LOGINS

    def delete_entry(self, entry):
        """길게 눌러 '삭제' 선택(Sheet1 G9/G10)."""
        self.store.remove(entry)

    def back_from_recent(self):
        """'<' 버튼(Sheet1 A22/G11) — 로그인 화면으로."""
        self.screen = Screen.LOGIN

    def logout(self):
        """Sheet2에서 뒤로가기 2번(G20) — 자동로그인 '무장'만 해제하고,
        최근 로그인 목록과 마지막 로그인 값은 그대로 둔다(G21). 로그인
        화면 폼에는 마지막 로그인 정보가 다시 채워지되(값은 남지만 무장은
        꺼진 상태), 자동로그인 체크박스는 꺼진 채로 보여준다."""
        self.store.auto_login_enabled = False
        self.screen = Screen.LOGIN
        last = self.store.last()
        if last:
            self.form = LoginFormState(my_id=last.my_id, target_pc=last.target_pc,
                                       channel=last.channel, auto_login_checked=False)
        else:
            self.form = LoginFormState()


# ============================================================
# [5. Sheet2 로그 창 (항상 표시, 마지막 8줄만 유지 — Sheet2 A15/G15)]
# ============================================================
class MobileLogBuffer:
    """PC와 달리 접기 기능이 없고 항상 펼쳐진 상태로 마지막 maxlen줄만
    유지한다. 'x' 버튼(Sheet2 E15)을 누르면 clear(). 채워 넣을 내용은
    dispatch()의 reply/report 결과, report_text()의 반환 문장, 명령 발신
    에코 등 — domiman.py GUI 로그창에 print()되는 것과 같은 내용이다."""

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

    if len(sys.argv) != 4:
        print("사용법: python domiman_m.py <내 ID> <대상PC 이름> <ntfy 채널 이름>")
        raise SystemExit(1)

    my_id, target_pc, channel = sys.argv[1], sys.argv[2], sys.argv[3]

    store = LoginStore()
    flow = LoginFlow(store)
    make_client = lambda i, p, c: DomimanClient(i, p, c)   # noqa: E731

    print(f"[테스트] '{my_id}' -> '{target_pc}' 로그인 시도 (채널: {channel})...")
    result = flow.submit_login(make_client, my_id, target_pc, channel,
                               auto_login_checked=True)

    if result is None:
        print("실패하였습니다. 다시 시도해주세요")
    else:
        client, status = result
        print(f"[성공] 상태: {status}")
        print(f"[최근 로그인] {[e.to_dict() for e in store.recent]}")
        print(f"[자동로그인 무장] {store.auto_login_enabled}")

        # 로그아웃 -> 재실행(자동로그인 시도) 시나리오까지 확인
        flow.logout()
        print(f"[로그아웃 후] 화면={flow.screen}, 무장={store.auto_login_enabled}, "
              f"폼={flow.form}")
        retry = flow.try_auto_login_on_launch(make_client)
        print(f"[로그아웃 직후 자동로그인 재시도] {'시도 안 함(무장 꺼짐, 정상)' if retry is None else '시도됨(버그)'}")
