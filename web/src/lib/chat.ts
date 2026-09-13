/**
 * 채팅 — domichat 규격을 브라우저에서 쓰기 위한 얇은 계층.
 *
 * 규격의 단일 기준은 `domichat.md`이고, 브라우저는 **domiweb를 통해서만** 서버와
 * 말한다. 그래서 여기에는 프레이밍도 TLS도 없다 — 방 목록의 표시 규칙, 이미지를
 * PNG로 바꿔 조각내 올리는 일, 그 두 가지뿐이다.
 *
 * ⚠ **웹은 domiweb의 계정 하나(`web`)를 여럿이 나눠 쓴다.** 입장 자격도 그 ID
 * 기준이고(사후 승인 방은 `web`을 승인해야 한다), 다른 브라우저가 보낸 말도 내
 * 말로 보인다. 웹앱에 계정을 두지 않기로 한 설계의 대가다(web.md '왜 중계인가').
 *
 * ⚠ **구독·알림은 웹에 없다.** 방을 닫으면 그 방을 보는 브라우저가 없어져 중계가
 * 방에서 나오고, 서버는 대화를 저장하지 않으므로 그동안의 말은 공백으로 남는다.
 */
import { RoomRow } from "./relay";

/** 보내는 이미지 규격 — domichat.py와 같은 값(그쪽도 PNG로 바꿔 올린다). */
export const IMG_MAX_SIDE = 2560;            // 긴 변이 이보다 크면 줄인다
export const IMG_MAX_BYTES = 16 * 1024 * 1024;  // domiweb CHAT_IMG_MAX와 같은 값
/** base64 조각 길이. domiweb의 WS_MAX_RX(64KB)보다 넉넉히 작아야 한다
 *  (48,000자 = 원본 36,000바이트). */
export const B64_CHUNK = 48000;

export const KIND_TEXT: Record<string, string> = {
  open: "공개",
  pw: "비밀번호",
  allow: "사전 승인",
  approve: "사후 승인",
};

/** 입장 거절 사유 -> 사람이 읽을 문장 (domichat.md §9 그대로). */
export const DENY_TEXT: Record<string, string> = {
  bad_pw_room: "비밀번호가 틀렸습니다.",
  not_allowed: "등록된 ID가 아닙니다.",
  await_approval: "방장의 승인을 기다리고 있습니다.",
  blocked: "강제 퇴장되어 입장할 수 없습니다.",
  room_missing: "없는 채팅방입니다.",
  offline: "서버 연결이 끊겼습니다.",
};

/** 목록에 그릴 한 줄 요약 — '왜 못 들어가는지'를 누르기 전에 보여준다. */
export function roomHint(r: RoomRow, myId: string): string {
  if (r.blocked) return "입장 불가";
  if (r.owner === myId) return "내가 만든 방";
  if (r.kind === "open" || r.allowed) return "입장 가능";
  if (r.kind === "pw") return "비밀번호 필요";
  if (r.kind === "approve") return r.waiting ? "승인 대기 중" : "승인 필요";
  return "등록된 ID만";
}

/** 이름 오름차순. domichat은 정렬 4가지를 기억하지만 웹은 한 가지로 둔다 —
 *  방이 100개를 넘지 못하고(서버 상한), 검색칸이 그 일을 대신한다. */
export function sortRooms(rows: RoomRow[]): RoomRow[] {
  return [...rows].sort((a, b) => a.name.localeCompare(b.name, "ko"));
}

export function hhmm(ts: number): string {
  const d = new Date(ts * 1000);
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

/** 32자리 hex — domiserver가 `[0-9a-f]{32}`로 검사하는 이미지 식별자. */
export function newFid(): string {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function b64ToBlobUrl(b64: string, type = "image/png"): string {
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) buf[i] = bin.charCodeAt(i);
  return URL.createObjectURL(new Blob([buf], { type }));
}

function bytesToB64(bytes: Uint8Array): string {
  // 한 번에 String.fromCharCode(...bytes)를 하면 인자 수 상한에 걸려 큰 이미지에서
  // 터진다 — 조각내 이어 붙인다.
  let out = "";
  const STEP = 0x8000;
  for (let i = 0; i < bytes.length; i += STEP) {
    out += String.fromCharCode(...bytes.subarray(i, i + STEP));
  }
  return btoa(out);
}

/**
 * 무엇이 들어오든 **PNG로 바꿔서** 보낸다(domichat.py와 같은 규칙).
 * 받는 쪽이 tkinter라 PNG/GIF만 그릴 수 있고, 보내는 쪽에서 한 번 맞춰두면
 * 어느 클라이언트도 형식 문제를 겪지 않는다.
 *
 * 긴 변이 `IMG_MAX_SIDE`를 넘으면 줄이고, 그래도 상한을 넘으면 **더 줄여 다시
 * 인코딩한다** — PNG는 사진에서 쉽게 수십 MB가 되기 때문이다.
 */
export async function toPng(file: File | Blob): Promise<{ png: Uint8Array; w: number; h: number }> {
  const bmp = await createImageBitmap(file);
  let { width: w, height: h } = bmp;
  let scale = Math.min(1, IMG_MAX_SIDE / Math.max(w, h));
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const cw = Math.max(1, Math.round(w * scale));
    const ch = Math.max(1, Math.round(h * scale));
    const cv = document.createElement("canvas");
    cv.width = cw;
    cv.height = ch;
    const ctx = cv.getContext("2d");
    if (!ctx) throw new Error("캔버스를 열 수 없습니다.");
    ctx.drawImage(bmp, 0, 0, cw, ch);
    const blob: Blob | null = await new Promise((r) => cv.toBlob(r, "image/png"));
    if (!blob) throw new Error("PNG로 바꾸지 못했습니다.");
    if (blob.size <= IMG_MAX_BYTES) {
      bmp.close?.();
      return { png: new Uint8Array(await blob.arrayBuffer()), w: cw, h: ch };
    }
    scale *= Math.sqrt(IMG_MAX_BYTES / blob.size) * 0.9;
  }
  bmp.close?.();
  throw new Error("이미지가 너무 큽니다.");
}

/** sha256 16진 문자열. `crypto.subtle`은 https·localhost에서만 있으므로, 없으면
 *  해시를 붙이지 않는다(서버·중계 모두 sha256이 없으면 크기만 확인한다). */
export async function sha256Hex(bytes: Uint8Array): Promise<string | undefined> {
  try {
    // as BufferSource: TS의 Uint8Array는 SharedArrayBuffer도 품을 수 있다고 보지만
    // 여기 들어오는 것은 언제나 toPng가 만든 평범한 ArrayBuffer다.
    const buf = await crypto.subtle.digest("SHA-256", bytes as unknown as BufferSource);
    return Array.from(new Uint8Array(buf), (x) => x.toString(16).padStart(2, "0")).join("");
  } catch {
    return undefined;
  }
}

export function b64Chunks(bytes: Uint8Array): string[] {
  const all = bytesToB64(bytes);
  const out: string[] = [];
  for (let i = 0; i < all.length; i += B64_CHUNK) out.push(all.slice(i, i + B64_CHUNK));
  return out;
}
