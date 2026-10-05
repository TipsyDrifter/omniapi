import { LiveSection } from "@/components/live";
import RunFeed from "@/components/feed/RunFeed";
import { RunsTable } from "@/components/history";
import { Ledgers } from "@/components/ledger";
import NoProviderNotice from "@/components/NoProviderNotice";
import { isLiveRun, useBoard } from "@/store/board";

/* 看板 `/`：上半＝執行中（票券＋活動流），下半＝歷史派工表＋兩本帳（決策記錄 M4-d） */
export default function BoardPage() {
  const ready = useBoard((s) => s.ready);
  const error = useBoard((s) => s.error);
  // 沒有執行中的 run 時，活動流顯示最近一筆結束的 run（標 END），看板不留白
  const lastId = useBoard((s) => s.order.find((id) => !isLiveRun(s.runs[id])) ?? null);
  return (
    <>
      {error ? (
        <section className="wrap" style={{ paddingTop: 22 }}>
          <div className="warn">
            <b>連不上 daemon</b>：{error}。看板需要 <span className="code">omni serve</span> 在 127.0.0.1:7788 跑著；連上後會自動接續。
          </div>
        </section>
      ) : null}
      <NoProviderNotice />
      <LiveSection renderFeed={(id) => <RunFeed runId={id ?? lastId} title={id ? undefined : "最近一筆"} phoneLimit={20} />} />
      <section className="lower wrap" aria-busy={!ready}>
        <RunsTable />
        <Ledgers />
      </section>
    </>
  );
}
