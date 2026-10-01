import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Run } from "@/api/types";
import EventStrip from "@/components/feed/EventStrip";
import CancelButton from "@/components/thread/CancelButton";
import { dt, dur, harnessClass, isLive, summarizeInput, toolParts } from "@/lib/format";
import { loadRunEvents, selectEvents, useBoard } from "@/store/board";

/* 歷史表的展開詳情（THEME.md §5.3）：tr.detail > .dt（掛 harness class，事件條碼的 tool_call 才有油墨）。
   左：Prompt＋基本資料；右：事件條碼、前五個工具呼叫、動作（整段還原／中止）與錯誤。 */

export interface RunDetailRowProps {
  run: Run;
  /** 表格欄數（colSpan），預設 8 */
  cols?: number;
}

const ERR_MAX = 300;

export default function RunDetailRow({ run, cols = 8 }: RunDetailRowProps) {
  const events = useBoard(selectEvents(run.id));
  const loaded = useBoard((s) => !!s.loaded[run.id]);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoadErr(null);
    loadRunEvents(run.id).catch((e: unknown) => {
      if (alive) setLoadErr(e instanceof Error ? e.message : String(e));
    });
    return () => {
      alive = false;
    };
  }, [run.id]);

  const live = isLive(run);
  const calls = events.filter((e) => e.type === "tool_call").slice(0, 5);
  const endpoint = typeof run.meta?.endpoint === "string" ? run.meta.endpoint : null;
  const elapsed =
    run.ended_at != null
      ? `${dur(run.ended_at - run.started_at)}（started_at → ended_at）`
      : live
        ? `${dur(Date.now() / 1000 - run.started_at)}（進行中）`
        : "—";

  return (
    <tr className="detail">
      <td colSpan={cols}>
        <div className={`dt ${harnessClass(run.harness)}`}>
          <div>
            <h4>Prompt</h4>
            <div className="prompt">{run.prompt || "（無 prompt 欄位）"}</div>
            <dl>
              <dt>RUN ID</dt>
              <dd>{run.id}</dd>
              <dt>CWD</dt>
              <dd>{run.cwd || "—"}</dd>
              <dt>耗時</dt>
              <dd>{elapsed}</dd>
              <dt>結束</dt>
              <dd>{run.ended_at != null ? dt(run.ended_at) : "—"}</dd>
              <dt>SESSION</dt>
              <dd>{run.session_id || "—"}</dd>
              <dt>端點</dt>
              <dd>{endpoint || "—"}</dd>
            </dl>
          </div>
          <div>
            <h4>
              事件條碼 · {loaded ? <span className="n">{events.length}</span> : "—"} 則
            </h4>
            {loadErr ? (
              <div className="warn">
                <b>事件載入失敗</b>：{loadErr}
              </div>
            ) : !loaded ? (
              <div className="mini">載入中…</div>
            ) : (
              <>
                <EventStrip events={events} />
                <div className="mini">
                  前五個工具呼叫
                  {calls.length ? (
                    <ol>
                      {calls.map((e) => (
                        <li key={e.id}>
                          {toolParts(e.payload?.name).n} · {summarizeInput(e.payload?.input)}
                        </li>
                      ))}
                    </ol>
                  ) : (
                    "：沒有工具呼叫。"
                  )}
                </div>
              </>
            )}
            <div className="actions">
              <Link to={`/runs/${encodeURIComponent(run.id)}`}>整段還原 →</Link>
              {live && <CancelButton runId={run.id} />}
            </div>
            {run.error && (
              <div className="warn">
                <b>錯誤</b>：{run.error.length > ERR_MAX ? `${run.error.slice(0, ERR_MAX)}…` : run.error}
              </div>
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}
