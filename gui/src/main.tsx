import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./styles/base.css";
import "./styles/board.css";
import "./styles/dispatch.css";
import "./styles/thread.css";
import "./styles/chat.css";
import "./styles/make.css";
import "./styles/works.css";
// 1.3-M4：設定頁、模型頁、首次啟動引導（寬版；各段的覆寫在 rwd.css）
import "./styles/settings.css";
// 1.3-M3 RWD：斷點與觸控規則，排最後（往下覆寫各頁的寬版樣式）
import "./styles/rwd.css";
// v1.4 影片：影片才有的樣式（自帶四段斷點），排在 rwd.css 之後
import "./styles/video.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
