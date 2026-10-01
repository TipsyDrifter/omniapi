import { useState } from "react";
import { api } from "@/api/client";

/* 共用「中止」鈕（決策記錄 M5-h）：確認 → POST /api/runs/{id}/cancel。
   墨框直角小鈕，框與字取 currentColor（放在油墨票券上跟著票面字色走，同 .resu）。
   只負責送出；票券轉成「已取消」靠 WS 的 run.finished 更新 store。呼叫端自己決定只在 live 時掛上。 */

export interface CancelButtonProps {
  runId: string;
  /** 中止請求送出成功後 */
  onDone?: () => void;
  /** s＝票券／表頭用的小鈕（預設）；m＝稍大 */
  size?: "s" | "m";
  className?: string;
}

type Phase = "idle" | "busy" | "sent";

export default function CancelButton({ runId, onDone, size = "s", className }: CancelButtonProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [err, setErr] = useState<string | null>(null);

  const onClick = async (e: React.MouseEvent) => {
    // 可能放在可點的容器裡，不要往上冒
    e.stopPropagation();
    if (phase !== "idle") return;
    if (!window.confirm(`中止 ${runId}？agent 會立刻停下，已經改的檔案不會復原。`)) return;
    setPhase("busy");
    setErr(null);
    try {
      await api.cancelRun(runId);
      setPhase("sent");
      onDone?.();
    } catch (x) {
      setPhase("idle");
      setErr(x instanceof Error ? x.message : String(x));
    }
  };

  const label = phase === "busy" ? "中止中…" : phase === "sent" ? "已送出" : "中止";
  return (
    <span className={`cancel ${size === "m" ? "m" : "s"}${className ? ` ${className}` : ""}`}>
      <button type="button" className="cancel-btn" onClick={onClick} disabled={phase !== "idle"} title={err ? `中止失敗：${err}` : `中止 ${runId}`}>
        {label}
      </button>
      {err ? (
        <span className="cancel-err" role="alert">
          中止失敗：{err}
        </span>
      ) : null}
    </span>
  );
}
