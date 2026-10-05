import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLockScroll } from "@/lib/rwd";

/* 從底部升起的單子（1.3-M3，NOTES §2「更多」、§4 訊息動作、§3.6 作品牆篩選）：墨框紙底、上緣一條抓手，不用陰影漸層。
   點單子外的網點、按 Esc 收起。掛在 body 上（不被頁面的捲動容器裁掉）。 */
export default function Sheet({ open, onClose, label, children, className }: { open: boolean; onClose: () => void; label: string; children: ReactNode; className?: string }) {
  useLockScroll(open);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <>
      <div className="sheet-veil" onClick={onClose} aria-hidden="true" />
      <div className={`sheet${className ? ` ${className}` : ""}`} role="dialog" aria-modal="true" aria-label={label}>
        <div className="sheet-grab" aria-hidden="true" />
        {children}
      </div>
    </>,
    document.body,
  );
}
