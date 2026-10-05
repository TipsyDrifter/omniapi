import { useEffect, useRef, useState, type ReactNode } from "react";
import type { EventRow, Run } from "@/api/types";
import type { NowLine } from "@/store/board";
import EventBars from "@/components/feed/EventBars";
import CancelButton from "@/components/thread/CancelButton";
import { dt, dur, harnessClass, harnessName, isLive, splitTitle, stateLabel, tween, usd } from "@/lib/format";

/* 執行中票券（決策記錄 D24 護欄二；THEME.md §5.1／§5.5）。
   從 d3-final/index.html 的 #ticket 移植，但吃真資料：回合／費用直接取 Run.turns／Run.cost_usd（store 收到 WS 事件就更新），
   不再內插；旁標「即時」（執行中）→「真值」（結束後）。
   這支檔同時匯出票券的零件，TicketStack／Focus 共用。 */

export interface LiveRunProps {
  run: Run;
  events: EventRow[];
  now?: NowLine;
  /** 多張模式右側細節帶的排法（Model／開始在前，harness 已在標題旁） */
  compact?: boolean;
}

/* ---------------- hooks ---------------- */

/** 計數器翻牌：值變了就用 format.tween（420ms／6 格）跳過去；key（run id）換了直接換字、不跳。null 原樣回傳。 */
export function useFlip(value: number | null, key: string): number | null {
  const [shown, setShown] = useState<number | null>(value);
  const last = useRef<{ value: number | null; key: string }>({ value, key });
  const shownRef = useRef<number | null>(value);
  useEffect(() => {
    const prev = last.current;
    last.current = { value, key };
    const apply = (v: number | null) => {
      shownRef.current = v;
      setShown(v);
    };
    if (prev.key !== key || value == null || prev.value == null) {
      apply(value);
      return;
    }
    if (prev.value === value) return;
    // 從畫面上目前那一格起跳（上一段翻牌可能還沒跳完）
    return tween(shownRef.current ?? prev.value, value, apply);
  }, [value, key]);
  // 換 run 的那一次 render，effect 還沒跑：直接給新值，不閃舊票的數字
  return last.current.key === key ? shown : value;
}

/** 耗時（秒）：執行中每秒更新（started_at → 現在）；結束後用 ended_at。沒有 ended_at 的結束 run 回 null。 */
export function useElapsed(run: Run): number | null {
  const live = isLive(run);
  const [nowS, setNowS] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    setNowS(Date.now() / 1000);
    const t = window.setInterval(() => setNowS(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [live]);
  if (live) return Math.max(0, nowS - run.started_at);
  return run.ended_at != null ? Math.max(0, run.ended_at - run.started_at) : null;
}

/* ---------------- 零件 ---------------- */

/** 狀態章（§5.5）：執行中＝紙底實框；完成／取消＝空心；錯誤／失聯＝墨底反白 */
export function stampOf(run: Run): { cls: string; text: string } {
  if (isLive(run)) return { cls: "", text: run.state === "starting" ? "啟動中" : "執行中" };
  switch (run.state) {
    case "done":
      return { cls: "done", text: "已完成" };
    case "cancelled":
      return { cls: "done", text: "已取消" };
    case "error":
      return { cls: "error", text: "錯誤" };
    case "dead":
      return { cls: "error", text: "失聯" };
    default:
      return { cls: "done", text: stateLabel(run.state) };
  }
}

export function StateStamp({ run }: { run: Run }) {
  const s = stampOf(run);
  return <span className={s.cls ? `live-stamp ${s.cls}` : "live-stamp"}>{s.text}</span>;
}

/** 滾筒：執行中轉、結束停轉 */
export function Drum({ live }: { live: boolean }) {
  return <span className={live ? "drum" : "drum stop"} aria-hidden="true" />;
}

export function RunId({ id }: { id: string }) {
  return (
    <span className="rid">
      <span className="lbl">Run</span> {id}
    </span>
  );
}

/** 標題：開頭的 ↩ 轉成「續」標記（多次寫「續×N」） */
export function RunTitle({ title }: { title: string | null }) {
  const st = splitTitle(title);
  return (
    <>
      {st.n ? <span className="resu">續{st.n > 1 ? `×${st.n}` : ""}</span> : null}
      {st.t || "（未命名）"}
    </>
  );
}

/** meta 表：大票券 Harness／Model／派工者／Cwd／開始；compact（細節帶）Model／開始／派工者／Cwd */
export function MetaTable({ run, compact }: { run: Run; compact?: boolean }) {
  const start = `${dt(run.started_at)}　Asia/Taipei`;
  const rows: [string, string, string?][] = compact
    ? [
        ["Model", run.model ?? "—", "id"],
        ["開始", start, "num"],
        ["派工者", run.dispatcher || "—"],
        ["Cwd", run.cwd ?? "—", "cwd"],
      ]
    : [
        ["Harness", harnessName(run.harness)],
        ["Model", run.model ?? "—", "id"],
        ["派工者", run.dispatcher || "—"],
        ["Cwd", run.cwd ?? "—", "cwd"],
        ["開始", start, "num"],
      ];
  return (
    <dl className="meta">
      {rows.map(([k, v, c]) => (
        <MetaRow key={k} k={k} v={v} c={c} />
      ))}
    </dl>
  );
}
/** 手機（S 段）的票券頁首收起的列：派工者、開始（活動流的 SESSION_START 有） */
const HIDE_S = new Set(["派工者", "開始"]);
function MetaRow({ k, v, c }: { k: string; v: string; c?: string }) {
  const s = HIDE_S.has(k);
  return (
    <>
      <dt className={s ? "x-s" : undefined}>{k}</dt>
      <dd className={[c, s ? "x-s" : ""].filter(Boolean).join(" ") || undefined}>{v}</dd>
    </>
  );
}

const NA = <span className="na">未回報</span>;
const fmtCost = (v: number) => (
  <>
    <small>$</small>
    {v.toFixed(4)}
  </>
);

/** 回合／費用的旁標：執行中＝即時；結束＝真值 */
export const turnTag = (live: boolean) => (live ? "即時" : "真值");
export const costTag = (run: Run, live: boolean) => (live ? "即時 · USD" : run.cost_usd == null ? "USD" : "真值 · USD");

/** 回合＋費用大數字（翻牌；cost null → 未回報，不當 0） */
export function Counters({ run }: { run: Run }) {
  const live = isLive(run);
  const turns = useFlip(run.turns ?? 0, run.id) ?? 0;
  const cost = useFlip(run.cost_usd, run.id);
  return (
    <div className="counters">
      <div className="ctr">
        <label>
          回合 <i>{turnTag(live)}</i>
        </label>
        <div className="big">{Math.round(turns)}</div>
        <div className="sub">
          {live ? (
            "依 tool_call 累計"
          ) : (
            <>
              終值 <span className="n">{run.turns}</span>
            </>
          )}
        </div>
      </div>
      <div className="ctr">
        <label>
          費用 <i>{costTag(run, live)}</i>
        </label>
        <div className="big">{cost == null ? NA : fmtCost(cost)}</div>
        <div className="sub">
          {run.cost_usd == null ? (
            live ? (
              "尚未回報"
            ) : (
              "harness 未回報"
            )
          ) : live ? (
            "harness 最新回報"
          ) : (
            <>
              終值 <span className="n">{usd(run.cost_usd, 6)}</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** NOW 行：執行中跑最新一則摘要；結束時「收工。」（錯誤／失聯附反白章與錯誤訊息） */
export function NowBlock({ run, now }: { run: Run; now?: NowLine }) {
  const live = isLive(run);
  let body: ReactNode;
  if (live) {
    body = now ? <NowText now={now} /> : "等待 session_start…";
  } else if (run.state === "error" || run.state === "dead") {
    body = (
      <>
        收工。<mark>{stampOf(run).text}</mark>
        {run.error ? <code>{run.error}</code> : null}
      </>
    );
  } else if (run.state === "cancelled") {
    body = "收工。（已取消）";
  } else {
    body = "收工。";
  }
  return (
    <div className="now">
      <span className="k">{live ? "NOW · 正在做" : "END · 已結束"}</span>
      <div className="v">{body}</div>
    </div>
  );
}

function NowText({ now }: { now: NowLine }) {
  let text: ReactNode = now.text;
  if (now.error) {
    // tool_result 錯誤：「Bash 回應 [出錯了]」；其他錯誤整句反白
    text =
      now.type === "tool_result" ? (
        <>
          {now.text} <mark>出錯了</mark>
        </>
      ) : (
        <mark>{now.text}</mark>
      );
  }
  return (
    <>
      {text}
      {now.code ? <code>{now.code}</code> : null}
    </>
  );
}

/** 事件條下方：事件 N 則／耗時 */
export function BarsLegend({ run, events }: { run: Run; events: EventRow[] }) {
  const s = useElapsed(run);
  return (
    <div className="bars-leg">
      <span>
        事件 <span className="n">{events.length}</span> 則
      </span>
      <span>
        耗時 <span className="n">{s == null ? "—" : dur(s)}</span>
      </span>
    </div>
  );
}

/* ---------------- 大票券 ---------------- */

export default function Ticket({ run, events, now, compact }: LiveRunProps) {
  const live = isLive(run);
  const resumeFrom = typeof run.meta?.resume_run_id === "string" ? run.meta.resume_run_id : null;
  return (
    <article className={`ticket ${harnessClass(run.harness)}`} aria-label={`${stampOf(run).text}的 run`}>
      <div className="row">
        <StateStamp run={run} />
        <Drum live={live} />
        <RunId id={run.id} />
        {live ? <CancelButton runId={run.id} /> : null}
      </div>
      {resumeFrom ? (
        <div className="replay-note">
          ▲ 續接自 <span className="code">{resumeFrom}</span>
        </div>
      ) : null}
      <h1>
        <RunTitle title={run.title} />
      </h1>
      <MetaTable run={run} compact={compact} />
      <div className="perf" aria-hidden="true" />
      <Counters run={run} />
      <NowBlock run={run} now={now} />
      <EventBars events={events} live={live} />
      <BarsLegend run={run} events={events} />
    </article>
  );
}
