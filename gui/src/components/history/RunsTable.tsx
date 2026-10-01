import { Fragment, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";
import type { Run } from "@/api/types";
import { dt, harnessClass, harnessName, isLive, splitTitle, stateLabel, usd } from "@/lib/format";
import { selectHistory, useBoard } from "@/store/board";
import RunDetailRow from "./RunDetailRow";

/* 歷史派工表（THEME.md §5.3）：.lower 的左半。
   篩選記在 URL query（?state=&harness=&q=），可以直接分享；分頁每次多 50 列。
   資料＝store 已載入的 runs（bootBoard 載最近 200 筆，之後靠 WS 增量）。 */

export const PAGE = 50;

type StateFilter = "all" | "running" | "done" | "error" | "other";
type HarnessFilter = "all" | "claude" | "codex" | "gemini";

const STATE_OPTS: { v: StateFilter; label: string }[] = [
  { v: "all", label: "全部" },
  { v: "running", label: "執行中" },
  { v: "done", label: "完成" },
  { v: "error", label: "錯誤" },
  { v: "other", label: "其他" },
];
const HARNESS_OPTS: { v: HarnessFilter; label: string }[] = [
  { v: "all", label: "全部" },
  { v: "claude", label: "Claude Code" },
  { v: "codex", label: "Codex" },
  { v: "gemini", label: "Gemini CLI" },
];

const pick = <T extends string>(v: string | null, opts: { v: T }[]): T => (opts.find((o) => o.v === v)?.v ?? opts[0].v);

function matchState(r: Run, f: StateFilter): boolean {
  switch (f) {
    case "all":
      return true;
    case "running":
      return isLive(r);
    case "done":
    case "error":
      return !isLive(r) && r.state === f;
    case "other":
      return !isLive(r) && r.state !== "done" && r.state !== "error";
  }
}

function matchQuery(r: Run, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return [r.title, r.model, r.dispatcher, r.id].some((s) => !!s && s.toLowerCase().includes(needle));
}

export default function RunsTable() {
  const all = useBoard(selectHistory);
  const ready = useBoard((s) => s.ready);
  const [params, setParams] = useSearchParams();
  const state = pick(params.get("state"), STATE_OPTS);
  const harness = pick(params.get("harness"), HARNESS_OPTS);
  const q = params.get("q") ?? "";

  const [limit, setLimit] = useState(PAGE);
  const [openId, setOpenId] = useState<string | null>(null);

  const rows = useMemo(
    () => all.filter((r) => r && matchState(r, state) && (harness === "all" || r.harness === harness) && matchQuery(r, q.trim())),
    [all, state, harness, q],
  );

  // 換篩選條件就回到第一頁
  useEffect(() => setLimit(PAGE), [state, harness, q]);

  const setParam = (k: string, v: string, dflt: string) => {
    // 注意：react-router 的 setSearchParams（含函式形式）都以本次 render 的 params 為底，同一個事件裡連呼兩次後者會蓋掉前者；這裡每次只改一個鍵
    const next = new URLSearchParams(params);
    if (!v || v === dflt) next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: true });
  };

  const toggle = (id: string) => setOpenId((cur) => (cur === id ? null : id));
  const onKey = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle(id);
    }
  };

  const shown = rows.slice(0, limit);
  const starts = rows.map((r) => r.started_at);
  const filtered = state !== "all" || harness !== "all" || q.trim() !== "";

  return (
    <div>
      <div className="sechead">
        <h2>
          <span className="ovp" data-t="Runs">
            Runs
          </span>
        </h2>
        <span className="zh">歷史派工</span>
        <span className="aside">
          <span className="n">{rows.length}</span> 筆
          {filtered && (
            <>
              （共 <span className="n">{all.length}</span>）
            </>
          )}
          {rows.length > 0 && (
            <>
              {" "}
              · <span className="n">{dt(Math.min(...starts))}</span> → <span className="n">{dt(Math.max(...starts))}</span>
            </>
          )}{" "}
          · 點一列看詳情
        </span>
      </div>

      <div className="filters">
        <div className="sim" role="group" aria-label="依狀態篩選">
          <span className="k">狀態</span>
          {STATE_OPTS.map((o) => (
            <button key={o.v} type="button" aria-pressed={state === o.v} onClick={() => setParam("state", o.v, "all")}>
              {o.label}
            </button>
          ))}
        </div>
        <div className="sim" role="group" aria-label="依 harness 篩選">
          <span className="k">HARNESS</span>
          {HARNESS_OPTS.map((o) => (
            <button key={o.v} type="button" aria-pressed={harness === o.v} onClick={() => setParam("harness", o.v, "all")}>
              {o.label}
            </button>
          ))}
        </div>
        <input
          type="search"
          value={q}
          placeholder="搜尋標題／模型／派工者／id"
          aria-label="搜尋標題、模型、派工者或 run id"
          onChange={(e) => setParam("q", e.target.value, "")}
        />
      </div>

      <table className="runs">
        <thead>
          <tr>
            <th>狀態</th>
            <th>開始</th>
            <th>Harness</th>
            <th>模型</th>
            <th>標題</th>
            <th>派工者</th>
            <th className="r">回合</th>
            <th className="r">費用</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r) => {
            const live = isLive(r);
            const st = splitTitle(r.title);
            const sel = openId === r.id;
            const stClass = live && r.state !== "running" && r.state !== "starting" ? "running" : r.state;
            return (
              <Fragment key={r.id}>
                <tr
                  className={sel ? "run sel" : "run"}
                  tabIndex={0}
                  aria-expanded={sel}
                  onClick={() => toggle(r.id)}
                  onKeyDown={(e) => onKey(e, r.id)}
                >
                  <td>
                    <span className={live ? `st ${stClass} ${harnessClass(r.harness)}` : `st ${stClass}`}>{stateLabel(stClass)}</span>
                  </td>
                  <td className="t">{dt(r.started_at)}</td>
                  <td>
                    <span className={`hm ${harnessClass(r.harness)}`}>{harnessName(r.harness)}</span>
                  </td>
                  <td className="model">{r.model ?? <span className="nil">—</span>}</td>
                  <td className="title">
                    {st.n > 0 && <span className="resu">續{st.n > 1 ? `×${st.n}` : ""}</span>}
                    {st.t || <span className="nil">（無標題）</span>}
                  </td>
                  <td className="disp" title={r.dispatcher ?? ""}>
                    {r.dispatcher ?? <span className="nil">—</span>}
                  </td>
                  <td className="num r">{r.turns ?? 0}</td>
                  <td className="num r">{r.cost_usd != null ? usd(r.cost_usd) : <span className="nil">{live ? "—" : "未回報"}</span>}</td>
                </tr>
                {sel && <RunDetailRow run={r} />}
              </Fragment>
            );
          })}
          {shown.length === 0 && (
            <tr className="empty">
              <td colSpan={8} className="nil">
                {!ready ? "載入中…" : filtered ? "沒有符合篩選條件的 run。" : "還沒有任何 run。"}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {rows.length > limit && (
        <div className="pager">
          <button type="button" onClick={() => setLimit((n) => n + PAGE)}>
            再看 {Math.min(PAGE, rows.length - limit)} 筆（還有 {rows.length - limit} 筆）
          </button>
        </div>
      )}
    </div>
  );
}
