/* 生成頁 store（1.1-M3）。
   發號施令走 REST（api.startGeneration／cancelGeneration），開始與結束從看板那條 /ws 進來
   （generation.started／generation.finished，事件裡帶整筆 Generation）。
   單一真相：gens（id → Generation，新的在前）、drafts（四種模態的草稿，各自存 localStorage）、
   dismissed（出件口收起的 id，存 localStorage；不呼叫後端）。
   App 掛載時 bootMake()：先抓一次最近的生成工作，頂欄「生成」旁的件數在任何一頁都對得上。 */
import { useRef, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { BusEvent, BusGeneration, GenKind, GenOptions, GenRequest, Generation } from "@/api/types";
import { onBusEvent, onResync } from "@/store/board";
import { KINDS, loadDraft, saveDraft, type Drafts } from "@/components/make/draft";

/** 出件口列幾件（跨四種模態，新到舊） */
export const TRAY_LIMIT = 8;

export interface MakeState {
  options: GenOptions | null;
  optionsError: string | null;
  gens: Record<string, Generation>;
  /** 依 created_at 新到舊 */
  order: string[];
  loaded: boolean;
  /** 這次進站後從這頁送出的（出件口一定列） */
  mine: string[];
  dismissed: string[];
  drafts: Drafts;
  /** 從作品牆帶進來的（生成頁頂端那一行「從作品牆帶入：⋯」）；只記在這次進站 */
  carry: Carry | null;
}

/** 1.1-M4：作品牆 → 生成頁帶了什麼 */
export interface Carry {
  /** 帶進哪一種表單 */
  kind: GenKind;
  /** 哪一件作品 */
  id: string;
  /** 作品的種類（章用） */
  artKind: string;
  /** 一句話：帶入了什麼 */
  what: string;
  /** 資料不全時的說明（例：當時的來源圖沒有留存） */
  note: string | null;
  thumb: string | null;
}

const DISMISS_KEY = "omniapi.make.dismissed";
const loadDismissed = (): string[] => {
  try {
    const j = JSON.parse(localStorage.getItem(DISMISS_KEY) ?? "[]") as unknown;
    return Array.isArray(j) ? j.filter((x): x is string => typeof x === "string").slice(-200) : [];
  } catch {
    return [];
  }
};

const initial = (): MakeState => ({
  options: null,
  optionsError: null,
  gens: {},
  order: [],
  loaded: false,
  mine: [],
  dismissed: loadDismissed(),
  drafts: Object.fromEntries(KINDS.map((k) => [k, loadDraft(k)])) as unknown as Drafts,
  carry: null,
});

let state: MakeState = initial();
const listeners = new Set<() => void>();
const set = (patch: Partial<MakeState> | ((s: MakeState) => Partial<MakeState>)) => {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  listeners.forEach((l) => l());
};

function putGens(list: Generation[]): void {
  if (!list.length) return;
  set((s) => {
    const gens = { ...s.gens };
    for (const g of list) {
      const cur = gens[g.id];
      // 匯流排與 REST 可能交錯：已經是終態的不要被較舊的 running 快照拉回去
      if (cur && cur.status !== "running" && g.status === "running") continue;
      gens[g.id] = g;
    }
    const order = Object.keys(gens).sort((a, b) => gens[b].created_at - gens[a].created_at);
    return { gens, order };
  });
}

/* ---------------- 匯流排 ---------------- */

function onBus(e: BusEvent): void {
  if (e.type === "generation.started" || e.type === "generation.finished") {
    const g = (e as BusGeneration).generation;
    if (g && g.id) putGens([g]);
  }
}

let wired = false;
function wire(): void {
  if (wired) return;
  wired = true;
  onBusEvent(onBus);
  onResync(() => void loadGenerations());
}

/* ---------------- 動作 ---------------- */

export async function loadGenerations(): Promise<void> {
  wire();
  try {
    const list = await api.generations({ limit: 30 });
    putGens(list);
  } finally {
    set({ loaded: true });
  }
}

export async function loadOptions(force = false): Promise<void> {
  if (state.options && !force) return;
  try {
    const options = await api.generateOptions();
    set({ options, optionsError: null });
  } catch (e) {
    set({ optionsError: e instanceof Error ? e.message : String(e) });
  }
}

let booted = false;
/** App 掛載時呼叫一次 */
export function bootMake(): void {
  if (booted) return;
  booted = true;
  wire();
  void loadGenerations().catch(() => {});
}

export async function startGeneration(req: GenRequest): Promise<Generation> {
  wire();
  const g = await api.startGeneration(req);
  putGens([g]);
  set((s) => ({ mine: [g.id, ...s.mine.filter((x) => x !== g.id)], dismissed: s.dismissed.filter((x) => x !== g.id) }));
  return g;
}

export async function cancelGeneration(id: string): Promise<void> {
  const g = await api.cancelGeneration(id);
  putGens([g]);
}

/** 出件口收起一張（只記在這台瀏覽器） */
export function dismissGeneration(id: string): void {
  set((s) => {
    const dismissed = [...s.dismissed.filter((x) => x !== id), id].slice(-200);
    try {
      localStorage.setItem(DISMISS_KEY, JSON.stringify(dismissed));
    } catch {
      /* ignore */
    }
    // 開發用的示意卡收起就直接拿掉
    if (id.startsWith("demo-")) {
      const gens = { ...s.gens };
      delete gens[id];
      return { dismissed, gens, order: s.order.filter((x) => x !== id) };
    }
    return { dismissed };
  });
}

export function setDraft<K extends GenKind>(kind: K, patch: Partial<Drafts[K]>): void {
  set((s) => {
    const next = { ...s.drafts[kind], ...patch } as Drafts[K];
    saveDraft(kind, next);
    return { drafts: { ...s.drafts, [kind]: next } };
  });
}

/** 作品牆帶資料進來：先寫草稿、再記一行「從作品牆帶入」；呼叫端接著導頁（不自動送出） */
export function carryIn<K extends GenKind>(kind: K, patch: Partial<Drafts[K]>, carry: Omit<Carry, "kind">): void {
  setDraft(kind, patch);
  set({ carry: { ...carry, kind } });
}
export function clearCarry(): void {
  if (state.carry) set({ carry: null });
}

/** 開發／沙盒用：塞幾張假的失敗卡（?demo=fail），不經後端 */
export function injectDemoFailures(): void {
  const now = Date.now() / 1000;
  const base = { finished_at: now - 1, tool: "", sources: {}, estimate: null, cost_usd: null, artifacts: [] };
  const demos: Generation[] = [
    {
      ...base,
      id: "demo-quota",
      created_at: now - 9,
      kind: "image",
      tool: "generate_image",
      model: "gpt-image-2",
      title: "示意：額度不夠的失敗卡",
      params: { prompt: "示意：額度不夠的失敗卡", model: "gpt-image-2", size: "1536x1024", quality: "high", n: 1 },
      status: "error",
      error: "Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check your plan and billing details.', 'type': 'insufficient_quota'}}",
      error_kind: "quota",
      demo: true,
    },
    {
      ...base,
      id: "demo-rejected",
      created_at: now - 6,
      kind: "speech",
      tool: "generate_speech",
      model: "eleven_v3",
      title: "示意：內容被擋下的失敗卡",
      params: { text: "示意：內容被擋下的失敗卡", model: "eleven_v3" },
      status: "error",
      error: "status_code: 400, body: {'detail': {'status': 'content_policy_violation'}}",
      error_kind: "rejected",
      demo: true,
    },
    {
      ...base,
      id: "demo-cancelled",
      created_at: now - 3,
      kind: "music",
      tool: "generate_music",
      model: "V6",
      title: "示意：中止的音樂",
      params: { prompt: "示意：中止的音樂", model: "V6", instrumental: false },
      status: "cancelled",
      error: "cancelled",
      error_kind: "interrupted",
      demo: true,
    },
  ];
  putGens(demos);
  // 當成這次送出的：出件口一定列
  set((s) => ({ dismissed: s.dismissed.filter((id) => !id.startsWith("demo-")), mine: [...demos.map((g) => g.id), ...s.mine.filter((id) => !id.startsWith("demo-"))] }));
}

/* ---------------- 選擇器 ---------------- */

export const isRunning = (g: Generation | undefined): boolean => !!g && g.status === "running";
export const selectRunningCount = (s: MakeState): number => s.order.reduce((n, id) => n + (isRunning(s.gens[id]) ? 1 : 0), 0);
/** 出件口的內容：最近 TRAY_LIMIT 件＋這次送出的，扣掉收起的；新到舊 */
export const selectTrayIds = (s: MakeState): string[] => {
  const hidden = new Set(s.dismissed);
  const recent = s.order.filter((id) => !hidden.has(id)).slice(0, TRAY_LIMIT);
  const extra = s.mine.filter((id) => !hidden.has(id) && !recent.includes(id) && s.gens[id]);
  return [...recent, ...extra].sort((a, b) => s.gens[b].created_at - s.gens[a].created_at);
};

/* ---------------- hooks ---------------- */

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

/** selector 回傳新陣列／新物件也沒關係：淺比較相同就沿用上一次的參考 */
export function useMake<T>(selector: (s: MakeState) => T): T {
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

export const getMakeState = (): MakeState => state;
