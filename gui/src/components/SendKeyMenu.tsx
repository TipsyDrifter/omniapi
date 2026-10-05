import { useEffect, useRef, useState, type ReactNode } from "react";
import { sendHint, useSendKey, type SendKey } from "@/lib/sendKey";

/* 送出鍵選單（決策記錄 D40）：兩個選項＋三件事（四處一起生效、選字的 Enter 不送、記在這個瀏覽器）。
   聊天放在細列第三項（ChatDock）；追問、派工、生成頁沒有細列，讓它們的「Ctrl+Enter 送出」小字可以點，開同一個選單（SendKeyHint）。 */

/** 選單內容（展開區的表頭、兩個選項、說明） */
export function SendKeyOptions({ onPicked }: { onPicked?: (k: SendKey) => void }) {
  const [key, setKey] = useSendKey();
  const pick = (k: SendKey) => {
    setKey(k);
    onPicked?.(k);
  };
  return (
    <>
      <div className="dp-fh">
        <span className="lbl">Send key</span>
        <span className="zh">送出鍵</span>
        <span className="aside">四處的輸入框一起改</span>
      </div>
      <div className="ck-opts" role="radiogroup" aria-label="送出鍵">
        <button type="button" className="ck-opt" role="radio" aria-checked={key === "ctrl"} onClick={() => pick("ctrl")}>
          <span className="ck-dot" aria-hidden="true" />
          <span className="ck-t">
            <span className="kbd">Ctrl</span>
            <span className="ck-plus">＋</span>
            <span className="kbd">Enter</span> 送出
          </span>
          <span className="ck-sub">單按 Enter 是換行。適合一次寫好幾段再送。</span>
        </button>
        <button type="button" className="ck-opt" role="radio" aria-checked={key === "enter"} onClick={() => pick("enter")}>
          <span className="ck-dot" aria-hidden="true" />
          <span className="ck-t">
            <span className="kbd">Enter</span> 直接送出
          </span>
          <span className="ck-sub">換行用 Shift＋Enter；Ctrl＋Enter 也照樣送出。適合一來一往的短句。</span>
        </button>
      </div>
      <div className="ck-where">
        <b>一起生效</b>
        <span>聊天、追問、派工、生成頁的輸入框（提示字跟著改）</span>
        <b>不會誤送</b>
        <span>注音、倉頡選字時按的 Enter 只是選字。</span>
        <b>記在哪</b>
        <span>這個瀏覽器；之後有設定頁會搬過去。</span>
      </div>
    </>
  );
}

/** 鍵帽：Ctrl+Enter／Enter */
export function SendKeyCap({ mode }: { mode: SendKey }) {
  return <span className="kbd">{mode === "enter" ? "Enter" : "Ctrl+Enter"}</span>;
}

/** 點開／收起、點外面或 Esc 收起 */
function usePop() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

/** 追問、派工、生成頁的「Ctrl+Enter 送出」小字：可以點，開送出鍵選單。
 *  children 沒給＝顯示提示字本身；up＝選單往上長（輸入列在頁面底部時） */
export function SendKeyHint({ up, cap, before, sep }: { up?: boolean; cap?: boolean; before?: ReactNode; sep?: boolean }) {
  const [key] = useSendKey();
  const { open, setOpen, ref } = usePop();
  return (
    <span className={`ck-wrap${cap ? " cap" : ""}`} ref={ref}>
      {/* 跟前面字數之間的「·」放在這一塊裡：觸控時整塊藏起來，不會留下一個孤單的點 */}
      {sep ? " · " : null}
      <button type="button" className={cap ? "ck-capbtn" : "ck-hint"} aria-expanded={open} aria-haspopup="dialog" title="選送出鍵" onClick={() => setOpen((o) => !o)}>
        {before}
        {cap ? <SendKeyCap mode={key} /> : sendHint(key)}
      </button>
      {open ? (
        <span className={`cs-pop ck-pop ck-float${up ? "" : " down"}`} role="dialog" aria-label="送出鍵">
          <SendKeyOptions onPicked={() => setOpen(false)} />
        </span>
      ) : null}
    </span>
  );
}
