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

/** 방 목록 한 줄 — domiserver `rooms_snapshot()` 그대로. */
export type RoomRow = {
  name: string;
  kind: "open" | "pw" | "allow" | "approve";
  owner: string | null;
  created: number;
  allowed: boolean;      // 추가 절차 없이 바로 들어갈 수 있는지
  blocked?: boolean;
  waiting?: boolean;     // 사후 승인 방에 요청을 넣어 둔 상태
};

export type RelayEvent =
  | { t: "link"; state: "connecting" | "open" | "closed"; msg?: string }
  | { t: "ready"; my_id: string; pcs: string[]; connected: boolean; chat?: boolean;
      version?: string }
  | ({ t: "snap"; pc: string; status: string | null; tank: string | null;
       reports: [number, string][] } & PcState)
  | { t: "msg"; pc: string; body: string }
  | { t: "pcs"; pcs: string[] }
  | ({ t: "pc"; pc: string } & PcState)
  | { t: "up"; connected: boolean; msg?: string }
  | { t: "shot"; pc: string; ok: boolean; name?: string; b64?: string; reason?: string }
  | { t: "err"; msg: string }
  // --- 채팅 (domiweb 260913a) ---
  | { t: "rooms"; list: RoomRow[] }
  | { t: "room"; room: string;
      state: "joined" | "denied" | "closed" | "deleted" | "kicked" | "member"
           | "approve_res";
      kind?: string; owner?: string | null; reason?: string; msg?: string;
      id?: string; in?: boolean; ok?: boolean }
  | { t: "chat"; room: string; from: string; body: string; mid?: string; ts?: number;
      cid?: string }
  | { t: "chat_img"; room: string; from: string; fid: string; name: string;
      ts: number; b64: string }
  | { t: "chat_hist"; room: string; items: { room: string; from: string; body: string;
      mid?: string; ts?: number }[] }
  | { t: "chat_err"; room: string; msg: string };

// domiweb 쪽과 같은 백오프(1·2·5·10·30초). 휴대폰이 절전에서 깨어날 때는
// wake()가 즉시 재연결시키므로 이 표는 '서버가 죽었을 때'의 간격이다.
const BACKOFF = [1000, 2000, 5000, 10000, 30000];

export class Relay {
  private ws: WebSocket | null = null;
  private timer: number | null = null;
  private idx = 0;
  private closed = false;
  private pc = "";
  /** 열어 둔 채팅방 -> 입력했던 비밀번호(있으면). **메모리에만 둔다** —
   *  domichat은 '구독한 방만 비번을 기억'하고 웹에는 구독이 없다. 끊겼다 붙을 때
   *  같은 방에 말없이 다시 들어가기 위한 것이다. */
  private rooms = new Map<string, string | undefined>();

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
      // 보던 방에 다시 들어간다. 중계가 그 방에 그대로 있었다면 곧바로 joined가
      // 오고, 마지막 사람이 나가 방을 떠났었다면 비번으로 다시 입장한다.
      for (const [room, pw] of this.rooms) this.send({ t: "room_open", room, pw });
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

  // ---------- 채팅 ----------
  roomsAsk() {
    this.send({ t: "rooms" });
  }

  roomOpen(room: string, pw?: string) {
    this.rooms.set(room, pw);
    return this.send({ t: "room_open", room, pw });
  }

  roomClose(room: string) {
    this.rooms.delete(room);
    this.send({ t: "room_close", room });
  }

  /** 입장이 거절됐거나 방이 사라졌다 — 재접속 때 다시 들어가려 하지 않게 지운다. */
  roomForget(room: string) {
    this.rooms.delete(room);
  }

  chat(room: string, body: string, cid: string): boolean {
    return this.send({ t: "chat", room, body, cid });
  }

  /** 이미지 청크는 크다 — 소켓 버퍼가 부푸는 동안 잠깐 쉰다(모바일 회선 보호). */
  async drain(limit = 1024 * 1024): Promise<boolean> {
    for (let i = 0; i < 600; i += 1) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
      if (this.ws.bufferedAmount <= limit) return true;
      await new Promise((r) => window.setTimeout(r, 50));
    }
    return false;
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
