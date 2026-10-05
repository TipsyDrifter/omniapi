import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ChatList, ChatView, NewChat, preloadMarkdown } from "@/components/chat";
import { loadChats, selectChatDeleted, useChat } from "@/store/chat";

/* 聊天 `/chat`、`/chat/:id`（決策記錄 M6-f）：比照單筆頁兩欄。
   左＝對話清單（欄內捲）；右＝對話（表頭＋訊息區內捲＋貼底輸入列）或空白新對話。整頁不出現直向捲軸。
   開著的對話被刪掉（這個分頁或別的分頁，1.2-M3）：換到清單裡的下一筆，清單空了就回空白新對話。 */
export default function ChatPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [listErr, setListErr] = useState<string | null>(null);
  const deletedNext = useChat(selectChatDeleted(id));
  useEffect(() => {
    // 聊天頁幾乎每則回覆都是 markdown：一進來就先抓 chunk，第一則訊息不必先閃純文字
    preloadMarkdown();
    loadChats()
      .then(() => setListErr(null))
      .catch((e: unknown) => setListErr(e instanceof Error ? e.message : String(e)));
  }, []);
  useEffect(() => {
    if (deletedNext === undefined) return;
    navigate(deletedNext ? `/chat/${encodeURIComponent(deletedNext)}` : "/chat", { replace: true });
  }, [deletedNext, navigate]);
  // 手機（S 段）清單與對話拆成兩層（rwd.css）：/chat＝清單；/chat/:id 與 /chat?new＝對話那一層（返回列「‹ 聊天」）
  const thread = !!id || params.has("new");
  return (
    <section className={`chatpage wrap${thread ? " is-thread" : ""}`}>
      <ChatList activeId={id} error={listErr} />
      <div className="chat-main">
        {id ? (
          <ChatView key={id} id={id} />
        ) : (
          <>
            <div className="ctxbar">
              <Link className="back" to="/chat">
                ‹ 聊天
              </Link>
              <span className="t">新聊天</span>
            </div>
            <NewChat />
          </>
        )}
      </div>
    </section>
  );
}
