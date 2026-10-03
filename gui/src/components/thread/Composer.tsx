import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { api } from "@/api/client";
import type { Run, Thread } from "@/api/types";
import { SendKeyHint } from "@/components/SendKeyMenu";
import { harnessName } from "@/lib/format";
import { isSendKey, sendHint, sendKeyName, useSendKey } from "@/lib/sendKey";

/* 追問輸入列（決策記錄 M5-g；2026-09-29 主人回饋後改成精簡版）：
   貼在追問串最底的一列——平常只有一行高，打字才往上長（最多約 8 行，再多就在框內捲），
   把高度讓給閱讀區，像各家 harness 的 GUI／TUI 那樣。
   thread.resumable 才能輸入；不能追問時整列收成一行原因。
   送出＝POST /api/runs 帶 resume_run_id＝leaf；成功後交給 onStarted（換網址、重抓串）；失敗保留輸入內容。
   送出鍵照設定（1.2-M3）：追問沒有細列，輸入列裡的鍵帽可以點，開送出鍵選單。 */

export interface ComposerProps {
  thread: Thread;
  /** 串的最後一筆（store 版，會即時更新）；harness／模型說明取自它 */
  leaf: Run | undefined;
  /** leaf 還在跑（store 比 thread 快一步知道） */
  leafLive: boolean;
  /** 新的 run 已建立 */
  onStarted: (runId: string) => void | Promise<void>;
}

/** 輸入框最多長到幾行（超過在框內捲） */
const MAX_LINES = 8;

export default function Composer({ thread, leaf, leafLive, onStarted }: ComposerProps) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const [sendKey] = useSendKey();

  const enabled = thread.resumable && !leafLive;
  const reason = leafLive ? thread.reason ?? "最後一段還在執行中，結束後才能追問。" : thread.reason ?? "這條串目前不能追問。";
  const canSend = enabled && !sending && text.trim().length > 0;
  const via = leaf ? `${harnessName(leaf.harness)} · ${leaf.model ?? "—"}` : "";

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
  }, [text, enabled]);

  const submit = async () => {
    if (!canSend) return;
    setSending(true);
    setErr(null);
    try {
      const res = await api.startRun({ prompt: text.trim(), resume_run_id: thread.leaf_id, dispatcher: "GUI" });
      setText("");
      await onStarted(res.run_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // 送出鍵照設定（lib/sendKey）；選字中（注音／日文輸入法）的 Enter 是確認選字，不是送出
    if (isSendKey(e)) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <div className={`composer${enabled ? "" : " off"}`}>
      {err ? (
        <div className="warn" role="alert">
          <b>追問失敗：</b>
          {err}
        </div>
      ) : null}
      {enabled ? (
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
            disabled={sending}
            rows={1}
            placeholder={`追問……（沿用同一個模型：${via}；${sendHint(sendKey)}）`}
            title={`沿用最後一段：${via}`}
            aria-label="追問內容"
          />
          <SendKeyHint up cap />
          <button type="button" className="composer-send" onClick={() => void submit()} disabled={!canSend} title={sendKeyName(sendKey)}>
            {sending ? "送出中…" : "追問"}
          </button>
        </div>
      ) : (
        <p className="composer-reason">
          <span className="composer-mark" aria-hidden="true">
            ›
          </span>
          {reason}
        </p>
      )}
    </div>
  );
}
