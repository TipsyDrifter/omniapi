/* REST 客戶端：同源相對路徑（dev 由 Vite proxy 到 7788，build 後由 daemon 直接掛）。 */
import type {
  BusEvent,
  ChatDetail,
  ChatParams,
  ChatSummary,
  ChatTurn,
  Costs,
  CwdEntry,
  FsDirs,
  HarnessesResponse,
  ModelsResponse,
  Run,
  RunDetail,
  RunSpecInput,
  RunState,
  StartRunResponse,
  Status,
  Thread,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json", ...(init?.headers ?? {}) }, ...init });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = (await res.json()) as { detail?: unknown };
      if (j && typeof j.detail === "string") msg = j.detail;
      else if (j && j.detail) msg = JSON.stringify(j.detail);
    } catch {
      /* 非 JSON 錯誤體 */
    }
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

const qs = (o: Record<string, string | number | boolean | undefined | null>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  health: () => req<{ status: string; version: string; pid: number; ts: number }>("/api/health"),
  status: () => req<Status>("/api/status"),
  /** limit 上限 500（後端 Query(le=500)） */
  runs: (opts: { limit?: number; state?: RunState } = {}) => req<Run[]>(`/api/runs${qs({ limit: opts.limit ?? 200, state: opts.state })}`),
  /** after＝只要 id 大於 after 的事件（斷線補差用） */
  run: (id: string, opts: { after?: number; events?: boolean } = {}) =>
    req<RunDetail>(`/api/runs/${encodeURIComponent(id)}${qs({ after: opts.after ?? 0, events: opts.events === false ? "false" : undefined })}`),
  costs: (days = 30) => req<Costs>(`/api/costs${qs({ days })}`),
  /** 匯流排最近事件（daemon 端保留 500 筆），since_seq 之後的 */
  events: (sinceSeq = 0, limit = 500) => req<BusEvent[]>(`/api/events${qs({ since_seq: sinceSeq, limit })}`),
  /** 派工（也用於續接：帶 resume_run_id）。400 的 detail 是給人看的原因（模型不認得、cwd 不存在、provider 沒設定…） */
  startRun: (spec: RunSpecInput) =>
    req<StartRunResponse>("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(spec),
    }),
  /** 文字模型清單（約 500 筆，含 OpenRouter 長尾） */
  models: (modality = "text") => req<ModelsResponse>(`/api/models${qs({ modality })}`),
  harnesses: () => req<HarnessesResponse>("/api/harnesses"),
  cwds: (limit = 30) => req<CwdEntry[]>(`/api/cwds${qs({ limit })}`),
  fsDirs: (path = "") => req<FsDirs>(`/api/fs/dirs${qs({ path })}`),
  thread: (id: string) => req<Thread>(`/api/runs/${encodeURIComponent(id)}/thread`),

  /* ---- M6 聊天：回覆的逐字內容走 /ws（chat.delta），這裡只負責發號施令 ---- */
  chats: (opts: { limit?: number; archived?: boolean } = {}) => req<ChatSummary[]>(`/api/chat${qs({ limit: opts.limit ?? 100, archived: opts.archived ? "true" : undefined })}`),
  chat: (id: string) => req<ChatDetail>(`/api/chat/${encodeURIComponent(id)}`),
  /** 建立對話；帶 message 就順便送出第一則（回應的 turn 欄位） */
  createChat: (body: { model?: string; system?: string; title?: string; message?: string; params?: ChatParams }) =>
    req<ChatDetail & { turn?: ChatTurn }>("/api/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /** 409＝上一則還在回覆中或對話已封存；400＝模型不認得等 */
  sendChat: (id: string, body: { text: string; model?: string; params?: ChatParams }) =>
    req<ChatTurn>(`/api/chat/${encodeURIComponent(id)}/messages`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  cancelChat: (id: string) => req<{ conversation_id: string; cancelled: boolean }>(`/api/chat/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  updateChat: (id: string, patch: { title?: string; model?: string; system_prompt?: string; archived?: boolean }) =>
    req<ChatSummary>(`/api/chat/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(patch) }),
  /** 匯出 markdown 的網址：直接當連結的 href（瀏覽器會下載） */
  chatExportUrl: (id: string, download = true) => `/api/chat/${encodeURIComponent(id)}/export${download ? "" : "?download=false"}`,
  cancelRun: (id: string) => req<{ run_id: string; state: string; cancelled: boolean }>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
};
