import type { ReactNode } from "react";

/* 表單欄位的表頭：拉丁標籤（.lbl）＋中文標籤（label 700）＋右側注記，下方 2px 墨線 */
export function Field({ lbl, zh, aside, htmlFor, children, className }: { lbl: string; zh: string; aside?: ReactNode; htmlFor?: string; children: ReactNode; className?: string }) {
  return (
    <div className={`dp-f${className ? " " + className : ""}`}>
      <div className="dp-fh">
        <label className="lbl" htmlFor={htmlFor}>
          {lbl}
        </label>
        <span className="zh">{zh}</span>
        {aside ? <span className="aside">{aside}</span> : null}
      </div>
      {children}
    </div>
  );
}

/* 方形勾選框（不用瀏覽器原生的圓角框） */
export function Check({ checked, onChange, children, disabled }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode; disabled?: boolean }) {
  return (
    <label className="dp-chk" aria-disabled={disabled || undefined}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{children}</span>
    </label>
  );
}
