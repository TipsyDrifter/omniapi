/* REST 客戶端：同源相對路徑（dev 由 Vite proxy 到 7788，build 後由 daemon 直接掛）。 */
import type {
  Artifact,
  ArtifactDetail,
  ArtifactFacets,
  ArtifactList,
  CallRow,
  WallQuery,
  BusEvent,
  Estimate,
  GenOptions,
  GenRequest,
  Generation,
  Upload,
  UploadLimits,
  ChatAttachmentRef,
  ChatDeleted,
  ChatDetail,
  ChatParams,
  ChatProposal,
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
  SettingsView,
  SettingsPatch,
  SettingsPatchResult,
  Slot,
  TestKeyResult,
  ToolsResponse,
  ClaudeMcpStatus,
  DesktopAutostart,
} from "./types";

export class ApiError extends Error {
  /** 409 之類帶結構的 detail（例如 /api/claude-mcp 的 {reason, message}） */
  detail?: unknown;
  constructor(public status: number, message: string, detail?: unknown) {
    super(message);
    this.detail = detail;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json", ...(init?.headers ?? {}) }, ...init });
  if (!res.ok) {
    let msg = res.statusText;
    let detail: unknown;
    try {
      const j = (await res.json()) as { detail?: unknown };
      detail = j?.detail;
      if (j && typeof j.detail === "string") msg = j.detail;
      else if (j && j.detail) msg = JSON.stringify(j.detail);
    } catch {
      /* 非 JSON 錯誤體 */
    }
    throw new ApiError(res.status, msg, detail);
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
  createChat: (body: { model?: string; system?: string; title?: string; message?: string; attachments?: ChatAttachmentRef[]; params?: ChatParams }) =>
    req<ChatDetail & { turn?: ChatTurn }>("/api/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /** 409＝上一則還在回覆中或對話已封存；400＝模型不認得等。有附件時 text 可空 */
  sendChat: (id: string, body: { text: string; attachments?: ChatAttachmentRef[]; model?: string; params?: ChatParams }) =>
    req<ChatTurn>(`/api/chat/${encodeURIComponent(id)}/messages`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /* 1.2-M3：重新生成（mid＝回覆或使用者訊息）、編輯舊訊息（沒帶 attachments＝沿用原圖，[]＝拿掉）、切換版本、刪除 */
  regenerateChat: (id: string, mid: string, body: { model?: string; params?: ChatParams } = {}) =>
    req<ChatTurn>(`/api/chat/${encodeURIComponent(id)}/messages/${encodeURIComponent(mid)}/regenerate`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  editChat: (id: string, mid: string, body: { text: string; attachments?: ChatAttachmentRef[]; model?: string; params?: ChatParams }) =>
    req<ChatTurn>(`/api/chat/${encodeURIComponent(id)}/messages/${encodeURIComponent(mid)}/edit`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  switchChat: (id: string, messageId: string) =>
    req<ChatDetail>(`/api/chat/${encodeURIComponent(id)}/switch`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ message_id: messageId }) }),
  deleteChat: (id: string) => req<ChatDeleted>(`/api/chat/${encodeURIComponent(id)}`, { method: "DELETE" }),
  cancelChat: (id: string) => req<{ conversation_id: string; cancelled: boolean }>(`/api/chat/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  updateChat: (id: string, patch: { title?: string; model?: string; system_prompt?: string; archived?: boolean }) =>
    req<ChatSummary>(`/api/chat/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(patch) }),
  /** 匯出 markdown 的網址：直接當連結的 href（瀏覽器會下載） */
  chatExportUrl: (id: string, download = true) => `/api/chat/${encodeURIComponent(id)}/export${download ? "" : "?download=false"}`,
  /* 1.2-M4：聊天裡的生成提議。accept＝按「生成」（帶改過的提示詞、模型、額外參數）；pending、失敗、中止、不用了都能按，生成中／做好了 409 */
  acceptProposal: (id: string, toolCallId: string, body: { prompt?: string; model?: string; params?: Record<string, unknown> } = {}) =>
    req<ChatProposal>(`/api/chat/${encodeURIComponent(id)}/proposals/${encodeURIComponent(toolCallId)}/accept`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /** 按「不用了」（轉錄提問＝「不轉錄，直接回覆」）：pending、失敗、中止的能按（其他 409） */
  declineProposal: (id: string, toolCallId: string) =>
    req<ChatProposal>(`/api/chat/${encodeURIComponent(id)}/proposals/${encodeURIComponent(toolCallId)}/decline`, { method: "POST" }),
  cancelRun: (id: string) => req<{ run_id: string; state: string; cancelled: boolean }>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),

  /* ---- 1.1-M3 生成頁：工作在背景跑，開始／結束走 /ws（generation.started／finished） ---- */
  generateOptions: () => req<GenOptions>("/api/generate/options"),
  /** 預估費用（半填的表單也會回答） */
  estimate: (body: GenRequest) =>
    req<Estimate>("/api/generate/estimate", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /** 立刻回來，status＝running。400 的 detail 是給人看的原因 */
  startGeneration: (body: GenRequest) =>
    req<Generation>("/api/generations", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /** 新的在前 */
  generations: (opts: { limit?: number; status?: string; kind?: string } = {}) => req<Generation[]>(`/api/generations${qs({ limit: opts.limit ?? 30, status: opts.status, kind: opts.kind })}`),
  generation: (id: string) => req<Generation>(`/api/generations/${encodeURIComponent(id)}`),
  cancelGeneration: (id: string) => req<Generation>(`/api/generations/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  /** 1.4-M3：影片「不等了」（供應商沒有取消：錢照樣可能收；預設背景照樣收回來） */
  stopWaitingGeneration: (id: string) => req<Generation>(`/api/generations/${encodeURIComponent(id)}/stop-waiting`, { method: "POST" }),
  /** 1.4-M3：影片「再去問一次」（等太久或不收了的那支；不重送、不多收） */
  recheckGeneration: (id: string) => req<Generation>(`/api/generations/${encodeURIComponent(id)}/recheck`, { method: "POST" }),
  /** 上傳：請求本文就是檔案內容（不是 multipart）。圖 50 MB、音檔 25 MB（413）；型別不收 415 */
  upload: (file: File) =>
    req<Upload>(`/api/uploads${qs({ filename: file.name })}`, { method: "POST", headers: { "content-type": file.type || "application/octet-stream" }, body: file }),
  uploadFileUrl: (id: string) => `/api/uploads/${encodeURIComponent(id)}/file`,
  /** 1.2-M5：聊天的上傳（purpose=chat，什麼檔都收）。用 XHR 才拿得到上傳進度；錯誤同 req 丟 ApiError */
  uploadChat: (file: File, onProgress?: (loaded: number, total: number) => void) =>
    new Promise<Upload>((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", `/api/uploads${qs({ filename: file.name, purpose: "chat" })}`);
      x.setRequestHeader("content-type", file.type || "application/octet-stream");
      x.setRequestHeader("accept", "application/json");
      if (onProgress) x.upload.onprogress = (e) => onProgress(e.loaded, e.lengthComputable ? e.total : file.size);
      x.onload = () => {
        let body: unknown = null;
        try {
          body = JSON.parse(x.responseText);
        } catch {
          /* 非 JSON 錯誤體 */
        }
        if (x.status >= 200 && x.status < 300) return resolve(body as Upload);
        const d = (body as { detail?: unknown } | null)?.detail;
        reject(new ApiError(x.status, typeof d === "string" ? d : d ? JSON.stringify(d) : x.statusText || "上傳失敗"));
      };
      x.onerror = () => reject(new ApiError(0, "連線中斷"));
      x.onabort = () => reject(new ApiError(0, "上傳中止"));
      x.send(file);
    }),
  /** 一筆上傳（抽取慢的檔：info 之後才補上） */
  uploadRow: (id: string) => req<Upload>(`/api/uploads/${encodeURIComponent(id)}`),
  uploadLimits: () => req<UploadLimits>("/api/uploads/limits"),
  /** 作品庫，新的在前；before＝上一頁的 next_before */
  artifacts: (opts: { kind?: string; limit?: number; before?: number | null } = {}) =>
    req<ArtifactList>(`/api/artifacts${qs({ kind: opts.kind, limit: opts.limit ?? 24, before: opts.before ?? undefined })}`),
  /** 單筆（文字全文；清單裡只有預覽） */
  artifact: (id: string) => req<Artifact>(`/api/artifacts/${encodeURIComponent(id)}`),

  /* ---- 1.1-M4 作品牆 ---- */
  /** 牆的一頁：新的在前；before＝上一頁的 next_before；limit 上限 500 */
  wall: (f: WallQuery, opts: { limit?: number; before?: number | null; id?: string } = {}) =>
    req<ArtifactList>(`/api/artifacts${qs({ ...wallQs(f), limit: opts.limit ?? 60, before: opts.before ?? undefined, id: opts.id })}`),
  facets: () => req<ArtifactFacets>("/api/artifacts/facets"),
  /** 單筆＋parent／children＋目前篩選下的 newer／older */
  artifactDetail: (id: string, f: WallQuery = {}) => req<ArtifactDetail>(`/api/artifacts/${encodeURIComponent(id)}${qs(wallQs(f))}`),
  /** 從牆上移除（＝隱藏，檔案不動）／放回 */
  setHidden: (id: string, hidden: boolean) =>
    req<Artifact>(`/api/artifacts/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ hidden }) }),
  /** 聊天・生成帳的原始呼叫紀錄，新的在前；tool 可多個 */
  calls: (opts: { tool?: string[]; limit?: number } = {}) => req<CallRow[]>(`/api/calls${qs({ tool: opts.tool?.join(","), limit: opts.limit ?? 20 })}`),

  /* ---- 1.3-M4 設定頁、模型頁、首次啟動引導 ---- */
  /** 所有模態＋已下架的（模型頁、設定頁的挑選器） */
  modelsAll: () => req<ModelsResponse>(`/api/models${qs({ include_retired: "true" })}`),
  settings: () => req<SettingsView>("/api/settings"),
  /** 點了就存；null＝把那一項從 settings.json 拿掉。400＝欄位不對（不會回完整的 key） */
  patchSettings: (body: SettingsPatch) =>
    req<SettingsPatchResult>("/api/settings", { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  /** 測一把 key（不花錢）；不帶 api_key＝測現在生效的那把。回應只拿 ok／reason／status／models／message */
  testKey: (provider: Slot, apiKey?: string) =>
    req<TestKeyResult>("/api/settings/test-key", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(apiKey === undefined ? { provider } : { provider, api_key: apiKey }),
    }),
  tools: (refresh = false) => req<ToolsResponse>(`/api/tools${qs({ refresh: refresh ? "true" : undefined })}`),
  claudeMcp: () => req<ClaudeMcpStatus>("/api/claude-mcp"),
  /** 寫 ~/.claude.json（只在按了才呼叫）；檔案不存在或讀不懂回 409 */
  connectClaudeMcp: () => req<ClaudeMcpStatus>("/api/claude-mcp", { method: "POST" }),
  desktopAutostart: () => req<DesktopAutostart>("/api/desktop/autostart"),
  /** 開或關開機啟動（寫登錄的 Run 值；路徑由服務決定）；不是桌面版起的服務回 409 */
  setDesktopAutostart: (enabled: boolean) =>
    req<DesktopAutostart>("/api/desktop/autostart", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ enabled }) }),
};

function wallQs(f: WallQuery): Record<string, string | number | undefined> {
  return {
    kind: f.kind || undefined,
    model: f.model || undefined,
    source: f.source || undefined,
    q: f.q?.trim() || undefined,
    since: f.since ?? undefined,
    until: f.until ?? undefined,
    only_hidden: f.only_hidden ? "true" : undefined,
  };
}
