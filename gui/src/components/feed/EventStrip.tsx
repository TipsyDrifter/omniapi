import type { EventRow } from "@/api/types";

/* 事件條碼（歷史詳情用，THEME.md §5.2 詳情條碼）：每則事件一格，高度依類型；錯誤＝墨斜紋。
   .strip 樣式在 board.css。父層需掛 harness class（.h-claude 等）才有 tool_call 的油墨色。 */
export default function EventStrip({ events, max = 400 }: { events: EventRow[]; max?: number }) {
  const list = events.length > max ? events.slice(events.length - max) : events;
  return (
    <div className="strip" aria-hidden="true">
      {list.map((e) => (
        <i key={e.id} className={`t-${e.type}${e.payload?.is_error ? " err" : ""}`} title={`#${e.id} ${e.type}`} />
      ))}
    </div>
  );
}
