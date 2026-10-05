import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { ChatMessage } from "@/api/types";
import { sendHint, useSendKey } from "@/lib/sendKey";
import { createChat, localMessage, type OutgoingAttachment } from "@/store/chat";
import { useSettings } from "@/store/settings";
import ChatDock from "./ChatDock";
import { UserMessage } from "./Message";

/* 右欄・空白新對話（/chat）：沒有訊息區，中間放引導；貼底區同一條細列＋輸入列。
   送出第一則＝建立對話（帶模型、system prompt 與附件）→ 換到 /chat/:id。
   送出當下引導換成那一則（含附件），不等後端。 */

export default function NewChat() {
  const navigate = useNavigate();
  // 1.3-M4：一開始用設定頁的「預設聊天」（等級別名或模型 id）；使用者在細列挑過就照挑的
  const defChat = useSettings((s) => s.settings?.defaults.chat.model ?? null);
  const [picked, setModel] = useState<string | null>(null);
  const model = picked ?? defChat ?? "cheap";
  const [system, setSystem] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [pending, setPending] = useState<ChatMessage | null>(null);
  const [sendKey] = useSendKey();

  const onSend = async (text: string, attachments: OutgoingAttachment[]): Promise<boolean> => {
    setErr(null);
    setPending(localMessage("", text, attachments));
    try {
      const id = await createChat({ model, system: system || undefined, message: text, attachments });
      navigate("/chat/" + encodeURIComponent(id), { replace: true });
      return true;
    } catch (e) {
      setPending(null);
      setErr(e instanceof Error ? e.message : String(e));
      return false;
    }
  };

  return (
    <div className="chatbox">
      <div className="ch-head">
        <span className="ch-title static">新聊天</span>
      </div>
      {pending ? (
        <div className="ch-feed">
          <UserMessage msg={pending} />
        </div>
      ) : (
        <div className="ch-intro">
          <h1>開一段新聊天</h1>
          <p>在下面的輸入列寫第一則，{sendHint(sendKey)}；回覆會一個字一個字出現。</p>
          <p>
            送出前可以先在細列換模型（預設 <span className="code">{defChat ?? "cheap"}</span>，在設定頁可以改）、填 system prompt；之後每一則都能換模型，每則回覆會標明是誰答的、花了多少。
          </p>
          <p>要問圖或檔案的事：把檔案貼上、拖進輸入框，或按「附件」從電腦或作品牆挑；PDF、試算表、文件、錄音都可以，只有附件沒有字也能送。</p>
          <p>聊天只讀你附上的檔案，碰不到電腦裡的其他東西。要 agent 動手做事，從頂欄「＋新對話」選「派工」。</p>
        </div>
      )}
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
