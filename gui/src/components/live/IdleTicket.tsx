import { Link } from "react-router-dom";
import type { Run } from "@/api/types";
import { dt, dur, stateLabel } from "@/lib/format";
import { RunTitle } from "./Ticket";

/* 沒有執行中的 run：左欄空票位（.idle 墨虛線框，THEME.md §5.5「樣本／示意」同族）。
   提示兩種派工入口，底下列最近一筆結束的 run 摘要（整合者傳 lastRun）。 */
export default function IdleTicket({ lastRun }: { lastRun?: Run }) {
  return (
    <div className="idle" aria-label="沒有執行中的 run">
      <h1>現在沒有執行中的 run</h1>
      <p className="m-0">
        從 Claude Code 呼叫 <span className="code">run_agent</span>（MCP），或在終端機下{" "}
        <span className="code">omni run "…" --model cheap</span>（CLI）派工，票券會出現在這裡。
      </p>
      {lastRun ? (
        <dl className="meta">
          <dt>最近一筆</dt>
          <dd className="f-kai">
            <Link to={`/runs/${encodeURIComponent(lastRun.id)}`}>
              <RunTitle title={lastRun.title} />
            </Link>
          </dd>
          <dt>狀態</dt>
          <dd>
            <span className={`st ${lastRun.state}`}>{stateLabel(lastRun.state)}</span>
          </dd>
          <dt>時間</dt>
          <dd className="num">
            {dt(lastRun.started_at)}
            {lastRun.ended_at != null ? ` · 耗時 ${dur(lastRun.ended_at - lastRun.started_at)}` : ""}
          </dd>
        </dl>
      ) : null}
    </div>
  );
}
