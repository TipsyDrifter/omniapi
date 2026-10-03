/* 聊天 store（決策記錄 M6-b、M6-c；分岔與刪除 1.2-M3）。
   發號施令走 REST（api.createChat／sendChat…），回覆的逐字內容從看板那條 /ws 進來（chat.delta）。
   單一真相：list（對話清單）、convs（已開過的對話：messages 只有現行那一條）、live（回覆進行中的暫存字）。
   斷線重連補不回來時（delta 不進匯流排歷史），重抓已開的對話——daemon 的 live 欄位會帶回目前累積的字。
   分岔：重新生成／編輯開始時（chat.started 或送出的回應，哪個先到都行）把現行那一條剪到分岔點再接上；
   回覆存下後重抓一次，版本數以後端為準。切換版本以後端回的詳情為準；別的分頁切換時靠 chat.updated 的 leaf_id 重抓。
   則數與費用合計算全部分支（同後端）：剪掉的訊息不扣，只有真的新增的才加。
   提議（1.2-M4）：掛在回覆的 proposals 上；chat.proposal（含別的分頁按的、送出下一則時自動略過的）原地換掉那一張。
   不在現行那一條上的回覆（別的版本）不管，換過去時 GET 會帶最新的。卡上要看的生成工作交給生成頁的 store（ensureGeneration）。
   檔案（1.2-M5）：回覆前先問主人（轉錄提問）時回合停在 awaiting——沒有 live，那則空的回覆照常進訊息；全部了結後回覆
   沿用同一個 turn_id 寫進那一則（chat.started 帶 fill_id），畫面在那一則的位置顯示進行中，存下時原地換掉、不長出第二則。
   chat.tool＝回覆進行中「正在讀…」與讀過的。這段聊天的生成費用以後端的合計（generation_cost_usd）為準，生成做完時重抓。 */
import { useRef, useSyncExternalStore } from "react";
import { api } from "@/api/client";
import type {
  BusChatDelta,
  BusChatDeleted,
  BusChatFinished,
  BusChatProposal,
  BusChatStarted,
  BusChatTool,
  BusChatUpdated,
  BusEvent,
  BusGeneration,
  ChatAttachment,
  ChatAttachmentRef,
  ChatDetail,
  ChatLive,
  ChatMessage,
  ChatParams,
  ChatProposal,
  ChatSummary,
  ChatTurn,
  ChatVersions,
} from "@/api/types";
import { onBusEvent, onResync } from "@/store/board";
import { ensureGeneration } from "@/store/make";

export interface ChatState {
  /** 對話清單（最近活動的在前） */
  list: ChatSummary[];
  listLoaded: boolean;
  convs: Record<string, ChatDetail>;
  /** 回覆進行中的對話：conversation id → 已經到的字 */
  live: Record<string, ChatLive>;
  /** 最近一次回覆失敗的原因（對話 id → 訊息），下一次送出時清掉 */
  errors: Record<string, string>;
  /** 刪掉的對話 → 接著要顯示的那一筆（清單裡它的下一筆，沒有就上一筆；null＝清單空了） */
  deleted: Record<string, string | null>;
}

const initial: ChatState = { list: [], listLoaded: false, convs: {}, live: {}, errors: {}, deleted: {} };
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

/** 統計加上一則真的新增的訊息（local 不算；同一則不會加兩次，由呼叫端保證） */
function counted(conv: ChatDetail, msg: ChatMessage): Pick<ChatDetail, "n_messages" | "cost_usd" | "priced" | "unpriced"> {
  const reply = msg.role === "assistant";
  return {
    n_messages: conv.n_messages + 1,
    cost_usd: conv.cost_usd + (reply ? (msg.cost_usd ?? 0) : 0),
    priced: (conv.priced ?? 0) + (reply && msg.cost_usd != null ? 1 : 0),
    unpriced: (conv.unpriced ?? 0) + (reply && msg.cost_usd == null && msg.content ? 1 : 0),
  };
}

/** 現行那一條最後一則後端的訊息（不算 local） */
const lastReal = (conv: ChatDetail): ChatMessage | undefined => [...conv.messages].reverse().find((m) => !m.local);

/** 同一則換成新的樣子時，統計只差在費用（則數不變） */
function recounted(conv: ChatDetail, old: ChatMessage, msg: ChatMessage): Pick<ChatDetail, "cost_usd" | "priced" | "unpriced"> {
  if (msg.role !== "assistant") return { cost_usd: conv.cost_usd, priced: conv.priced, unpriced: conv.unpriced };
  const priced = (m: ChatMessage) => (m.cost_usd != null ? 1 : 0);
  const unpriced = (m: ChatMessage) => (m.cost_usd == null && m.content ? 1 : 0);
  return {
    cost_usd: conv.cost_usd - (old.cost_usd ?? 0) + (msg.cost_usd ?? 0),
    priced: (conv.priced ?? 0) - priced(old) + priced(msg),
    unpriced: (conv.unpriced ?? 0) - unpriced(old) + unpriced(msg),
  };
}

function addMessage(cid: string, msg: ChatMessage, patch: Partial<ChatDetail> = {}): void {
  set((st) => {
    const conv = st.convs[cid];
    if (!conv) return {};
    // 後端那一則到了：送出當下先放的那一則（local）就功成身退
    const real = !msg.local && msg.role === "user";
    const kept = real ? conv.messages.filter((m) => !m.local) : conv.messages;
    if (real) delete localIds[cid];
    const old = kept.find((m) => m.id === msg.id);
    // 1.2-M5：先問過主人的回覆寫進同一則（fill）——已經在的那則回覆換成新的樣子，不長出第二則
    const swap = !!old && msg.role === "assistant" && !msg.local;
    const messages = old ? (swap ? kept.map((m) => (m.id === msg.id ? { ...msg, versions: msg.versions ?? m.versions } : m)) : kept) : [...kept, msg].sort((a, b) => a.seq - b.seq);
    const stats = msg.local ? {} : old ? (swap ? recounted(conv, old, msg) : {}) : counted(conv, msg);
    const next: ChatDetail = { ...conv, ...patch, ...stats, messages, updated_at: Math.max(conv.updated_at ?? 0, msg.created_at ?? 0) };
    return { convs: { ...st.convs, [cid]: next } };
  });
}

/* ---------------- 樂觀顯示：送出當下使用者那一則（含圖）就出現 ----------------
   一段對話同時只有一則在送（後端有鎖），所以每段對話最多一則 local。
   後端的那一則（chat.started 或送出的回應，哪個先到都行）來了就換掉它；送出失敗就拿掉。 */
const localIds: Record<string, string> = {};
let localSeq = 0;

/** 送出時的一張附件：送給後端的編號＋樂觀顯示用的樣子 */
export interface OutgoingAttachment {
  ref: ChatAttachmentRef;
  view: ChatAttachment;
}

export function localMessage(cid: string, text: string, atts: OutgoingAttachment[], after: ChatMessage[] = []): ChatMessage {
  return {
    id: `local-${++localSeq}`,
    conversation_id: cid,
    seq: after.reduce((a, m) => Math.max(a, m.seq), 0) + 1,
    role: "user",
    content: text,
    created_at: Date.now() / 1000,
    model: null,
    usage: null,
    cost_usd: null,
    reasoning: null,
    meta: null,
    attachments: atts.length ? atts.map((a) => a.view) : null,
    local: true,
  };
}

function dropLocal(cid: string): void {
  const lid = localIds[cid];
  if (!lid) return;
  delete localIds[cid];
  set((st) => {
    const conv = st.convs[cid];
    if (!conv) return {};
    const messages = conv.messages.filter((m) => m.id !== lid);
    return { convs: { ...st.convs, [cid]: { ...conv, messages } } };
  });
}

/* ---------------- 回覆開始：一般送出、重新生成、編輯 ----------------
   chat.started（WS）與送出類的回應（REST）帶的是同一件事；先到的那個生效，後到的只確認訊息在。
   已經結束的回合（回覆很快時 chat.finished 可能比 REST 回應先到）不再開一次。 */
type Started = Pick<ChatTurn, "turn_id" | "model" | "resolved_model" | "user_message" | "action" | "parent_id" | "vision" | "images_skipped" | "regenerate_of" | "edit_of"> & {
  title?: string | null;
  ts?: number;
  /** 1.2-M5：回覆前先問主人（沒有回覆在跑；那則空的回覆由 chat.finished 或送出的回應帶來） */
  awaiting?: boolean;
  /** 1.2-M5：回覆寫進既有的那一則（先問過主人的） */
  fill_id?: string;
};

/** 送出類的回應 → 開始（回應的 state 是 awaiting 時，message 就是那則空的回覆） */
function startedOf(turn: ChatTurn): Started {
  return { ...turn, awaiting: turn.state === "awaiting" };
}

const doneTurns = new Set<string>();
/** 1.2-M5：先問主人的回合（停在轉錄提問）；之後的回覆沿用同一個 turn_id 寫進那一則，所以不算結束 */
const awaitingTurns = new Set<string>();
/** 回合 → 動作（回覆存下時，重新生成與編輯要重抓一次拿版本數） */
const turnAction: Record<string, string | undefined> = {};

const versionsPlusOne = (v: ChatVersions | undefined, id: string, newId: string): ChatVersions => {
  const ids = v?.ids?.length ? v.ids : [id];
  return { count: ids.length + 1, index: ids.length + 1, ids: [...ids, newId] };
};

function applyStarted(cid: string, ev: Started, reply?: ChatMessage | null): void {
  if (doneTurns.has(ev.turn_id)) return;
  const action = ev.action ?? "send";
  if (state.live[cid]?.turn_id === ev.turn_id || (ev.awaiting && awaitingTurns.has(ev.turn_id))) {
    // 另一條路已經開過了：只確認使用者那一則（與先問主人的那則回覆）在
    addMessage(cid, ev.user_message);
    if (reply) addMessage(cid, reply);
    return;
  }
  turnAction[ev.turn_id] = action;
  if (ev.awaiting) awaitingTurns.add(ev.turn_id);
  const conv = state.convs[cid];
  // 寫進既有那一則的回覆：‹ n/m › 沿用那一則的
  const fill = ev.fill_id ? conv?.messages.find((m) => m.id === ev.fill_id) : undefined;
  const live: ChatLive = {
    turn_id: ev.turn_id,
    model: ev.model,
    resolved_model: ev.resolved_model,
    started_at: ev.ts ?? Date.now() / 1000,
    text: "",
    reasoning: "",
    vision: ev.vision,
    images_skipped: ev.images_skipped,
    parent_id: ev.parent_id ?? ev.user_message.id,
    action,
    fill_id: ev.fill_id ?? null,
    versions: fill?.versions,
    reads: [],
    reading: null,
  };
  let reload = false;
  let user = ev.user_message;
  if (conv && action !== "send" && !ev.fill_id) {
    const base = conv.messages.filter((m) => !m.local);
    const local = conv.messages.find((m) => m.local);
    let path: ChatMessage[] | null = null;
    if (action === "regenerate") {
      // 剪到被重新回答的那一則使用者訊息；原本接在它後面的回覆＝新的一版的兄弟
      const qi = base.findIndex((m) => m.id === live.parent_id);
      if (qi >= 0) {
        const replaced = base[qi + 1];
        if (replaced && replaced.role === "assistant") live.versions = versionsPlusOne(replaced.versions, replaced.id, `live-${ev.turn_id}`);
        path = base.slice(0, qi + 1);
      }
    } else if (action === "edit") {
      // 剪到原本那一則之前，接上改寫後的新一則（版本＝原本那則的兄弟多一個）
      const ei = base.findIndex((m) => m.id === ev.edit_of);
      if (ei >= 0) {
        user = { ...user, versions: versionsPlusOne(base[ei].versions, base[ei].id, user.id) };
        path = base.slice(0, ei);
      } else if (local?.versions) {
        // 送出當下已經先剪好、先放了一則（樂觀顯示）
        user = { ...user, versions: { ...local.versions, ids: [...local.versions.ids.slice(0, -1), user.id] } };
        path = base;
      }
    }
    if (path) {
      const messages = path;
      delete localIds[cid];
      set((st) => ({ convs: { ...st.convs, [cid]: { ...conv, messages } } }));
    } else reload = true;
  }
  set((st) => {
    const errors = { ...st.errors };
    delete errors[cid];
    // 先問主人：沒有回覆在跑，對話不算忙碌（1.2-M5-a）
    return ev.awaiting ? { errors } : { live: { ...st.live, [cid]: live }, errors };
  });
  const status = ev.awaiting ? "open" : "running";
  addMessage(cid, user, { model: ev.model, status, title: ev.title ?? state.convs[cid]?.title ?? null, live: ev.awaiting ? null : live });
  if (reply) addMessage(cid, reply);
  if (reload) void loadChat(cid, true);
  const known = state.list.find((x) => x.id === cid);
  // 重新生成沒有新的使用者訊息；寫進既有那一則的回覆也沒有
  const added = action === "regenerate" || ev.fill_id ? 0 : 1;
  if (known) putSummary({ ...known, title: ev.title ?? known.title, model: ev.model, status, live: !ev.awaiting, updated_at: ev.ts ?? Date.now() / 1000, n_messages: known.n_messages + added });
  else void loadChats();
}

/** 對話從清單與快取拿掉；記下接著要顯示哪一筆（目前開著它的頁面據此換過去） */
function removeChat(cid: string): void {
  set((st) => {
    const i = st.list.findIndex((x) => x.id === cid);
    const next = i >= 0 ? (st.list[i + 1] ?? st.list[i - 1] ?? null) : (st.list[0] ?? null);
    const convs = { ...st.convs };
    delete convs[cid];
    const live = { ...st.live };
    delete live[cid];
    const errors = { ...st.errors };
    delete errors[cid];
    return { list: st.list.filter((x) => x.id !== cid), convs, live, errors, deleted: { ...st.deleted, [cid]: next ? next.id : null } };
  });
}

/* ---------------- 提議（1.2-M4） ---------------- */

/** 訊息上的提議要看的生成工作：先備好（重新整理後、或從清單打開較舊的聊天時，最近 30 件裡可能沒有） */
function wantGenerations(messages: (ChatMessage | null | undefined)[]): void {
  for (const m of messages) for (const p of m?.proposals ?? []) ensureGeneration(p.generation_id);
}

/** 一張提議換成新的樣子（事件或按鈕的回應，哪個先到都行；同一張不會變回舊狀態以外的東西，以最後到的為準） */
function applyProposal(cid: string, mid: string, p: ChatProposal): void {
  ensureGeneration(p.generation_id);
  set((st) => {
    const conv = st.convs[cid];
    if (!conv) return {};
    const i = conv.messages.findIndex((m) => m.id === mid);
    if (i < 0) return {};
    const m = conv.messages[i];
    const list = m.proposals ?? [];
    const j = list.findIndex((x) => x.id === p.id);
    const proposals = j < 0 ? [...list, p] : list.map((x, k) => (k === j ? p : x));
    // 1.2-M5-b：轉錄提問被自動了結（主人直接送了下一則）＝等著的那則回覆略過了（後端同時把它記成 skipped）
    const skipped = p.gate && p.state === "declined" && p.auto && m.meta?.state === "awaiting";
    const next = { ...m, proposals, ...(skipped ? { meta: { ...m.meta, state: "skipped" as const } } : {}) };
    const messages = conv.messages.map((x, k) => (k === i ? next : x));
    return { convs: { ...st.convs, [cid]: { ...conv, messages } } };
  });
}

/* ---------------- 這段聊天的生成費用（後端的合計） ---------------- */
const genCostTimers: Record<string, number> = {};
function refreshGenCost(cid: string): void {
  if (!state.convs[cid] && !state.list.some((x) => x.id === cid)) return;
  window.clearTimeout(genCostTimers[cid]);
  genCostTimers[cid] = window.setTimeout(() => {
    delete genCostTimers[cid];
    api
      .chat(cid)
      .then((d) => {
        const v = d.generation_cost_usd ?? null;
        set((st) => {
          const conv = st.convs[cid];
          return {
            ...(conv ? { convs: { ...st.convs, [cid]: { ...conv, generation_cost_usd: v } } } : {}),
            list: st.list.map((x) => (x.id === cid ? { ...x, generation_cost_usd: v } : x)),
          };
        });
      })
      .catch(() => undefined);
  }, 300);
}

/** 按「生成」：prompt／model 是卡上（可能改過）的值；params 是額外的生成參數（語音的聲音） */
export async function acceptProposal(cid: string, mid: string, pid: string, body: { prompt?: string; model?: string; params?: Record<string, unknown> }): Promise<ChatProposal> {
  wire();
  const p = await api.acceptProposal(cid, pid, body);
  applyProposal(cid, mid, p);
  return p;
}

/** 按「不用了」 */
export async function declineProposal(cid: string, mid: string, pid: string): Promise<ChatProposal> {
  wire();
  const p = await api.declineProposal(cid, pid);
  applyProposal(cid, mid, p);
  return p;
}

/* ---------------- 匯流排 ---------------- */

function onBus(e: BusEvent): void {
  switch (e.type) {
    case "chat.started": {
      const ev = e as BusChatStarted;
      applyStarted(ev.conversation_id, ev);
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
      // 先問主人（awaiting）不算結束：之後的回覆用同一個 turn_id 寫進那一則
      if (ev.state === "awaiting") awaitingTurns.add(ev.turn_id);
      else {
        doneTurns.add(ev.turn_id);
        awaitingTurns.delete(ev.turn_id);
      }
      const prev = state.live[ev.conversation_id];
      const action = turnAction[ev.turn_id];
      delete turnAction[ev.turn_id];
      set((st) => {
        const live = { ...st.live };
        if (live[ev.conversation_id]?.turn_id === ev.turn_id || ev.state !== "awaiting") delete live[ev.conversation_id];
        const errors = { ...st.errors };
        if (ev.state === "error" && ev.error) errors[ev.conversation_id] = ev.error;
        return { live, errors };
      });
      if (ev.message) {
        // 先佔好的 ‹ n/n › 接到存下的那一則上（版本數等下面重抓再以後端為準）；寫進既有那一則的沿用它的版本
        const v = prev?.turn_id === ev.turn_id && !prev.fill_id ? prev.versions : undefined;
        const msg = v ? { ...ev.message, versions: { ...v, ids: [...v.ids.slice(0, -1), ev.message.id] } } : ev.message;
        addMessage(ev.conversation_id, msg, { status: "open", live: null });
        wantGenerations([msg]);
        if ((action === "regenerate" || action === "edit") && state.convs[ev.conversation_id]) void loadChat(ev.conversation_id, true);
      } else void loadChat(ev.conversation_id, true);
      const conv = state.convs[ev.conversation_id];
      const known = state.list.find((x) => x.id === ev.conversation_id);
      if (conv) putSummary(summaryOf({ ...conv, live: null }));
      else if (known) {
        const m = ev.message;
        // 先問過主人的回覆寫進既有那一則：則數不變
        const fresh = m && !m.meta?.asked_first;
        putSummary({
          ...known,
          status: "open",
          live: false,
          updated_at: ev.ts,
          n_messages: known.n_messages + (fresh ? 1 : 0),
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
      const before = state.convs[ev.conversation_id];
      set((st) => {
        const conv = st.convs[ev.conversation_id];
        if (!conv) return {};
        const { live: _l, ...fields } = c;
        return { convs: { ...st.convs, [ev.conversation_id]: { ...conv, ...(fields as Partial<ChatDetail>) } } };
      });
      // 切換版本（可能是別的分頁）：現行那一條換了，重抓
      if (before && c.leaf_id !== undefined && !state.live[ev.conversation_id] && c.leaf_id !== (lastReal(before)?.id ?? null)) void loadChat(ev.conversation_id, true);
      const known = state.list.find((x) => x.id === ev.conversation_id);
      if (known || c.status === "archived") {
        if (known) putSummary({ ...known, ...c, live: known.live } as ChatSummary);
      } else void loadChats();
      break;
    }
    case "chat.proposal": {
      const ev = e as BusChatProposal;
      if (ev.proposal) applyProposal(ev.conversation_id, ev.message_id, ev.proposal);
      break;
    }
    case "chat.tool": {
      // 1.2-M5：回覆進行中模型用工具讀檔：「正在讀…」→ 讀完記一筆
      const ev = e as BusChatTool;
      set((st) => {
        const cur = st.live[ev.conversation_id];
        if (!cur || cur.turn_id !== ev.turn_id) return {};
        const next: ChatLive = ev.state === "running" ? { ...cur, reading: ev.read } : { ...cur, reading: null, reads: [...(cur.reads ?? []), ev.read] };
        return { live: { ...st.live, [ev.conversation_id]: next } };
      });
      break;
    }
    case "generation.finished": {
      // 從聊天來的生成或轉錄做完：表頭與左欄的「＋生成」以後端的合計為準，重抓
      const g = (e as BusGeneration).generation;
      const cid = g?.meta?.conversation_id;
      if (cid) refreshGenCost(cid);
      break;
    }
    case "chat.deleted": {
      const ev = e as BusChatDeleted;
      if (state.list.some((x) => x.id === ev.conversation_id) || state.convs[ev.conversation_id]) removeChat(ev.conversation_id);
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
  wantGenerations(d.messages);
  // 清單的摘要也跟上：新對話的標題是後端在第一則送出時才定的，回覆很快時 chat.started 可能比清單那一筆先到
  if (state.list.some((x) => x.id === id)) putSummary(summaryOf(d));
  return d;
}

/** 建立對話（可順便送第一則，可以帶圖）。回傳對話 id */
export async function createChat(body: { model?: string; system?: string; title?: string; message?: string; attachments?: OutgoingAttachment[]; params?: ChatParams }): Promise<string> {
  wire();
  const { attachments, ...rest } = body;
  const d = await api.createChat({ ...rest, ...(attachments?.length ? { attachments: attachments.map((a) => a.ref) } : {}) });
  const { turn, ...detail } = d;
  // 第一則就先問主人（1.2-M5）：那則空的回覆一起放進來
  const asked = turn?.state === "awaiting" && turn.message ? turn.message : null;
  if (asked && turn) awaitingTurns.add(turn.turn_id);
  const messages = turn ? [turn.user_message, ...(asked ? [asked] : [])] : [];
  const conv: ChatDetail = { ...(detail as ChatDetail), messages, ...totals(messages), live: state.live[d.id] ?? detail.live ?? null };
  set((st) => ({ convs: { ...st.convs, [d.id]: st.convs[d.id] ?? conv } }));
  putSummary(summaryOf({ ...conv, live: turn ? (state.live[d.id] ?? null) : null, status: turn && !asked ? "running" : conv.status }));
  return d.id;
}

export async function sendChat(id: string, text: string, opts: { model?: string; params?: ChatParams; attachments?: OutgoingAttachment[] } = {}): Promise<void> {
  wire();
  set((st) => {
    const errors = { ...st.errors };
    delete errors[id];
    return { errors };
  });
  const atts = opts.attachments ?? [];
  const conv = state.convs[id];
  if (conv) {
    const local = localMessage(id, text, atts, conv.messages);
    localIds[id] = local.id;
    addMessage(id, local);
  }
  let turn: ChatTurn;
  try {
    turn = await api.sendChat(id, { text, model: opts.model, params: opts.params, ...(atts.length ? { attachments: atts.map((a) => a.ref) } : {}) });
  } catch (e) {
    dropLocal(id);
    throw e;
  }
  // WS 的 chat.started 通常先到；沒到（WS 斷線）就自己補上（同一回合不會開兩次）
  applyStarted(id, startedOf(turn), turn.message);
}

const clearError = (id: string) =>
  set((st) => {
    const errors = { ...st.errors };
    delete errors[id];
    return { errors };
  });

/** 重新生成：mid＝要換掉的那則回覆（或要重新回答的使用者訊息）；model＝細列「下一則用」的模型（D40） */
export async function regenerateChat(id: string, mid: string, opts: { model?: string; params?: ChatParams } = {}): Promise<void> {
  wire();
  clearError(id);
  const turn = await api.regenerateChat(id, mid, { model: opts.model, params: opts.params });
  applyStarted(id, startedOf(turn), turn.message);
}

/** 編輯舊訊息再送出：從那則的位置分出新的一條。
 *  attachments：undefined＝沿用原圖（後端的規則）；陣列＝換成這些（空陣列＝拿掉）。
 *  送出當下先把現行那一條剪到那則之前、放上改寫後的一則（樂觀顯示）；失敗就還原。 */
export async function editChat(id: string, mid: string, text: string, opts: { attachments?: OutgoingAttachment[]; original?: ChatAttachment[] | null; model?: string; params?: ChatParams } = {}): Promise<void> {
  wire();
  clearError(id);
  const conv = state.convs[id];
  const prev = conv?.messages;
  if (conv) {
    const i = conv.messages.findIndex((m) => m.id === mid);
    if (i >= 0) {
      const old = conv.messages[i];
      const kept = conv.messages.slice(0, i).filter((m) => !m.local);
      const views = opts.attachments ? opts.attachments.map((a) => a.view) : (opts.original ?? old.attachments ?? []);
      const local = localMessage(id, text, [], kept);
      local.attachments = views.length ? views : null;
      local.parent_id = old.parent_id ?? null;
      local.versions = versionsPlusOne(old.versions, old.id, local.id);
      localIds[id] = local.id;
      set((st) => ({ convs: { ...st.convs, [id]: { ...conv, messages: [...kept, local] } } }));
    }
  }
  let turn: ChatTurn;
  try {
    turn = await api.editChat(id, mid, { text, model: opts.model, params: opts.params, ...(opts.attachments ? { attachments: opts.attachments.map((a) => a.ref) } : {}) });
  } catch (e) {
    delete localIds[id];
    const cur = state.convs[id];
    if (cur && prev) set((st) => ({ convs: { ...st.convs, [id]: { ...cur, messages: prev } } }));
    throw e;
  }
  applyStarted(id, startedOf(turn), turn.message);
}

/** 換到另一個版本（mid＝要換過去的那一則）：後端把現行那一條換成它底下最新的分支，回傳的詳情為準 */
export async function switchChat(id: string, mid: string): Promise<ChatDetail> {
  wire();
  const d = await api.switchChat(id, mid);
  set((st) => {
    const live = { ...st.live };
    if (!d.live) delete live[id];
    return { convs: { ...st.convs, [id]: d }, live };
  });
  wantGenerations(d.messages);
  return d;
}

/** 刪除聊天（真的刪，D37）：清單與快取拿掉，記下接著顯示哪一筆 */
export async function deleteChat(id: string): Promise<void> {
  wire();
  await api.deleteChat(id);
  removeChat(id);
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
/** 這段對話被刪了：undefined＝沒有；否則是接著要顯示的那一筆（null＝沒有下一筆） */
export const selectChatDeleted = (id: string | null | undefined) => (s: ChatState): string | null | undefined => (id && id in s.deleted ? s.deleted[id] : undefined);
