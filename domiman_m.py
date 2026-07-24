# -*- coding: utf-8 -*-
"""
domiman_m.py — DOMIMAN 모바일(안드로이드) 원격제어용 ntfy 프로토콜 클라이언트
====================================================================
domiman.py에서 데스크톱 전용 코드(tkinter GUI, pyautogui/win32 클릭, WGC 캡처,
OCR, 낚시 자동화 루틴)를 전부 제외하고 ntfy 통신 부분만 뽑아 새로 정리했다.

휴대폰은 domiman.py의 PC_NAME과 똑같은 방식으로 자기 이름(my_id)을 ntfy
Title에 실어 보낸다(더 이상 무명 발신이 아님) — domiman.py가 항상
`headers={"Title": PC_NAME, ...}`로 보내는 것과 동일. 대상 PC의
`_handle_command(sender=title, ...)`가 그 my_id를 그대로 응답 앞 필드에
echo해 주므로(`f"{sender},Z,{tail}"`), 이쪽에서 my_id로 응답을 매칭한다.
단, 보고 브로드캐스트(`,Z,F,코드`)는 domiman.py에서도 항상 첫 필드가
빈 문자열(무명 broadcast)이라 my_id와 무관하게 그대로 매칭한다.
"제어(dB) 역할"만 맡는다(자기 앞으로 온 '명령'을 받아 실행하는 '피제어(d3)'
쪽 로직 — domiman.py의 _handle_command — 은 필요 없다). 원본 domiman.py의
[2. 설정 파일 + ntfy] 섹션 주석과 같은 폴더 CLAUDE.md의 "ntfy 원격 제어
프로토콜" 절이 원본 규격이다.

Android 포팅 시 Chaquopy로 앱에 내장해 Kotlin에서 호출하는 것을 염두에 두고
작성했다 — 플랫폼 의존성 없이 requests/json/re/time만 쓴다. 안드로이드 API
호출(Foreground Service, 알림, 다크모드 감지, 설정 저장)은 전부 Kotlin 쪽 책임이며
이 모듈은 통신 프로토콜 조립/파싱만 담당한다.

■ 수신 방식(중요): stream()이 권장 방식이다. poll()의 반복 GET은 무료 ntfy
  한도(GET/POST가 같은 IP 버킷을 공유, 5~10초당 1개 충전)를 빠르게 소모해,
  같은 Wi-Fi(같은 공인 IP)를 쓰는 PC/휴대폰의 발신이 429로 실패한다. stream()은
  연결 하나를 열어두고 도착 메시지를 실시간으로 받아 요청 토큰을 '연결당 1회'만
  쓴다(수신 지연도 사실상 0). poll()은 스트리밍이 어려운 환경의 폴백으로만 남긴다.
  Kotlin에서는 OkHttp 스트리밍 응답(response.body().source() 줄 읽기) 또는 /sse
  엔드포인트 + EventSource로 stream()과 동일하게 구현하고, dispatch()/parse_status()
  /report_text()는 그대로 재사용한다.

단독 실행하면 실제 PC의 domiman.py에 로그인(S 질의)만 스트리밍으로 시도해보는
간단한 스모크 테스트가 된다:
    python domiman_m.py <내 ID> <대상PC 이름> <ntfy 채널 이름>
"""
import re
import time
import json

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
# 안드로이드 '상태 문구' 토글을 켰을 때 하단 토스트로 띄울 텍스트.
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
        """로그인 = 상태 질의(S). 15초 안에 응답 오면 성공."""
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
        """실시간 수량 확인(N). 응답: ',Z,N,<cur>,<mx>' 또는 ',Z,N,fail'.
        피제어 PC는 마지막 파싱값이 있으면 즉시, 없으면 창을 띄우고 3초 뒤
        1회 파싱해 응답하므로 최대 5초 안팎 걸릴 수 있다(15초 타임아웃 내)."""
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
        """S/V/T/C 응답 뒤 필드 파싱: 타이머,해상도,a|m,로그[,낚싯대,미끼]."""
        if len(rest) < 4:
            return None
        status = {
            "timer": rest[0],
            "resolution": rest[1],
            "res_auto": rest[2] == "a",
            "logsave": rest[3] == "t",
        }
        if len(rest) >= 6:
            status["rod"] = rest[4] == "t"
            status["bait"] = rest[5] == "t"
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


if __name__ == "__main__":
    import sys
    import queue as _queue
    import threading as _threading

    if len(sys.argv) != 4:
        print("사용법: python domiman_m.py <내 ID> <대상PC 이름> <ntfy 채널 이름>")
        raise SystemExit(1)

    client = DomimanClient(sys.argv[1], sys.argv[2], sys.argv[3])

    # 스트리밍 연결을 배경 스레드로 열어 도착 메시지를 큐로 넘긴다
    # (domiman.py의 _ntfy_stream_loop + _ntfy_queue 구조와 동일한 참조 패턴.
    #  Kotlin에서는 Foreground Service의 백그라운드 스레드가 이 역할을 맡는다.)
    inbox = _queue.Queue()
    stop = _threading.Event()

    def _reader():
        while not stop.is_set():
            try:
                client.stream(lambda tt, bb: inbox.put((tt, bb)), stop.is_set)
            except Exception:
                pass
            if not stop.is_set():
                time.sleep(1.0)   # 재연결 전 짧은 대기

    _threading.Thread(target=_reader, daemon=True).start()
    time.sleep(1.0)   # 스트림이 열릴 시간을 준 뒤 로그인 발신

    print(f"[테스트] '{client.my_id}' -> '{client.target_id}' 로그인 시도 (채널: {client.channel})...")
    client.cmd_login()

    deadline = time.time() + PENDING_TIMEOUT_SEC
    ok = False
    while time.time() < deadline and not ok:
        try:
            title, body = inbox.get(timeout=0.5)
        except _queue.Empty:
            continue
        kind, rest = client.dispatch(title, body)
        if kind == "reply":
            client.resolve_pending()
            print(f"[성공] 상태: {client.parse_status(rest)}")
            ok = True
        elif kind == "report":
            text, _ = client.report_text(rest, client.target_id)
            if text:
                print(f"[보고] {text}")

    stop.set()
    if not ok:
        print("실패하였습니다. 다시 시도해주세요")
