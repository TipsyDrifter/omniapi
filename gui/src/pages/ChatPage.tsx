import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ChatList, ChatView, NewChat } from "@/components/chat";
import { loadChats } from "@/store/chat";

/* 聊天 `/chat`、`/chat/:id`（決策記錄 M6-f）：比照單筆頁兩欄。
   左＝對話清單（欄內捲）；右＝對話（表頭＋訊息區內捲＋貼底輸入列）或空白新對話。整頁不出現直向捲軸。 */
export default function ChatPage() {
  const { id } = useParams();
  const [listErr, setListErr] = useState<string | null>(null);
  useEffect(() => {
    loadChats()
      .then(() => setListErr(null))
      .catch((e: unknown) => setListErr(e instanceof Error ? e.message : String(e)));
  }, []);
  return (
    <section className="chatpage wrap">
      <ChatList activeId={id} error={listErr} />
      <div className="chat-main">{id ? <ChatView key={id} id={id} /> : <NewChat />}</div>
    </section>
  );
}
