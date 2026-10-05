import { Link } from "react-router-dom";
import type { ChatSummary } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { useTier } from "@/lib/rwd";
import { sendKeyName, useSendKey } from "@/lib/sendKey";
import { selectChatList, useChat } from "@/store/chat";
import { selectChatsGenerating, useMake } from "@/store/make";

/* 左欄：對話清單（最近活動在前；封存的 store 已濾掉）。目前開著的那筆＝紙深底＋墨框。
   1.2-M4：這段聊天裡按了生成、還在做的，標「生成中」；金額＝聊天＋從這段聊天來的生成與轉錄（後端的合計 generation_cost_usd）。 */

export interface ChatListProps {
  /** 目前開著的對話 id（/chat 時為空） */
  activeId?: string;
  /** 讀清單失敗的原因 */
  error?: string | null;
}

export default function ChatList({ activeId, error }: ChatListProps) {
  const list = useChat(selectChatList);
  const loaded = useChat((s) => s.listLoaded);
  const liveIds = useChat((s) => Object.keys(s.live).sort().join(","));
  const liveSet = new Set(liveIds ? liveIds.split(",") : []);
  const genIds = useMake(selectChatsGenerating);
  const [sendKey] = useSendKey();
  const tier = useTier();
  return (
    <div className="chat-side">
      <div className="sechead chat-sechead">
        <h2>
          <span className="ovp" data-t="Chat">
            Chat
          </span>
        </h2>
        <span className="zh">聊天</span>
        {/* 手機（S 段）清單自己一層：「＋新聊天」開新聊天那一層（/chat?new） */}
        <Link className="chat-new" to={tier === "s" ? "/chat?new=1" : "/chat"} aria-current={activeId || tier === "s" ? undefined : "page"}>
          ＋新聊天
        </Link>
      </div>
      <div className="chat-list" role="list">
        {error ? (
          <div className="warn">
            <b>讀不到聊天清單</b>：{error}
          </div>
        ) : null}
        {!loaded && !error ? <div className="feed-empty">載入中…</div> : null}
        {loaded && list.length === 0 ? (
          <div className="chat-list-empty">
            <p>還沒有聊天。</p>
            <p>在右邊輸入列寫下第一則、按 {sendKeyName(sendKey)}，就會開一段新聊天；也可以從頂欄「＋新對話」選「聊天」。</p>
          </div>
        ) : null}
        {list.map((c) => (
          <ChatItem key={c.id} c={c} active={c.id === activeId} live={c.live === true || liveSet.has(c.id)} generating={genIds.includes(c.id)} genCost={c.generation_cost_usd ?? undefined} />
        ))}
      </div>
    </div>
  );
}

function ChatItem({ c, active, live, generating, genCost }: { c: ChatSummary; active: boolean; live: boolean; generating: boolean; genCost?: number }) {
  // 一則有價的回覆都沒有、卻有未計價的回覆 → 整個寫「未計價」；否則寫合計（同表頭的規則）；在這段聊天裡生成的另外加進去（1.2-M4）
  const chatCost = (c.priced ?? 0) === 0 && c.unpriced > 0 ? null : c.cost_usd;
  const cost = chatCost == null && genCost == null ? null : usd((chatCost ?? 0) + (genCost ?? 0), 4);
  return (
    <Link role="listitem" className="ci" to={`/chat/${encodeURIComponent(c.id)}`} aria-current={active ? "page" : undefined}>
      <span className="ci-row">
        <span className="ci-title">{c.title || "（未命名）"}</span>
        {live ? <span className="cm-tag on">回覆中</span> : null}
        {generating ? (
          <span className="cm-tag on" title="這段聊天裡按了生成的，還在做">
            生成中
          </span>
        ) : null}
      </span>
      <span className="ci-meta">
        <span className="code">{c.model ?? "—"}</span>
        <span className="ci-r">
          <span className="n">{dt(c.updated_at)}</span>
          <span>
            <span className="n">{c.n_messages}</span> 則
          </span>
          {cost ? (
            <span className="n" title={[genCost != null ? `含在這段聊天裡生成的 ${usd(genCost, 4)}` : "", c.unpriced > 0 ? `另有 ${c.unpriced} 則未計價` : ""].filter(Boolean).join("；") || undefined}>
              {cost}
              {c.unpriced > 0 ? "＋" : ""}
            </span>
          ) : (
            <span className="na">未計價</span>
          )}
        </span>
      </span>
    </Link>
  );
}
