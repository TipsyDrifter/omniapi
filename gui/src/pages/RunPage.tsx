import { useCallback } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "@/api/client";
import { Ticket } from "@/components/live";
import { ThreadView, mergeRun, useThread } from "@/components/thread";
import { selectEvents, selectRun, useBoard } from "@/store/board";

/* 單筆 run 還原＋追問串 `/runs/:id`（決策記錄 M5-g）：
   左＝串的最後一段（leaf）的票券（sticky）；右＝整條串（每段：表頭 → prompt → 事件流）＋底部追問框。
   網址的 id 可以是串裡任何一筆。 */
export default function RunPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { thread, error, prime } = useThread(id);

  const leafId = thread?.leaf_id ?? null;
  const leafStore = useBoard(selectRun(leafId));
  const leafEvents = useBoard(selectEvents(leafId));
  const now = useBoard((s) => (leafId ? s.now[leafId] : undefined));
  const leafFromThread = thread?.runs[thread.runs.length - 1];
  const leaf = leafFromThread ? mergeRun(leafFromThread, leafStore) : undefined;

  // 追問成功：先抓新的串放進去，再換網址（畫面不斷）
  const onStarted = useCallback(
    async (runId: string) => {
      try {
        prime(await api.thread(runId));
      } catch {
        /* 換網址後 useThread 會再抓一次 */
      }
      navigate(`/runs/${encodeURIComponent(runId)}`, { replace: true });
    },
    [navigate, prime],
  );

  const n = thread?.runs.length ?? 0;
  return (
    <section className="runpage wrap">
      <div>
        <div className="crumbs">
          <Link to="/">BOARD</Link> › RUN <span className="n">{thread?.root_id ?? id}</span>
          {n > 1 ? (
            <>
              {" "}
              · <span className="n">{n}</span> 段
            </>
          ) : null}
        </div>
        {leaf ? (
          <Ticket run={leaf} events={leafEvents} now={now} />
        ) : (
          <div className="idle">
            <h1>{error ? "找不到這筆 run" : "載入中…"}</h1>
            {error ? <p className="code">{error}</p> : null}
          </div>
        )}
      </div>
      <div className="hero-r">{thread ? <ThreadView key={thread.root_id} thread={thread} targetId={id} onStarted={onStarted} /> : null}</div>
    </section>
  );
}
