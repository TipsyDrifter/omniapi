/* daemon 的資料形狀（以 2026-09-28 實機 /api 回應為準；後端定義在 mcp/omniapi_mcp/harness/events.py 與 store/db.py） */

export type Harness = "claude" | "codex" | "gemini";
export type RunState = "starting" | "running" | "done" | "error" | "cancelled" | "dead";
export const TERMINAL_STATES: readonly RunState[] = ["done", "error", "cancelled", "dead"];

export type EventType =
  | "session_start"
  | "text"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "status"
  | "result"
  | "error";

/** GET /api/runs 的一列（也是 /api/runs/{id} 去掉 events 的部分） */
export interface Run {
  id: string;
  conversation_id: string | null;
  title: string | null;
  prompt: string | null;
  harness: Harness | string;
  model: string | null;
  cwd: string | null;
  state: RunState;
  started_at: number;
  ended_at: number | null;
  turns: number;
  context_tokens: number | null;
  /** harness 自報費用；null＝未回報（Codex 不回報），不當 0 */
  cost_usd: number | null;
  pid: number | null;
  session_id: string | null;
  dispatcher: string | null;
  result: string | null;
  error: string | null;
  meta: RunMeta | null;
  /** daemon 目前還握著這個 run 的 task */
  live: boolean;
}

export interface RunMeta {
  requested_model?: string;
  provider?: string;
  endpoint?: string;
  yolo?: boolean;
  search?: boolean;
  max_turns?: number | null;
  resume_run_id?: string | null;
  resume_session_id?: string | null;
  [k: string]: unknown;
}

/** events 表的一列（/api/runs/{id}?after=N 的 events[]） */
export interface EventRow {
  id: number;
  run_id: string;
  ts: number;
  type: EventType | string;
  payload: EventPayload;
}

export interface EventPayload {
  // session_start
  session_id?: string;
  tools?: string[];
  mcp_servers?: string[];
  model?: string;
  // text / thinking
  text?: string;
  // tool_call / tool_result
  id?: string;
  name?: string;
  input?: Record<string, unknown> | string | null;
  output?: string;
  is_error?: boolean;
  // status / error
  message?: string;
  data?: unknown;
  // result
  usage?: { prompt_tokens?: number; completion_tokens?: number; [k: string]: unknown };
  cost_usd?: number | null;
  num_turns?: number;
  stderr?: string;
  errors?: unknown;
  [k: string]: unknown;
}

export interface RunDetail extends Run {
  events: EventRow[];
}

/** GET /api/costs */
export interface Costs {
  days: number;
  /** 聊天・生成帳（calls 表；畫面上 2026-09-30 前叫「工具呼叫帳」） */
  total: { cost: number; n: number };
  by_model: { model: string; n: number; cost: number }[];
  by_day: { day: string; n: number; cost: number }[];
  /** 派工帳（runs 表）：M4 後端加入；舊 daemon 沒有這欄，前端要能退回用 runs 自己算 */
  runs?: RunsLedger;
}

export interface RunsLedger {
  total: { cost: number; n: number; unreported: number };
  by_harness: { harness: string; n: number; cost: number | null; unreported: number }[];
  by_day: { day: string; n: number; cost: number }[];
}

/** GET /api/status */
export interface Status {
  version: string;
  pid: number;
  mode: string;
  started_at: number;
  uptime_s: number;
  host: string;
  port: number;
  providers: { configured: string[]; text: string[]; image: string[] };
  discovery: Record<string, { models: number; fetched_at: number | null; from_cache: boolean; error: string | null }>;
  tiers: Record<string, string>;
  store: { path: string; calls: number; conversations: number; messages: number; runs: number; events: number };
  bus_subscribers: number;
  live_runs: number;
  live_chats?: number;
  /** 開發沙盒（OMNIAPI_DEV=1）：有 echo 模型與重播 harness */
  dev?: boolean;
  /** 離線（OMNIAPI_OFFLINE=1）：拒絕所有真供應商呼叫 */
  offline?: boolean;
  data_home: string;
}

/* ---------------- 事件匯流排（/ws 與 /api/events 同一種形狀） ---------------- */

export interface BusBase {
  seq: number;
  ts: number;
}
export interface BusRunStarted extends BusBase {
  type: "run.started";
  run_id: string;
  title: string | null;
  harness: string;
  model: string | null;
  cwd: string | null;
}
export interface BusRunEvent extends BusBase {
  type: "run.event";
  run_id: string;
  event_id: number;
  event: { type: EventType | string; ts: number; payload: EventPayload };
  summary: string | null;
}
export interface BusRunFinished extends BusBase {
  type: "run.finished";
  run_id: string;
  state: RunState;
  cost_usd: number | null;
  error: string | null;
}
export interface BusCall extends BusBase {
  type: "call.started" | "call.finished";
  [k: string]: unknown;
}
export interface BusOther extends BusBase {
  type: string;
  [k: string]: unknown;
}
export type BusEvent = BusRunStarted | BusRunEvent | BusRunFinished | BusCall | BusChatStarted | BusChatDelta | BusChatFinished | BusChatUpdated | BusOther;

/** /ws 額外會送的握手與心跳 */
export interface WsHello { type: "hello"; version: string; ts: number }
export interface WsPing { type: "ping"; ts: number }
export type WsMessage = WsHello | WsPing | BusEvent;

/* ---------------- M5：派工與續接 ---------------- */

/** GET /api/models?modality=text 的一筆 */
export interface ModelEntry {
  id: string;
  provider: string;
  modality: string;
  name?: string;
  /** current＝人工維護的名單；discovered＝各家 /models 即時抓到、沒有定價的 */
  status: "current" | "discovered" | "deprecated" | "retired" | string;
  online?: boolean | null;
  /** 預設走哪個 harness；沒有＝不能當 agent */
  harness?: Harness | string | null;
  /** USD / 百萬 token */
  pricing?: { input?: number; output?: number; cached_input?: number; note?: string; [k: string]: unknown } | null;
  capabilities?: { tools?: boolean; reasoning?: boolean; vision?: boolean; [k: string]: unknown } | null;
  context?: number | null;
  aliases?: string[];
  note?: string;
  [k: string]: unknown;
}

export interface ModelsResponse {
  updated?: string;
  /** 等級別名 → 模型 id（cheap／standard／strong） */
  tiers: Record<string, string>;
  models: Record<string, ModelEntry[]> | ModelEntry[];
  counts?: Record<string, number>;
}

/** GET /api/harnesses */
export interface HarnessInfo {
  name: string;
  available: boolean;
  resume: boolean;
  cli?: boolean;
  /** claude harness 的端點：anthropic（訂閱）／anthropic-api（計費）／deepseek／openrouter */
  endpoints?: Record<string, boolean>;
}
export type HarnessesResponse = Record<string, HarnessInfo>;

/** GET /api/cwds */
export interface CwdEntry {
  cwd: string;
  n: number;
  last_used: number;
  exists: boolean;
}

/** GET /api/fs/dirs?path= */
export interface FsDirs {
  path: string;
  parent: string | null;
  exists: boolean;
  /** 完整路徑 */
  dirs: string[];
  home: string;
}

/** GET /api/runs/{id}/thread：追問串，root 在前，不含事件 */
export interface Thread {
  run_id: string;
  root_id: string;
  leaf_id: string;
  runs: Run[];
  /** 最後一筆能不能追問 */
  resumable: boolean;
  reason: string | null;
}

/* ---------------- M6：聊天 ---------------- */

export type ChatTurnState = "done" | "cancelled" | "error";

/** messages 表的一列 */
export interface ChatMessage {
  id: string;
  conversation_id: string;
  seq: number;
  role: "user" | "assistant" | "system" | "tool" | string;
  /** 聊天裡一律是純文字（markdown） */
  content: string;
  created_at: number;
  /** 回覆是哪個模型答的（中途換模型後每則不同） */
  model: string | null;
  usage: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number; [k: string]: unknown } | null;
  /** null＝沒有定價可算，不當 0 */
  cost_usd: number | null;
  /** 思考內容（模型有給才有） */
  reasoning: string | null;
  meta: {
    state?: ChatTurnState;
    finish_reason?: string | null;
    provider?: string | null;
    requested_model?: string;
    duration_ms?: number;
    turn_id?: string;
    error?: string;
  } | null;
}

/** 回覆進行中的暫存（daemon 記憶體裡的；頁面中途打開時用它補上已經到的字） */
export interface ChatLive {
  turn_id: string;
  /** 使用者選的（可能是等級別名 cheap／standard／strong） */
  model: string;
  /** 實際對到的模型 id */
  resolved_model: string;
  started_at: number;
  text: string;
  reasoning: string;
}

/** conversations 表的一列＋統計（GET /api/chat 的一筆） */
export interface ChatSummary {
  id: string;
  kind: "chat";
  title: string | null;
  created_at: number;
  updated_at: number;
  /** 最後一次用的模型（或別名） */
  model: string | null;
  /** open | running | archived */
  status: string | null;
  system_prompt: string | null;
  meta: { source?: string; [k: string]: unknown } | null;
  n_messages: number;
  cost_usd: number;
  /** 有算出費用的回覆數（合計 $0.0000 要有 priced>0 才代表「真的免費」） */
  priced: number;
  /** 有內容但沒有定價的回覆數 */
  unpriced: number;
  /** 清單裡是 boolean；單筆是 ChatLive | null */
  live: boolean | ChatLive | null;
}

/** GET /api/chat/{id} */
export interface ChatDetail extends Omit<ChatSummary, "live"> {
  messages: ChatMessage[];
  live: ChatLive | null;
}

/** POST /api/chat/{id}/messages 的回應（不等回覆） */
export interface ChatTurn {
  conversation_id: string;
  turn_id: string;
  state: "streaming" | ChatTurnState;
  model: string;
  resolved_model: string;
  user_message: ChatMessage;
  /** ?wait=true 才有 */
  message?: ChatMessage | null;
}

export interface ChatParams {
  temperature?: number;
  reasoning_effort?: "none" | "low" | "medium" | "high" | "xhigh";
  max_completion_tokens?: number;
}

export interface BusChatStarted extends BusBase {
  type: "chat.started";
  conversation_id: string;
  turn_id: string;
  model: string;
  resolved_model: string;
  user_message: ChatMessage;
  title: string | null;
}
export interface BusChatDelta extends BusBase {
  type: "chat.delta";
  conversation_id: string;
  turn_id: string;
  kind: "text" | "reasoning";
  delta: string;
}
export interface BusChatFinished extends BusBase {
  type: "chat.finished";
  conversation_id: string;
  turn_id: string;
  state: ChatTurnState;
  message: ChatMessage | null;
  error: string | null;
}
export interface BusChatUpdated extends BusBase {
  type: "chat.updated";
  conversation_id: string;
  conversation: Partial<ChatSummary>;
}

/** POST /api/runs 的回應 */
export interface StartRunResponse {
  run_id: string;
  state: RunState;
  title: string | null;
  harness: string;
  model: string | null;
  provider?: string | null;
  endpoint?: string | null;
  cwd: string | null;
}

/** POST /api/runs */
export interface RunSpecInput {
  prompt: string;
  model?: string;
  /** "replay" 只有測試 daemon（OMNIAPI_DEV=1）才收 */
  harness?: Harness | "replay";
  cwd?: string;
  title?: string;
  yolo?: boolean;
  search?: boolean;
  max_turns?: number;
  resume_run_id?: string;
  dispatcher?: string;
  system_append?: string;
  auth?: "subscription" | "api";
}
