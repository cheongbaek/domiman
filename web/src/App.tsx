import { useCallback, useEffect, useRef, useState } from "react";
import { PcState, Relay, RelayEvent, relayUrl } from "./lib/relay";
import {
  CMD, PENDING_TIMEOUT_MS, STATUS_TEXT, Status, Tank, dispatch, isWatchMode,
} from "./lib/protocol";
import { ScreenshotView } from "./ui/Screenshot";

type LogLine = { t: string; s: string };
type Pending = { kind: string; at: number };
type Theme = "system" | "light" | "dark";

const LOG_MAX = 200;
const SHOT_WAIT_MS = 30000;      // ack 이후 사진이 오기까지 기다리는 상한
const TIMER_DEBOUNCE_MS = 1500;  // 모바일 앱과 같은 값(입력 멈춘 뒤 T 발신)

const RES_TEXT: Record<string, string> = {
  "1080": "1920 x 1080", "1440": "2560 x 1440",
};

function now(): string {
  // domiman/domiserver 로그와 같은 HH:MM:SS. toLocaleTimeString('ko-KR')은
  // "1시 45분 18초"처럼 길고 자리수도 흔들려 로그를 읽기 어렵다.
  const d = new Date();
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

function loadTheme(): Theme {
  try {
    const v = localStorage.getItem("domiman_theme");
    if (v === "light" || v === "dark") return v;
  } catch { /* 무시 */ }
  return "system";
}

export function App() {
  // ---------- 연결 ----------
  const [link, setLink] = useState<"connecting" | "open" | "closed">("connecting");
  const [up, setUp] = useState(false);            // domiweb ↔ domiserver
  const [myId, setMyId] = useState("web");
  const myIdRef = useRef("web");

  // ---------- 대상 PC ----------
  const [pcs, setPcs] = useState<string[]>([]);
  const [pc, setPc] = useState("");
  const pcRef = useRef("");
  const [pcState, setPcState] = useState<PcState>({ joined: false, online: null, reason: "" });

  // ---------- 상태 ----------
  const [status, setStatus] = useState<Status | null>(null);
  const [tank, setTank] = useState<Tank | null>(null);
  const [tankFail, setTankFail] = useState(false);
  const [statusKey, setStatusKey] = useState("loading");
  const [pending, setPending] = useState<Pending | null>(null);
  const [log, setLog] = useState<LogLine[]>([]);

  // ---------- 입력 ----------
  const [timerInput, setTimerInput] = useState("");
  const timerDirty = useRef(false);
  const [theme, setTheme] = useState<Theme>(loadTheme);

  // ---------- 창 ----------
  const [resDlg, setResDlg] = useState(false);
  const [schedDlg, setSchedDlg] = useState(false);
  const [schedMin, setSchedMin] = useState("60");
  const [shotWait, setShotWait] = useState(0);
  const [shot, setShot] = useState<{ name: string; url: string } | null>(null);

  const relayRef = useRef<Relay | null>(null);
  const logBox = useRef<HTMLDivElement | null>(null);

  const addLog = useCallback((s: string) => {
    setLog((prev) => [...prev, { t: now(), s }].slice(-LOG_MAX));
  }, []);

  // ---------- 테마 ----------
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      if (theme === "system") localStorage.removeItem("domiman_theme");
      else localStorage.setItem("domiman_theme", theme);
    } catch { /* 무시 */ }
  }, [theme]);

  // ---------- 수신 처리 ----------
  /** 메시지 한 줄 적용. hist=true(스냅샷 재생)면 **응답 대기를 풀지 않는다** —
   *  나중에 붙은 브라우저가 옛 응답으로 pending을 소모하면 UI가 잘못 열린다. */
  const applyBody = useCallback((body: string, hist: boolean) => {
    const d = dispatch(body, myIdRef.current, pcRef.current);
    if (!d) return;
    if (d.kind === "status") {
      setStatus(d.status);
      if (!timerDirty.current) setTimerInput(d.status.timer);
      setStatusKey(d.status.running ? "fishing" : "idle");
      if (!hist) setPending(null);
      return;
    }
    if (d.kind === "echo") {
      if (!hist) setPending(null);
      if (d.echo === "G") {
        setStatus((s) => (s ? { ...s, running: true } : s));
        setStatusKey("fishing");
        if (!hist) addLog("낚시를 시작했습니다.");
      } else if (d.echo === "P") {
        setStatus((s) => (s ? { ...s, running: false } : s));
        setStatusKey("idle");
        if (!hist) addLog("낚시를 중지했습니다.");
      } else if (d.echo === "W") {
        if (!hist) { setStatusKey("collect3"); addLog("즉시 회수를 요청했습니다."); }
      } else if (d.echo === "Q") {
        if (!hist) addLog("프로그램 종료를 요청했습니다.");
      } else if (d.echo === "Y") {
        if (hist) return;
        if (d.schedMinutes === undefined) {
          setSchedDlg(true);                  // 1단계 ack — 분 입력 창을 연다
        } else {
          setSchedDlg(false);
          const n = Number(d.schedMinutes);
          addLog(n === 0 ? "예약 종료를 해제했습니다." : `${n}분 뒤 종료로 예약했습니다.`);
        }
      } else if (d.echo === "I") {
        if (hist) return;
        if (d.shotFail) {
          setShotWait(0);
          addLog("스크린샷을 찍을 수 없습니다.");
        } else {
          setShotWait(Date.now());
          addLog("강태공이 사진을 찍고 있습니다.");
        }
      }
      return;
    }
    if (d.kind === "tankReply") {
      setTank(d.tank);
      setTankFail(d.tank === null);
      if (!hist) {
        setPending(null);
        addLog(d.tank ? `살림망 ${d.tank[0]}/${d.tank[1]}` : "살림망 수량을 읽지 못했습니다.");
      }
      return;
    }
    if (d.kind === "tank") {
      // 사이클마다 오는 방송 — 응답 대기를 건드리지 않고 표시만 갱신한다.
      setTank(d.tank);
      setTankFail(d.tank === null);
      return;
    }
    if (d.kind === "report") {
      if (d.statusKey) setStatusKey(d.statusKey);
      addLog(d.text);
    }
  }, [addLog]);

  const onEvent = useCallback((e: RelayEvent) => {
    switch (e.t) {
      case "link":
        setLink(e.state);
        if (e.state === "closed") setPending(null);
        return;
      case "ready": {
        myIdRef.current = e.my_id;
        setMyId(e.my_id);
        setPcs(e.pcs);
        setUp(e.connected);
        // 지난번에 보던 PC를 되살리고, 없으면 첫 항목을 고른다.
        let want = pcRef.current;
        if (!want) {
          try { want = localStorage.getItem("domiman_pc") || ""; } catch { want = ""; }
        }
        const next = e.pcs.includes(want) ? want : (e.pcs[0] ?? "");
        if (next) {
          pcRef.current = next;
          setPc(next);
          relayRef.current?.select(next);
        }
        return;
      }
      case "pcs":
        setPcs(e.pcs);
        return;
      case "up":
        setUp(e.connected);
        if (!e.connected) setPending(null);
        return;
      case "pc":
        if (e.pc !== pcRef.current) return;
        setPcState({ joined: e.joined, online: e.online, reason: e.reason });
        return;
      case "snap": {
        if (e.pc !== pcRef.current) return;
        setPcState({ joined: e.joined, online: e.online, reason: e.reason });
        setStatus(null);
        setTank(null);
        setTankFail(false);
        setStatusKey("loading");
        if (e.status) applyBody(e.status, true);
        if (e.tank) applyBody(e.tank, true);
        for (const [, body] of e.reports) applyBody(body, true);
        return;
      }
      case "msg":
        if (e.pc !== pcRef.current) return;
        applyBody(e.body, false);
        return;
      case "shot": {
        if (e.pc !== pcRef.current) return;
        setShotWait(0);
        if (!e.ok || !e.b64) {
          addLog(`사진을 받지 못했습니다. (${e.reason ?? "실패"})`);
          return;
        }
        const bin = atob(e.b64);
        const buf = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i += 1) buf[i] = bin.charCodeAt(i);
        const url = URL.createObjectURL(new Blob([buf], { type: "image/png" }));
        setShot((old) => {
          if (old) URL.revokeObjectURL(old.url);   // 안 풀면 사진마다 2MB가 남는다
          return { name: e.name ?? "screenshot.png", url };
        });
        addLog(`사진을 받았습니다. (${(bin.length / 1048576).toFixed(2)}MB)`);
        return;
      }
      case "err":
        addLog(e.msg);
        return;
    }
  }, [addLog, applyBody]);

  // ---------- 연결 수립 ----------
  useEffect(() => {
    const r = new Relay(relayUrl(), onEvent);
    relayRef.current = r;
    r.start();
    // 모바일 브라우저는 백그라운드 탭의 소켓을 얼린다 — 복귀하면 즉시 다시 붙고
    // 상태를 새로 받아온다(모바일 앱의 ON_RESUME resync와 같은 취지).
    const wake = () => {
      if (document.visibilityState === "visible") {
        r.wake();
        if (r.live && pcRef.current) r.select(pcRef.current);
      }
    };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("online", wake);
    return () => {
      document.removeEventListener("visibilitychange", wake);
      window.removeEventListener("online", wake);
      r.stop();
    };
  }, [onEvent]);

  // ---------- 응답 대기 · 사진 대기 타임아웃 ----------
  useEffect(() => {
    if (pending === null && shotWait === 0) return;
    const id = window.setInterval(() => {
      if (pending && Date.now() - pending.at > PENDING_TIMEOUT_MS) {
        setPending(null);
        setStatusKey("noresp");
        addLog("응답이 없습니다.");
        setSchedDlg(false);
      }
      if (shotWait && Date.now() - shotWait > SHOT_WAIT_MS) {
        setShotWait(0);
        addLog("사진이 오지 않았습니다.");
      }
    }, 250);
    return () => window.clearInterval(id);
  }, [pending, shotWait, addLog]);

  // ---------- 로그 자동 스크롤 ----------
  useEffect(() => {
    const el = logBox.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  // ---------- 발신 ----------
  const canSend = link === "open" && up && !!pc && pcState.joined && pending === null;

  const send = useCallback((body: string, kind: string) => {
    const r = relayRef.current;
    if (!r || !pcRef.current) return;
    if (r.cmd(pcRef.current, body)) setPending({ kind, at: Date.now() });
  }, []);

  // 타이머: 입력이 멈춘 뒤 1.5초에 발신(모바일 앱과 같은 규칙)
  useEffect(() => {
    if (!timerDirty.current) return;
    const id = window.setTimeout(() => {
      const n = Number(timerInput.trim());
      if (!Number.isFinite(n) || n < 0) return;
      timerDirty.current = false;
      send(CMD.timer(n), "T");
    }, TIMER_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [timerInput, send]);

  function selectPc(next: string) {
    pcRef.current = next;
    setPc(next);
    setPending(null);
    setStatus(null);
    setTank(null);
    setStatusKey("loading");
    try { localStorage.setItem("domiman_pc", next); } catch { /* 무시 */ }
    relayRef.current?.select(next);
  }

  function addPc() {
    const name = window.prompt("추가할 PC의 domichat ID")?.trim();
    if (!name) return;
    relayRef.current?.addPc(name);
  }

  function delPc() {
    if (!pc) return;
    if (!window.confirm(`'${pc}'를 목록에서 지웁니다.`)) return;
    relayRef.current?.delPc(pc);
  }

  function setFlags(next: { rod?: boolean; bait?: boolean }) {
    if (!status) return;
    const watch = isWatchMode(status.timer);
    const rod = next.rod ?? status.rod ?? true;
    const bait = next.bait ?? status.bait ?? true;
    setStatus({ ...status, rod, bait });
    send(watch ? CMD.flags(status.logsave, rod, bait) : CMD.flags(status.logsave), "C");
  }

  // ---------- 표시값 ----------
  const watch = status ? isWatchMode(status.timer) : false;
  const resLabel = status
    ? `${RES_TEXT[status.resolution] ?? "미설정"}${
        status.resolution === "0" ? "" : status.resAuto ? " (자동 감지됨)" : " (직접 설정)"}`
    : "-";
  const linkText = link === "open"
    ? (up ? (pcState.joined
        ? (pcState.online === false ? `${pc} 꺼짐` : `${pc} 제어 중`)
        : pcState.reason === "no_room" ? `${pc} 미접속` : `${pc} 입장 불가`)
      : "서버 연결 끊김")
    : link === "connecting" ? "연결 중…" : "중계 연결 끊김";
  const statusMsg = pending
    ? "명령을 보냈습니다. 응답을 기다립니다."
    : shotWait ? STATUS_TEXT.loading && "강태공이 사진을 찍고 있습니다."
    : (STATUS_TEXT[statusKey] ?? "");

  return (
    <div className="wrap">
      <div className="top">
        <span className={`dot ${link === "open" && up ? "on" : link === "connecting" ? "" : "off"}`} />
        <h1>DOMIMAN</h1>
        <span className="value">{linkText}</span>
        <span className="spacer" />
        <span className={`tank ${tankFail ? "fail" : ""}`}>
          {tank ? `살림망 ${tank[0]}/${tank[1]}` : tankFail ? "살림망 판독 실패" : "살림망 –"}
        </span>
      </div>

      <div className="row tight">
        <select className="pc" value={pc} onChange={(e) => selectPc(e.target.value)}>
          {pcs.length === 0 && <option value="">(목록 없음)</option>}
          {pcs.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button className="icon" onClick={addPc} title="PC 추가">＋</button>
        <button className="icon" onClick={delPc} title="목록에서 삭제" disabled={!pc}>－</button>
      </div>

      <div className="cols">
        <div>
          <div className="row tight">
            <span className="label">해상도</span>
            <span className="spacer" />
            <span className="value">{resLabel}</span>
          </div>
          <div className="grid3">
            <button disabled={!canSend} onClick={() => setResDlg(true)}>직접 설정</button>
            <button disabled={!canSend} onClick={() => send(CMD.resolution("a"), "V")}>자동 감지</button>
            <button disabled={!canSend || shotWait !== 0}
                    onClick={() => send(CMD.screenshot(), "I")}>스크린샷</button>
          </div>

          <div className="row tight">
            <span className="label">타이머</span>
            <input className="timer" type="number" min={0} inputMode="numeric"
                   value={timerInput} disabled={!canSend}
                   onChange={(e) => { timerDirty.current = true; setTimerInput(e.target.value); }} />
            <span className="value">(분)</span>
          </div>
          <div className="note">0을 입력하면 살림망 감시 모드로 작동합니다.</div>

          <label className={`check ${watch ? "" : "off"}`}>
            <input type="checkbox" checked={status?.rod ?? false} disabled={!canSend || !watch}
                   onChange={(e) => setFlags({ rod: e.target.checked })} />
            <span>낚싯대 자동교체</span>
          </label>
          <label className={`check ${watch ? "" : "off"}`}>
            <input type="checkbox" checked={status?.bait ?? false} disabled={!canSend || !watch}
                   onChange={(e) => setFlags({ bait: e.target.checked })} />
            <span>미끼 자동교체</span>
          </label>
          <div className="note">낚싯대, 미끼 자동교체는 살림망 감시 모드에서만 사용 가능합니다.</div>

          <button className="big" disabled={!canSend}
                  onClick={() => send(status?.running ? CMD.stop() : CMD.start(),
                                      status?.running ? "P" : "G")}>
            {status === null ? "대기" : status.running ? "중지" : "시작"}
          </button>
          <div className={`status ${statusKey === "noresp" ? "warn" : ""}`}>{statusMsg}</div>

          <div className="grid2">
            <button disabled={!canSend} onClick={() => send(CMD.schedAsk(), "Y1")}>예약 종료</button>
            <button disabled={!canSend} onClick={() => send(CMD.collectNow(), "W")}>즉시 회수</button>
          </div>
          <div className="grid2">
            <button disabled={!canSend} onClick={() => send(CMD.tankQuery(), "N")}>실시간 수량확인</button>
            <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>다크모드</button>
          </div>
        </div>

        <div>
          <div className="loghead">
            <span className="label">로그</span>
            <button className="icon" onClick={() => setLog([])} title="로그 지우기">×</button>
          </div>
          <div className="log" ref={logBox}>
            {log.length === 0
              ? <div className="empty">(로그 없음)</div>
              : log.map((l, i) => (
                  <div key={i}><span className="t">{l.t}</span> {l.s}</div>
                ))}
          </div>
          <div className="note" style={{ marginTop: 8 }}>
            중계 {myId} · {relayUrl()}
          </div>
        </div>
      </div>

      {resDlg && (
        <div className="veil" onClick={() => setResDlg(false)}>
          <div className="card" onClick={(e) => e.stopPropagation()}>
            <h2>해상도 직접 설정</h2>
            <div className="grid2">
              <button onClick={() => { setResDlg(false); send(CMD.resolution("1080"), "V"); }}>
                1920 x 1080
              </button>
              <button onClick={() => { setResDlg(false); send(CMD.resolution("1440"), "V"); }}>
                2560 x 1440
              </button>
            </div>
          </div>
        </div>
      )}

      {schedDlg && (
        <div className="veil">
          <div className="card">
            <h2>예약 종료</h2>
            <div className="row tight">
              <input className="timer" type="number" min={0} value={schedMin}
                     onChange={(e) => setSchedMin(e.target.value)} />
              <span className="value">분 뒤 종료</span>
            </div>
            <div className="note">0을 넣으면 예약이 해제됩니다.</div>
            <div className="grid2">
              <button onClick={() => {
                const n = Number(schedMin.trim());
                if (!Number.isFinite(n) || n < 0) return;
                send(CMD.schedSet(n), "Y2");
              }}>확인</button>
              <button onClick={() => {
                // 취소도 서버에 알린다 — 알리지 않으면 PC가 1단계 상태로 남는다.
                setSchedDlg(false);
                send(CMD.schedSet(0), "Y2");
              }}>취소</button>
            </div>
          </div>
        </div>
      )}

      {shot && (
        <ScreenshotView name={shot.name} blobUrl={shot.url} onLog={addLog}
                        onClose={() => {
                          URL.revokeObjectURL(shot.url);
                          setShot(null);
                        }} />
      )}
    </div>
  );
}
