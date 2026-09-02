import { useEffect, useState } from "react";

/**
 * 원격 스크린샷 보기. PC판 `ScreenshotWindow`와 같은 구성(1:1 보기 ↔ 창에 맞춤,
 * 저장, 클립보드 복사)이며, 브라우저에서는 Blob URL과 Clipboard API로 한다.
 */
export function ScreenshotView(
  { name, blobUrl, onClose, onLog }:
  { name: string; blobUrl: string; onClose: () => void; onLog: (s: string) => void },
) {
  const [fit, setFit] = useState(true);

  // ESC로 닫기 — 사진은 화면을 다 덮으므로 탈출구가 있어야 한다.
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  async function copy() {
    try {
      const blob = await (await fetch(blobUrl)).blob();
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      onLog("사진을 클립보드에 복사했습니다.");
    } catch {
      // Safari·구형 브라우저는 이미지 클립보드를 막는다 — 저장으로 안내한다.
      onLog("이 브라우저는 이미지 복사를 지원하지 않습니다. '저장'을 쓰세요.");
    }
  }

  return (
    <div className="veil" onClick={onClose}>
      <div className="card shotcard" onClick={(e) => e.stopPropagation()}>
        <div className="row tight">
          <span className="label">스크린샷</span>
          <span className="value">{name}</span>
          <span className="spacer" />
          <button onClick={() => setFit((v) => !v)}>{fit ? "1:1 보기" : "창에 맞춤"}</button>
          <a href={blobUrl} download={name}><button>저장</button></a>
          <button onClick={copy}>복사</button>
          <button onClick={onClose}>닫기</button>
        </div>
        <div className="shotbox">
          <img className={fit ? "fit" : "raw"} src={blobUrl} alt="게임 화면" />
        </div>
      </div>
    </div>
  );
}
