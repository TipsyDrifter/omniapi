import { useEffect } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import TopBar from "@/components/TopBar";
import Footer from "@/components/Footer";
import TabBar from "@/components/TabBar";
import { useTapReasons } from "@/lib/rwd";
import { bootBoard } from "@/store/board";
import { bootMake } from "@/store/make";
import { bootSettings, selectNoKey, useSettings } from "@/store/settings";
import BoardPage from "@/pages/BoardPage";
import RunPage from "@/pages/RunPage";
import CostsPage from "@/pages/CostsPage";
import NewRunPage from "@/pages/NewRunPage";
import ChatPage from "@/pages/ChatPage";
import MakePage from "@/pages/MakePage";
import WorksPage from "@/pages/WorksPage";
import SettingsPage from "@/pages/SettingsPage";
import ModelsPage from "@/pages/ModelsPage";
import WelcomePage, { WELCOME_KEY } from "@/pages/WelcomePage";
import { bootWorks } from "@/store/works";

/* 路由（決策記錄 M4-d、M5-a、M6-f）：/ 看板、/runs/:id 單筆還原＋追問、/costs 費用、/new 新對話、/chat 與 /chat/:id 聊天、/make 與 /make/:kind 生成（1.1-M3）、/works 與 /works/:id 作品牆（1.1-M4）、
   /models 模型、/settings 設定、/welcome 首次啟動引導（1.3-M4；引導是獨立一頁，不帶頂欄、狀態列與分頁列） */
export default function App() {
  useEffect(() => {
    bootBoard();
    bootMake();
    bootWorks();
    bootSettings();
  }, []);
  useTapReasons();
  useWelcomeRedirect();
  // 深一層的頁（單筆、一段聊天、手機上的空白新聊天）：S 段收起頂欄與分頁列，換成頁面自己的返回列（.ctxbar，rwd.css）
  const { pathname, search } = useLocation();
  const deep = /^\/runs\/[^/]+/.test(pathname) || /^\/chat\/[^/]+/.test(pathname) || (pathname === "/chat" && new URLSearchParams(search).has("new"));
  useEffect(() => {
    document.documentElement.classList.toggle("deep", deep);
  }, [deep]);
  const welcome = pathname === "/welcome";
  useEffect(() => {
    document.documentElement.classList.toggle("on-welcome", welcome);
  }, [welcome]);
  if (welcome) return <WelcomePage />;
  return (
    <>
      <TopBar />
      <main>
        <Routes>
          <Route path="/" element={<BoardPage />} />
          <Route path="/runs/:id" element={<RunPage />} />
          <Route path="/costs" element={<CostsPage />} />
          <Route path="/new" element={<NewRunPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:id" element={<ChatPage />} />
          <Route path="/make" element={<MakePage />} />
          <Route path="/make/:kind" element={<MakePage />} />
          {/* 牆與燈箱同一個元件：開關燈箱不重掛牆，捲動位置與已載入的頁都留著 */}
          <Route path="/works/:id?" element={<WorksPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
      <Footer />
      <TabBar />
    </>
  );
}

/** 首次啟動：七家都沒有 key、這個瀏覽器沒有略過或走完過引導 → 打開看板就導到 /welcome。
 *  只帶一次：同一個分頁導過一次就不再導（按上一頁回看板不會被拉回去）；略過或走完記在 localStorage，之後都不帶。
 *  已經有 key 的環境不會被帶去。 */
function useWelcomeRedirect(): void {
  const noKey = useSettings(selectNoKey);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (pathname !== "/" || noKey !== true) return;
    try {
      if (localStorage.getItem(WELCOME_KEY)) return;
      if (sessionStorage.getItem(WELCOME_KEY)) return;
      sessionStorage.setItem(WELCOME_KEY, "shown");
    } catch {
      return;
    }
    navigate("/welcome", { replace: true });
  }, [pathname, noKey, navigate]);
}

function NotFound() {
  return (
    <section className="wrap" style={{ padding: "40px 0" }}>
      <h1 className="f-kai" style={{ fontSize: "var(--fs-h2)", margin: 0 }}>
        找不到這一頁
      </h1>
      <p style={{ color: "var(--ink-70)" }}>可以去的地方：看板、費用、聊天、生成、作品、模型、設定，或從右上的「＋新對話」派工。</p>
    </section>
  );
}
