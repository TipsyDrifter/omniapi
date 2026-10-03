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
export type BusEvent = BusRunStarted | BusRunEvent | BusRunFinished | BusCall | BusChatStarted | BusChatDelta | BusChatFinished | BusChatUpdated | BusChatDeleted | BusChatProposal | BusChatTool | BusGeneration | BusArtifactCreated | BusOther;

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
  /** current／deprecated＝整理過的名單（deprecated＝官方已公告關閉日）；discovered＝各家 /models 即時抓到、還沒整理的 */
  status: "current" | "discovered" | "deprecated" | "retired" | string;
  /** 官方公告的關閉日 YYYY-MM-DD */
  shutdown?: string;
  replacement?: string;
  online?: boolean | null;
  /** 預設走哪個 harness；沒有＝不能當 agent */
  harness?: Harness | string | null;
  /** USD / 百萬 token */
  pricing?: { input?: number; output?: number; cached_input?: number; note?: string; [k: string]: unknown } | null;
  /** 1.2-M5：pdf／audio＝原樣收 PDF／音訊（保證是布林） */
  capabilities?: { tools?: boolean; reasoning?: boolean; vision?: boolean; pdf?: boolean; audio?: boolean; [k: string]: unknown } | null;
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
  /** 廠商 id → 顯示名稱等設定；物件的順序就是清單分組的順序 */
  providers?: Record<string, { label?: string; [k: string]: unknown }>;
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

/** awaiting＝回覆前先問主人（轉錄提問，1.2-M5-a）；skipped＝主人沒回答就送了下一則，這則不再回覆（1.2-M5-b） */
export type ChatTurnState = "done" | "cancelled" | "error" | "awaiting" | "skipped";

export type AttachKind = "image" | "audio" | "file";

/** 送出時帶的附件：只能是上傳檔或作品的編號（不能送路徑；個數上限看 /api/uploads/limits） */
export type ChatAttachmentRef = ({ upload_id: string; artifact_id?: undefined } | { artifact_id: string; upload_id?: undefined }) & { kind?: AttachKind };

/** 1.2-M5：檔案抽取的結果（上傳時就有；後端 chat/files.py 的 public_info） */
export interface FileInfo {
  type: string;
  /** 給人看的類型：PDF、Excel、CSV、音檔、二進位檔… */
  type_name?: string;
  readable?: boolean;
  /** binary｜encrypted｜corrupt｜no_text｜empty｜too_large｜timeout｜unsupported｜missing… */
  reason?: string;
  reason_text?: string;
  chars?: number;
  pages?: number;
  slides?: number;
  rows?: number;
  cols?: number;
  sheets?: { name: string; rows: number; cols?: number }[];
  lines?: number;
  truncated?: boolean;
  /** 音檔 */
  format?: string | null;
  duration_s?: number;
  [k: string]: unknown;
}

/** 訊息上的附件（1.2-M1：後端補上給頁面看的名稱與網址，不給路徑；1.2-M5：檔案與音檔多 mime／bytes／info／download_url） */
export interface ChatAttachment {
  kind: AttachKind | string;
  upload_id?: string;
  artifact_id?: string;
  name: string | null;
  file_url: string;
  /** 作品才有縮圖；上傳檔是 null */
  thumb_url: string | null;
  /** 檔案還在不在 */
  exists: boolean;
  mime?: string | null;
  bytes?: number | null;
  info?: FileInfo | null;
  download_url?: string;
  /** 音檔：轉好的逐字稿（作品） */
  transcript?: { artifact_id: string; chars: number; file_url: string } | null;
}

/** 1.2-M5：回覆的 meta.files 一筆＝這個檔這次怎麼送給模型 */
export interface FileDelivery {
  id: string;
  name: string;
  kind: AttachKind | string;
  type: string;
  /** 這個檔在哪一則使用者訊息上 */
  message_id?: string;
  mode: "native" | "text" | "excerpt" | "unreadable" | string;
  /** native：document｜file｜input_audio */
  format?: string;
  audio_format?: string;
  pages?: number;
  chars?: number;
  sent_chars?: number;
  rows?: number;
  sent_rows?: number;
  /** excerpt：其餘由模型用工具讀 */
  tools?: boolean;
  truncated?: boolean;
  transcribed?: boolean;
  transcript_id?: string;
  reason?: string;
  reason_text?: string;
}

/** 1.2-M5：模型用工具讀檔的一步（what 是後端寫好的中文範圍，如「第 3–7 頁」） */
export interface FileRead {
  round?: number;
  tool: string;
  file_id?: string | null;
  name?: string | null;
  what?: string;
  chars?: number;
  error?: boolean;
}

/** 這一則在同一個 parent 底下的第幾個版本（index 從 1 起算）：‹ index/count › 的版本切換 */
export interface ChatVersions {
  count: number;
  index: number;
  ids: string[];
}

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
    /* 1.2-M2：這一則回覆所在的分支有圖時才有 */
    /** 答的模型會不會看圖 */
    vision?: boolean;
    images_sent?: number;
    /** 沒送出的：模型不看圖（vision=false），或超過一次請求的大小上限（vision=true） */
    images_skipped?: number;
    /** 檔案已不在或讀不出來 */
    images_missing?: number;
    /* 1.2-M5：這一則回覆所在的分支有檔案時才有（images_* 照舊只算圖片） */
    files?: FileDelivery[];
    files_native?: number;
    files_text?: number;
    files_excerpt?: number;
    files_unreadable?: number;
    files_missing?: number;
    /** 這一回合有沒有給模型讀檔工具 */
    files_tools?: boolean;
    /** 模型用工具讀檔的紀錄 */
    reads?: FileRead[];
    tool_rounds?: number;
    model_calls?: number;
    read_chars?: number;
    /** 這則回覆是先問過主人（轉錄）才回的 */
    asked_first?: boolean;
  } | null;
  /** 使用者訊息的圖；沒有＝null */
  attachments?: ChatAttachment[] | null;
  /** 1.2-M4：模型這一則提出的工具呼叫（OpenAI 形狀；arguments 是 JSON 字串）——提議的原始值看這裡 */
  tool_calls?: ChatToolCall[] | null;
  /** 1.2-M4：這一則回覆附的生成提議（只有回覆才有） */
  proposals?: ChatProposal[];
  /** 上一則（分岔用）；舊資料升級時照順序串起來 */
  parent_id?: string | null;
  /** 只有 GET /api/chat/{id} 的訊息才有 */
  versions?: ChatVersions;
  /** 前端自己加的：送出當下先顯示、還沒拿到後端編號的那一則 */
  local?: boolean;
}

export interface ChatToolCall {
  id: string;
  type?: string;
  function: { name: string; arguments: string | Record<string, unknown> };
}

/** 提議的狀態：pending 提議中｜generating 生成中｜done｜declined（auto＝送了下一則而略過）｜failed｜cancelled */
export type ProposalState = "pending" | "generating" | "done" | "declined" | "failed" | "cancelled";

/** 1.2-M4：回覆上的一張生成提議。prompt／model 在按下生成之後換成實際用的值，模型原本提議的在 proposed。
 *  1.2-M5：轉錄提問也是這個形狀（kind: transcript、gate: true、file、duration_s）。 */
export interface ChatProposal {
  /** ＝tool_call_id */
  id: string;
  kind: "image" | "speech" | "transcript" | string;
  prompt: string;
  model: string | null;
  state: ProposalState;
  voice?: string;
  note?: string;
  /** 模型原本提議的值（按下生成之後外層換成實際用的值，這裡不變） */
  proposed?: { prompt?: string; model?: string; voice?: string };
  /** 轉錄提問：回覆前先問主人的那一張 */
  gate?: boolean;
  file?: { id: string; name: string; kind: string; bytes?: number | null; duration_s?: number | null };
  duration_s?: number;
  generation_id?: string;
  artifact?: { id: string; kind: string; name: string | null; file_url: string; thumb_url: string | null; chars?: number };
  error?: string;
  error_kind?: GenErrorKind | string;
  /** 不是主人按的「不用了」，而是送了下一則（或重新生成、編輯）時自動了結的 */
  auto?: boolean;
  /** 按了幾次生成（失敗、中止、不用了之後可以再按） */
  attempts?: number;
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
  /** chat.started 帶來的：這次答的模型會不會看圖、因此略過幾張（送出當下的數字） */
  vision?: boolean;
  images_skipped?: number;
  /** 這次回覆接在哪一則使用者訊息後面（GET 的 live 與 chat.started 都有） */
  parent_id?: string | null;
  action?: ChatAction;
  /** 前端自己算的：新的這一版在兄弟裡是第幾版（重新生成時先佔好 ‹ n/n › 的位置） */
  versions?: ChatVersions;
  /** 1.2-M5：先問過主人的回覆寫進既有的那一則（訊息 id） */
  fill_id?: string | null;
  /** 1.2-M5：這次回覆已經讀過的、正在讀的 */
  reads?: FileRead[];
  reading?: FileRead | null;
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
  /** 從這段聊天來的生成與轉錄的實際費用合計（所有分支；後端算的）；null＝沒有回報費用的生成 */
  generation_cost_usd?: number | null;
  /** 清單裡是 boolean；單筆是 ChatLive | null */
  live: boolean | ChatLive | null;
}

/** GET /api/chat/{id} */
export interface ChatDetail extends Omit<ChatSummary, "live"> {
  /** 只有現行路徑的訊息 */
  messages: ChatMessage[];
  live: ChatLive | null;
  /** 現行分支的最後一則 */
  leaf_id?: string | null;
}

export type ChatAction = "send" | "regenerate" | "edit";

/** 送出類的回應（「回合」，不等回覆） */
export interface ChatTurn {
  conversation_id: string;
  turn_id: string;
  /** awaiting＝回覆前先問主人：message 是那則空的回覆（帶轉錄提問） */
  state: "streaming" | ChatTurnState;
  action?: ChatAction;
  /** 這次回覆接在哪一則後面 */
  parent_id?: string | null;
  model: string;
  resolved_model: string;
  user_message: ChatMessage;
  /** 答的模型會不會看圖 */
  vision?: boolean;
  /** 送出當下只算「模型不看圖」略過的張數；?wait=true 才是最終數字 */
  images_skipped?: number;
  /** 重新生成：被換掉的那則回覆（對使用者訊息重新生成時是 null） */
  regenerate_of?: string | null;
  /** 編輯：原本那則使用者訊息 */
  edit_of?: string | null;
  /** ?wait=true 才有 */
  message?: ChatMessage | null;
  /** 1.2-M4：這一回合有沒有給模型工具（會不會出現提議） */
  tools?: boolean;
}

/** DELETE /api/chat/{id} */
export interface ChatDeleted {
  conversation_id: string;
  deleted: boolean;
  /** 刪掉的訊息數（含所有版本） */
  messages: number;
  /** 刪除前先停掉了進行中的回覆 */
  cancelled: boolean;
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
  action?: ChatAction;
  parent_id?: string | null;
  regenerate_of?: string | null;
  edit_of?: string | null;
  vision?: boolean;
  images_skipped?: number;
  /** 1.2-M5：回覆前先問主人（沒有回覆在跑，接著來的 chat.finished 帶那則空的回覆） */
  awaiting?: boolean;
  /** 1.2-M5：回覆寫進既有的那一則（先問過主人的） */
  fill_id?: string;
}
/** 1.2-M5：回覆進行中模型用工具讀檔（running → done） */
export interface BusChatTool extends BusBase {
  type: "chat.tool";
  conversation_id: string;
  turn_id: string;
  round: number;
  tool_call_id: string;
  tool: string;
  state: "running" | "done";
  read: FileRead;
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
  /** 切換版本時多 leaf_id（與統計）；改名、換模型、封存時是對話那一列 */
  conversation: Partial<ChatSummary> & { leaf_id?: string | null };
}
export interface BusChatDeleted extends BusBase {
  type: "chat.deleted";
  conversation_id: string;
}
/** 1.2-M4：提議出現（created）或狀態變了（updated：按了生成、不用了、略過、做好、失敗、中止） */
export interface BusChatProposal extends BusBase {
  type: "chat.proposal";
  conversation_id: string;
  message_id: string;
  tool_call_id: string;
  action: "created" | "updated" | string;
  proposal: ChatProposal;
}

/* ---------------- 1.1-M3：生成頁（後端 mcp/omniapi_mcp/generate/） ---------------- */

/** 生成的四種模態（＝/make/:kind） */
export type GenKind = "image" | "speech" | "music" | "transcript";
/** 作品的種類（多一種歌詞） */
export type ArtifactKind = "image" | "speech" | "music" | "transcript" | "lyrics";

/** GET /api/generate/options 的一個模型：ModelEntry＋能不能叫 */
export interface GenModel {
  id: string;
  provider: string;
  modality?: string;
  name?: string;
  /** current／deprecated（已公告關閉日）／discovered（還沒整理） */
  status: string;
  shutdown?: string;
  replacement?: string;
  note?: string;
  available: boolean;
  unavailable?: { reason: "missing_key" | "not_implemented" | "offline" | string; env?: string };
  /** 生成模型的定價：unit 決定其他欄位（per_image／per_1m_tokens／per_1k_chars／per_1m_chars／per_minute／per_song／credits） */
  pricing?: { unit?: string; [k: string]: unknown } | null;
  [k: string]: unknown;
}

export interface ImageCaps {
  provider: string;
  sizes: string[];
  qualities: string[];
  formats: string[];
  max_images: number;
  supports_background: boolean;
  [k: string]: unknown;
}

export interface Voice {
  id: string;
  name: string;
  note?: string | null;
  /** 有值＝只有這些模型能用 */
  only?: string[] | null;
  preview_url?: string | null;
}
export interface VoiceSet {
  default: string;
  voices: Voice[];
  /** ElevenLabs：清單是現查的 */
  live?: boolean;
  /** 現查失敗的原因（仍可用預設聲音） */
  error?: string;
}

export interface GenKindOptions {
  models: GenModel[];
  default_model: string | null;
}
export interface GenOptions {
  offline: boolean;
  /** 離線開發沙盒：不呼叫供應商、不花錢、每個模型都可選 */
  sandbox: boolean;
  kinds: {
    image: GenKindOptions & { capabilities: Record<string, ImageCaps> };
    speech: GenKindOptions & { voices: Record<string, VoiceSet> };
    music: GenKindOptions;
    transcript: GenKindOptions;
  };
}

export type EstimateBasis =
  | "per_image"
  | "per_1k_chars"
  | "per_minute"
  | "per_song"
  | "history"
  | "history_model"
  | "history_chars"
  | "credits"
  | "needs_length"
  | "unknown"
  | "sandbox";

/** POST /api/generate/estimate：basis 決定其他欄位 */
export interface Estimate {
  basis: EstimateBasis | string;
  usd: number | null;
  unit_price?: number;
  n?: number;
  tier?: string;
  chars?: number;
  seconds?: number;
  low?: number;
  high?: number;
  samples?: number;
  credit_usd?: number;
  unit?: string;
  [k: string]: unknown;
}

/** 來源只能是作品或上傳檔的 id（不能送路徑） */
export type SourceRef = { artifact_id: string; upload_id?: undefined } | { upload_id: string; artifact_id?: undefined };
export type SourceView = SourceRef & { name: string | null; file_url: string; thumb_url: string | null };
export interface GenSources {
  images?: SourceRef[];
  audio?: SourceRef;
}

export type GenStatus = "running" | "done" | "error" | "cancelled" | "interrupted";
export type GenErrorKind = "quota" | "auth" | "rejected" | "timeout" | "too_large" | "unavailable" | "offline" | "interrupted" | "invalid" | "other";

/** 作品（artifacts 表的一列，去掉路徑） */
export interface Artifact {
  id: string;
  created_at: number;
  kind: ArtifactKind | string;
  tool: string | null;
  model: string | null;
  provider?: string | null;
  title: string | null;
  prompt: string | null;
  params: Record<string, unknown> | null;
  mime: string | null;
  bytes: number | null;
  width: number | null;
  height: number | null;
  duration_s: number | null;
  /** 逐字稿／歌詞的文字（清單裡超過 400 字會截成預覽） */
  text: string | null;
  cost_usd: number | null;
  source: string | null;
  parent_id: string | null;
  file_url: string;
  thumb_url: string | null;
  exists: boolean;
  /** 後端有帶，但頁面不顯示路徑 */
  file_path?: string;
  [k: string]: unknown;
}

export interface Generation {
  id: string;
  created_at: number;
  finished_at: number | null;
  kind: GenKind;
  tool: string;
  model: string | null;
  title: string | null;
  params: Record<string, unknown>;
  sources: { images?: SourceView[]; audio?: SourceView };
  status: GenStatus;
  error: string | null;
  error_kind: GenErrorKind | null;
  estimate: Estimate | null;
  cost_usd: number | null;
  artifacts: Artifact[];
  source?: string;
  /** 1.2-M4：聊天來的生成記著是哪段聊天、哪一則、哪張提議 */
  meta?: { conversation_id?: string; message_id?: string; tool_call_id?: string; [k: string]: unknown } | null;
  [k: string]: unknown;
}

export interface GenRequest {
  kind: GenKind;
  params: Record<string, unknown>;
  sources?: GenSources;
  duration_s?: number;
}

/** POST /api/uploads?filename=（生成頁只收圖與音檔；purpose=chat 什麼都收，1.2-M5） */
export interface Upload {
  id: string;
  created_at?: number;
  kind: AttachKind;
  filename: string;
  bytes: number;
  mime: string;
  /** 1.2-M1：用編號取檔（/api/uploads/{id}/file），不給路徑 */
  file_url?: string;
  download_url?: string;
  /** 1.2-M5：聊天的上傳才有；抽取超過幾秒時先是 null、info_pending=true，之後用 GET /api/uploads/{id} 補拿 */
  info?: FileInfo | null;
  info_pending?: boolean;
}

/** GET /api/uploads/limits（不寫死在前端） */
export interface UploadLimits {
  max_bytes: Record<AttachKind, number>;
  max_attachments: number;
}

/** GET /api/artifacts */
export interface ArtifactList {
  items: Artifact[];
  /** 整面牆各模態的件數（不含已移除） */
  counts: Record<string, number>;
  /** 1.1-M4：套用目前篩選（kind 除外）後各模態的件數；舊 daemon 沒有 */
  matching?: Record<string, number>;
  next_before: number | null;
}

/* ---------------- 1.1-M4：作品牆 ---------------- */

/** 牆的篩選（送給 GET /api/artifacts 與 GET /api/artifacts/{id}） */
export interface WallQuery {
  /** 逗號分隔，如 music,lyrics */
  kind?: string;
  /** `-`＝沒有模型的作品 */
  model?: string;
  source?: string;
  q?: string;
  since?: number;
  until?: number;
  only_hidden?: boolean;
}

/** GET /api/artifacts/facets */
export interface ArtifactFacets {
  models: { value: string | null; n: number }[];
  sources: { value: string; n: number }[];
  /** 已移除幾件 */
  hidden: number;
  oldest: number | null;
  newest: number | null;
}

/** GET /api/artifacts/{id}：完整一件＋來源與衍生＋目前篩選下的鄰居 */
export interface ArtifactDetail extends Artifact {
  hidden?: boolean;
  parent: Artifact | null;
  children: Artifact[];
  /** 比它新的那一件（燈箱的 ←）；null＝到頭了 */
  newer: string | null;
  /** 比它舊的那一件（燈箱的 →） */
  older: string | null;
}

/** GET /api/calls 的一列（聊天・生成帳的原始紀錄） */
export interface CallRow {
  id: string;
  ts: number;
  tool: string;
  status: string;
  duration_ms: number | null;
  model: string | null;
  provider: string | null;
  cost_usd: number | null;
  error: string | null;
  source: string | null;
  /** 1.1-M4：這筆呼叫做出來的作品 */
  artifact_ids?: string[];
  [k: string]: unknown;
}

export interface BusArtifactCreated extends BusBase {
  type: "artifact.created";
  /** 不含 text／params／meta */
  artifact: Artifact;
}

export interface BusGeneration extends BusBase {
  type: "generation.started" | "generation.finished";
  generation: Generation;
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
