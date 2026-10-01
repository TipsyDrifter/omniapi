import { useLayoutEffect, useRef, useState } from "react";

/* 思考內容（THEME §2.2 thinking 傍注）：點線章「想」＋sans 12/1.55 墨 70，預設收兩行，點一下展開。
   不用斜體、不降透明度。 */
export default function Reasoning({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [over, setOver] = useState(false);
  const ref = useRef<HTMLParagraphElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !open) setOver(el.scrollHeight > el.clientHeight + 2);
  }, [text, open]);
  if (!text) return null;
  const toggle = () => (over || open) && setOpen((o) => !o);
  return (
    <div
      className={`cm-think${open ? " open" : ""}${over || open ? " can" : ""}`}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      onClick={toggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggle();
        }
      }}
    >
      <span className="cm-think-gl" aria-hidden="true">
        想
      </span>
      <div className="cm-think-b">
        <p ref={ref}>{text}</p>
        {over || open ? <span className="cm-think-tog">{open ? "收合 ▴" : "展開 ▾"}</span> : null}
      </div>
    </div>
  );
}
