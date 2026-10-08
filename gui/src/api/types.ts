/* daemon 的資料形狀（以 2026-09-28 實機 /api 回應為準；後端定義在 mcp/omniapi_mcp/harness/events.py 與 store/db.py） */
import type { ArtifactKind, GenKind } from "@/lib/modalities";
export type { ArtifactKind, GenKind } from "@/lib/modalities";

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
  /** 1.3-M4：服務資訊（設定頁第五段） */
  service?: ServiceInfo;
  storage?: string;
  /** 1.3-M6：桌面版每天一次的新版檢查（殼寫檔、服務讀出來）；沒有桌面版＝null */
  desktop_update?: DesktopUpdate | null;
}

/** /api/desktop/autostart：桌面版的「開機時啟動」（跟系統匣選單的勾是同一個登錄值） */
export interface DesktopAutostart {
  /** 這個服務是桌面版起的（從 repo 或 zip 跑的服務＝false，設定頁不出現這一列） */
  available: boolean;
  /** 系統匣的勾現在是不是勾著 */
  enabled: boolean;
  /** 以前用 omni autostart 設的舊開機啟動還在時的一句說明 */
  legacy: string | null;
  /** 登錄值指向別的 OmniAPI.exe 時，那一行指令 */
  other: string | null;
}

export interface DesktopUpdate {
  /** 殼的設定關掉了新版檢查 */
  enabled: boolean;
  /** 正在跑的版本 */
  current: string;
  /** 公開 repo 最新的 Release（還沒查到過＝null） */
  latest: string | null;
  /** latest 比 current 新，而且有下載頁網址 */
  newer: boolean;
  /** Release 頁 */
  url: string | null;
  checked_at_ms: number | null;
  attempted_at_ms: number | null;
  error: string | null;
}

export interface ServiceInfo {
  version: string;
  layout: string;
  data_home: string;
  storage: string;
  storage_env: string;
  env_file: string | null;
  env_file_suggested: string | null;
  settings_file: string;
  logs: string;
}

/* ---------------- 1.3-M4 設定頁（GET/PATCH /api/settings）：任何情況都沒有完整的 key ---------------- */

export type Slot = "openai" | "anthropic" | "gemini" | "deepseek" | "openrouter" | "elevenlabs" | "kie";
export const SLOTS: readonly Slot[] = ["openai", "anthropic", "gemini", "deepseek", "openrouter", "elevenlabs", "kie"];

/** 一把 key 的連線狀況（最近一次列模型或測試）；沒有 key 時整個是 null */
export interface ProviderHealth {
  state: "ok" | "failed" | "untested";
  ok: boolean | null;
  source: "discovery" | "test" | null;
  checked_at: number | null;
  listed: number | null;
  online_models: number | null;
  reason: string | null;
  status: number | null;
  message: string | null;
  last_ok_at: number | null;
  credits?: number | null;
}

export interface GetKey {
  url: string | null;
  docs: string | null;
  note: string | null;
}

export type TierMap = { cheap: string; standard: string; strong: string };

export interface SettingsProvider {
  provider: string;
  label: string;
  key: {
    set: boolean;
    last4: string | null;
    source: "settings" | "env" | null;
    /** 設定頁的 key 蓋過的 .env 那把（拿掉設定頁的就回到它） */
    shadowed: { set: boolean; last4: string | null; source: "env"; where: "env_file" | "environment" } | null;
  };
  enabled: boolean;
  enabled_source: "settings" | "env" | "default";
  configured: boolean;
  health: ProviderHealth | null;
  get_key: GetKey | null;
  suggested_tiers: TierMap | null;
}

/** 設定頁的「預設模型」：聊天、派工，加上每一種生成模態 */
export type DefaultKind = "chat" | "dispatch" | GenKind;

export interface SettingsView {
  path: string;
  env_file: string | null;
  restart_required: string[];
  providers: Record<Slot, SettingsProvider>;
  tiers: Record<string, { model: string; source: "settings" | "env" | "catalog"; catalog: string }>;
  defaults: Record<DefaultKind, { model: string | null; source: "settings" | "env" | "default" }>;
  /** 1.4-M3：影片的設定（MCP／CLI 的單支上限、最長等待、不等了之後要不要繼續收） */
  video?: Record<keyof VideoSettings, { value: number | boolean; source: "settings" | "env" | "default" }>;
}

/** 1.4-M3：settings.json 的 video 一節 */
export interface VideoSettings {
  /** MCP／CLI：預估超過這個金額（美元）就不送出，除非呼叫帶 max_cost_usd */
  mcp_max_usd: number;
  /** MCP／CLI：不設上限（此時 mcp_max_usd 不看） */
  mcp_unlimited: boolean;
  /** 我們這邊最多等幾分鐘；超過標成「等太久」，保留遠端編號可以再去問一次 */
  max_wait_minutes: number;
  /** 不等了之後，背景照樣問、做好照樣收進作品牆 */
  keep_collecting: boolean;
}

export interface SettingsPatchResult extends SettingsView {
  changed: string[];
  providers_changed: string[];
  warnings: string[];
}

/** PATCH 本體：settings.json 的形狀；null＝把那一項從檔案拿掉（回到 .env／內建） */
export interface SettingsPatch {
  providers?: Partial<Record<Slot, { api_key?: string | null; enabled?: boolean | null }>>;
  tiers?: Partial<Record<string, string | null>>;
  defaults?: Partial<Record<DefaultKind, string | null>>;
  video?: Partial<{ [K in keyof VideoSettings]: VideoSettings[K] | null }>;
}

export interface TestKeyResult {
  provider: string;
  key: { set: boolean; last4: string | null };
  ok: boolean | null;
  reason?: string | null;
  status?: number | null;
  models?: number | null;
  message: string;
  simulated?: boolean;
  checked_at: number;
  credits?: number | null;
}

export interface ExternalTool {
  id: "node" | "ffmpeg" | "claude_code" | "claude_login" | "codex" | "gemini_cli" | string;
  name: string;
  found: boolean;
  path: string | null;
  version: string | null;
  installed_after_start: boolean;
  detail: string | null;
  affects: string;
  features: string[];
  install: { method: string; command: string | null; note?: string | null }[];
  source: string | null;
}

export interface ToolsResponse {
  checked_at: number;
  platform: string;
  tools: ExternalTool[];
  summary: { found: number; missing: number };
}

export interface ClaudeMcpStatus {
  path: string;
  name: string;
  state: "connected" | "other" | "missing" | "no_config" | "unreadable";
  entry: { type: string; url?: string; command?: string } | null;
  expected: { type: string; url: string };
  backup: string | null;
  message?: string;
  changed?: boolean;
  backed_up?: boolean;
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
  /** false = listed in the catalogue but not wired up in this version */
  implemented?: boolean;
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
  /** 1.4-M2：經 OpenRouter 的圖片模型＝原廠（id 前綴與名稱） */
  vendor?: string;
  vendor_label?: string;
  /** 1.4-M2：OpenRouter 圖片模型的名單寫明它收哪些參數 */
  image_params?: OrImageParams;
  /** 熱門：在 Artificial Analysis 各榜的名次（後端 catalog/popularity.py）；沒上榜就沒有這三欄 */
  popularity?: Record<string, number>;
  rank?: number;
  rank_badge?: RankBadgeInfo;
  [k: string]: unknown;
}

/** 名次徽章：最好的那個「不是猜的對應」的名次（猜的對應只拿來排序，不出徽章） */
export interface RankBadgeInfo {
  board: string;
  /** 榜名（智慧指數、文生圖、改圖…） */
  label: string;
  rank: number;
  score?: number | null;
}

/** OpenRouter 圖片模型收的參數（後端 catalog/openrouter_images.image_params） */
export interface OrImageParams {
  resolutions: string[];
  aspect_ratios: string[];
  qualities: string[];
  output_formats: string[];
  backgrounds: string[];
  /** 0＝不收參考圖（不能改圖） */
  max_references: number;
  /** >0＝沒有參考圖不能用（Recraft 的風格系列） */
  min_references: number;
  max_n: number;
  seed: boolean;
}
/** OpenRouter 名單上的一條定價（pricing.unit === "openrouter" 時的 pricing.lines） */
export interface OrPriceLine {
  billable: string;
  unit: string;
  cost_usd: number;
  variant?: string;
}

export interface ModelsResponse {
  updated?: string;
  /** 等級別名 → 模型 id（cheap／standard／strong） */
  tiers: Record<string, string>;
  models: Record<string, ModelEntry[]> | ModelEntry[];
  /** 廠商 id → 顯示名稱等設定；物件的順序就是清單分組的順序 */
  providers?: Record<string, ModelsProvider>;
  counts?: Record<string, number>;
}

/** /api/models 的 providers.<名>（目錄的供應商名：google 對到設定頁的 gemini） */
export interface ModelsProvider {
  label?: string;
  harness?: string | null;
  modalities?: string[];
  /** 設定頁的 slot 名（跟目錄名不同時才有，例如 google → gemini） */
  settings_key?: string;
  /** 1.3-M2：有 key 而且啟用 */
  configured?: boolean;
  health?: ProviderHealth | null;
  get_key?: GetKey | null;
  suggested_tiers?: TierMap | null;
  suggested_tiers_note?: string;
  [k: string]: unknown;
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

/* GenKind（生成的模態，＝/make/:kind）與 ArtifactKind（作品的種類，多一種歌詞）定義在 lib/modalities.ts，檔頭轉出 */

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
  /** 生成模型的定價：unit 決定其他欄位（per_image／per_1m_tokens／per_1k_chars／per_1m_chars／per_minute／per_song／credits／openrouter） */
  pricing?: { unit?: string; [k: string]: unknown } | null;
  /** 1.4-M2：經 OpenRouter 的圖片模型 */
  vendor?: string;
  vendor_label?: string;
  image_params?: OrImageParams;
  /** 熱門（同 ModelEntry） */
  popularity?: Record<string, number>;
  rank?: number;
  rank_badge?: RankBadgeInfo;
  [k: string]: unknown;
}

export interface ImageCaps {
  provider: string;
  sizes: string[];
  qualities: string[];
  formats: string[];
  max_images: number;
  supports_background: boolean;
  /** 1.4-M2：provider === "openrouter" 時，模型自己宣告的參數（第三種表單形狀） */
  or?: OrImageParams;
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
  kinds: { [K in GenKind]: GenKindOptions & GenKindExtras[K] };
}
/** 各模態在 /api/generate/options 多帶的欄位（每一種都要列，沒有就寫 NoExtras） */
type NoExtras = Record<never, never>;
interface GenKindExtras {
  image: { capabilities: Record<string, ImageCaps> };
  speech: { voices: Record<string, VoiceSet> };
  music: NoExtras;
  transcript: NoExtras;
  video: VideoKindExtras;
}

/* ---------------- 1.4-M3：影片（後端 mcp/omniapi_mcp/video/、catalog/openrouter_videos.py） ---------------- */

/** OpenRouter 影片模型自己宣告收什麼（名單現查）；audio／seed：true／false／null＝名單沒標 */
export interface VideoParams {
  durations: number[];
  resolutions: string[];
  aspect_ratios: string[];
  sizes: string[];
  frames: ("first_frame" | "last_frame")[];
  audio: boolean | null;
  seed: boolean | null;
  passthrough: string[];
  /** 一定有聲音、沒有開關（Gemini Omni 直連）：不送 generate_audio */
  audio_fixed?: boolean;
  /** 沒選解析度時模型自己用的那一級（Gemini Omni：720p，也是唯一算得出價的一級） */
  default_resolution?: string;
  /** 尾幀只能跟首幀一起給（Gemini Omni 直連） */
  last_frame_needs_first?: boolean;
}
export interface VideoKindExtras {
  limits: {
    /** MCP／CLI 的單支上限（美元）；null＝不限 */
    mcp_max_usd: number | null;
    max_wait_s: number;
    keep_collecting: boolean;
    /** 問供應商的間隔：第 1、2、3 次之後每次 */
    poll_s: number[];
  };
  /** 名單上有、這一版不收的（編輯、放大、數位人） */
  unlisted: { id: string; name: string; reason: string; note: string | null }[];
}
/** 影片工作的等待資訊（generation.video） */
export interface VideoJobView {
  provider: string | null;
  remote_id: string | null;
  /** 送出時間（已等多久從這裡算，離開再回來是連續的） */
  submitted_at: number | null;
  /** 供應商上次說的：pending／in_progress／completed／failed… */
  remote_status: string | null;
  polled_at: number | null;
  polls: number;
  waited_s: number | null;
  wait_from: number | null;
  max_wait_s: number;
  deadline: number | null;
  next_poll_at: number | null;
  detached_at: number | null;
  /** 不等了之後才收回來的 */
  late: boolean;
  keep_collecting: boolean;
  /** 服務重啟過、接回來的紀錄（重啟前已等多久） */
  resumed: { at: number; after_s: number }[];
  /** 有沒有可能已經計費：yes／no／likely／unknown */
  charged: "yes" | "no" | "likely" | "unknown" | string;
  request: Record<string, unknown>;
  warnings: string[];
  last_error: string | null;
  can_stop_waiting: boolean;
  can_recheck: boolean;
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
  | "sandbox"
  | "variant"
  | "range"
  | "per_megapixel"
  | "per_token"
  | "no_price";

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
  /** 1.4-M2（OpenRouter）：對到的分級、參考圖張數與它們的費用、約略值、沙盒裡「真的送出」的估價 */
  variant?: string;
  refs?: number;
  ref_usd?: number;
  refs_unpriced?: boolean;
  approx?: boolean;
  megapixels?: number;
  mp_price?: number;
  listed?: Estimate;
  [k: string]: unknown;
}

/** 來源只能是作品或上傳檔的 id（不能送路徑） */
export type SourceRef = { artifact_id: string; upload_id?: undefined } | { upload_id: string; artifact_id?: undefined };
export type SourceView = SourceRef & { name: string | null; file_url: string; thumb_url: string | null };
export interface GenSources {
  images?: SourceRef[];
  audio?: SourceRef;
  /** 1.4-M3：影片的首幀、尾幀 */
  frames?: { first?: SourceRef; last?: SourceRef };
}

/** 影片多三種：detached（不等了，背景還在收）、gave_up（等太久，可以再去問一次）、abandoned（不等了也不收） */
export type GenStatus = "running" | "done" | "error" | "cancelled" | "interrupted" | "detached" | "gave_up" | "abandoned";
export type GenErrorKind =
  | "quota"
  | "auth"
  | "rejected"
  | "timeout"
  | "too_large"
  | "unavailable"
  | "offline"
  | "interrupted"
  | "invalid"
  | "other"
  | "gave_up"
  | "lost"
  | "download";

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
  /** 影片：有沒有封面（沒有＝縮圖端點回 204，畫面讓 <video> 自己顯示第一格） */
  poster?: boolean;
  /** 影片：檔案裡有沒有聲音（讀檔案，不信名單）；null＝讀不出來 */
  has_audio?: boolean | null;
  /** 詳情才有：影片的等待紀錄、送出時的預估、首尾幀 */
  meta?: ArtifactMeta | null;
  [k: string]: unknown;
}

/** 作品的 meta（影片用到的那幾個；其他種類的另有欄位） */
export interface ArtifactMeta {
  fps?: number | null;
  waited_s?: number | null;
  late?: boolean;
  resumed?: boolean;
  estimate?: { basis?: string; usd?: number; low?: number; high?: number } | null;
  frames?: { first?: { artifact_id?: string; upload_id?: string }; last?: { artifact_id?: string; upload_id?: string } } | null;
  requested?: Record<string, unknown> | null;
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
  sources: { images?: SourceView[]; audio?: SourceView; frames?: { first?: SourceView; last?: SourceView } };
  status: GenStatus;
  error: string | null;
  error_kind: GenErrorKind | null;
  estimate: Estimate | null;
  cost_usd: number | null;
  artifacts: Artifact[];
  source?: string;
  /** 1.2-M4：聊天來的生成記著是哪段聊天、哪一則、哪張提議 */
  meta?: { conversation_id?: string; message_id?: string; tool_call_id?: string; [k: string]: unknown } | null;
  /** 1.4-M3：只有影片有 */
  video?: VideoJobView;
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
  /** generation.updated：1.4-M3，影片每次問到供應商、重啟後接回、不等了、再去問一次 */
  type: "generation.started" | "generation.updated" | "generation.finished";
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
