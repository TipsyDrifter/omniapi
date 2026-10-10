/* 費用頁「聊天・生成帳」底下：最近 20 筆生成類呼叫（1.1-M4）。
   有做出作品的那一列可以點，跳到作品牆的那一件（多件標「共 N 件」）；失敗或舊資料沒有作品，不可點。 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { CallRow } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { useBoard } from "@/store/board";
import { GEN_TOOLS, TOOL_ZH } from "@/components/works/wall";
import { EstMark } from "@/components/EstMark";

/** unsettled＝影片送出了但沒收回來（不等了、接不回）：供應商多半照樣收費，實際多少不知道 */
const STATUS_ZH: Record<string, string> = { ok: "完成", error: "失敗", running: "進行中", cancelled: "中止", unsettled: "費用不明" };

export default function RecentGenerations() {
  const costs = useBoard((s) => s.costs);
  const [rows, setRows] = useState<CallRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .calls({ tool: GEN_TOOLS, limit: 20 })
      .then((r) => {
        if (!alive) return;
        setRows(r);
        setErr(null);
      })
      .catch((e: unknown) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [costs]); // 帳一更新（有新呼叫）就跟著重抓

  return (
    <div className="ledger rg">
      <div className="lh">
        <h3>最近的生成</h3>
        <span className="src">calls · 最近 {rows?.length ?? 20} 筆</span>
      </div>
      <div className="def">生圖、語音、音樂、轉錄、影片的每一次呼叫；有作品的那列點了會跳到作品牆。影片送出了卻沒收回來的，記「費用不明」（供應商多半照樣收費，看 OpenRouter 的帳單）</div>
      {err ? (
        <div className="warn">
          <b>讀不到呼叫紀錄</b>：{err}
        </div>
      ) : !rows ? (
        <div className="def">讀取中…</div>
      ) : !rows.length ? (
        <div className="def">還沒有生成類的呼叫。</div>
      ) : (
        <ul className="rg-list">
          {rows.map((c) => {
            const ids = c.artifact_ids ?? [];
            const body = (
              <>
                <span className="n rg-t">{dt(c.ts)}</span>
                <span className="rg-tool">{TOOL_ZH[c.tool] ?? c.tool}</span>
                <span className="code rg-m">{c.model ?? "—"}</span>
                <span className="n rg-c">{c.cost_usd != null ? <>{usd(c.cost_usd)}<EstMark x={c} /></> : <span className="nil">{c.status === "unsettled" ? "費用不明" : c.status === "running" ? "還在等" : "未回報"}</span>}</span>
                <span className={`rg-s${c.status === "ok" ? "" : " bad"}`}>{STATUS_ZH[c.status] ?? c.status}</span>
                <span className="rg-go">{ids.length ? (ids.length > 1 ? `共 ${ids.length} 件 →` : "作品 →") : "沒有作品"}</span>
              </>
            );
            return (
              <li key={c.id}>
                {ids.length ? (
                  <Link className="rg-row" to={`/works/${ids[0]}`} title={ids.length > 1 ? `共 ${ids.length} 件，先看第一件` : "在作品牆看"}>
                    {body}
                  </Link>
                ) : (
                  <div className={`rg-row off${c.status === "unsettled" ? " unsettled" : ""}`} title={c.status === "unsettled" ? "送出了但沒收回來：供應商多半照樣收費，實際多少不知道" : c.error ?? "這筆呼叫沒有留下作品"}>
                    {body}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
