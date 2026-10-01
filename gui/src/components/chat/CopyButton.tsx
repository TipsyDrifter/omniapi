import { useEffect, useRef, useState } from "react";

/* 複製原始 markdown；成功後短暫顯示「已複製」 */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    /* 非安全來源或沒授權：退回舊做法 */
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  if (!ok) throw new Error("copy failed");
}

export default function CopyButton({ text, label = "複製" }: { text: string; label?: string }) {
  const [st, setSt] = useState<"idle" | "ok" | "fail">("idle");
  const t = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => void (t.current && clearTimeout(t.current)), []);
  const onClick = () => {
    copyText(text)
      .then(() => setSt("ok"))
      .catch(() => setSt("fail"))
      .finally(() => {
        if (t.current) clearTimeout(t.current);
        t.current = setTimeout(() => setSt("idle"), 1600);
      });
  };
  return (
    <button type="button" className={`cm-copy${st === "ok" ? " ok" : ""}`} onClick={onClick} title="複製這則的原始 markdown">
      {st === "ok" ? "已複製" : st === "fail" ? "複製失敗" : label}
    </button>
  );
}
