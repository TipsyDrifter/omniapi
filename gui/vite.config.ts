import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The daemon (`omni serve`) listens on 127.0.0.1:7788 and serves gui/dist at `/`.
// In dev, Vite serves the app and proxies the three daemon doors to it (決策記錄 M4-b).
//
// `--mode sandbox`（npm run dev:sandbox）：連到隔離的測試 daemon（7799，資料庫副本＋重播 harness），
// 前端開在 5179。派工／中止／續接都在這裡試，不會動到主人的真實歷史，也不花錢。
export default defineConfig(({ mode }) => {
  const sandbox = mode === "sandbox";
  const DAEMON = process.env.OMNIAPI_DAEMON ?? (sandbox ? "http://127.0.0.1:7799" : "http://127.0.0.1:7788");
  return {
    plugins: [react(), tailwindcss()],
    resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
    define: { __OMNI_SANDBOX__: JSON.stringify(sandbox) },
    server: {
      // 綁 127.0.0.1：Vite 預設的 localhost 在 Windows 只聽 ::1，curl 127.0.0.1 會 refused（Chrome 兩邊都會試）
      host: "127.0.0.1",
      port: sandbox ? 5179 : 5178,
      strictPort: true,
      proxy: {
        "/api": { target: DAEMON, changeOrigin: true },
        "/mcp": { target: DAEMON, changeOrigin: true },
        "/ws": { target: DAEMON.replace(/^http/, "ws"), ws: true, changeOrigin: true },
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
      sourcemap: false,
    },
  };
});
