import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// 서비스워커는 **절대 등록하지 않는다.** 같은 오리진(cheongbaek.github.io)에
// 옛 PWA 워커가 남아 화면을 캐시에서 돌려주던 사고가 find_wc에 기록돼 있다.
// 혹시 남아 있는 등록이 있으면 걷어낸다(그쪽 /sw.js 청소 워커와 같은 취지).
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations()
    .then((rs) => rs.forEach((r) => { void r.unregister(); }))
    .catch(() => { /* 지원하지 않는 브라우저 — 무시 */ });
}

createRoot(document.getElementById("root")!).render(<App />);
