import { useState, type KeyboardEvent } from "react";
import { api } from "@/api/client";
import type { ChatDetail } from "@/api/types";
import { usd } from "@/lib/format";
import { updateChat } from "@/store/chat";

/* 對話表頭一列：標題（點一下改名：Enter 確定、Esc 或點別處取消）、id、訊息數、費用合計、匯出、封存 */

export interface ChatHeaderProps {
  conv: ChatDetail;
  /** 封存成功（外層負責回 /chat） */
  onArchived: () => void;
}

export default function ChatHeader({ conv, onArchived }: ChatHeaderProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const archived = conv.status === "archived";

  const start = () => {
    setDraft(conv.title ?? "");
    setErr(null);
    setEditing(true);
  };
  const commit = async () => {
    const t = draft.trim();
    if (!t || t === (conv.title ?? "")) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await updateChat(conv.id, { title: t });
      setEditing(false);
    } catch (e) {
      setErr(`改名失敗：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Enter") {
      e.preventDefault();
      void commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      setEditing(false);
    }
  };

  const archive = async () => {
    if (!window.confirm(`封存「${conv.title || "未命名"}」？封存後不會出現在清單裡（不會刪除）。`)) return;
    setErr(null);
    try {
      await updateChat(conv.id, { archived: true });
      onArchived();
    } catch (e) {
      setErr(`封存失敗：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // 一則有價的回覆都沒有才整個寫「未計價」；否則寫合計，另標未計價的則數（null 不當 0）
  const allUnpriced = (conv.priced ?? 0) === 0 && conv.unpriced > 0;
  return (
    <>
      <div className="ch-head">
        {editing ? (
          <input
            className="ch-title-in"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            onBlur={() => !busy && setEditing(false)}
            disabled={busy}
            autoFocus
            aria-label="聊天標題"
            placeholder="聊天標題"
          />
        ) : (
          <button type="button" className="ch-title" onClick={start} title="點一下改名" disabled={archived}>
            {conv.title || "（未命名）"}
          </button>
        )}
        <span className="ch-id code" title="聊天 id">
          {conv.id}
        </span>
        <span className="ch-stat">
          <span className="n">{conv.n_messages}</span> 則
        </span>
        <span className="ch-stat">
          <span className="k">費用</span>{" "}
          {allUnpriced ? (
            <span className="na">未計價</span>
          ) : (
            <>
              <span className="n">{usd(conv.cost_usd, 4)}</span>
              {conv.unpriced > 0 ? (
                <span className="na">
                  {" "}
                  另有 <span className="n">{conv.unpriced}</span> 則未計價
                </span>
              ) : null}
            </>
          )}
        </span>
        {archived ? <span className="cm-tag">已封存</span> : null}
        <span className="ch-acts">
          <a className="ch-act" href={api.chatExportUrl(conv.id)} download>
            匯出 markdown
          </a>
          {!archived ? (
            <button type="button" className="ch-act" onClick={() => void archive()}>
              封存
            </button>
          ) : null}
        </span>
      </div>
      {err ? (
        <div className="warn ch-headerr" role="alert">
          {err}
        </div>
      ) : null}
    </>
  );
}
