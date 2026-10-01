import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import TopBar from "@/components/TopBar";
import Footer from "@/components/Footer";
import { bootBoard } from "@/store/board";
import BoardPage from "@/pages/BoardPage";
import RunPage from "@/pages/RunPage";
import CostsPage from "@/pages/CostsPage";
import NewRunPage from "@/pages/NewRunPage";
import ChatPage from "@/pages/ChatPage";

/* 路由（決策記錄 M4-d、M5-a、M6-f）：/ 看板、/runs/:id 單筆還原＋追問、/costs 費用、/new 新對話、/chat 與 /chat/:id 聊天 */
export default function App() {
  useEffect(() => {
    bootBoard();
  }, []);
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
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
      <Footer />
    </>
  );
}

function NotFound() {
  return (
    <section className="wrap" style={{ padding: "40px 0" }}>
      <h1 className="f-kai" style={{ fontSize: "var(--fs-h2)", margin: 0 }}>
        找不到這一頁
      </h1>
      <p style={{ color: "var(--ink-70)" }}>可以去的地方：看板、費用、聊天，或從右上的「＋新對話」派工。</p>
    </section>
  );
}
