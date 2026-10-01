/* 兩本帳的純計算（不碰 React）。
   派工帳形狀與後端 Store.cost_summary()["runs"] 一致（types.ts 的 RunsLedger）：舊 daemon 沒有這欄時，前端用已載入的 runs 自己算。
   口徑：cost_usd 為 null（Codex 不回報）不當 0，另計 unreported；某 harness 全部未回報時 cost 為 null（THEME.md §5.5）。 */
import type { Run, RunsLedger } from "@/api/types";
import { day } from "@/lib/format";

/** 派工帳：近 `days` 天（started_at >= now - days*86400）的 runs。day 以 Asia/Taipei 計，與後端 localtime 同口徑（daemon 跑在台灣時區的機器上）。 */
export function computeRunsLedger(runs: Run[], days: number, now: number = Date.now() / 1000): RunsLedger {
  const since = now - days * 86400;
  const inRange = runs.filter((r) => r && r.started_at >= since);

  let cost = 0;
  let unreported = 0;
  const byH = new Map<string, { harness: string; n: number; cost: number | null; unreported: number }>();
  const byD = new Map<string, { day: string; n: number; cost: number }>();

  for (const r of inRange) {
    const c = r.cost_usd;
    if (c == null) unreported++;
    else cost += c;

    const h = byH.get(r.harness) ?? { harness: r.harness, n: 0, cost: null, unreported: 0 };
    h.n++;
    if (c == null) h.unreported++;
    else h.cost = (h.cost ?? 0) + c;
    byH.set(r.harness, h);

    const d = day(r.started_at);
    const dr = byD.get(d) ?? { day: d, n: 0, cost: 0 };
    dr.n++;
    if (c != null) dr.cost += c;
    byD.set(d, dr);
  }

  // 與後端 ORDER BY (SUM IS NULL), SUM DESC, n DESC 相同
  const by_harness = [...byH.values()].sort((a, b) => {
    if ((a.cost == null) !== (b.cost == null)) return a.cost == null ? 1 : -1;
    return (b.cost ?? 0) - (a.cost ?? 0) || b.n - a.n;
  });
  const by_day = [...byD.values()].sort((a, b) => (a.day < b.day ? 1 : a.day > b.day ? -1 : 0));

  return { total: { cost, n: inRange.length, unreported }, by_harness, by_day };
}

/** 日軸：以 lastDay（YYYY-MM-DD）為終點往前 `days` 天，由舊到新。以 UTC 做日期加減，避免時區造成跳日。 */
export function dayAxis(lastDay: string, days: number): string[] {
  const end = new Date(`${lastDay}T00:00:00Z`);
  return Array.from({ length: days }, (_, i) => {
    const d = new Date(end);
    d.setUTCDate(d.getUTCDate() - (days - 1 - i));
    return d.toISOString().slice(0, 10);
  });
}

/** 今天（Asia/Taipei） */
export const today = (): string => day(Date.now() / 1000);
