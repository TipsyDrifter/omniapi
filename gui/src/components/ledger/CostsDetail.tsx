import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { Costs } from "@/api/types";
import { usd } from "@/lib/format";
import { useBoard } from "@/store/board";
import { CallsLedgerCard, FOOT_NOTE, RunsLedgerCard, useRunsLedger } from "./Ledgers";
import { dayAxis, today } from "./ledger";

/* /costs 頁的加量版兩本帳：並排（.costs-page），每本底下多一個「依日」列表（.dayrows）。
   天數由頁面切換（7／30／90）：這裡自己抓 /api/costs?days=N，不寫回 store（看板側欄固定看 30 天）。
   store 的 costs 一更新（run 結束、工具呼叫完成），這裡跟著重抓。 */

export interface CostsDetailProps {
  days: 7 | 30 | 90 | number;
}

interface DayRow {
  day: string;
  n: number;
  cost: number;
}

function DayRows({ rows, unit }: { rows: DayRow[]; unit: string }) {
  if (!rows.length) return null;
  const max = Math.max(0, ...rows.map((r) => r.cost));
  return (
    <>
      <div className="lh sub">
        <h3>依日</h3>
        <span className="src">
          <span className="n">{rows.length}</span> 天有資料
        </span>
      </div>
      <ul className="dayrows">
        {rows.map((r) => (
          <li key={r.day}>
            <span className="n">{r.day}</span>
            <span className="bar">
              <i style={{ width: `${max ? (r.cost / max) * 100 : 0}%` }} />
            </span>
            <span className="v">
              {usd(r.cost)}
              <small>
                <span className="n">{r.n}</span> {unit}
              </small>
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

export default function CostsDetail({ days }: CostsDetailProps) {
  const storeCosts = useBoard((s) => s.costs);
  const [costs, setCosts] = useState<Costs | null>(storeCosts && storeCosts.days === days ? storeCosts : null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .costs(days)
      .then((c) => {
        if (!alive) return;
        setCosts(c);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [days, storeCosts]);

  const shown = costs && costs.days === days ? costs : null;
  const { ledger, estimated, sampleN } = useRunsLedger(shown, days);
  const axis = useMemo(() => dayAxis(today(), days), [days]);

  return (
    <div className="costs-page">
      <section className="costs" aria-label="派工帳">
        <RunsLedgerCard ledger={ledger} axis={axis} estimated={estimated} sampleN={sampleN}>
          <DayRows rows={ledger.by_day} unit="筆" />
        </RunsLedgerCard>
      </section>
      <section className="costs" aria-label="聊天・生成帳">
        <CallsLedgerCard costs={shown} axis={axis}>
          {shown && <DayRows rows={[...shown.by_day].sort((a, b) => (a.day < b.day ? 1 : -1))} unit="次" />}
        </CallsLedgerCard>
        {err && (
          <div className="warn">
            <b>費用抓取失敗</b>：{err}
          </div>
        )}
      </section>
      <div className="full">
        <div className="split">口徑不同　不可相加</div>
        <p className="foot-note">{FOOT_NOTE}</p>
      </div>
    </div>
  );
}
