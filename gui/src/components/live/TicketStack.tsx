import { useState, type UIEvent } from "react";
import type { Run } from "@/api/types";
import { harnessClass, harnessName, isLive, splitTitle } from "@/lib/format";
import { Drum, RunId, RunTitle, StateStamp, costTag, turnTag, useFlip } from "./Ticket";

/* 多個執行中：左欄堆疊小票券（THEME.md §5.1 小票券；原型 #stack）。
   點一張 → onSelect(id)；選中那張 aria-pressed＝true（位移＋墨影＋右緣三角，樣式在 board.css .tk）。 */

export interface TicketStackProps {
  runs: Run[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export default function TicketStack({ runs, selectedId, onSelect }: TicketStackProps) {
  // 手機（S 段）票券改橫向滑（rwd.css）：下面兩個以上的圓點標出現在看到第幾張
  const [seen, setSeen] = useState(0);
  const onScroll = (e: UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const first = el.firstElementChild as HTMLElement | null;
    if (!first || el.scrollWidth <= el.clientWidth) return;
    setSeen(Math.round(el.scrollLeft / (first.offsetWidth + 12)));
  };
  return (
    <div className="stack" aria-label="執行中 runs">
      <div className="stack-h">
        <b>執行中</b>
        <span>{runs.length} RUNS</span>
        <span>
          <span className="x-s">點票券切換 →</span>
          <span className="only-s">左右滑看下一件</span>
        </span>
      </div>
      <div className="stack-list" onScroll={onScroll}>
        {runs.map((r) => (
          <StackTicket key={r.id} run={r} selected={r.id === selectedId} onSelect={onSelect} />
        ))}
      </div>
      <div className="stack-dots" aria-hidden="true">
        {runs.map((r, i) => (
          <i key={r.id} className={i === Math.min(seen, runs.length - 1) ? "on" : undefined} />
        ))}
      </div>
    </div>
  );
}

function StackTicket({ run, selected, onSelect }: { run: Run; selected: boolean; onSelect: (id: string) => void }) {
  const live = isLive(run);
  const turns = useFlip(run.turns ?? 0, run.id) ?? 0;
  const cost = useFlip(run.cost_usd, run.id);
  const st = splitTitle(run.title);
  return (
    <button type="button" className={`tk ${harnessClass(run.harness)}`} aria-pressed={selected} onClick={() => onSelect(run.id)}>
      <div className="row">
        <StateStamp run={run} />
        <Drum live={live} />
        <RunId id={run.id} />
      </div>
      <div className="tk-title" title={st.t || undefined}>
        <RunTitle title={run.title} />
      </div>
      <div className="tk-meta">
        <span>{harnessName(run.harness)}</span>
        <span className="m">{run.model ?? "—"}</span>
      </div>
      <div className="perf" aria-hidden="true" />
      <div className="tk-nums">
        <div>
          <label>
            回合 <i>{turnTag(live)}</i>
          </label>
          <b>{Math.round(turns)}</b>
        </div>
        <div>
          <label>
            費用 <i>{run.cost_usd == null && !live ? "harness 未回報" : costTag(run, live)}</i>
          </label>
          <b>
            {cost == null ? (
              <span className="na">未回報</span>
            ) : (
              <>
                <small>$</small>
                {cost.toFixed(4)}
              </>
            )}
          </b>
        </div>
      </div>
    </button>
  );
}
