import { useEffect, useState } from "react";
import { CostsDetail } from "@/components/ledger";
import { loadCosts } from "@/store/board";

const RANGES = [7, 30, 90] as const;

/* 費用頁 `/costs`：兩本帳並排＋依日／依模型／依 harness 分布（M4-g：原生 CSS 條，不用圖表庫） */
export default function CostsPage() {
  const [days, setDays] = useState<(typeof RANGES)[number]>(30);
  useEffect(() => {
    void loadCosts(days);
  }, [days]);
  return (
    <>
      <section className="wrap" style={{ paddingTop: 22 }}>
        <div className="sechead">
          <h2>
            <span className="ovp" data-t="Ledger">
              Ledger
            </span>
          </h2>
          <span className="zh">兩本帳 · 口徑不同不可相加</span>
          <div className="sim" role="group" aria-label="期間" style={{ marginLeft: "auto" }}>
            <span className="k">期間</span>
            {RANGES.map((d) => (
              <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}>
                {d} 天
              </button>
            ))}
          </div>
        </div>
      </section>
      <div className="wrap">
        <CostsDetail days={days} />
      </div>
    </>
  );
}
