import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

/* 聊天輸入列（沿用 M5 追問列的精簡貼底做法與 .composer 樣式）：
   一個墨框一列：提示符 ›、自動長高的 textarea（1～8 行）、右側按鈕。
   回覆進行中：按鈕變「停止」，仍可打字但不能送。Ctrl+Enter 送出；選字中的 Enter 不觸發。
   送出失敗保留輸入內容（錯誤由外層顯示）。 */

export interface ChatComposerProps {
  /** 回覆進行中 */
  live: boolean;
  /** 送出；回傳 true＝成功（清空輸入） */
  onSend: (text: string) => Promise<boolean>;
  /** 停止進行中的回覆；沒給就不顯示停止鈕 */
  onStop?: () => Promise<void>;
  placeholder: string;
  /** 整列停用時的一行原因（例如已封存） */
  blocked?: string | null;
  autoFocus?: boolean;
}

const MAX_LINES = 8;

export default function ChatComposer({ live, onSend, onStop, placeholder, blocked, autoFocus }: ChatComposerProps) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  // 跟著內容長高：先歸零量 scrollHeight，再夾在 1～MAX_LINES 行之間
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight) || 26;
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const max = line * MAX_LINES + pad;
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [text, blocked]);

  // 回覆結束：「停止中」收掉
  useLayoutEffect(() => {
    if (!live) setStopping(false);
  }, [live]);

  const canSend = !live && !sending && text.trim().length > 0;

  const submit = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      if (await onSend(text.trim())) setText("");
    } finally {
      setSending(false);
      ref.current?.focus();
    }
  };

  const stop = async () => {
    if (!onStop || stopping) return;
    setStopping(true);
    try {
      await onStop();
    } catch {
      setStopping(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // 選字中（注音／日文輸入法）的 Enter 是確認選字，不是送出
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void submit();
    }
  };

  if (blocked)
    return (
      <div className="composer off">
        <p className="composer-reason">
          <span className="composer-mark" aria-hidden="true">
            ›
          </span>
          {blocked}
        </p>
      </div>
    );

  return (
    <div className="composer">
      <div className="composer-row">
        <span className="composer-mark" aria-hidden="true">
          ›
        </span>
        <textarea
          ref={ref}
          className="composer-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          rows={1}
          placeholder={placeholder}
          aria-label="訊息內容"
          autoFocus={autoFocus}
        />
        {live && onStop ? (
          <button type="button" className="composer-send" onClick={() => void stop()} disabled={stopping} title="停止這則回覆">
            {stopping ? "停止中…" : "停止"}
          </button>
        ) : (
          <button type="button" className="composer-send" onClick={() => void submit()} disabled={!canSend} title={live ? "回覆結束後才能送下一則" : "Ctrl+Enter"}>
            {sending ? "送出中…" : "送出"}
          </button>
        )}
      </div>
    </div>
  );
}
