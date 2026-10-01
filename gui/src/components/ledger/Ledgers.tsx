import { useMemo, type ReactNode } from "react";
import type { Costs, RunsLedger } from "@/api/types";
import { harnessClass, harnessName, usd } from "@/lib/format";
import { selectHistory, useBoard } from "@/store/board";
import { computeRunsLedger, dayAxis, today } from "./ledger";

/* 兩本帳（THEME.md §5.4）：看板右側 .costs 側欄。
   派工帳＝runs 表 harness 自報 cost_usd；工具呼叫帳＝calls 表 OmniAPI 工具呼叫計費。口徑不同，不可相加。
   油墨只出現在派工帳「依 harness 拆」的長條與標記（合法的面積用法，§6-2）。 */

/* ---------------- 共用零件（CostsDetail 也用） ---------------- */

/** 日格：有資料＝墨實心；沒資料＝點線空格。預設 30 格（board.css 的 .days 是 30 欄），其他天數改寫欄數 */
export function DayStrip({ axis, has }: { axis: string[]; has: Set<string> }) {
  if (!axis.length) return null;
  return (
    <>
      <div className="days" style={axis.length !== 30 ? { gridTemplateColumns: `repeat(${axis.length}, 1fr)` } : undefined}>
        {axis.map((d) => (
          <i key={d} className={has.has(d) ? "has" : undefined} title={has.has(d) ? `${d} 有資料` : `${d} 無資料`} />
        ))}
      </div>
      <div className="days-leg">
        <span className="n">{axis[0].slice(5)}</span>
        <span>
          <span className="n">{axis.length}</span> 天
        </span>
        <span className="n">{axis[axis.length - 1].slice(5)}</span>
      </div>
    </>
  );
}

export interface BrkRow {
  key: string;
  label: string;
  /** harness 代號：有值＝長條與標記用該 harness 油墨 */
  harness?: string;
  /** null＝未回報（長條留空，不當 0） */
  value: number | null;
  n: number;
  /** 另計未回報的筆數（部分回報時顯示） */
  unreported?: number;
}

/** 拆解長條 */
export function Brk({ rows }: { rows: BrkRow[] }) {
  const max = Math.max(0, ...rows.map((r) => r.value ?? 0));
  return (
    <ul className="brk">
      {rows.map((r) => (
        <li key={r.key}>
          <span className={r.harness ? `lb hm ${harnessClass(r.harness)}` : "lb"} title={r.label}>
            {r.label}
          </span>
          <span className="bar">
            {r.value != null && <i className={r.harness ? harnessClass(r.harness) : undefined} style={{ width: `${max ? (r.value / max) * 100 : 0}%` }} />}
          </span>
          <span className="v">
            {r.value == null ? <span className="nil">未回報</span> : usd(r.value)}
            <small>
              <span className="n">{r.n}</span> 筆
            </small>
            {r.value != null && r.unreported ? (
              <small>
                未回報 <span className="n">{r.unreported}</span>
              </small>
            ) : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

const money = (v: number) => (
  <span className="big">
    <small>$</small>
    {v.toFixed(4)}
  </span>
);

/** 派工帳卡片 */
export function RunsLedgerCard({
  ledger,
  axis,
  estimated,
  sampleN,
  children,
}: {
  ledger: RunsLedger;
  axis: string[];
  /** true＝daemon 沒給 costs.runs，前端用已載入的 runs 試算 */
  estimated: boolean;
  /** 試算時用了幾筆 run（store 最多載 200 筆） */
  sampleN?: number;
  children?: ReactNode;
}) {
  const rows: BrkRow[] = ledger.by_harness.map((h) => ({
    key: h.harness,
    label: harnessName(h.harness),
    harness: h.harness,
    value: h.cost,
    n: h.n,
    unreported: h.unreported,
  }));
  const has = new Set(ledger.by_day.map((d) => d.day));
  return (
    <div className="ledger">
      <div className="lh">
        <h3>派工帳</h3>
        <span className="src">runs · cost_usd{estimated ? " · 前端試算" : ""}</span>
      </div>
      <div className="def">
        每派一次工，由執行的 agent 回報的費用
        {estimated ? `（daemon 未提供派工帳，改用已載入的 ${sampleN ?? 0} 筆 run 試算）` : ""}
      </div>
      <div className="total">
        {money(ledger.total.cost)}
        <span className="tn">
          <span className="n">{ledger.total.n}</span> 筆 run
          <br />
          <span className="n">{ledger.total.unreported}</span> 筆未回報費用
        </span>
      </div>
      <DayStrip axis={axis} has={has} />
      {rows.length ? <Brk rows={rows} /> : <div className="def">這段期間沒有 run。</div>}
      {children}
    </div>
  );
}

/** 聊天・生成帳卡片（calls 表；2026-09-30 前叫「工具呼叫帳」） */
export function CallsLedgerCard({ costs, axis, children }: { costs: Costs | null; axis: string[]; children?: ReactNode }) {
  if (!costs) {
    return (
      <div className="ledger">
        <div className="lh">
          <h3>聊天・生成帳</h3>
          <span className="src">costs</span>
        </div>
        <div className="def">費用資料尚未載入（/api/costs 沒有回應）。</div>
      </div>
    );
  }
  const rows: BrkRow[] = costs.by_model
    .map((m) => ({ key: m.model, label: m.model === "?" ? "?（未標模型）" : m.model, value: m.cost, n: m.n }))
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0) || b.n - a.n);
  const has = new Set(costs.by_day.map((d) => d.day));
  const dataDays = axis.filter((d) => has.has(d)).length;
  return (
    <div className="ledger">
      <div className="lh">
        <h3>聊天・生成帳</h3>
        <span className="src">costs · {costs.days} 天</span>
      </div>
      <div className="def">每叫一次模型記一筆：聊天、生圖、語音、音樂、轉錄</div>
      <div className="total">
        {money(costs.total.cost)}
        <span className="tn">
          <span className="n">{costs.total.n}</span> 次呼叫
          <br />
          <span className="n">
            {dataDays} / {axis.length}
          </span>{" "}
          天有資料
        </span>
      </div>
      <DayStrip axis={axis} has={has} />
      {rows.length ? <Brk rows={rows} /> : <div className="def">這段期間沒有工具呼叫。</div>}
      {children}
    </div>
  );
}

/** 派工帳資料：daemon 有給 costs.runs 就用；沒有就拿 store 的 runs 自己算（同一形狀） */
export function useRunsLedger(costs: Costs | null, days: number): { ledger: RunsLedger; estimated: boolean; sampleN: number } {
  const history = useBoard(selectHistory);
  return useMemo(() => {
    if (costs?.runs && costs.days === days) return { ledger: costs.runs, estimated: false, sampleN: 0 };
    return { ledger: computeRunsLedger(history, days), estimated: true, sampleN: history.length };
  }, [costs, days, history]);
}

export const FOOT_NOTE =
  "派工帳＝每派一次工，由執行的 agent 回報的費用加總（Codex 不回報 → 以「未回報」計，不當 0）。聊天・生成帳＝每叫一次模型記一筆（聊天，以及從 Claude Code 叫的生圖、語音、音樂、轉錄）。兩本來源與口徑不同，不能相加。";

/* ---------------- 看板側欄 ---------------- */

export default function Ledgers() {
  const costs = useBoard((s) => s.costs);
  const days = costs?.days ?? 30;
  const { ledger, estimated, sampleN } = useRunsLedger(costs, days);
  const axis = useMemo(() => dayAxis(today(), days), [days]);
  return (
    <aside className="costs" aria-label="費用">
      <div className="sechead">
        <h2>
          <span className="ovp" data-t="Ledger">
            Ledger
          </span>
        </h2>
        <span className="zh">兩本帳</span>
      </div>
      <RunsLedgerCard ledger={ledger} axis={axis} estimated={estimated} sampleN={sampleN} />
      <div className="split">口徑不同　不可相加</div>
      <CallsLedgerCard costs={costs} axis={axis} />
      <p className="foot-note">{FOOT_NOTE}</p>
    </aside>
  );
}
