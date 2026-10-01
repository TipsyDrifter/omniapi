/* 聊天 store（決策記錄 M6-b、M6-c）。
   發號施令走 REST（api.createChat／sendChat…），回覆的逐字內容從看板那條 /ws 進來（chat.delta）。
   單一真相：list（對話清單）、convs（已開過的對話全文）、live（回覆進行中的暫存字）。
   斷線重連補不回來時（delta 不進匯流排歷史），重抓已開的對話——daemon 的 live 欄位會帶回目前累積的字。 */
import { useRef, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type { BusChatDelta, BusChatFinished, BusChatStarted, BusChatUpdated, BusEvent, ChatDetail, ChatLive, ChatMessage, ChatParams, ChatSummary } from "@/api/types";
import { onBusEvent, onResync } from "@/store/board";

export interface ChatState {
  /** 對話清單（最近活動的在前） */
  list: ChatSummary[];
  listLoaded: boolean;
  convs: Record<string, ChatDetail>;
  /** 回覆進行中的對話：conversation id → 已經到的字 */
  live: Record<string, ChatLive>;
  /** 最近一次回覆失敗的原因（對話 id → 訊息），下一次送出時清掉 */
  errors: Record<string, string>;
}

const initial: ChatState = { list: [], listLoaded: false, convs: {}, live: {}, errors: {} };
let state: ChatState = initial;
const listeners = new Set<() => void>();
const set = (patch: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>)) => {
  const p = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...p };
  listeners.forEach((l) => l());
};

const byUpdated = (a: ChatSummary, b: ChatSummary) => (b.updated_at ?? 0) - (a.updated_at ?? 0);

function summaryOf(d: ChatDetail): ChatSummary {
  const { messages: _m, live, ...rest } = d;
  return { ...rest, live: !!live };
}

function putSummary(s: ChatSummary): void {
  set((st) => {
    const rest = st.list.filter((x) => x.id !== s.id);
    const prev = st.list.find((x) => x.id === s.id);
    if (s.status === "archived") return { list: rest };
    return { list: [{ ...prev, ...s }, ...rest].sort(byUpdated) };
  });
}

function totals(messages: ChatMessage[]): Pick<ChatDetail, "n_messages" | "cost_usd" | "priced" | "unpriced"> {
  const replies = messages.filter((m) => m.role === "assistant");
  return {
    n_messages: messages.length,
    cost_usd: replies.reduce((a, m) => a + (m.cost_usd ?? 0), 0),
    priced: replies.filter((m) => m.cost_usd != null).length,
    unpriced: replies.filter((m) => m.cost_usd == null && m.content).length,
  };
}

function addMessage(cid: string, msg: ChatMessage, patch: Partial<ChatDetail> = {}): void {
  set((st) => {
    const conv = st.convs[cid];
    if (!conv) return {};
    const messages = conv.messages.some((m) => m.id === msg.id) ? conv.messages : [...conv.messages, msg].sort((a, b) => a.seq - b.seq);
    const next: ChatDetail = { ...conv, ...patch, messages, ...totals(messages), updated_at: Math.max(conv.updated_at ?? 0, msg.created_at ?? 0) };
    return { convs: { ...st.convs, [cid]: next } };
  });
}

/* ---------------- 匯流排 ---------------- */

function onBus(e: BusEvent): void {
  switch (e.type) {
    case "chat.started": {
      const ev = e as BusChatStarted;
      const live: ChatLive = { turn_id: ev.turn_id, model: ev.model, resolved_model: ev.resolved_model, started_at: ev.ts, text: "", reasoning: "" };
      set((st) => {
        const errors = { ...st.errors };
        delete errors[ev.conversation_id];
        return { live: { ...st.live, [ev.conversation_id]: live }, errors };
      });
      addMessage(ev.conversation_id, ev.user_message, { model: ev.model, status: "running", title: ev.title ?? state.convs[ev.conversation_id]?.title ?? null, live });
      const known = state.list.find((x) => x.id === ev.conversation_id);
      if (known) putSummary({ ...known, title: ev.title ?? known.title, model: ev.model, status: "running", live: true, updated_at: ev.ts, n_messages: known.n_messages + 1 });
      else void loadChats();
      break;
    }
    case "chat.delta": {
      const ev = e as BusChatDelta;
      set((st) => {
        const cur = st.live[ev.conversation_id];
        if (!cur || cur.turn_id !== ev.turn_id) return {};
        const next = ev.kind === "reasoning" ? { ...cur, reasoning: cur.reasoning + ev.delta } : { ...cur, text: cur.text + ev.delta };
        return { live: { ...st.live, [ev.conversation_id]: next } };
      });
      break;
    }
    case "chat.finished": {
      const ev = e as BusChatFinished;
      set((st) => {
        const live = { ...st.live };
        delete live[ev.conversation_id];
        const errors = { ...st.errors };
        if (ev.state === "error" && ev.error) errors[ev.conversation_id] = ev.error;
        return { live, errors };
      });
      if (ev.message) addMessage(ev.conversation_id, ev.message, { status: "open", live: null });
      else void loadChat(ev.conversation_id, true);
      const conv = state.convs[ev.conversation_id];
      const known = state.list.find((x) => x.id === ev.conversation_id);
      if (conv) putSummary(summaryOf({ ...conv, live: null }));
      else if (known) {
        const m = ev.message;
        putSummary({
          ...known,
          status: "open",
          live: false,
          updated_at: ev.ts,
          n_messages: known.n_messages + (m ? 1 : 0),
          cost_usd: known.cost_usd + (m?.cost_usd ?? 0),
          priced: (known.priced ?? 0) + (m && m.cost_usd != null ? 1 : 0),
          unpriced: (known.unpriced ?? 0) + (m && m.cost_usd == null && m.content ? 1 : 0),
        });
      }
      break;
    }
    case "chat.updated": {
      const ev = e as BusChatUpdated;
      const c = ev.conversation;
      set((st) => {
        const conv = st.convs[ev.conversation_id];
        if (!conv) return {};
        const { live: _l, ...fields } = c;
        return { convs: { ...st.convs, [ev.conversation_id]: { ...conv, ...(fields as Partial<ChatDetail>) } } };
      });
      const known = state.list.find((x) => x.id === ev.conversation_id);
      if (known || c.status === "archived") {
        if (known) putSummary({ ...known, ...c, live: known.live } as ChatSummary);
      } else void loadChats();
      break;
    }
    default:
      break;
  }
}

let wired = false;
function wire(): void {
  if (wired) return;
  wired = true;
  onBusEvent(onBus);
  onResync(() => {
    void loadChats();
    for (const id of Object.keys(state.convs)) void loadChat(id, true);
  });
}

/* ---------------- 動作 ---------------- */

export async function loadChats(): Promise<void> {
  wire();
  const list = await api.chats();
  set({ list: list.sort(byUpdated), listLoaded: true });
}

export async function loadChat(id: string, force = false): Promise<ChatDetail> {
  wire();
  if (state.convs[id] && !force) return state.convs[id];
  const d = await api.chat(id);
  set((st) => {
    const live = { ...st.live };
    if (d.live) {
      // daemon 帶回目前累積的字；若 WS 已經收得比它多（同一輪），留 WS 的
      const cur = live[id];
      live[id] = cur && cur.turn_id === d.live.turn_id && cur.text.length >= d.live.text.length ? cur : d.live;
    } else delete live[id];
    return { convs: { ...st.convs, [id]: d }, live };
  });
  return d;
}

/** 建立對話（可順便送第一則）。回傳對話 id */
export async function createChat(body: { model?: string; system?: string; title?: string; message?: string; params?: ChatParams }): Promise<string> {
  wire();
  const d = await api.createChat(body);
  const { turn, ...detail } = d;
  const messages = turn ? [turn.user_message] : [];
  const conv: ChatDetail = { ...(detail as ChatDetail), messages, ...totals(messages), live: state.live[d.id] ?? detail.live ?? null };
  set((st) => ({ convs: { ...st.convs, [d.id]: st.convs[d.id] ?? conv } }));
  putSummary(summaryOf({ ...conv, live: turn ? (state.live[d.id] ?? null) : null, status: turn ? "running" : conv.status }));
  return d.id;
}

export async function sendChat(id: string, text: string, opts: { model?: string; params?: ChatParams } = {}): Promise<void> {
  wire();
  set((st) => {
    const errors = { ...st.errors };
    delete errors[id];
    return { errors };
  });
  const turn = await api.sendChat(id, { text, model: opts.model, params: opts.params });
  // WS 的 chat.started 通常先到；沒到（WS 斷線）就自己補上使用者那一則
  addMessage(id, turn.user_message, { model: turn.model, status: "running" });
}

export async function cancelChat(id: string): Promise<boolean> {
  const r = await api.cancelChat(id);
  return r.cancelled;
}

export async function updateChat(id: string, patch: { title?: string; model?: string; system_prompt?: string; archived?: boolean }): Promise<void> {
  wire();
  const c = await api.updateChat(id, patch);
  set((st) => {
    const conv = st.convs[id];
    if (!conv) return {};
    const { live: _l, ...fields } = c;
    return { convs: { ...st.convs, [id]: { ...conv, ...(fields as Partial<ChatDetail>) } } };
  });
  const known = state.list.find((x) => x.id === id);
  if (c.status === "archived") set((st) => ({ list: st.list.filter((x) => x.id !== id) }));
  else if (known) putSummary({ ...known, ...c, live: known.live });
  else void loadChats();
}

/* ---------------- hooks ---------------- */

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

/** selector 回傳新陣列／新物件也沒關係：淺比較相同就沿用上一次的參考 */
export function useChat<T>(selector: (s: ChatState) => T): T {
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

export const getChatState = (): ChatState => state;
export const selectChat = (id: string | null | undefined) => (s: ChatState): ChatDetail | undefined => (id ? s.convs[id] : undefined);
export const selectChatLive = (id: string | null | undefined) => (s: ChatState): ChatLive | undefined => (id ? s.live[id] : undefined);
export const selectChatError = (id: string | null | undefined) => (s: ChatState): string | undefined => (id ? s.errors[id] : undefined);
export const selectChatList = (s: ChatState): ChatSummary[] => s.list;
