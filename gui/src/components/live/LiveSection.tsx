import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Run } from "@/api/types";
import { isLiveRun, loadRunEvents, selectEvents, selectLiveIds, selectRun, useBoard } from "@/store/board";
import Ticket from "./Ticket";
import TicketStack from "./TicketStack";
import Focus from "./Focus";
import IdleTicket from "./IdleTicket";

/* 執行中區（.hero；決策記錄 D24 護欄二：可堆疊票券，1～N 個 run）。
   模式（看 store 的 live run 數）：
   · 0 個 → 若先前有選中的 run 就保留那張（顯示結束狀態）直到新 run 出現；從沒有過 → IdleTicket
   · 1 個 → 大票券 Ticket（.hero 不加 .m3）
   · ≥2 個 → .hero.m3：左 TicketStack，右上 Focus（選中那張的細節）
   選中規則：預設（沒點過）永遠跟著最新的 live run；使用者點過的那張只要還在跑就不搶走，它結束了 → 回到跟最新的 live；
   全部結束 → 保留最後選中那張（結束狀態）直到新 run 出現。
   右欄 .hero-r 的活動流由整合者插：renderFeed(選中 id) 與 children 依序放在 Focus 之下。 */

export interface LiveSectionProps {
  /** 右欄活動流；參數是目前選中的 run id（idle 時 null） */
  renderFeed?: (runId: string | null) => ReactNode;
  /** 其他要放進右欄（Focus／feed 之後）的東西 */
  children?: ReactNode;
  /** 選中改變時回報（含自動切換）；idle 時 null */
  onSelect?: (runId: string | null) => void;
  /** 額外 class；預設已帶 .wrap（與原型相同） */
  className?: string;
}

export default function LiveSection({ renderFeed, children, onSelect, className }: LiveSectionProps) {
  const liveIds = useBoard(selectLiveIds);
  // manual＝使用者點過票券；自動選的會一直跟著最新的 live run
  const [picked, setPicked] = useState<{ id: string | null; manual: boolean }>({ id: null, manual: false });

  // 有 live：使用者點的那張還在跑就沿用，否則取最新；沒 live：保留上一張（從沒有過就是 null → idle）
  const selectedId = liveIds.length ? (picked.manual && picked.id && liveIds.includes(picked.id) ? picked.id : liveIds[0]) : picked.id;
  const manual = picked.manual && selectedId === picked.id && liveIds.length > 0;
  useEffect(() => {
    if (selectedId !== picked.id || manual !== picked.manual) setPicked({ id: selectedId, manual });
  }, [selectedId, manual, picked]);
  const pick = (id: string) => setPicked({ id, manual: true });

  const run = useBoard(selectRun(selectedId));
  const events = useBoard(selectEvents(selectedId));
  const now = useBoard((s) => (selectedId ? s.now[selectedId] : undefined));
  const liveRuns = useBoard((s) => liveIds.map((id) => s.runs[id]).filter((r): r is Run => !!r));
  const lastRun = useBoard((s) => {
    for (const id of s.order) {
      const r = s.runs[id];
      if (r && !isLiveRun(r)) return r;
    }
    return undefined;
  });

  // 選中改變：確保事件已載入（store 內已載過的會直接略過），並回報整合者
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  });
  useEffect(() => {
    onSelectRef.current?.(selectedId);
    if (selectedId) loadRunEvents(selectedId).catch(() => {
      /* 事件抓不到不擋票券：回合／費用仍來自 Run 列 */
    });
  }, [selectedId]);

  const mode: 0 | 1 | 3 = !run ? 0 : liveIds.length >= 2 ? 3 : 1;
  const cls = ["hero", "wrap", mode === 3 ? "m3" : "", className ?? ""].filter(Boolean).join(" ");

  return (
    <section className={cls} aria-label="執行中">
      {mode === 0 ? <IdleTicket lastRun={lastRun} /> : null}
      {mode === 1 && run ? <Ticket run={run} events={events} now={now} /> : null}
      {mode === 3 ? <TicketStack runs={liveRuns} selectedId={selectedId} onSelect={pick} /> : null}
      <div className="hero-r">
        {mode === 3 && run ? <Focus run={run} events={events} now={now} compact /> : null}
        {renderFeed?.(mode === 0 ? null : selectedId)}
        {children}
      </div>
    </section>
  );
}
