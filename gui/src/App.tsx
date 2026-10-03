import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import TopBar from "@/components/TopBar";
import Footer from "@/components/Footer";
import { bootBoard } from "@/store/board";
import { bootMake } from "@/store/make";
import BoardPage from "@/pages/BoardPage";
import RunPage from "@/pages/RunPage";
import CostsPage from "@/pages/CostsPage";
import NewRunPage from "@/pages/NewRunPage";
import ChatPage from "@/pages/ChatPage";
import MakePage from "@/pages/MakePage";
import WorksPage from "@/pages/WorksPage";
import { bootWorks } from "@/store/works";

/* 路由（決策記錄 M4-d、M5-a、M6-f）：/ 看板、/runs/:id 單筆還原＋追問、/costs 費用、/new 新對話、/chat 與 /chat/:id 聊天、/make 與 /make/:kind 生成（1.1-M3）、/works 與 /works/:id 作品牆（1.1-M4） */
export default function App() {
  useEffect(() => {
    bootBoard();
    bootMake();
    bootWorks();
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
          <Route path="/make" element={<MakePage />} />
          <Route path="/make/:kind" element={<MakePage />} />
          {/* 牆與燈箱同一個元件：開關燈箱不重掛牆，捲動位置與已載入的頁都留著 */}
          <Route path="/works/:id?" element={<WorksPage />} />
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
      <p style={{ color: "var(--ink-70)" }}>可以去的地方：看板、費用、聊天、生成、作品，或從右上的「＋新對話」派工。</p>
    </section>
  );
}
