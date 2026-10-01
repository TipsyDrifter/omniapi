import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { cancelChat, loadChat, selectChat, selectChatError, selectChatLive, sendChat, updateChat, useChat } from "@/store/chat";
import ChatDock from "./ChatDock";
import ChatHeader from "./ChatHeader";
import { AssistantMessage, LiveReply, UserMessage } from "./Message";

/* 右欄・一段對話（/chat/:id）：表頭 → 訊息區（內捲，貼底時跟著捲）→ 貼底區（細列＋輸入列）。
   逐字內容由 store 從 /ws 收，這裡只訂閱。 */

export interface ChatViewProps {
  id: string;
}

/** 距底多少 px 內算「貼底」（同活動流） */
const STICK_PX = 80;

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function ChatView({ id }: ChatViewProps) {
  const navigate = useNavigate();
  const conv = useChat(selectChat(id));
  const live = useChat(selectChatLive(id));
  const replyErr = useChat(selectChatError(id));
  const [loadErr, setLoadErr] = useState<{ status: number; msg: string } | null>(null);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const [stopErr, setStopErr] = useState<string | null>(null);
  /** 使用者在細列挑的模型；null＝沿用對話最後用的 */
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    // 進頁強制重抓一次（清單裡的摘要不含訊息；也補上回覆進行中已經到的字）
    loadChat(id, true).catch((e: unknown) => setLoadErr({ status: e instanceof ApiError ? e.status : 0, msg: errMsg(e) }));
  }, [id]);

  /* ---------- 捲動：貼底才跟 ---------- */
  const feedRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const first = useRef(true);
  const toBottom = () => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  const nMsg = conv?.messages.length ?? 0;
  const liveLen = live ? live.text.length + live.reasoning.length : -1;
  useLayoutEffect(() => {
    if (!conv) return;
    if (first.current) {
      first.current = false;
      stick.current = true;
    }
    if (stick.current) toBottom();
  }, [conv, nMsg, liveLen]);
  const onScroll = () => {
    const el = feedRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
  };

  if (!conv) {
    return (
      <div className="chatbox">
        <div className="idle ch-missing">
          <h1>{loadErr ? (loadErr.status === 404 ? "找不到這段聊天" : "讀不到這段聊天") : "載入中…"}</h1>
          {loadErr ? (
            <>
              <p className="code">{loadErr.status === 404 ? id : loadErr.msg}</p>
              <p>
                <Link to="/chat">← 回聊天清單</Link>
              </p>
            </>
          ) : null}
        </div>
      </div>
    );
  }

  const model = picked ?? conv.model ?? "cheap";
  const archived = conv.status === "archived";

  const onSend = async (text: string): Promise<boolean> => {
    setSendErr(null);
    setStopErr(null);
    stick.current = true;
    try {
      await sendChat(id, text, { model: picked ?? undefined });
      return true;
    } catch (e) {
      setSendErr(errMsg(e));
      return false;
    }
  };
  const onStop = async () => {
    setStopErr(null);
    try {
      await cancelChat(id);
    } catch (e) {
      setStopErr(errMsg(e));
      throw e;
    }
  };

  const notices = [
    sendErr ? (
      <div key="send" className="warn" role="alert">
        <b>送出失敗</b>：{sendErr}
      </div>
    ) : null,
    stopErr ? (
      <div key="stop" className="warn" role="alert">
        <b>停止失敗</b>：{stopErr}
      </div>
    ) : null,
    replyErr && !live ? (
      <div key="reply" className="warn" role="alert">
        <b>上一則回覆失敗</b>：{replyErr}
      </div>
    ) : null,
  ];

  const msgs = conv.messages.filter((m) => m.role === "user" || m.role === "assistant");
  return (
    <div className="chatbox">
      <ChatHeader conv={conv} onArchived={() => navigate("/chat")} />
      <div ref={feedRef} className="ch-feed" onScroll={onScroll} aria-live="polite">
        {msgs.length === 0 && !live ? <div className="feed-empty">這段聊天還沒有訊息。</div> : null}
        {msgs.map((m) => (m.role === "user" ? <UserMessage key={m.id} msg={m} /> : <AssistantMessage key={m.id} msg={m} />))}
        {live ? <LiveReply live={live} /> : null}
      </div>
      <ChatDock
        model={model}
        onModel={setPicked}
        system={conv.system_prompt ?? ""}
        onSystem={(v) => updateChat(id, { system_prompt: v })}
        systemMode="save"
        live={!!live}
        onSend={onSend}
        onStop={onStop}
        notices={notices}
        blocked={archived ? "這段聊天已封存，不能再送訊息。" : null}
      />
    </div>
  );
}
