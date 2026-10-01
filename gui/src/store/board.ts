/* 看板資料 store（不依賴狀態管理套件；useSyncExternalStore）。
   資料流（決策記錄 M4-c）：初載走 REST，之後靠 /ws 推播增量；斷線由 BoardSocket 補差，補不回來就整批重抓。
   單一真相：runs（id → Run）、events（run id → 事件列，依 id 遞增）、now（run id → 最新一則摘要）。 */
import { useRef, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import { BoardSocket, type SocketState } from "@/api/ws";
import type { BusEvent, Costs, EventRow, Run, RunState, Status, EventType } from "@/api/types";
import { summarizeInput, toolParts } from "@/lib/format";

export interface NowLine {
  type: EventType | string;
  /** 人話（「呼叫 Bash」「思考中……」） */
  text: string;
  /** 代號部分（指令、路徑），另行以 .code 顯示 */
  code?: string;
  /** 錯誤 → mark 反白 */
  error?: boolean;
  ts: number;
}

export interface BoardState {
  runs: Record<string, Run>;
  /** 依 started_at 由新到舊 */
  order: string[];
  events: Record<string, EventRow[]>;
  /** 哪些 run 的事件已從 REST 完整載入過（之後只吃 WS 增量） */
  loaded: Record<string, boolean>;
  now: Record<string, NowLine>;
  costs: Costs | null;
  status: Status | null;
  socket: SocketState;
  /** 初載完成 */
  ready: boolean;
  error: string | null;
}

const initial: BoardState = {
  runs: {},
  order: [],
  events: {},
  loaded: {},
  now: {},
  costs: null,
  status: null,
  socket: "closed",
  ready: false,
  error: null,
};

let state: BoardState = initial;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const set = (patch: Partial<BoardState> | ((s: BoardState) => Partial<BoardState>)) => {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  emit();
};

const byStart = (runs: Record<string, Run>) => (a: string, b: string) => (runs[b]?.started_at ?? 0) - (runs[a]?.started_at ?? 0);

function putRuns(list: Run[]): void {
  set((s) => {
    const runs = { ...s.runs };
    for (const r of list) runs[r.id] = { ...runs[r.id], ...r };
    const order = Object.keys(runs).sort(byStart(runs));
    return { runs, order };
  });
}

function patchRun(id: string, patch: Partial<Run>): void {
  set((s) => {
    const cur = s.runs[id];
    if (!cur) return {};
    return { runs: { ...s.runs, [id]: { ...cur, ...patch } } };
  });
}

function appendEvent(row: EventRow, summary: string | null): void {
  set((s) => {
    const list = s.events[row.run_id] ?? [];
    if (list.length && list[list.length - 1].id >= row.id) return {}; // 補差重疊：已經有了
    const events = { ...s.events, [row.run_id]: [...list, row] };
    const now = { ...s.now, [row.run_id]: nowLine(row, summary) };
    // 執行中票券的回合數：tool_call 計數（與後端 RunManager 同口徑）
    const runs = { ...s.runs };
    const r = runs[row.run_id];
    if (r) {
      const patch: Partial<Run> = {};
      if (row.type === "tool_call") patch.turns = (r.turns ?? 0) + 1;
      if (row.type === "session_start") {
        patch.state = "running";
        patch.live = true;
        if (row.payload.session_id) patch.session_id = row.payload.session_id;
      }
      if (row.type === "result") {
        if (row.payload.cost_usd != null) patch.cost_usd = row.payload.cost_usd;
        if (row.payload.num_turns) patch.turns = row.payload.num_turns;
        if (row.payload.usage?.prompt_tokens != null) patch.context_tokens = row.payload.usage.prompt_tokens;
      }
      runs[row.run_id] = { ...r, ...patch };
    }
    return { events, now, runs };
  });
}

export function nowLine(e: EventRow, summary: string | null = null): NowLine {
  const p = e.payload ?? {};
  const tn = toolParts(p.name).n;
  switch (e.type) {
    case "tool_call":
      return { type: e.type, text: `呼叫 ${tn}`, code: summarizeInput(p.input), ts: e.ts };
    case "text":
      return { type: e.type, text: `「${String(p.text ?? "").trim()}」`, ts: e.ts };
    case "thinking":
      return { type: e.type, text: "思考中……", ts: e.ts };
    case "tool_result":
      return p.is_error ? { type: e.type, text: `${tn} 回應`, error: true, ts: e.ts } : { type: e.type, text: `${tn} 回應了，讀取中`, ts: e.ts };
    case "session_start":
      return { type: e.type, text: `session 開始，載入 ${(p.tools ?? []).length} 個工具`, ts: e.ts };
    case "status":
      return { type: e.type, text: String(p.message ?? summary ?? "狀態更新"), ts: e.ts };
    case "result":
      return { type: e.type, text: p.is_error ? "結束：harness 回報錯誤" : "收工。result 在活動流末端。", error: !!p.is_error, ts: e.ts };
    case "error":
      return { type: e.type, text: String(p.message ?? "出錯了"), error: true, ts: e.ts };
    default:
      return { type: e.type, text: summary ?? e.type, ts: e.ts };
  }
}

/* ---------------- 匯流排 ---------------- */

let costsTimer: number | null = null;
const refreshCostsSoon = () => {
  if (costsTimer) return;
  costsTimer = window.setTimeout(() => {
    costsTimer = null;
    void loadCosts();
  }, 1500);
};

/* 其他 store（聊天）也要聽同一條匯流排：WS 只開一條 */
type BusListener = (e: BusEvent) => void;
const busListeners = new Set<BusListener>();
const resyncListeners = new Set<() => void>();
/** 訂閱匯流排事件；回傳取消訂閱 */
export function onBusEvent(l: BusListener): () => void {
  busListeners.add(l);
  return () => void busListeners.delete(l);
}
/** 斷線重連後補不回來（或 daemon 重啟）時通知：各 store 自己重抓 */
export function onResync(l: () => void): () => void {
  resyncListeners.add(l);
  return () => void resyncListeners.delete(l);
}

function onBus(e: BusEvent): void {
  busListeners.forEach((l) => {
    try {
      l(e);
    } catch {
      /* 一個 store 出錯不拖累其他人 */
    }
  });
  switch (e.type) {
    case "run.started": {
      const ev = e as Extract<BusEvent, { type: "run.started" }>;
      if (!state.runs[ev.run_id]) {
        putRuns([
          {
            id: ev.run_id,
            conversation_id: null,
            title: ev.title,
            prompt: null,
            harness: ev.harness,
            model: ev.model,
            cwd: ev.cwd,
            state: "starting",
            started_at: ev.ts,
            ended_at: null,
            turns: 0,
            context_tokens: null,
            cost_usd: null,
            pid: null,
            session_id: null,
            dispatcher: null,
            result: null,
            error: null,
            meta: null,
            live: true,
          },
        ]);
      }
      // 抓完整列（prompt、dispatcher、meta 只有 REST 有）
      void api.run(ev.run_id, { events: false }).then((r) => putRuns([{ ...r, live: true }])).catch(() => {});
      void loadStatus(); // 狀態列的「執行中 N」馬上跟上
      break;
    }
    case "run.event": {
      const ev = e as Extract<BusEvent, { type: "run.event" }>;
      appendEvent({ id: ev.event_id, run_id: ev.run_id, ts: ev.event.ts, type: ev.event.type, payload: ev.event.payload ?? {} }, ev.summary);
      break;
    }
    case "run.finished": {
      const ev = e as Extract<BusEvent, { type: "run.finished" }>;
      patchRun(ev.run_id, { state: ev.state, live: false, ended_at: ev.ts, cost_usd: ev.cost_usd ?? state.runs[ev.run_id]?.cost_usd ?? null, error: ev.error });
      void api.run(ev.run_id, { events: false }).then((r) => putRuns([r])).catch(() => {});
      refreshCostsSoon();
      window.setTimeout(() => void loadStatus(), 300); // daemon 收尾後 live_runs 才會減一
      break;
    }
    case "call.finished":
      refreshCostsSoon();
      break;
    default:
      break;
  }
}

/* ---------------- 載入 ---------------- */

export async function loadRuns(limit = 200): Promise<void> {
  const list = await api.runs({ limit });
  putRuns(list);
}

export async function loadRunEvents(id: string, force = false): Promise<void> {
  if (state.loaded[id] && !force) return;
  const detail = await api.run(id, { after: 0 });
  set((s) => {
    // REST 是「發問當下」的快照；等它回來的這段時間 WS 可能已經先塞了更新的事件。
    // 整批覆蓋會把那些事件蓋掉，所以把快照之後（id 更大）的既有事件接回去。
    const snap = detail.events;
    const lastSnap = snap.length ? snap[snap.length - 1].id : 0;
    const newer = (s.events[id] ?? []).filter((e) => e.id > lastSnap);
    const events = newer.length ? [...snap, ...newer] : snap;
    // run 欄位同理：WS 已經把它推進到終態的話，不要被較舊的快照拉回「執行中」
    const cur = s.runs[id];
    const fresh = stripEvents(detail);
    const keepTerminal = cur && terminal(cur.state) && !terminal(fresh.state);
    const merged: Run = keepTerminal ? { ...fresh, ...cur } : { ...cur, ...fresh };
    if (newer.length) merged.turns = Math.max(merged.turns ?? 0, cur?.turns ?? 0);
    const runs = { ...s.runs, [id]: merged };
    return {
      runs,
      order: cur ? s.order : [...s.order, id].sort(byStart(runs)),
      events: { ...s.events, [id]: events },
      loaded: { ...s.loaded, [id]: true },
      now: events.length ? { ...s.now, [id]: nowLine(events[events.length - 1]) } : s.now,
    };
  });
}

/** 斷線期間漏掉的事件：拿最後一個 id 之後的 */
export async function catchUpRunEvents(id: string): Promise<void> {
  const list = state.events[id] ?? [];
  const after = list.length ? list[list.length - 1].id : 0;
  const detail = await api.run(id, { after });
  for (const row of detail.events) appendEvent(row, null);
  putRuns([stripEvents(detail)]);
}

const stripEvents = (d: Run & { events?: EventRow[] }): Run => {
  const { events: _e, ...r } = d;
  return r;
};

export async function loadCosts(days = 30): Promise<void> {
  try {
    const costs = await api.costs(days);
    set({ costs });
  } catch {
    /* 費用抓不到不擋看板 */
  }
}

export async function loadStatus(): Promise<void> {
  try {
    const status = await api.status();
    set({ status });
  } catch {
    set({ status: null });
  }
}

let socket: BoardSocket | null = null;
let booted = false;

/** App 掛載時呼叫一次：初載＋開 WS。 */
export function bootBoard(): void {
  if (booted) return;
  booted = true;
  void (async () => {
    try {
      await Promise.all([loadRuns(), loadCosts(), loadStatus()]);
      set({ ready: true, error: null });
      // 執行中的 run 先把事件抓齊，票券才有 NOW 與事件條
      for (const id of state.order) {
        const r = state.runs[id];
        if (r.live || r.state === "running" || r.state === "starting") void loadRunEvents(id);
      }
    } catch (e) {
      set({ ready: true, error: e instanceof Error ? e.message : String(e) });
    }
  })();
  socket = new BoardSocket({
    onEvent: onBus,
    onState: (s) => {
      set({ socket: s });
      if (s === "open") void loadStatus();
    },
    onResync: () => {
      void loadRuns();
      for (const id of Object.keys(state.loaded)) void catchUpRunEvents(id);
      void loadCosts();
      resyncListeners.forEach((l) => l());
    },
  });
  socket.start();
  // 狀態列每 30 秒更新一次（uptime、live_runs）
  window.setInterval(() => void loadStatus(), 30_000);
}

/* ---------------- hooks ---------------- */

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

/** selector 回傳新陣列／新物件也沒關係：內容淺比較相同就沿用上一次的參考（useSyncExternalStore 要求 snapshot 穩定） */
export function useBoard<T>(selector: (s: BoardState) => T): T {
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
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!Object.is(a[i], b[i])) return false;
    return true;
  }
  if (a && b && typeof a === "object" && typeof b === "object") {
    const ka = Object.keys(a as object), kb = Object.keys(b as object);
    if (ka.length !== kb.length) return false;
    for (const k of ka) if (!Object.is((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k])) return false;
    return true;
  }
  return false;
}

export const getBoardState = (): BoardState => state;

/** 執行中的 run（含 starting）；順序＝最新在前 */
export const selectLiveIds = (s: BoardState): string[] => s.order.filter((id) => isLiveRun(s.runs[id]));
export const isLiveRun = (r: Run | undefined): boolean => !!r && (r.live || r.state === "running" || r.state === "starting");
export const selectRun = (id: string | null) => (s: BoardState): Run | undefined => (id ? s.runs[id] : undefined);
export const selectEvents = (id: string | null) => (s: BoardState): EventRow[] => (id ? s.events[id] ?? EMPTY_EVENTS : EMPTY_EVENTS);
const EMPTY_EVENTS: EventRow[] = [];
export const selectHistory = (s: BoardState): Run[] => s.order.map((id) => s.runs[id]);
export const terminal = (st: RunState): boolean => st === "done" || st === "error" || st === "cancelled" || st === "dead";

/** 測試／開發用：重設 */
export function _resetBoard(): void {
  socket?.stop();
  socket = null;
  booted = false;
  state = initial;
  emit();
}
