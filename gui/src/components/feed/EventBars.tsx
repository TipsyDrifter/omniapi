import type { EventRow } from "@/api/types";

/* 票券底部的事件條（THEME.md §5.1）：已發生的事件亮成墨色、高度依類型；執行中時右側留未亮的短條當「還在跑」的暗示。
   .bars 樣式在 board.css。 */
export default function EventBars({ events, live, slots = 70 }: { events: EventRow[]; live: boolean; slots?: number }) {
  const shown = events.length > slots ? events.slice(events.length - slots) : events;
  const pad = live ? Math.max(0, Math.min(slots - shown.length, Math.ceil(slots * 0.15))) : 0;
  return (
    <div className="bars" aria-hidden="true">
      {shown.map((e) => (
        <i key={e.id} className={`on t-${e.type}${e.payload?.is_error ? " err" : ""}`} />
      ))}
      {Array.from({ length: pad }, (_, i) => (
        <i key={`pad-${i}`} />
      ))}
    </div>
  );
}
