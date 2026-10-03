/* 作品牆 store（1.1-M4）。
   篩選條件的真相在網址 query（WorksPage 解析後丟進 loadWall）；這裡只放「目前這面牆」的資料：
   一頁一頁接上去的 items、整面牆的 counts、套用篩選後的 matching、下一頁的 next_before、篩選選單的 facets。
   即時：看板那條 /ws 送來 artifact.created → 人在牆上且符合篩選就插到最前面並標「新」；不在牆上就累計頂欄的「新 N」。 */
import { useRef, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { Artifact, ArtifactFacets, BusArtifactCreated, BusEvent, WallQuery } from "@/api/types";
import { onBusEvent, onResync } from "@/store/board";

export const PAGE = 60;

export interface WorksState {
  /** 目前這面牆的篩選（null＝還沒載過） */
  query: WallQuery | null;
  key: string | null;
  items: Artifact[];
  counts: Record<string, number>;
  matching: Record<string, number>;
  next: number | null;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  facets: ArtifactFacets | null;
  /** 這次在牆上即時插進來、還沒看過的（標「新」） */
  fresh: string[];
  /** 不在牆上時進來的件數（頂欄「作品」旁的「新 N」） */
  unseen: number;
  onWall: boolean;
}

let state: WorksState = {
  query: null,
  key: null,
  items: [],
  counts: {},
  matching: {},
  next: null,
  loading: false,
  loadingMore: false,
  error: null,
  facets: null,
  fresh: [],
  unseen: 0,
  onWall: false,
};
const listeners = new Set<() => void>();
const set = (patch: Partial<WorksState> | ((s: WorksState) => Partial<WorksState>)) => {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  listeners.forEach((l) => l());
};

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const keyOf = (q: WallQuery) => JSON.stringify(q);

/* ---------------- 載入 ---------------- */

/** 換篩選（或第一次進牆）：從頭載一頁。同一組篩選、已經載過就不重抓（燈箱關掉回牆時不跳） */
export async function loadWall(q: WallQuery, force = false): Promise<void> {
  wire();
  const key = keyOf(q);
  if (!force && key === state.key && (state.items.length || state.loading)) return;
  set({ query: q, key, loading: true, error: null, ...(key !== state.key ? { items: [], next: null } : {}) });
  try {
    const res = await api.wall(q, { limit: PAGE });
    if (state.key !== key) return; // 等的時候篩選又換了
    set({ items: res.items, counts: res.counts ?? {}, matching: res.matching ?? res.counts ?? {}, next: res.next_before, loading: false });
  } catch (e) {
    if (state.key !== key) return;
    set({ loading: false, error: errText(e) });
  }
}

/** 捲到底：接下一頁 */
export async function loadMore(): Promise<void> {
  const { query, key, next, loadingMore, loading } = state;
  if (!query || next == null || loadingMore || loading) return;
  set({ loadingMore: true });
  try {
    const res = await api.wall(query, { limit: PAGE, before: next });
    if (state.key !== key) return;
    set((s) => {
      const seen = new Set(s.items.map((a) => a.id));
      return { items: [...s.items, ...res.items.filter((a) => !seen.has(a.id))], next: res.next_before, loadingMore: false, counts: res.counts ?? s.counts, matching: res.matching ?? s.matching };
    });
  } catch (e) {
    set({ loadingMore: false, error: errText(e) });
  }
}

export async function loadFacets(): Promise<void> {
  try {
    set({ facets: await api.facets() });
  } catch {
    /* 選單沒有就只剩「全部」，不擋牆 */
  }
}

/** 只更新數字（counts／matching），不動已經載入的卡片 */
async function refreshCounts(): Promise<void> {
  const { query, key } = state;
  if (!query) return;
  try {
    const res = await api.wall(query, { limit: 1 });
    if (state.key !== key) return;
    set({ counts: res.counts ?? state.counts, matching: res.matching ?? state.matching });
  } catch {
    /* ignore */
  }
}

/** 從牆上移除（hidden=true）／放回（false）。檔案不動 */
export async function setHidden(id: string, hidden: boolean): Promise<void> {
  await api.setHidden(id, hidden);
  set((s) => {
    // 一般牆移除 → 卡片消失；「已移除」那面放回 → 也消失
    const leaves = !!s.query?.only_hidden !== hidden;
    return leaves ? { items: s.items.filter((a) => a.id !== id), fresh: s.fresh.filter((x) => x !== id) } : {};
  });
  void loadFacets();
  void refreshCounts();
}

/* ---------------- 牆上／牆外 ---------------- */

export function enterWall(): void {
  wire();
  set({ onWall: true, unseen: 0 });
}
/** 離開牆：「新」標記算看過了 */
export function leaveWall(): void {
  set({ onWall: false, fresh: [] });
}
export function markSeen(id: string): void {
  if (state.fresh.includes(id)) set((s) => ({ fresh: s.fresh.filter((x) => x !== id) }));
}

/* ---------------- 匯流排 ---------------- */

/** 新作品到了、人在牆上：符不符合目前的篩選由後端判斷（帶 id 問一次）。
    推播不帶逐字稿／歌詞全文，前端自己比會漏掉「搜尋字只在內文裡」的作品；而且篩選的口徑只留後端一份。
    代價是每件新作品多一個回一筆的查詢——新作品是人按出來的，一分鐘沒幾件。 */
async function arrive(id: string): Promise<void> {
  const { query, key } = state;
  if (!query) return;
  try {
    const res = await api.wall(query, { limit: 1, id });
    if (state.key !== key || !state.onWall) return;
    const hit = res.items[0];
    set((s) => {
      const nums = { counts: res.counts ?? s.counts, matching: res.matching ?? s.matching };
      if (!hit || s.items.some((x) => x.id === hit.id)) return nums;
      return { ...nums, items: [hit, ...s.items], fresh: [...s.fresh, hit.id] };
    });
  } catch {
    /* 這一件沒插進來；下次載入牆時會在 */
  }
}

let facetTimer: number | null = null;
function onBus(e: BusEvent): void {
  if (e.type !== "artifact.created") return;
  const a = (e as BusArtifactCreated).artifact;
  if (!a || !a.id) return;
  if (!state.onWall) set((s) => ({ unseen: s.unseen + 1 }));
  else void arrive(a.id);
  // 篩選選單（模型、來源）可能多一個選項；連續進來只抓一次
  if (facetTimer) window.clearTimeout(facetTimer);
  facetTimer = window.setTimeout(() => {
    facetTimer = null;
    void loadFacets();
  }, 800);
}

let wired = false;
function wire(): void {
  if (wired) return;
  wired = true;
  onBusEvent(onBus);
  onResync(() => {
    if (state.query && state.onWall) void loadWall(state.query, true);
  });
}
/** App 掛載時呼叫：不在牆上也要聽，頂欄「新 N」才數得到 */
export function bootWorks(): void {
  wire();
}

/* ---------------- hooks ---------------- */

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

export function useWorks<T>(selector: (s: WorksState) => T): T {
  const ref = useRef<{ value: T; has: boolean }>({ value: undefined as T, has: false });
  const get = () => {
    const next = selector(state);
    if (ref.current.has && shallowEqual(ref.current.value, next)) return ref.current.value;
    ref.current = { value: next, has: true };
    return next;
  };
  return useSyncExternalStore(subscribe, get, get);
}

function shallowEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object") {
    const ka = Object.keys(a as object);
    const kb = Object.keys(b as object);
    return ka.length === kb.length && ka.every((k) => Object.is((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
  }
  return false;
}

export const getWorksState = (): WorksState => state;
