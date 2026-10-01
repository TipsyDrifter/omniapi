import { useEffect } from "react";
import { Feed } from "@/components/feed";
import { isLiveRun, loadRunEvents, selectEvents, selectRun, useBoard } from "@/store/board";

/* 讀 store 的 Feed 包裝：給一個 run id，就把該 run 的事件流接上（看板與單筆頁共用）。
   事件還沒載過就自己去載（看板空票位時顯示的「最近一筆」不會經過 LiveSection 的載入）。 */
export default function RunFeed({ runId, title, headRight }: { runId: string | null; title?: string; headRight?: React.ReactNode }) {
  const run = useBoard(selectRun(runId));
  const events = useBoard(selectEvents(runId));
  const loaded = useBoard((s) => (runId ? !!s.loaded[runId] : false));
  const live = isLiveRun(run);
  useEffect(() => {
    if (runId && !loaded) loadRunEvents(runId).catch(() => {});
  }, [runId, loaded]);
  const emptyText = !runId ? "還沒有 run。派一個工，活動流就會亮起來。" : !loaded ? "載入中…" : live ? "等待 session_start…" : "這筆 run 沒有事件紀錄";
  return <Feed events={events} run={run ?? null} live={live} title={title} emptyText={emptyText} headRight={headRight} />;
}
