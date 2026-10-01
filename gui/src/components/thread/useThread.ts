import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Run, Thread } from "@/api/types";
import { isLiveRun, useBoard } from "@/store/board";

/* 追問串資料（決策記錄 M5-g）：GET /api/runs/{id}/thread（root → leaf，不含事件）。
   - 網址 id 換了就重抓；新 id 若不在目前的串裡才清空（避免續接後閃一下「載入中」）。
   - leaf 從執行中變成結束 → 400ms 後重抓一次，確認 resumable。 */

export interface UseThread {
  thread: Thread | null;
  error: string | null;
  /** 重新抓（回傳抓到的串；失敗回 null） */
  refresh: () => Promise<Thread | null>;
  /** 直接放入一份已抓好的串（續接成功、換網址前先放，畫面不斷） */
  prime: (t: Thread) => void;
}

/** 由結束到重抓的延遲（規格：1 秒內） */
const REFETCH_AFTER_END_MS = 400;

export function useThread(id: string): UseThread {
  const [thread, setThread] = useState<Thread | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const threadRef = useRef<Thread | null>(null);
  threadRef.current = thread;

  const refresh = useCallback(async () => {
    const my = ++seq.current;
    try {
      const t = await api.thread(id);
      if (my === seq.current) {
        setThread(t);
        setError(null);
      }
      return t;
    } catch (e) {
      if (my === seq.current) setError(e instanceof Error ? e.message : String(e));
      return null;
    }
  }, [id]);

  const prime = useCallback((t: Thread) => {
    seq.current++;
    setThread(t);
    setError(null);
  }, []);

  useEffect(() => {
    const cur = threadRef.current;
    if (cur && !cur.runs.some((r) => r.id === id)) setThread(null);
    setError(null);
    void refresh();
  }, [id, refresh]);

  // leaf 結束 → 重抓（store 由 WS 的 run.finished 更新）
  const leafId = thread?.leaf_id ?? null;
  const leafLive = useBoard((s) => (leafId ? isLiveRun(s.runs[leafId]) : false));
  const prevLive = useRef(leafLive);
  useEffect(() => {
    const was = prevLive.current;
    prevLive.current = leafLive;
    if (!was || leafLive) return;
    const t = window.setTimeout(() => void refresh(), REFETCH_AFTER_END_MS);
    return () => window.clearTimeout(t);
  }, [leafLive, refresh]);

  return { thread, error, refresh, prime };
}

/** 串裡的一筆：store 有就用 store 的（WS 即時更新），缺的欄位（prompt、meta）用串裡的補 */
export function mergeRun(fromThread: Run, fromStore: Run | undefined): Run {
  if (!fromStore) return fromThread;
  return {
    ...fromThread,
    ...fromStore,
    prompt: fromStore.prompt ?? fromThread.prompt,
    meta: fromStore.meta ?? fromThread.meta,
    dispatcher: fromStore.dispatcher ?? fromThread.dispatcher,
  };
}
