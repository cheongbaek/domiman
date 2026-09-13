import {
  useCallback, useEffect, useLayoutEffect, useRef, useState,
} from "react";
import { Relay, RelayEvent, RoomRow } from "../lib/relay";
import {
  DENY_TEXT, IMG_MAX_BYTES, KIND_TEXT, b64Chunks, b64ToBlobUrl, hhmm, newFid,
  roomHint, sha256Hex, sortRooms, toPng,
} from "../lib/chat";

/**
 * 채팅 화면 — domichat의 '채팅방 리스트'와 '채팅방 창'을 **전체 화면 전환**으로
 * 옮긴 것. 휴대폰에서 더 많이 쓰므로 창(모달)을 띄우지 않고 화면 자체를 바꾼다.
 *
 * 여기 없는 것(의도적): 구독·알림·방 만들기·방장 도구(승인 대기/블랙리스트/삭제).
 * 웹은 중계의 계정 하나를 공유하므로 방장이 될 일이 없고, 브라우저에는 백그라운드
 * 수신이 없어 구독이 의미를 갖지 못한다(domichat.md §4).
 */

export type ChatMsg = {
  key: string;                 // mid(서버) | cid(보내는 중) | fid(이미지)
  from: string;
  mine: boolean;
  ts: number;
  body?: string;
  img?: { name: string; url: string };
  note?: boolean;              // 입·퇴장 같은 가운데 정렬 시스템 줄
  sending?: boolean;           // 서버 도달(cid 에코) 대기
  failed?: boolean;
  pct?: number;                // 이미지 올리는 중(0~100)
};

const SEND_TIMEOUT_MS = 15000;   // 명령 응답 대기와 같은 값

export function useChat(relayRef: React.MutableRefObject<Relay | null>, myId: string) {
  const [enabled, setEnabled] = useState(true);
  const [view, setView] = useState<"list" | "room">("list");
  const [rooms, setRooms] = useState<RoomRow[]>([]);
  const [room, setRoom] = useState("");
  const [meta, setMeta] = useState<{ kind: string; owner: string | null }>({
    kind: "", owner: null,
  });
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [notice, setNotice] = useState("");
  const [pwAsk, setPwAsk] = useState("");
  const [busy, setBusy] = useState("");         // 입장을 청해 둔 방
  const roomRef = useRef("");
  const busyRef = useRef("");                   // handle 안에서 보는 '입장 청한 방'
  const urls = useRef<string[]>([]);            // 이 방에서 만든 Blob URL
  const sentAt = useRef(new Map<string, number>());
  busyRef.current = busy;

  /** 방을 떠날 때 이미지 Blob을 전부 되돌린다 — 안 풀면 사진마다 메모리가 남는다
   *  (스크린샷 쪽과 같은 규칙). */
  const dropUrls = useCallback(() => {
    for (const u of urls.current) URL.revokeObjectURL(u);
    urls.current = [];
  }, []);

  useEffect(() => dropUrls, [dropUrls]);

  const add = useCallback((m: ChatMsg) => {
    setMsgs((prev) => {
      // mid는 서버가 찍는 고유값이다 — 재입장 때 중계가 되돌려주는 대화와
      // 이미 받은 것이 겹치면 여기서 걸러진다.
      if (prev.some((x) => x.key === m.key)) return prev;
      return [...prev, m].slice(-400);
    });
  }, []);

  const handle = useCallback((e: RelayEvent) => {
    if (e.t === "ready") {
      // **중계가 채팅을 중계한다고 말할 때만** 칸을 보여준다. 웹앱은 GitHub Pages로
      // 먼저 배포되고 서버(중계)는 나중에 올라갈 수 있는데, 그 사이에 버튼만 떠
      // 있으면 눌러도 아무 방도 안 나오는 죽은 칸이 된다.
      setEnabled(e.chat === true);
      return;
    }
    if (e.t === "link" && e.state !== "open") {
      setBusy("");
      return;
    }
    if (e.t === "rooms") {
      setRooms(sortRooms(e.list || []));
      return;
    }
    if (e.t === "chat_err") {
      if (!e.room || e.room === roomRef.current) setNotice(e.msg);
      return;
    }
    if (e.t === "room") {
      const cur = roomRef.current;
      if (e.state === "joined") {
        if (e.room !== busyRef.current && e.room !== cur) return;
        setBusy("");
        setNotice("");
        setPwAsk("");
        if (e.room !== cur) {
          dropUrls();
          setMsgs([]);
        }
        roomRef.current = e.room;
        setRoom(e.room);
        setMeta({ kind: e.kind || "", owner: e.owner ?? null });
        setView("room");
        return;
      }
      if (e.state === "denied") {
        if (e.room !== busyRef.current && e.room !== cur) return;
        setBusy("");
        const why = DENY_TEXT[e.reason || ""] || e.msg || "입장할 수 없습니다.";
        if (e.reason === "bad_pw_room") setPwAsk(e.room);   // 다시 물어본다
        else setPwAsk("");
        if (e.room === cur) {
          // 보고 있던 방에서 끊긴 것(재입장 실패) — 목록으로 돌려보낸다.
          roomRef.current = "";
          setRoom("");
          setView("list");
          dropUrls();
          setMsgs([]);
        }
        relayRef.current?.roomForget(e.room);
        setNotice(`${e.room}: ${why}`);
        return;
      }
      if (e.state === "member" && e.room === cur) {
        add({ key: `m-${e.id}-${e.in}-${Date.now()}`, from: "", mine: false,
              ts: Date.now() / 1000, note: true,
              body: `${e.id}님이 ${e.in ? "들어왔습니다" : "나갔습니다"}.` });
        return;
      }
      if (e.state === "kicked" || e.state === "deleted") {
        relayRef.current?.roomForget(e.room);
        if (e.room === cur) {
          roomRef.current = "";
          setRoom("");
          setView("list");
          dropUrls();
          setMsgs([]);
        }
        setNotice(`${e.room}: ${e.msg || ""}`);
        return;
      }
      if (e.state === "approve_res") {
        setNotice(`${e.room}: ${e.msg || (e.ok ? "입장이 승인되었습니다." : "입장이 거절되었습니다.")}`);
        return;
      }
      return;
    }
    if (e.t === "chat_hist") {
      if (e.room !== roomRef.current) return;
      for (const it of e.items || []) {
        add({ key: it.mid || `h-${it.ts}`, from: it.from, mine: it.from === myId,
              ts: it.ts || 0, body: it.body });
      }
      return;
    }
    if (e.t === "chat") {
      if (e.room !== roomRef.current) return;
      if (e.cid && sentAt.current.has(e.cid)) {
        // 내가 보낸 말이 서버에 닿았다 — 말풍선을 새로 그리지 않고 ✓만 켠다.
        sentAt.current.delete(e.cid);
        setMsgs((prev) => prev.map((m) => (m.key === e.cid
          ? { ...m, key: e.mid || m.key, sending: false, ts: e.ts || m.ts } : m)));
        return;
      }
      add({ key: e.mid || `c-${Date.now()}-${Math.random()}`, from: e.from,
            mine: e.from === myId, ts: e.ts || Date.now() / 1000, body: e.body });
      return;
    }
    if (e.t === "chat_img") {
      if (e.room !== roomRef.current) return;
      const url = b64ToBlobUrl(e.b64);
      urls.current.push(url);
      setMsgs((prev) => {
        const next = prev.filter((m) => m.key !== `up-${e.fid}`);  // 올리던 자리
        if (next.some((m) => m.key === e.fid)) return next;
        return [...next, { key: e.fid, from: e.from, mine: e.from === myId,
                           ts: e.ts, img: { name: e.name, url } }].slice(-400);
      });
      return;
    }
  }, [add, dropUrls, myId, relayRef]);

  // 보낸 말이 15초 안에 돌아오지 않으면 실패 표시(응답 대기 규칙과 같은 값).
  useEffect(() => {
    if (!msgs.some((m) => m.sending)) return;
    const id = window.setInterval(() => {
      const now = Date.now();
      let hit = false;
      for (const [cid, at] of sentAt.current) {
        if (now - at > SEND_TIMEOUT_MS) {
          sentAt.current.delete(cid);
          hit = true;
        }
      }
      if (!hit) return;
      setMsgs((prev) => prev.map((m) => (m.sending && !sentAt.current.has(m.key)
        ? { ...m, sending: false, failed: true } : m)));
    }, 1000);
    return () => window.clearInterval(id);
  }, [msgs]);

  const open = useCallback((name: string, pw?: string) => {
    setNotice("");
    setBusy(name);
    relayRef.current?.roomOpen(name, pw);
  }, [relayRef]);

  const close = useCallback(() => {
    const cur = roomRef.current;
    if (cur) relayRef.current?.roomClose(cur);
    roomRef.current = "";
    setRoom("");
    dropUrls();
    setMsgs([]);
    setView("list");
    relayRef.current?.roomsAsk();
  }, [dropUrls, relayRef]);

  const send = useCallback((body: string) => {
    const cur = roomRef.current;
    const text = body.replace(/\s+$/, "");
    if (!cur || !text) return;
    const cid = newFid();
    if (!relayRef.current?.chat(cur, text, cid)) {
      setNotice("연결이 끊겨 보내지 못했습니다.");
      return;
    }
    sentAt.current.set(cid, Date.now());
    add({ key: cid, from: myId, mine: true, ts: Date.now() / 1000, body: text,
          sending: true });
  }, [add, myId, relayRef]);

  /** 이미지는 **PNG로 바꿔** 조각내 올린다. 올리는 동안 말풍선 자리에 진행률을
   *  보여주고, 중계가 되돌려주는 `chat_img`가 그 자리를 대신한다. */
  const sendImage = useCallback(async (file: File | Blob) => {
    const cur = roomRef.current;
    const r = relayRef.current;
    if (!cur || !r) return;
    const fid = newFid();
    const key = `up-${fid}`;
    add({ key, from: myId, mine: true, ts: Date.now() / 1000, pct: 0,
          body: "이미지" });
    try {
      const { png, w, h } = await toPng(file);
      if (png.length > IMG_MAX_BYTES) throw new Error("이미지가 너무 큽니다.");
      const sha256 = await sha256Hex(png);
      const parts = b64Chunks(png);
      r.send({ t: "img_begin", room: cur, fid, name: "image.png",
               size: png.length, sha256, w, h });
      for (let i = 0; i < parts.length; i += 1) {
        if (!await r.drain()) throw new Error("연결이 끊겼습니다.");
        r.send({ t: "img_chunk", fid, seq: i, b64: parts[i] });
        const pct = Math.round(((i + 1) / parts.length) * 100);
        setMsgs((prev) => prev.map((m) => (m.key === key ? { ...m, pct } : m)));
      }
      r.send({ t: "img_end", fid });
    } catch (err) {
      setMsgs((prev) => prev.filter((m) => m.key !== key));
      setNotice(`이미지를 보내지 못했습니다. (${err instanceof Error ? err.message : err})`);
    }
  }, [add, myId, relayRef]);

  const refresh = useCallback(() => relayRef.current?.roomsAsk(), [relayRef]);

  return {
    enabled, view, rooms, room, meta, msgs, notice, pwAsk, busy,
    setNotice, setPwAsk, setView, handle, open, close, send, sendImage, refresh,
  };
}

export type Chat = ReturnType<typeof useChat>;

// ---------------------------------------------------------------- 방 목록 화면

export function RoomListView(
  { chat, myId, onBack }: { chat: Chat; myId: string; onBack: () => void },
) {
  const [q, setQ] = useState("");
  const [pw, setPw] = useState("");

  useEffect(() => { chat.refresh(); }, []);   // 화면에 들어올 때 한 번 새로 받는다

  const rows = chat.rooms.filter((r) => !q || r.name.toLowerCase().includes(q.toLowerCase()));

  function enter(r: RoomRow) {
    if (r.blocked) return chat.setNotice(`${r.name}: ${DENY_TEXT.blocked}`);
    if (r.kind === "pw" && !r.allowed) {
      setPw("");
      return chat.setPwAsk(r.name);
    }
    chat.open(r.name);
  }

  return (
    <div className="screen">
      <div className="bar">
        <button className="icon" onClick={onBack} title="낚시 제어로 돌아가기">←</button>
        <h1>채팅방 목록</h1>
        <span className="spacer" />
        <button className="icon" onClick={chat.refresh} title="목록 새로 받기">⟳</button>
      </div>

      <div className="note">
        내 ID <b>{myId}</b> — 사후 승인 방은 방장이 이 ID를 승인해야 들어갑니다.
        (구독·알림은 웹에 없습니다)
      </div>

      <div className="row tight">
        <input type="text" placeholder="방 이름으로 찾기" value={q} style={{ flex: 1 }}
               onChange={(e) => setQ(e.target.value)} />
      </div>

      {chat.notice && <div className="status warn">{chat.notice}</div>}

      <div className="rooms">
        {rows.length === 0
          ? <div className="empty">{chat.rooms.length === 0 ? "(방 없음)" : "(찾는 방이 없습니다)"}</div>
          : rows.map((r) => (
              <button key={r.name}
                      className={`roomrow ${r.blocked ? "off" : ""}`
                                 + (r.name === chat.room ? " on" : "")}
                      disabled={chat.busy === r.name}
                      onClick={() => enter(r)}>
                <span className={`badge k-${r.kind}`}>{KIND_TEXT[r.kind] ?? r.kind}</span>
                <span className="rname">{r.name}</span>
                <span className="rhint">
                  {chat.busy === r.name ? "들어가는 중…" : roomHint(r, myId)}
                </span>
              </button>
            ))}
      </div>
      <div className="note">채팅방 {chat.rooms.length}개</div>

      {chat.pwAsk && (
        <div className="veil" onClick={() => chat.setPwAsk("")}>
          <div className="card" onClick={(e) => e.stopPropagation()}>
            <h2>{chat.pwAsk}</h2>
            <div className="note">비밀번호를 넣어야 들어갈 수 있는 방입니다.</div>
            <div className="row tight">
              <input type="password" autoFocus value={pw} style={{ flex: 1 }}
                     onChange={(e) => setPw(e.target.value)}
                     onKeyDown={(e) => {
                       if (e.key === "Enter" && pw) { chat.open(chat.pwAsk, pw); chat.setPwAsk(""); }
                     }} />
            </div>
            <div className="grid2">
              <button disabled={!pw}
                      onClick={() => { chat.open(chat.pwAsk, pw); chat.setPwAsk(""); }}>확인</button>
              <button onClick={() => chat.setPwAsk("")}>취소</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- 채팅방 화면

export function ChatRoomView(
  { chat, myId, onImage }: { chat: Chat; myId: string; onImage: (m: ChatMsg) => void },
) {
  const [text, setText] = useState("");
  const box = useRef<HTMLDivElement | null>(null);
  const atBottom = useRef(true);
  const [unread, setUnread] = useState(false);
  const file = useRef<HTMLInputElement | null>(null);

  // 새 말이 오면 맨 아래로. 다만 **위로 올려 읽는 중이면 멈춘다** — 안 그러면
  // 읽는 중에 화면이 튄다(domichat 채팅방 창과 같은 규칙).
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (atBottom.current) el.scrollTop = el.scrollHeight;
    else setUnread(true);
  }, [chat.msgs]);

  function onScroll() {
    const el = box.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom.current) setUnread(false);
  }

  function toBottom() {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
    atBottom.current = true;
    setUnread(false);
  }

  function submit() {
    if (!text.trim()) return;
    chat.send(text);
    setText("");
    atBottom.current = true;
  }

  return (
    <div className="screen">
      <div className="bar">
        <button className="icon narrow" onClick={chat.close} title="목록으로">←</button>
        <h1>{chat.room}</h1>
        <span className={`badge k-${chat.meta.kind}`}>
          {KIND_TEXT[chat.meta.kind] ?? chat.meta.kind}
        </span>
        <span className="spacer" />
        <span className="value">{myId}</span>
        <button className="icon wide" onClick={chat.close} title="이 방에서 나가기">
          나가기
        </button>
      </div>

      {chat.notice && <div className="status warn">{chat.notice}</div>}

      <div className="chatbox" ref={box} onScroll={onScroll}>
        {chat.msgs.length === 0 && (
          <div className="empty">
            (들어오기 전의 대화는 보이지 않습니다 — 서버는 대화를 저장하지 않습니다)
          </div>
        )}
        {chat.msgs.map((m) => (m.note ? (
          <div key={m.key} className="sysline">{m.body}</div>
        ) : (
          <div key={m.key} className={`bubbleRow ${m.mine ? "mine" : ""}`}>
            {!m.mine && <div className="who">{m.from}</div>}
            <div className="bubbleWrap">
              <div className={`bubble ${m.mine ? "mine" : ""} ${m.failed ? "failed" : ""}`}>
                {m.img
                  ? <img className="chatimg" src={m.img.url} alt={m.img.name}
                         onClick={() => onImage(m)} />
                  : m.pct !== undefined
                    ? <span className="value">이미지 보내는 중… {m.pct}%</span>
                    : m.body}
              </div>
              <div className="stamp">
                {m.sending ? "···" : m.failed ? "✕" : m.mine ? "✓" : ""}
                {m.ts ? ` ${hhmm(m.ts)}` : ""}
              </div>
            </div>
          </div>
        )))}
      </div>

      {unread && <button className="newmsg" onClick={toBottom}>새 메시지 ↓</button>}

      <div className="sendrow">
        {/* 글자 버튼이다 — 그림 문자(🖼)는 글꼴에 따라 두부(□)로 나온다(실측). */}
        <button className="icon" title="이미지 보내기"
                onClick={() => file.current?.click()}>사진</button>
        <input ref={file} type="file" accept="image/*" hidden
               onChange={(e) => {
                 const f = e.target.files?.[0];
                 if (f) void chat.sendImage(f);
                 e.target.value = "";
               }} />
        <textarea
          className="chatinput" value={text} rows={1} placeholder="메시지"
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => {
            // 클립보드 이미지 붙여넣기 — domichat의 Ctrl+V와 같은 자리.
            const item = Array.from(e.clipboardData.items)
              .find((x) => x.type.startsWith("image/"));
            const blob = item?.getAsFile();
            if (blob) { e.preventDefault(); void chat.sendImage(blob); }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
          }} />
        <button onClick={submit} disabled={!text.trim()}>보내기</button>
      </div>
    </div>
  );
}
