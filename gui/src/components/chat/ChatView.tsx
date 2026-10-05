import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { useTier } from "@/lib/rwd";
import type { ChatLive, ChatMessage } from "@/api/types";
import {
  cancelChat,
  editChat,
  loadChat,
  regenerateChat,
  selectChat,
  selectChatError,
  selectChatLive,
  sendChat,
  switchChat,
  updateChat,
  useChat,
  type OutgoingAttachment,
} from "@/store/chat";
import ChatDock from "./ChatDock";
import ChatHeader from "./ChatHeader";
import EditMessage from "./EditMessage";
import { capsOf } from "./files";
import { AssistantMessage, LiveReply, UserMessage, type MsgOff } from "./Message";
import { resolveChatModel, useModels } from "./useModels";

/* 右欄・一段對話（/chat/:id）：表頭 → 訊息區（內捲，貼底時跟著捲）→ 貼底區（細列＋輸入列）。
   逐字內容由 store 從 /ws 收，這裡只訂閱。
   1.2-M3：重新生成（用細列「下一則用」的模型）、‹ n/m › 切換版本（畫面釘住那一則、分岔點下方一行說明、
   換掉的那段網點印一下）、原地編輯舊訊息。說明行在下一個動作或按「知道了」時收掉。 */

export interface ChatViewProps {
  id: string;
}

/** 距底多少 px 內算「貼底」（同活動流） */
const STICK_PX = 80;

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** 分岔點下方的一行說明；anchor＝接在哪一則後面（id，或「某則的下一則」：重新生成／編輯時新的那則還沒有編號） */
interface Notice {
  key: number;
  anchor: { id: string } | { childOf: string | null; role: "user" | "assistant" };
  k: string;
  body: ReactNode;
  /** 切換版本：後面換掉的那一段網點印一下 */
  flash: boolean;
}
let noticeSeq = 0;

const N = ({ v }: { v: number }) => <span className="n">{v}</span>;

export default function ChatView({ id }: ChatViewProps) {
  const navigate = useNavigate();
  const conv = useChat(selectChat(id));
  const live = useChat(selectChatLive(id));
  const replyErr = useChat(selectChatError(id));
  const [loadErr, setLoadErr] = useState<{ status: number; msg: string } | null>(null);
  const [sendErr, setSendErr] = useState<string | null>(null);
  const [stopErr, setStopErr] = useState<string | null>(null);
  const [actErr, setActErr] = useState<string | null>(null);
  /** 使用者在細列挑的模型；null＝沿用對話最後用的 */
  const [picked, setPicked] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  /** 重新生成／切換的請求送出中（還沒等到 chat.started 或詳情）：先擋住第二下 */
  const [pending, setPending] = useState(false);
  const models = useModels();

  useEffect(() => {
    // 進頁強制重抓一次（清單裡的摘要不含訊息；也補上回覆進行中已經到的字）
    loadChat(id, true).catch((e: unknown) => setLoadErr({ status: e instanceof ApiError ? e.status : 0, msg: errMsg(e) }));
  }, [id]);

  /* ---------- 捲動：貼底才跟；切換版本時釘住那一則 ---------- */
  const feedRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const first = useRef(true);
  /** 切換版本：換之前那一則離訊息區頂端多遠；換完讓新的那一則回到同一個位置 */
  const anchor = useRef<{ to: string; top: number } | null>(null);
  // 1.3-M3：手機（S 段）訊息區不內捲、跟著整頁捲（輸入區貼底），捲動對象換成視窗
  const pageScroll = useTier() === "s";
  const pageRef = useRef(pageScroll);
  pageRef.current = pageScroll;
  const toBottom = () => {
    if (pageRef.current) {
      window.scrollTo({ top: document.documentElement.scrollHeight });
      return;
    }
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  useEffect(() => {
    if (!pageScroll) return;
    const onWin = () => {
      const d = document.documentElement;
      stick.current = d.scrollHeight - window.scrollY - window.innerHeight < STICK_PX;
    };
    window.addEventListener("scroll", onWin, { passive: true });
    return () => window.removeEventListener("scroll", onWin);
  }, [pageScroll]);
  const nMsg = conv?.messages.length ?? 0;
  const liveLen = live ? live.text.length + live.reasoning.length : -1;
  useLayoutEffect(() => {
    if (!conv) return;
    if (first.current) {
      first.current = false;
      stick.current = true;
    }
    const a = anchor.current;
    const feed = feedRef.current;
    if (a && feed) {
      const el = feed.querySelector<HTMLElement>(`[data-msg="${CSS.escape(a.to)}"]`);
      if (el) {
        anchor.current = null;
        if (pageRef.current) {
          // 整頁捲：a.top 記的是離視窗頂端多遠
          window.scrollBy(0, el.getBoundingClientRect().top - a.top);
          return;
        }
        feed.scrollTop += el.getBoundingClientRect().top - feed.getBoundingClientRect().top - a.top;
        stick.current = feed.scrollHeight - feed.scrollTop - feed.clientHeight < STICK_PX;
        return;
      }
    }
    if (stick.current) toBottom();
  }, [conv, nMsg, liveLen, notice]);
  const onScroll = () => {
    const el = feedRef.current;
    if (el && !pageRef.current) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
  };

  const model = picked ?? conv?.model ?? "cheap";
  const caps = capsOf(resolveChatModel(model, models).entry, !!models.data);
  const archived = conv?.status === "archived";
  const off: MsgOff = live ? "live" : archived ? "archived" : null;
  const msgs = (conv?.messages ?? []).filter((m) => m.role === "user" || m.role === "assistant");
  const after = (mid: string) => {
    const i = msgs.findIndex((m) => m.id === mid);
    return i < 0 ? 0 : msgs.length - 1 - i;
  };

  const clearForAction = () => {
    setNotice(null);
    setActErr(null);
    setSendErr(null);
    setStopErr(null);
  };

  const onRegenerate = (m: ChatMessage) => {
    if (pending) return;
    const n = after(m.id);
    const i = m.versions?.index ?? 1;
    clearForAction();
    setEditing(null);
    setPending(true);
    stick.current = true;
    regenerateChat(id, m.id, { model })
      .then(() => {
        // 從較早的回覆重新生成：分岔點下方說一句；最後一則就不另外說（‹ n/n › 本身就講清楚了）
        if (n > 0)
          setNotice({
            key: ++noticeSeq,
            anchor: { childOf: m.parent_id ?? null, role: "assistant" },
            k: "分岔",
            body: (
              <>
                從這裡重新生成。原本接在第 <N v={i} /> 版後面的 <N v={n} /> 則留在那條，按 ‹ 換回去就看得到。
              </>
            ),
            flash: false,
          });
      })
      .catch((e: unknown) => setActErr(`重新生成失敗：${errMsg(e)}`))
      .finally(() => setPending(false));
  };

  const onSwitch = useCallback(
    (m: ChatMessage, to: string, dir: -1 | 1) => {
      if (pending) return;
      const feed = feedRef.current;
      const el = feed?.querySelector<HTMLElement>(`[data-msg="${CSS.escape(m.id)}"]`);
      const top = el && feed ? el.getBoundingClientRect().top - (pageRef.current ? 0 : feed.getBoundingClientRect().top) : 0;
      const from = m.versions?.index ?? 1;
      const what = m.role === "user" ? "訊息" : "回覆";
      clearForAction();
      setEditing(null);
      setPending(true);
      switchChat(id, to)
        .then((d) => {
          anchor.current = { to, top };
          const path = d.messages.filter((x) => x.role === "user" || x.role === "assistant");
          const i = path.findIndex((x) => x.id === to);
          const n = i < 0 ? 0 : path.length - 1 - i;
          const idx = path[i]?.versions?.index ?? from + dir;
          // 1.2-M4：這一版帶著提議卡（和它做好的作品）：說一聲都在下面
          const props = path[i]?.role === "assistant" ? (path[i].proposals ?? []) : [];
          const madeN = props.filter((p) => p.state === "done").length;
          const extra = props.length ? (madeN ? <>這一版的提議和它生成的 <N v={madeN} /> 件作品都在下面。</> : <>這一版的提議在下面。</>) : null;
          setNotice({
            key: ++noticeSeq,
            anchor: { id: to },
            k: "換版",
            body:
              n > 0 ? (
                <>
                  已換到這則{what}的第 <N v={idx} /> 版，{extra}下面 <N v={n} /> 則是這一版接下去的對話。第 <N v={from} /> 版那條沒有不見，按 {dir > 0 ? "‹" : "›"} 換回去。
                </>
              ) : (
                <>
                  已換到這則{what}的第 <N v={idx} /> 版。{extra}這一版後面還沒有對話；在下面輸入，就從這裡接下去。
                </>
              ),
            flash: n > 0,
          });
        })
        .catch((e: unknown) => setActErr(`切換版本失敗：${errMsg(e)}`))
        .finally(() => setPending(false));
    },
    [id, pending],
  );

  const onEdit = useCallback((m: ChatMessage) => {
    setNotice(null);
    setActErr(null);
    setEditing(m.id);
  }, []);

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

  const onSend = async (text: string, attachments: OutgoingAttachment[]): Promise<boolean> => {
    clearForAction();
    setEditing(null);
    stick.current = true;
    try {
      await sendChat(id, text, { model: picked ?? undefined, attachments });
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
  const onEditSend = async (m: ChatMessage, text: string, attachments: OutgoingAttachment[] | undefined) => {
    const n = after(m.id);
    const i = m.versions?.index ?? 1;
    const count = m.versions?.count ?? 1;
    clearForAction();
    stick.current = true;
    await editChat(id, m.id, text, { attachments, original: m.attachments, model });
    setEditing(null);
    setNotice({
      key: ++noticeSeq,
      anchor: { childOf: m.parent_id ?? null, role: "user" },
      k: "分岔",
      body:
        n > 0 ? (
          <>
            這是改寫後的第 <N v={count + 1} /> 版。原本的第 <N v={i} /> 版和它後面的 <N v={n} /> 則都還在，按 ‹ 換回去。
          </>
        ) : (
          <>
            這是改寫後的第 <N v={count + 1} /> 版。原本的第 <N v={i} /> 版還在，按 ‹ 換回去。
          </>
        ),
      flash: false,
    });
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
    actErr ? (
      <div key="act" className="warn" role="alert">
        {actErr}
      </div>
    ) : null,
    replyErr && !live ? (
      <div key="reply" className="warn" role="alert">
        <b>上一則回覆失敗</b>：{replyErr}
      </div>
    ) : null,
  ];

  // 現行路徑上的圖（換到不會看圖的模型時要講它看不到幾張）
  const priorImages = msgs.reduce((a, m) => a + (m.role === "user" && !m.local ? (m.attachments?.filter((x) => x.kind !== "file" && x.kind !== "audio").length ?? 0) : 0), 0);
  const lastReply = live ? undefined : [...msgs].reverse().find((m) => m.role === "assistant");
  const pathCount = msgs.filter((m) => !m.local).length;

  /* 說明行接在哪一則後面：訊息與回覆進行中那一則排成一列找。
     1.2-M5：先問過主人的回覆寫進既有那一則（fill_id）：進行中的就畫在那一則的位置，不另起一則 */
  type Row = { kind: "msg"; m: ChatMessage } | { kind: "live"; live: ChatLive; fill?: ChatMessage };
  const fillId = live?.fill_id && msgs.some((m) => m.id === live.fill_id) ? live.fill_id : null;
  const rows: Row[] = msgs.map((m): Row => (live && m.id === fillId ? { kind: "live", live, fill: m } : { kind: "msg", m }));
  if (live && !fillId) rows.push({ kind: "live", live });
  let at = -1;
  if (notice) {
    const a = notice.anchor;
    at = rows.findIndex((r) =>
      "id" in a
        ? r.kind === "msg" && r.m.id === a.id
        : r.kind === "msg"
          ? r.m.role === a.role && (r.m.parent_id ?? null) === a.childOf
          : a.role === "assistant" && (r.live.parent_id ?? null) === a.childOf,
    );
  }
  const noticeEl =
    notice && at >= 0 ? (
      <div key={`note-${notice.key}`} className="cv-note" role="status">
        <span className="cv-note-k">{notice.k}</span>
        <span>{notice.body}</span>
        <button type="button" onClick={() => setNotice(null)}>
          知道了
        </button>
      </div>
    ) : null;

  // 換到帶提議卡的回覆：說明行放進那則回覆的資訊列正下方（版面稿第 8 題）
  const atRow = at >= 0 ? rows[at] : null;
  const noteIn = notice && "id" in notice.anchor && atRow?.kind === "msg" && atRow.m.role === "assistant" && atRow.m.proposals?.length ? atRow.m.id : null;

  const renderRow = (r: Row) => {
    if (r.kind === "live") return <LiveReply key={`live-${r.live.turn_id}`} live={r.live} fill={r.fill} />;
    const m = r.m;
    if (m.role === "user") {
      if (editing === m.id)
        return (
          <EditMessage
            key={`edit-${m.id}`}
            msg={m}
            after={after(m.id)}
            model={model}
            caps={caps}
            live={!!live}
            onCancel={() => setEditing(null)}
            onSend={(text, atts) => onEditSend(m, text, atts)}
          />
        );
      return <UserMessage key={m.id} msg={m} after={after(m.id)} off={off} onEdit={archived ? undefined : onEdit} onSwitch={onSwitch} />;
    }
    return (
      <AssistantMessage
        key={m.id}
        msg={m}
        last={m === lastReply}
        after={after(m.id)}
        off={off}
        nextModel={model}
        onRegenerate={archived || m.local ? undefined : onRegenerate}
        onSwitch={onSwitch}
        note={noteIn === m.id ? noticeEl : undefined}
      />
    );
  };

  const head = at >= 0 ? rows.slice(0, at + 1) : rows;
  const tail = at >= 0 ? rows.slice(at + 1) : [];
  return (
    // editing：手機上編輯舊訊息時把貼底的輸入區收起，鍵盤的空間讓給編輯框（rwd.css）
    <div className={`chatbox${editing ? " editing" : ""}`}>
      <ChatHeader conv={conv} pathCount={pathCount} live={!!live} onArchived={() => navigate("/chat")} />
      <div ref={feedRef} className="ch-feed" onScroll={onScroll} aria-live="polite">
        {msgs.length === 0 && !live ? <div className="feed-empty">這段聊天還沒有訊息。</div> : null}
        {head.map(renderRow)}
        {noteIn ? null : noticeEl}
        {tail.length ? (
          notice?.flash ? (
            <div key={`tail-${notice.key}`} className="cv-tail">
              {tail.map(renderRow)}
            </div>
          ) : (
            tail.map(renderRow)
          )
        ) : null}
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
        priorImages={priorImages}
        blocked={archived ? "這段聊天已封存，不能再送訊息。" : null}
      />
    </div>
  );
}
