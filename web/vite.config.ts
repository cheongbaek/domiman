import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 배포 위치에 따라 base가 달라진다.
// - GitHub Pages(cheongbaek/domiman): "/domiman/" — 워크플로가 BASE_PATH로 주입
// - 로컬 dev/preview: "/"
const base = process.env.BASE_PATH ?? "/";

export default defineConfig({
  base,
  plugins: [react()],
  // host: true 로 열어 두면 같은 공유기의 휴대폰에서도 개발 서버에 접속된다.
  server: { host: true },
});
