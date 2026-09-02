/**
 * domiweb(중계 서버)와의 WebSocket 연결.
 *
 * 브라우저는 raw TCP를 못 열고 domichat의 지문 고정(TOFU)도 못 쓴다. 그래서 이
 * 앱은 domiserver에 직접 붙지 않고 **domiweb**에 붙는다. domiweb 하나가 `web`
 * 계정으로 domiserver에 붙어 브라우저 여러 대를 다중화하므로, **이 앱에는 계정도
 * 비밀번호도 없다**(공개 정적 사이트에 자격을 박는 문제가 구조적으로 없다).
 *
 * 방에서 온 원문(`web,Z,...` 등)은 그대로 넘어온다 — 해석은 protocol.ts가 한다.
 */

/** 기본 접속 주소. domiweb를 다른 곳에 띄웠다면 `?ws=wss://호스트:포트/ws` 로 덮어쓴다
 *  (개발 중에는 `?ws=ws://localhost:47822/ws`). */
const DEFAULT_WS = "wss://domiman.duckdns.org:47822/ws";

export function relayUrl(): string {
  const q = new URLSearchParams(location.search).get("ws");
  if (q) {
    try {
      sessionStorage.setItem("domiman_ws", q);
    } catch { /* 사생활 보호 모드 등 — 무시 */ }
    return q;
  }
  try {
    return sessionStorage.getItem("domiman_ws") || DEFAULT_WS;
  } catch {
    return DEFAULT_WS;
  }
}

export type PcState = {
  joined: boolean;
  online: boolean | null;      // null = 아직 모름
  reason: string;
};

export type RelayEvent =
  | { t: "link"; state: "connecting" | "open" | "closed"; msg?: string }
  | { t: "ready"; my_id: string; pcs: string[]; connected: boolean; version?: string }
  | ({ t: "snap"; pc: string; status: string | null; tank: string | null;
       reports: [number, string][] } & PcState)
  | { t: "msg"; pc: string; body: string }
  | { t: "pcs"; pcs: string[] }
  | ({ t: "pc"; pc: string } & PcState)
  | { t: "up"; connected: boolean; msg?: string }
  | { t: "shot"; pc: string; ok: boolean; name?: string; b64?: string; reason?: string }
  | { t: "err"; msg: string };

// domiweb 쪽과 같은 백오프(1·2·5·10·30초). 휴대폰이 절전에서 깨어날 때는
// wake()가 즉시 재연결시키므로 이 표는 '서버가 죽었을 때'의 간격이다.
const BACKOFF = [1000, 2000, 5000, 10000, 30000];

export class Relay {
  private ws: WebSocket | null = null;
  private timer: number | null = null;
  private idx = 0;
  private closed = false;
  private pc = "";

  constructor(private url: string, private onEvent: (e: RelayEvent) => void) {}

  start() {
    this.closed = false;
    this.open();
  }

  private open() {
    if (this.closed) return;
    this.clearTimer();
    this.onEvent({ t: "link", state: "connecting" });
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      return this.scheduleRetry(String(e));
    }
    this.ws = ws;
    ws.onopen = () => {
      this.idx = 0;
      this.onEvent({ t: "link", state: "open" });
      if (this.pc) this.send({ t: "select", pc: this.pc });
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") return;
      let d: unknown;
      try {
        d = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (d && typeof d === "object" && "t" in d) this.onEvent(d as RelayEvent);
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.scheduleRetry("연결이 끊겼습니다");
    };
    ws.onerror = () => { /* onclose가 뒤따른다 — 여기서 재시도하면 두 번 걸린다 */ };
  }

  private scheduleRetry(msg: string) {
    if (this.closed || this.timer !== null) return;
    this.onEvent({ t: "link", state: "closed", msg });
    const wait = BACKOFF[Math.min(this.idx, BACKOFF.length - 1)];
    this.idx += 1;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.open();
    }, wait);
  }

  private clearTimer() {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /** 탭이 다시 보이거나 네트워크가 살아났을 때 호출. 모바일 브라우저는 백그라운드
   *  탭의 소켓을 얼려 버리므로, 복귀 시 **기다리지 않고 즉시** 다시 붙어야 한다. */
  wake() {
    if (this.closed) return;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
    this.idx = 0;
    this.clearTimer();
    this.open();
  }

  get live(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  send(obj: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }

  select(pc: string) {
    this.pc = pc;
    this.send({ t: "select", pc });
  }

  cmd(pc: string, body: string): boolean {
    return this.send({ t: "cmd", pc, body });
  }

  addPc(pc: string) {
    this.send({ t: "add_pc", pc });
  }

  delPc(pc: string) {
    this.send({ t: "del_pc", pc });
  }

  stop() {
    this.closed = true;
    this.clearTimer();
    const ws = this.ws;
    this.ws = null;
    try {
      ws?.close();
    } catch { /* 이미 닫힘 */ }
  }
}
