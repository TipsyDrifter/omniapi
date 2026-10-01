import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { EventRow, Run, Thread } from "@/api/types";
import { FEED_CHIPS, FeedList, NO_PRINT_OVER, STICK_PX, feedCounts, type FeedFilter } from "@/components/feed";
import { StateStamp } from "@/components/live";
import { dt, harnessClass, usd } from "@/lib/format";
import { isLiveRun, loadRunEvents, useBoard } from "@/store/board";
import CancelButton from "./CancelButton";
import Composer from "./Composer";
import { mergeRun } from "./useThread";

/* 追問串（決策記錄 M5-g）：整條串共用一個捲動區與一組篩選 chips；
   每段＝一筆 run：分段表頭 → prompt → 該段事件流。最底下是追問輸入框。 */

export interface ThreadViewProps {
  thread: Thread;
  /** 網址指到的那筆（標出來、載入後捲過去） */
  targetId: string;
  onStarted: (runId: string) => void | Promise<void>;
}

const NO_EVENTS: EventRow[] = [];

export default function ThreadView({ thread, targetId, onStarted }: ThreadViewProps) {
  const ids = useMemo(() => thread.runs.map((r) => r.id), [thread]);
  const storeRuns = useBoard((s) => ids.map((id) => s.runs[id]));
  const eventLists = useBoard((s) => ids.map((id) => s.events[id]));
  const loadedFlags = useBoard((s) => ids.map((id) => !!s.loaded[id]));
  const runs = useMemo(() => thread.runs.map((r, i) => mergeRun(r, storeRuns[i])), [thread, storeRuns]);
  const leaf = runs[runs.length - 1] as Run | undefined;
  const leafLive = isLiveRun(leaf);

  // 各段事件：這一頁第一次看到的 id 強制重載一次（同舊單筆頁），之後靠 WS 增量
  const [loadErr, setLoadErr] = useState<Record<string, string>>({});
  const forced = useRef(new Set<string>());
  useEffect(() => {
    for (const id of ids) {
      if (forced.current.has(id)) continue;
      forced.current.add(id);
      loadRunEvents(id, true).catch((e: unknown) => setLoadErr((m) => ({ ...m, [id]: e instanceof Error ? e.message : String(e) })));
    }
  }, [ids]);

  const [filter, setFilter] = useState<FeedFilter>("all");
  const allEvents = useMemo(() => eventLists.flatMap((l) => l ?? NO_EVENTS), [eventLists]);
  const counts = useMemo(() => feedCounts(allEvents), [allEvents]);

  /* ---------- 捲動 ---------- */
  const feedRef = useRef<HTMLDivElement>(null);
  const segRefs = useRef<Record<string, HTMLElement | null>>({});
  const stick = useRef(false);
  const scrolledFor = useRef<string | null>(null);
  const toBottom = () => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  // 網址指到的那段：它和它前面的段都載完才定位（前面的段長高會把它往下推）
  const ti = ids.indexOf(targetId);
  const readyToAim = ti >= 0 && loadedFlags.slice(0, ti + 1).every(Boolean);
  useLayoutEffect(() => {
    if (!readyToAim || scrolledFor.current === targetId) return;
    scrolledFor.current = targetId;
    const el = feedRef.current;
    if (!el) return;
    if (targetId === thread.leaf_id && leafLive) {
      // 正在跑的最後一段：貼底跟著跑
      stick.current = true;
      toBottom();
    } else {
      const seg = segRefs.current[targetId];
      stick.current = false;
      el.scrollTop = seg && ti > 0 ? seg.offsetTop : 0;
    }
  }, [readyToAim, targetId, thread.leaf_id, leafLive, ti]);

  // 新事件／新段進來：原本貼底才跟著捲
  const leafEvents = eventLists[eventLists.length - 1] ?? NO_EVENTS;
  const lastId = leafEvents.length ? leafEvents[leafEvents.length - 1].id : 0;
  useLayoutEffect(() => {
    if (stick.current) toBottom();
  }, [lastId, allEvents.length, ids.length]);

  // 篩選切換一律捲到底（同 Feed）
  const firstFilter = useRef(true);
  useLayoutEffect(() => {
    if (firstFilter.current) {
      firstFilter.current = false;
      return;
    }
    toBottom();
    stick.current = true;
  }, [filter]);

  const onScroll = () => {
    const el = feedRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
  };

  // 送出追問：新段接在下面、捲到底
  const started = async (runId: string) => {
    stick.current = true;
    await onStarted(runId);
  };

  return (
    <div className={`feedbox thread ${harnessClass(leaf?.harness)}`}>
      <div className="feedhead">
        <h2>
          <span className="ovp" data-t="Live">
            Live
          </span>
          <em>{runs.length > 1 ? "追問串" : "活動流"}</em>
        </h2>
        <span className={`onair${leafLive ? "" : " off"}`}>{leafLive ? "ON AIR" : "END"}</span>
        <div className="chips" role="group" aria-label="篩選">
          {FEED_CHIPS.map((c) => (
            <button key={c.f} type="button" aria-pressed={filter === c.f} onClick={() => setFilter(c.f)}>
              {c.label}
              {c.f !== "all" && <i>{counts[c.f]}</i>}
            </button>
          ))}
        </div>
        <span className="tag">
          {runs.length > 1 ? (
            <>
              <span className="n">{runs.length}</span> 段 ·{" "}
            </>
          ) : null}
          <span className="n">{allEvents.length}</span> 則
        </span>
      </div>
      <div ref={feedRef} className={`feed thread-feed${allEvents.length > NO_PRINT_OVER ? " no-print" : ""}`} aria-live="polite" onScroll={onScroll}>
        {runs.map((r, i) => (
          <Segment
            key={r.id}
            run={r}
            index={i}
            isLeaf={i === runs.length - 1}
            isTarget={r.id === targetId}
            multi={runs.length > 1}
            events={eventLists[i] ?? NO_EVENTS}
            loaded={loadedFlags[i]}
            loadErr={loadErr[r.id]}
            filter={filter}
            segRef={(el) => {
              segRefs.current[r.id] = el;
            }}
          />
        ))}
      </div>
      <Composer thread={thread} leaf={leaf} leafLive={leafLive} onStarted={started} />
    </div>
  );
}

/* ---------------- 一段 ---------------- */

interface SegmentProps {
  run: Run;
  index: number;
  isLeaf: boolean;
  isTarget: boolean;
  multi: boolean;
  events: EventRow[];
  loaded: boolean;
  loadErr?: string;
  filter: FeedFilter;
  segRef: (el: HTMLElement | null) => void;
}

function Segment({ run, index, isLeaf, isTarget, multi, events, loaded, loadErr, filter, segRef }: SegmentProps) {
  const live = isLiveRun(run);
  const resumed = index > 0;
  return (
    <section ref={segRef} className={`seg ${harnessClass(run.harness)}${isTarget && multi ? " target" : ""}`} aria-current={isTarget && multi ? "true" : undefined} data-run={run.id}>
      <header className="seg-head">
        <span className="seg-no">
          {resumed ? <span className="resu">續</span> : null}第 <span className="n">{index + 1}</span> 段
        </span>
        <StateStamp run={run} />
        <span className="seg-id">{run.id}</span>
        <span className="seg-meta">
          <span className="k">開始</span> <span className="n">{dt(run.started_at)}</span>
        </span>
        <span className="seg-meta">
          <span className="k">回合</span> <span className="n">{run.turns ?? 0}</span>
        </span>
        <span className="seg-meta">
          <span className="k">費用</span> {run.cost_usd == null ? <span className="na">未回報</span> : <span className="n">{usd(run.cost_usd, 4)}</span>}
        </span>
        {isLeaf && live ? <CancelButton runId={run.id} className="seg-cancel" /> : null}
      </header>
      <SegPrompt prompt={run.prompt} resumed={resumed} />
      <div className="seg-body">
        {loadErr ? (
          <div className="warn">
            <b>事件載入失敗</b>：{loadErr}
          </div>
        ) : !loaded ? (
          <div className="feed-empty">載入中…</div>
        ) : events.length === 0 ? (
          <div className="feed-empty">{live ? "等待 session_start…" : "這一段沒有事件紀錄"}</div>
        ) : (
          <FeedList events={events} run={run} filter={filter} />
        )}
      </div>
    </section>
  );
}

/** 該段的 prompt（楷 400 16/1.6）；超過約 6 行先收起來 */
function SegPrompt({ prompt, resumed }: { prompt: string | null; resumed: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [overflow, setOverflow] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && !open) setOverflow(el.scrollHeight > el.clientHeight + 2);
  }, [prompt, open]);
  if (!prompt) return null;
  return (
    <div className={`seg-prompt${open ? " open" : ""}`}>
      <h4>{resumed ? "追問" : "Prompt"}</h4>
      <div ref={ref} className="prompt">
        {prompt}
      </div>
      {overflow || open ? (
        <button type="button" className="seg-more" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? "收合 ▴" : "展開全文 ▾"}
        </button>
      ) : null}
    </div>
  );
}
