import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createChat } from "@/store/chat";
import ChatDock from "./ChatDock";

/* 右欄・空白新對話（/chat）：沒有訊息區，中間放引導；貼底區同一條細列＋輸入列。
   送出第一則＝建立對話（帶模型與 system prompt）→ 換到 /chat/:id。 */

export default function NewChat() {
  const navigate = useNavigate();
  const [model, setModel] = useState("cheap");
  const [system, setSystem] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const onSend = async (text: string): Promise<boolean> => {
    setErr(null);
    try {
      const id = await createChat({ model, system: system || undefined, message: text });
      navigate("/chat/" + encodeURIComponent(id), { replace: true });
      return true;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return false;
    }
  };

  return (
    <div className="chatbox">
      <div className="ch-head">
        <span className="ch-title static">新聊天</span>
      </div>
      <div className="ch-intro">
        <h1>開一段新聊天</h1>
        <p>在下面的輸入列寫第一則，按 Ctrl+Enter 送出；回覆會一個字一個字出現。</p>
        <p>
          送出前可以先在細列換模型（預設 <span className="code">cheap</span>）、填 system prompt；之後每一則都能換模型，每則回覆會標明是誰答的、花了多少。
        </p>
        <p>聊天只講話，碰不到檔案。要 agent 動手做事，從頂欄「＋新對話」選「派工」。</p>
      </div>
      <ChatDock
        model={model}
        onModel={setModel}
        system={system}
        onSystem={setSystem}
        systemMode="draft"
        live={false}
        onSend={onSend}
        notices={[
          err ? (
            <div key="send" className="warn" role="alert">
              <b>開聊失敗</b>：{err}
            </div>
          ) : null,
        ]}
        autoFocus
      />
    </div>
  );
}
