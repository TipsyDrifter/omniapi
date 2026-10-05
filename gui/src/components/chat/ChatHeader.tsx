import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { ChatDetail } from "@/api/types";
import { usd } from "@/lib/format";
import { deleteChat, updateChat } from "@/store/chat";

/* 對話表頭一列：標題（點一下改名：Enter 確定、Esc 或點別處取消）、id、則數、費用合計、匯出、封存、刪除…
   則數＝畫面上這一條；後端的合計含所有版本，不同時另標「共 N」。
   刪除（1.2-M3，D37 真的刪）：跟匯出、封存並排但隔一道線、字尾帶「…」；確認框從按鈕往下長，
   對話區蓋一層網點（按網點或 Esc 取消），三列對照刪除與封存，旁邊給「改成封存」的退路。
   刪除成功後由聊天頁換到清單裡的下一筆（store 記下；別的分頁刪掉時也一樣）。 */

export interface ChatHeaderProps {
  conv: ChatDetail;
  /** 畫面上現行這一條的則數 */
  pathCount: number;
  /** 回覆進行中（刪除會先停掉它） */
  live: boolean;
  /** 封存成功（外層負責回 /chat） */
  onArchived: () => void;
}

export default function ChatHeader({ conv, pathCount, live, onArchived }: ChatHeaderProps) {
  /** 從這段聊天來的生成與轉錄的實際費用合計（後端算的，所有分支；沒有回報費用的生成＝不顯示） */
  const genCost = conv.generation_cost_usd ?? undefined;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [del, setDel] = useState(false);
  const [delBusy, setDelBusy] = useState<null | "delete" | "archive">(null);
  const [delErr, setDelErr] = useState<string | null>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const [washTop, setWashTop] = useState(0);
  const archived = conv.status === "archived";
  const title = conv.title || "未命名";

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
    if (!window.confirm(`封存「${title}」？封存後不會出現在清單裡（不會刪除）。`)) return;
    setErr(null);
    try {
      await updateChat(conv.id, { archived: true });
      onArchived();
    } catch (e) {
      setErr(`封存失敗：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  /* ---------- 刪除確認框 ---------- */
  const openDel = () => {
    setDelErr(null);
    setWashTop(headRef.current?.offsetHeight ?? 0);
    setDel((d) => !d);
  };
  const closeDel = () => {
    if (delBusy) return;
    setDel(false);
  };
  useEffect(() => {
    if (!del) return;
    const onEsc = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") closeDel();
    };
    document.addEventListener("keydown", onEsc);
    return () => document.removeEventListener("keydown", onEsc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [del, delBusy]);
  const doDelete = async () => {
    setDelBusy("delete");
    setDelErr(null);
    try {
      await deleteChat(conv.id);
      // 聊天頁看到 store 的「已刪除」就換到下一筆；這裡不用再做什麼
    } catch (e) {
      setDelErr(`刪除失敗：${e instanceof Error ? e.message : String(e)}`);
      setDelBusy(null);
    }
  };
  const doArchive = async () => {
    setDelBusy("archive");
    setDelErr(null);
    try {
      await updateChat(conv.id, { archived: true });
      setDel(false);
      onArchived();
    } catch (e) {
      setDelErr(`封存失敗：${e instanceof Error ? e.message : String(e)}`);
      setDelBusy(null);
    }
  };

  /* ---------- 1.3-M3：窄的時候表頭的動作收進「⋯」（M 段在表頭右邊、S 段在返回列右邊） ---------- */
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    if (!menu) return;
    const off = (e: MouseEvent) => {
      if (!(e.target instanceof Element) || !e.target.closest(".ch-menu, .ch-kebab")) setMenu(false);
    };
    const esc = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setMenu(false);
    document.addEventListener("click", off);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("click", off);
      document.removeEventListener("keydown", esc);
    };
  }, [menu]);
  const kebab = (
    <button type="button" className="kebab ch-kebab" aria-label="這段聊天的動作" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
      ⋯
    </button>
  );
  const menuEl = menu ? (
    <div className="ch-menu" role="menu">
      {!archived ? (
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            setMenu(false);
            start();
          }}
        >
          改名
        </button>
      ) : null}
      <a role="menuitem" href={api.chatExportUrl(conv.id)} download onClick={() => setMenu(false)}>
        匯出 markdown
      </a>
      {!archived ? (
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            setMenu(false);
            void archive();
          }}
        >
          封存
        </button>
      ) : null}
      <button
        type="button"
        role="menuitem"
        onClick={() => {
          setMenu(false);
          openDel();
        }}
      >
        刪除…
      </button>
    </div>
  ) : null;

  // 一則有價的回覆都沒有才整個寫「未計價」；否則寫合計，另標未計價的則數（null 不當 0）
  const allUnpriced = (conv.priced ?? 0) === 0 && conv.unpriced > 0;
  const total = conv.n_messages;
  const hidden = Math.max(0, total - pathCount);
  return (
    <>
      <div className="ctxbar">
        <Link className="back" to="/chat">
          ‹ 聊天
        </Link>
        <span className="t">{conv.title || "（未命名）"}</span>
        <span className="ctx-r">
          {kebab}
          {menuEl}
        </span>
      </div>
      <div className={`ch-head${editing || del ? " open" : ""}`} ref={headRef}>
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
        <span className="ch-stat" title={hidden ? `畫面上這一條是 ${pathCount} 則；含切換版本才看得到的，共 ${total} 則` : undefined}>
          <span className="n">{pathCount}</span> 則
          {hidden ? (
            <>
              {" "}
              <span className="k">共</span> <span className="n">{total}</span>
            </>
          ) : null}
        </span>
        <span className="ch-stat" title={genCost != null ? `聊天本身 ${usd(conv.cost_usd, 4)}＋在這段聊天裡生成的 ${usd(genCost, 4)}` : undefined}>
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
          {genCost != null ? (
            <>
              {" "}
              <span className="k">＋生成</span> <span className="n">{usd(genCost, 4)}</span>
            </>
          ) : null}
        </span>
        {archived ? <span className="cm-tag">已封存</span> : null}
        <span className="ch-acts">
          <a className="ch-act" href={api.chatExportUrl(conv.id)} download>
            匯出 markdown
          </a>
          {!archived ? (
            <button type="button" className="ch-act" onClick={() => void archive()} title="收起來：不在清單裡，不會刪除">
              封存
            </button>
          ) : null}
          <i className="navsep" aria-hidden="true" />
          <button type="button" className="ch-act" aria-expanded={del} aria-haspopup="dialog" onClick={openDel}>
            刪除…
          </button>
          {kebab}
          {menuEl}
        </span>
        {del ? (
          <div className="cd-pop" role="alertdialog" aria-modal="true" aria-labelledby="cd-q" aria-describedby="cd-lead">
            <div className="dp-fh">
              <span className="lbl">Delete</span>
              <span className="zh">刪除聊天</span>
              <span className="aside">按下去就刪，沒有回收區</span>
            </div>
            <h3 className="cd-q" id="cd-q">
              刪除「{title}」？
            </h3>
            <p className="cd-lead" id="cd-lead">
              這段聊天的 <span className="n">{total}</span> 則訊息
              {hidden ? (
                <>
                  （含切換版本才看得到的 <span className="n">{hidden}</span> 則）
                </>
              ) : null}
              會<b>真的刪掉，無法復原</b>。{live ? "回覆還在進行中，刪除會先停掉它。" : null}
            </p>
            <div className="cd-cmp">
              <span className="h" />
              <span className="h this">
                刪除<small>這個按鈕</small>
              </span>
              <span className="h">
                封存<small>只是收起來</small>
              </span>
              <span className="k">聊天紀錄</span>
              <span className="x">刪掉，找不回來</span>
              <span>留著，不在清單裡</span>
              <span className="k">生成的作品</span>
              <span>仍在作品牆</span>
              <span className="same">仍在作品牆</span>
              <span className="k">花掉的費用</span>
              <span>仍記在費用頁</span>
              <span className="same">仍記在費用頁</span>
            </div>
            {delErr ? (
              <div className="warn cd-err" role="alert">
                {delErr}
              </div>
            ) : null}
            <div className="cd-acts">
              {!archived ? (
                <span className="cd-alt">
                  只是不想看到它？
                  <button type="button" onClick={() => void doArchive()} disabled={!!delBusy}>
                    {delBusy === "archive" ? "封存中…" : "改成封存"}
                  </button>
                </span>
              ) : (
                <span className="cd-alt">這段聊天已經封存了。</span>
              )}
              <button type="button" className="cs-act" onClick={closeDel} disabled={!!delBusy} autoFocus>
                取消
              </button>
              <button type="button" className="cs-act danger" onClick={() => void doDelete()} disabled={!!delBusy}>
                {delBusy === "delete" ? "刪除中…" : "刪除，無法復原"}
              </button>
            </div>
          </div>
        ) : null}
      </div>
      {del ? <div className="cd-wash" style={{ top: washTop }} onClick={closeDel} aria-hidden="true" /> : null}
      {err ? (
        <div className="warn ch-headerr" role="alert">
          {err}
        </div>
      ) : null}
    </>
  );
}
