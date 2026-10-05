import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { EventRow, Run } from "@/api/types";
import { harnessClass } from "@/lib/format";
import { useTier } from "@/lib/rwd";
import EventItem from "./EventItem";

/* 即時活動流（THEME.md §5.2）：表頭（Live／ON AIR／篩選 chips）＋捲動區。
   純 props 驅動，不讀 store；事件列需依 id 遞增。 */

export type FeedFilter = "all" | "text" | "tool" | "thinking";

export interface FeedProps {
  events: EventRow[];
  run: Run | null;
  /** 執行中 → ON AIR 閃；否則 END */
  live: boolean;
  /** 「Live」旁的中文副標，預設「即時活動流」 */
  title?: string;
  /** 沒有事件時的字，預設「還沒有事件」 */
  emptyText?: string;
  /** 表頭最右側的自訂內容（例如樣本章、連結） */
  headRight?: ReactNode;
  /** 手機（S 段）只放最近幾則，底下接「看完整活動流與追問 →」（1.3-M3，D48 第 5 題：看板最多 20 則） */
  phoneLimit?: number;
}

/** 超過這個數量關掉進場動畫（歷史還原不要每則都印一次） */
export const NO_PRINT_OVER = 300;
/** 只渲染最後 N 則 */
const MAX_RENDER = 400;
/** 距底小於此值視為「貼在底部」 */
export const STICK_PX = 80;

const CHIPS: { f: FeedFilter; label: string }[] = [
  { f: "all", label: "全部" },
  { f: "text", label: "說" },
  { f: "tool", label: "工具" },
  { f: "thinking", label: "想" },
];

const passes = (f: FeedFilter, type: string): boolean =>
  f === "all" ? true : f === "tool" ? type === "tool_call" || type === "tool_result" : type === f;

/** 把連續的 status 事件收成一組（長度 ≥2 才算一組；其他事件各自一組） */
function groupStatus(list: EventRow[]): EventRow[][] {
  const out: EventRow[][] = [];
  for (const e of list) {
    const last = out[out.length - 1];
    if (e.type === "status" && last && last[0].type === "status") last.push(e);
    else out.push([e]);
  }
  return out;
}

/** 一組連續 status：一列摘要（各 message 的次數），點開逐則展開 */
function StatusGroup({ events, seq, run, callSeq, hidden }: { events: EventRow[]; seq: number; run: Run | null; callSeq: Record<string, number>; hidden: boolean }) {
  const [open, setOpen] = useState(false);
  const counts = new Map<string, number>();
  for (const e of events) {
    const k = String(e.payload?.message ?? "（無訊息）");
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  const summary = [...counts.entries()].map(([k, n]) => `${k} ×${n}`).join(" · ");
  if (open) {
    return (
      <>
        {events.map((e, i) => (
          <EventItem key={e.id} event={e} seq={seq + i} callSeq={e.payload?.id ? callSeq[e.payload.id] : undefined} run={run} hidden={hidden} />
        ))}
        <div className="ev t-status" hidden={hidden}>
          <div className="seq" />
          <div />
          <div className="body">
            <button type="button" className="tag" onClick={() => setOpen(false)}>
              收合這 {events.length} 則 status ▴
            </button>
          </div>
        </div>
      </>
    );
  }
  return (
    <div className="ev t-status" hidden={hidden} role="button" tabIndex={0} onClick={() => setOpen(true)} onKeyDown={(k) => (k.key === "Enter" || k.key === " ") && setOpen(true)}>
      <div className="seq">#{String(seq).padStart(3, "0")}</div>
      <div className="gl t-status">況</div>
      <div className="body">
        <div className="tag">
          status ×<span className="n">{events.length}</span>
          <span className="tog" style={{ marginLeft: 8 }}>
            展開 ▾
          </span>
        </div>
        <p>{summary}</p>
      </div>
    </div>
  );
}

/** 篩選 chips 的計數（全部／說／工具／想） */
export function feedCounts(events: EventRow[]): Record<FeedFilter, number> {
  const counts = { all: events.length, text: 0, tool: 0, thinking: 0 };
  for (const e of events) {
    if (e.type === "text") counts.text++;
    else if (e.type === "thinking") counts.thinking++;
    else if (e.type === "tool_call" || e.type === "tool_result") counts.tool++;
  }
  return counts;
}

export const FEED_CHIPS = CHIPS;

export interface FeedListProps {
  events: EventRow[];
  run: Run | null;
  filter: FeedFilter;
}

/** 只有事件列本身（不含表頭與捲動框）：Feed 內部用，追問串也拿來當每一段的內容 */
export function FeedList({ events, run, filter }: FeedListProps) {
  // tool_call id → 序號（1 起，對全列）
  const callSeq = useMemo(() => {
    const m: Record<string, number> = {};
    events.forEach((e, i) => {
      if (e.type === "tool_call" && e.payload?.id) m[e.payload.id] = i + 1;
    });
    return m;
  }, [events]);
  const skipped = Math.max(0, events.length - MAX_RENDER);
  const shown = skipped ? events.slice(skipped) : events;
  return (
    <>
      {skipped > 0 && (
        <div className="feed-empty">
          前 <span className="n">{skipped}</span> 則已略，全文見{" "}
          {run ? <Link to={`/runs/${run.id}`} className="code">{`/runs/${run.id}`}</Link> : <span className="code">/runs/:id</span>}
        </div>
      )}
      {groupStatus(shown).map((g) => {
        if (g.length > 1) {
          // 連續的 status 雜訊（hook_started／hook_response…）收成一列，點開才逐則看
          const first = shown.indexOf(g[0]);
          return <StatusGroup key={g[0].id} events={g} seq={skipped + first + 1} run={run} callSeq={callSeq} hidden={filter !== "all"} />;
        }
        const e = g[0];
        const seq = skipped + shown.indexOf(e) + 1;
        // 結果卡與錯誤不受篩選影響（收尾資訊一律看得到）
        const always = e.type === "result" || e.type === "error";
        return (
          <EventItem
            key={e.id}
            event={e}
            seq={seq}
            callSeq={e.type === "tool_result" && e.payload?.id ? callSeq[e.payload.id] : undefined}
            run={run}
            hidden={!always && !passes(filter, String(e.type))}
          />
        );
      })}
    </>
  );
}

export default function Feed({ events: allEvents, run, live, title = "即時活動流", emptyText = "還沒有事件", headRight, phoneLimit }: FeedProps) {
  const [filter, setFilter] = useState<FeedFilter>("all");
  const tier = useTier();
  const cut = phoneLimit != null && tier === "s" && allEvents.length > phoneLimit;
  const events = useMemo(() => (cut ? allEvents.slice(-phoneLimit) : allEvents), [cut, allEvents, phoneLimit]);
  const feedRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const counts = useMemo(() => feedCounts(allEvents), [allEvents]);
  const lastId = events.length ? events[events.length - 1].id : 0;

  // 換 run 時：執行中的貼底跟著跑；已結束的從頭讀起（歷史還原不該先看到結尾）
  const runId = run?.id ?? null;
  useLayoutEffect(() => {
    stick.current = live;
    const el = feedRef.current;
    if (el && !live) el.scrollTop = 0;
  }, [runId, live]);

  // 新事件進來：原本貼底才跟著捲；篩選切換一律捲到底（同原型）
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lastId, events.length, runId]);
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    stick.current = true;
  }, [filter]);

  const onScroll = () => {
    const el = feedRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
  };

  return (
    <div className={`feedbox ${harnessClass(run?.harness)}`}>
      <div className="feedhead">
        <h2>
          <span className="ovp" data-t="Live">
            Live
          </span>
          <em>{title}</em>
        </h2>
        <span className={`onair${live ? "" : " off"}`}>{live ? "ON AIR" : "END"}</span>
        <div className="chips" role="group" aria-label="篩選">
          {CHIPS.map((c) => (
            <button key={c.f} type="button" aria-pressed={filter === c.f} onClick={() => setFilter(c.f)}>
              {c.label}
              {c.f !== "all" && <i>{counts[c.f]}</i>}
            </button>
          ))}
        </div>
        {headRight}
      </div>
      <div ref={feedRef} className={`feed${events.length > NO_PRINT_OVER ? " no-print" : ""}`} aria-live="polite" onScroll={onScroll}>
        {cut ? (
          <div className="feed-empty feed-cut">
            前面 <span className="n">{allEvents.length - events.length}</span> 則沒有列出
          </div>
        ) : null}
        {events.length === 0 ? <div className="feed-empty">{emptyText}</div> : <FeedList events={events} run={run} filter={filter} />}
      </div>
      {phoneLimit != null && run ? (
        <Link className="feed-more" to={`/runs/${encodeURIComponent(run.id)}`}>
          看完整活動流與追問 →
        </Link>
      ) : null}
    </div>
  );
}
