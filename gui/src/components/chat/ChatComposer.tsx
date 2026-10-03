import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { isSendKey, sendKeyName, useSendKey } from "@/lib/sendKey";
import type { OutgoingAttachment } from "@/store/chat";
import { AttachNotes, AttachPicker, AttachTray, useAttachments } from "./attachments";
import type { ModelCaps } from "./files";

/* 聊天輸入列（沿用 M5 追問列的精簡貼底做法與 .composer 樣式）：
   一個墨框一列：提示符 ›、自動長高的 textarea（1～8 行）、附件鈕、右側按鈕。
   回覆進行中：按鈕變「停止」，仍可打字但不能送。送出鍵照設定（lib/sendKey：Ctrl+Enter 或 Enter）；選字中的 Enter 不觸發。
   送出失敗保留輸入內容與附件（錯誤由外層顯示）。

   附件（1.2-M2 只有圖、1.2-M5 什麼檔都收）：貼上、拖進、附件鈕選檔、從作品牆挑（邏輯在 attachments.tsx，編輯舊訊息也用同一套）。
   檔案寫明這個模型會怎麼讀它（跟著細列的模型變）。模型不會看圖時圖片照舊不收，檔案不受影響。 */

export interface ChatComposerProps {
  /** 回覆進行中 */
  live: boolean;
  /** 送出；回傳 true＝成功（清空輸入與附件） */
  onSend: (text: string, attachments: OutgoingAttachment[]) => Promise<boolean>;
  /** 停止進行中的回覆；沒給就不顯示停止鈕 */
  onStop?: () => Promise<void>;
  placeholder: string;
  /** 整列停用時的一行原因（例如已封存） */
  blocked?: string | null;
  autoFocus?: boolean;
  /** 下一則的模型收什麼（看圖、PDF、音訊、工具）；不知道（模型清單沒讀到）先不擋，後端會講 */
  caps: ModelCaps;
}

const MAX_LINES = 8;

export default function ChatComposer({ live, onSend, onStop, placeholder, blocked, autoFocus, caps }: ChatComposerProps) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [picking, setPicking] = useState(false);
  const [sendKey] = useSendKey();
  const ref = useRef<HTMLTextAreaElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const a = useAttachments({ caps });

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
  }, [text, blocked, placeholder]);

  // 回覆結束：「停止中」收掉
  useLayoutEffect(() => {
    if (!live) setStopping(false);
  }, [live]);

  // 點到展開區與附件鈕以外就收起來
  useEffect(() => {
    if (!picking) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setPicking(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [picking]);

  const why = live ? "回覆結束後才能送下一則" : a.why;
  const canSend = !live && !sending && !why && (text.trim().length > 0 || a.ready.length > 0);

  const submit = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      if (await onSend(text.trim(), a.ready)) {
        setText("");
        a.clear();
        setPicking(false);
      }
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
    // 選字中（注音／日文輸入法）的 Enter 是確認選字，不是送出（isSendKey 會擋）
    if (isSendKey(e)) {
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

  const n = a.items.length;
  // 送不出去的原因（附件的問題）：滑過或聚焦送出鈕才說（同 1.2-M3 的停用樣式）
  const sendOff = !live && !!a.why;
  return (
    <div className={`composer${a.drop.over ? " over" : ""}`} ref={boxRef} {...a.drop.handlers}>
      {picking ? (
        <div className="cs-pop cx-pop" role="dialog" aria-label="附件">
          <AttachPicker a={a} />
        </div>
      ) : null}
      <AttachTray a={a} />
      <AttachNotes a={a} />
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
          onPaste={a.onPaste}
          rows={1}
          placeholder={placeholder}
          aria-label="訊息內容"
          autoFocus={autoFocus}
        />
        <button type="button" className="composer-att" aria-expanded={picking} onClick={() => setPicking((p) => !p)} title="附上檔案、圖片，或挑作品牆的作品">
          附件
          {n ? <span className="n"> {n}</span> : null}
        </button>
        {a.drop.input("")}
        {live && onStop ? (
          <button type="button" className="composer-send" onClick={() => void stop()} disabled={stopping} title="停止這則回覆">
            {stopping ? "停止中…" : "停止"}
          </button>
        ) : (
          <span className={sendOff ? "cx-attwrap" : "cx-sendwrap"}>
            <button type="button" className="composer-send" onClick={() => void submit()} disabled={!canSend} aria-describedby={sendOff ? "cx-send-tip" : undefined} title={sendOff ? undefined : (why ?? sendKeyName(sendKey))}>
              {sending ? "送出中…" : a.uploading ? "上傳中…" : "送出"}
            </button>
            {sendOff ? (
              <span className="cx-tip" id="cx-send-tip" role="tooltip">
                {a.why}
              </span>
            ) : null}
          </span>
        )}
      </div>
      {a.drop.over ? (
        <div className="cx-dropmsg" aria-hidden="true">
          放開就附上{a.blind ? "（這個模型不會看圖，圖不會附上）" : ""}
        </div>
      ) : null}
    </div>
  );
}
