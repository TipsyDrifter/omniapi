import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ChatMessage } from "@/api/types";
import { dt } from "@/lib/format";
import { isSendKey, sendHint, useSendKey } from "@/lib/sendKey";
import type { OutgoingAttachment } from "@/store/chat";
import { AttachNotes, AttachPicker, AttachTray, useAttachments } from "./attachments";
import type { ModelCaps } from "./files";

/* 編輯舊訊息（1.2-M3）：原地變成輸入框（2px 墨框＋偏移陰影＝輸入列聚焦的樣子）。
   上面一排附件（原本的圖與檔案可以拿掉、可以加：同輸入列那套附件，挑選區在框內展開），
   框內一句話講分岔，送出鍵照設定，Esc 或「取消」回到原樣、不留草稿。 */

export interface EditMessageProps {
  msg: ChatMessage;
  /** 後面還有幾則（「原本這條和後面的 N 則都留著」） */
  after: number;
  /** 細列「下一則用」的模型：回覆由它來 */
  model: string;
  caps: ModelCaps;
  /** 回覆進行中（別處開始的）：不能送 */
  live: boolean;
  onCancel: () => void;
  /** attachments：undefined＝沒動過附件（沿用原本的）；陣列＝換成這些 */
  onSend: (text: string, attachments: OutgoingAttachment[] | undefined) => Promise<void>;
}

const MAX_LINES = 10;

export default function EditMessage({ msg, after, model, caps, live, onCancel, onSend }: EditMessageProps) {
  const [text, setText] = useState(msg.content ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [sendKey] = useSendKey();
  const a = useAttachments({ caps, initial: msg.attachments });
  const ref = useRef<HTMLTextAreaElement>(null);
  const next = (msg.versions?.count ?? 1) + 1;

  // 打開就聚焦、游標放最後
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);

  // 跟著內容長高
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
  }, [text]);

  const why = live ? "回覆結束後才能送出" : a.why;
  const empty = !text.trim() && a.ready.length === 0;
  const canSend = !busy && !why && !empty;

  const send = async () => {
    if (!canSend) return;
    setBusy(true);
    setErr(null);
    try {
      await onSend(text.trim(), a.touched ? a.ready : undefined);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      e.stopPropagation();
      onCancel();
      return;
    }
    if (isSendKey(e)) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className={`cm-u editing${a.drop.over ? " over" : ""}`} data-msg={msg.id} {...a.drop.handlers}>
      <div className="cm-meta">
        <span className="k">你</span>
        <span>編輯中</span>
        <span className="n">{dt(msg.created_at)}</span>
      </div>
      <AttachTray
        a={a}
        className="cx-tray ce-tray"
        always
        extra={
          <button type="button" className="ce-add" aria-expanded={picking} onClick={() => setPicking((p) => !p)} title="從電腦選檔案，或挑作品牆的圖與文字">
            ＋<br />
            附件
          </button>
        }
      />
      {picking ? (
        <div className="ce-pick" role="group" aria-label="附件">
          <AttachPicker a={a} />
        </div>
      ) : null}
      {a.drop.input("")}
      <AttachNotes a={a} />
      <textarea
        ref={ref}
        className="ce-in"
        rows={2}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        onPaste={a.onPaste}
        aria-label="編輯這則訊息"
        disabled={busy}
      />
      <p className="ce-fork">
        <span className="ce-fork-k">分岔</span>
        <span>
          送出後從這裡分出新的一條（第 <span className="n">{next}</span> 版）；
          {after > 0 ? (
            <>
              原本這條和後面的 <span className="n">{after}</span> 則都留著
            </>
          ) : (
            "原本這一則留著"
          )}
          ，隨時按 ‹ › 切回來。
        </span>
      </p>
      {err ? (
        <div className="warn ce-err" role="alert">
          <b>送出失敗</b>：{err}
        </div>
      ) : null}
      <div className="ce-foot">
        <span className="cs-hint">
          {sendHint(sendKey)} · Esc 取消 · 由 <span className="code">{model}</span> 回覆
        </span>
        <button type="button" className="cs-act" onClick={onCancel}>
          取消
        </button>
        <button type="button" className="cs-act main" onClick={() => void send()} disabled={!canSend} title={why ?? (empty ? "寫點字或附個檔案" : undefined)}>
          {busy ? "送出中…" : "送出"}
        </button>
      </div>
    </div>
  );
}
