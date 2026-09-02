/**
 * domiman 원격제어 메시지 규격 — `domiman_m.py`의 파서·표를 그대로 옮긴 것.
 *
 * 규격의 단일 기준은 `domichat.md` / `domiman.py`이며, **글자 하나 다르지 않다**:
 *   명령:  "(대상PC),(명령)[,인자...]"      예) seoul,S / seoul,T,30
 *   응답:  "(요청자ID),Z,..."               예) web,Z,0,1080,a,f,t,t,t
 *   보고:  ",Z,F,(코드)[,(서브)]"           예) ,Z,F,y,b
 *   수량:  ",Z,N,(cur),(mx)" / ",Z,N,fail"  — 요청 없이 사이클마다 오는 방송
 *
 * 여기서 '요청자ID'는 domiweb의 domichat 로그인 ID(기본 "web")다. 브라우저는
 * 로그인하지 않고 domiweb가 대신 붙으므로, 그 ID를 domiweb가 `ready`로 알려준다.
 *
 * ⚠ 사본 동기화: `domiman_m.py`(모바일)와 이 파일은 자동으로 맞춰지지 않는다.
 * PC 쪽 프로토콜을 바꾸면 **세 곳을 같이** 고쳐야 한다.
 */

// 상태 문구 (domiman.py STATUS_TEXT / 엑셀 J8:K18 원본)
export const STATUS_TEXT: Record<string, string> = {
  loading: "강태공이 낚시터에 들어오고 있습니다.",
  idle: "강태공이 낚시를 준비합니다.",
  fishing: "강태공이 낚시를 시작했습니다.",
  collect: "강태공이 월척을 낚았습니다.",
  collect3: "강태공이 3초 후에 낚싯대를 올리기 시작합니다.",
  rod: "강태공이 낚싯대를 재정비합니다.",
  bait: "강태공이 미끼를 바꿉니다.",
  parsefail: "강태공이 볼일을 보러 나갔습니다.",
  ocrfail: "강태공이 오던 중 교통사고를 당했습니다.",
  nowindow: "강태공이 낚싯대를 잃어버렸습니다.",
  disconnect: "강태공이 물에 빠졌습니다.",
  noresp: "강태공이 의식을 잃었습니다.",
};

// 보고(,Z,F,*) -> 문장 / 상태문구 키. {name}=제어 중인 PC 이름.
const REPORT_TEXT: Record<string, string> = {
  s: "{name}의 낚시 루틴이 시작되었습니다.",
  g: "{name}의 살림망 회수가 완료되었습니다.",
  f: "{name}의 살림망 회수가 실패하였습니다.",
  rs: "{name}이(가) 낚싯대 교체를 시작합니다.",
  "y|r": "{name}의 낚싯대 교체가 성공하였습니다.",
  "x|r": "{name}의 낚싯대 교체가 실패하였습니다.",
  bs: "{name}이(가) 미끼 교체를 시작합니다.",
  "y|b": "{name}의 미끼 교체가 성공하였습니다.",
  "x|b": "{name}의 미끼 교체가 실패하였습니다.",
  "x|d": "{name}의 게임 연결이 끊겼습니다.",
};
const REPORT_STATUS: Record<string, string> = {
  s: "collect", g: "fishing", f: "fishing",
  rs: "rod", "y|r": "fishing", "x|r": "fishing",
  bs: "bait", "y|b": "fishing", "x|b": "fishing",
  "x|d": "disconnect",
};

export type Status = {
  timer: string;
  resolution: string;   // "1080" | "1440" | "0"(미설정)
  resAuto: boolean;
  logsave: boolean;
  running: boolean;
  rod?: boolean;        // 감시 모드(타이머 0)일 때만 온다
  bait?: boolean;
};

export type Tank = [number, number];

export type Dispatched =
  | { kind: "status"; status: Status }
  | { kind: "echo"; echo: string; schedMinutes?: string; shotFail?: boolean }
  | { kind: "tankReply"; tank: Tank | null }
  | { kind: "tank"; tank: Tank | null }
  | { kind: "report"; text: string; statusKey?: string }
  | null;

/** S/V/T/C 응답 뒤 필드: 타이머,해상도,a|m,로그,실행중[,낚싯대,미끼].
 *  '실행중'은 감시모드 여부와 무관하게 **항상 5번째 고정 위치**다 — 낚싯대/미끼처럼
 *  있다 없다 하면 자리가 밀려 파싱이 꼬인다(PC와 반드시 같이 맞출 것). */
export function parseStatus(rest: string[]): Status | null {
  if (rest.length < 5) return null;
  const st: Status = {
    timer: rest[0],
    resolution: rest[1],
    resAuto: rest[2] === "a",
    logsave: rest[3] === "t",
    running: rest[4] === "t",
  };
  if (rest.length >= 7) {
    st.rod = rest[5] === "t";
    st.bait = rest[6] === "t";
  }
  return st;
}

/** ['12','470'] -> [12,470]. 'fail'이나 규격 밖이면 null(판독 실패).
 *  N 응답과 수량 방송이 **같은 파서를 쓴다** — 어긋나면 새로고침으로 본 값과
 *  상시 표시가 서로 달라진다. */
export function parseTankFields(fields: string[]): Tank | null {
  if (fields.length < 2) return null;
  const cur = Number(fields[0]);
  const mx = Number(fields[1]);
  if (!Number.isInteger(cur) || !Number.isInteger(mx)) return null;
  return [cur, mx];
}

/**
 * 메시지 한 줄을 분류한다. `from`(발신 PC) 확인은 domiweb가 이미 했다.
 *
 * 'tank'(수량 방송)는 **응답이 아니다** — 요청 없이 사이클마다 오므로 응답 대기
 * (pending)를 소모해선 안 된다. 호출부는 kind로 그것을 구분한다.
 */
export function dispatch(body: string, myId: string, pcName: string): Dispatched {
  const parts = body.split(",").map((p) => p.trim());
  if (parts.length < 2) return null;

  if (parts[0] === myId && parts[1] === "Z") {
    const rest = parts.slice(2);
    const first = rest[0] ?? "";
    if (first === "N") {
      return { kind: "tankReply", tank: parseTankFields(rest.slice(1)) };
    }
    if (first === "I") {
      // 스크린샷 ack — ',Z,I'면 사진을 기다리고, ',Z,I,fail'이면 그 자리에서 끝.
      return { kind: "echo", echo: "I", shotFail: rest[1] === "fail" };
    }
    if (["G", "P", "W", "Q", "Y"].includes(first)) {
      // 상태 필드 없이 명령 글자만 되돌아오는 '에코'. 상태 응답(S/V/T/C)은 첫
      // 필드가 타이머 숫자라 이 글자들과 겹치지 않는다.
      return { kind: "echo", echo: first, schedMinutes: first === "Y" ? rest[1] : undefined };
    }
    const st = parseStatus(rest);
    return st ? { kind: "status", status: st } : null;
  }

  if (parts[0] === "" && parts[1] === "Z" && parts.length >= 3) {
    const rest = parts.slice(3);
    if (parts[2] === "F") {
      const key = rest.length >= 2 && REPORT_TEXT[`${rest[0]}|${rest[1]}`]
        ? `${rest[0]}|${rest[1]}`
        : rest[0];
      const tmpl = REPORT_TEXT[key];
      if (!tmpl) return null;
      return { kind: "report", text: tmpl.replace("{name}", pcName), statusKey: REPORT_STATUS[key] };
    }
    if (parts[2] === "N") {
      return { kind: "tank", tank: parseTankFields(rest) };
    }
  }
  return null;
}

/** 명령 문자열 빌더 (domiweb가 `{대상},{여기}` 로 조립해 방에 보낸다). */
export const CMD = {
  status: () => "S",
  start: () => "G",
  stop: () => "P",
  schedAsk: () => "Y",
  schedSet: (minutes: number) => `Y,${minutes}`,
  collectNow: () => "W",
  exitProgram: () => "Q",
  resolution: (mode: "a" | "1080" | "1440") => `V,${mode}`,
  timer: (minutes: number) => `T,${minutes}`,
  flags: (logsave: boolean, rod?: boolean, bait?: boolean) => {
    const tf = (b: boolean) => (b ? "t" : "f");
    const parts = [tf(logsave)];
    if (rod !== undefined && bait !== undefined) parts.push(tf(rod), tf(bait));
    return `C,${parts.join(",")}`;
  },
  tankQuery: () => "N",
  screenshot: () => "I",
};

/** 타이머 0(또는 o/O) = 살림망 감시 모드 — 낚싯대·미끼 자동교체가 그때만 유효. */
export function isWatchMode(timer: string): boolean {
  const t = timer.trim();
  return t === "" || t === "0" || t.toLowerCase() === "o" || Number(t) === 0;
}

export const PENDING_TIMEOUT_MS = 15000;   // domiman.py _check_pending_timeout과 동일
