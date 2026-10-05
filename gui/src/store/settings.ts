/* 設定的 store（1.3-M4）：/api/settings、全部模態的 /api/models、外部工具、Claude Code 的 MCP 項目，以及這個分頁的變更紀錄。
   規則（版面稿 NOTES §4）：
   · 等級別名、預設模型、啟用開關：點了就 PATCH（畫面先換，失敗就退回原本的值、紀錄標「沒存成」＋重試）；
   · key：按「存起來」才送；送出的值不留在這裡（呼叫端送完就清掉），紀錄只寫末四碼；
   · PATCH 回 warnings＝「已存・有提醒」；存 key 之後等 catalog.refreshed 才標「列到 N 個模型」（離線沙盒不會重列，直接標已生效）；
   · 別的分頁、CLI 改了設定：settings.changed 事件進來就重抓，這一頁跟上。 */
import { useRef, useSyncExternalStore } from "react";
import { ApiError, api } from "@/api/client";
import type { BusEvent, ClaudeMcpStatus, DefaultKind, DesktopAutostart, ModelsResponse, SettingsPatch, SettingsView, Slot, TestKeyResult, ToolsResponse } from "@/api/types";
import { invalidateTextModels } from "@/components/chat/useModels";
import { getBoardState, onBusEvent, onResync } from "@/store/board";

export type LogSt = "ok" | "warn" | "pending" | "local" | "err";
/** 一筆紀錄的文字：純字、模型 id（等寬）、遮住的 key（只有末四碼） */
export type Seg = string | { code: string } | { mask: string };
export interface LogEntry {
  id: number;
  at: number;
  what: Seg[];
  st: LogSt;
  sub?: Seg[];
  /** 這個分頁這次操作產生的（窄畫面的回饋列只顯示這種） */
  fresh: boolean;
  /** 存 key 之後等背景重列的那一家（目錄的供應商名） */
  waitFor?: string;
  retry?: () => void;
}

export interface SettingsState {
  settings: SettingsView | null;
  error: string | null;
  models: ModelsResponse | null;
  modelsError: string | null;
  tools: ToolsResponse | null;
  toolsBusy: boolean;
  toolsError: string | null;
  claude: ClaudeMcpStatus | null;
  claudeBusy: boolean;
  /** 409 等：為什麼沒辦法自動接 */
  claudeError: { reason: string; message: string } | null;
  /** 桌面版的開機啟動（讀不到或不是桌面版起的服務＝available false／null） */
  autostart: DesktopAutostart | null;
  autostartBusy: boolean;
  log: LogEntry[];
  /** 剛存好的那一列：target → 時間（列上蓋「已存・已生效」章） */
  saved: Record<string, number>;
}

let state: SettingsState = {
  settings: null,
  error: null,
  models: null,
  modelsError: null,
  tools: null,
  toolsBusy: false,
  toolsError: null,
  claude: null,
  claudeBusy: false,
  claudeError: null,
  autostart: null,
  autostartBusy: false,
  log: [],
  saved: {},
};
const listeners = new Set<() => void>();
function set(patch: Partial<SettingsState> | ((s: SettingsState) => Partial<SettingsState>)): void {
  state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
  listeners.forEach((l) => l());
}
export const getSettingsState = (): SettingsState => state;

export function useSettings<T>(sel: (s: SettingsState) => T): T {
  const last = useRef<{ s: SettingsState; v: T } | null>(null);
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => {
      if (last.current && last.current.s === state) return last.current.v;
      const v = sel(state);
      last.current = { s: state, v };
      return v;
    },
  );
}

/* ---------------- 紀錄 ---------------- */
let seq = 0;
function addLog(e: Omit<LogEntry, "id" | "at" | "fresh"> & { fresh?: boolean }): LogEntry {
  const entry: LogEntry = { id: ++seq, at: Date.now(), fresh: e.fresh ?? true, ...e };
  set((s) => ({ log: [entry, ...s.log].slice(0, 40) }));
  return entry;
}
function patchLog(id: number, p: Partial<LogEntry>): void {
  set((s) => ({ log: s.log.map((x) => (x.id === id ? { ...x, ...p } : x)) }));
}
/** 主題、送出鍵：只記在這個瀏覽器（跟「已存・已生效」分開） */
export function logLocal(what: Seg[]): void {
  addLog({ what, st: "local" });
}

/* ---------------- 載入 ---------------- */
const errText = (e: unknown): string => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

export async function loadSettings(): Promise<void> {
  try {
    const settings = await api.settings();
    set({ settings, error: null });
  } catch (e) {
    set({ error: errText(e) });
  }
}

let modelsLoading: Promise<void> | null = null;
export function loadModels(force = false): Promise<void> {
  if (modelsLoading && !force) return modelsLoading;
  if (state.models && !force) return Promise.resolve();
  modelsLoading = api
    .modelsAll()
    .then((models) => set({ models, modelsError: null }))
    .catch((e) => set({ modelsError: errText(e) }))
    .finally(() => {
      modelsLoading = null;
    });
  return modelsLoading;
}

export async function loadTools(refresh = false): Promise<void> {
  set({ toolsBusy: true });
  try {
    const tools = await api.tools(refresh);
    set({ tools, toolsError: null });
  } catch (e) {
    set({ toolsError: errText(e) });
  } finally {
    set({ toolsBusy: false });
  }
}

export async function loadClaude(): Promise<void> {
  try {
    set({ claude: await api.claudeMcp() });
  } catch {
    /* 讀不到就不顯示這一塊的狀態 */
  }
}

/** 「把 Claude Code 接上」：只在按了才寫 ~/.claude.json */
export async function connectClaude(): Promise<void> {
  set({ claudeBusy: true, claudeError: null });
  try {
    const r = await api.connectClaudeMcp();
    set({ claude: r });
    addLog({ what: ["Claude Code 接上了這個服務"], st: "ok", sub: [r.backed_up ? "原本的項目已備份；" : "", "Claude Code 下次啟動時生效"] });
  } catch (e) {
    const d = e instanceof ApiError ? (e.detail as { reason?: string; message?: string } | undefined) : undefined;
    set({ claudeError: { reason: d?.reason ?? (e instanceof ApiError ? String(e.status) : "error"), message: d?.message ?? errText(e) } });
    void loadClaude();
  } finally {
    set({ claudeBusy: false });
  }
}

/** 桌面版的開機啟動：設定頁打開、視窗拿回焦點（系統匣那邊可能改過）時重讀 */
export async function loadAutostart(): Promise<void> {
  try {
    set({ autostart: await api.desktopAutostart() });
  } catch {
    /* 讀不到（舊版服務沒有這支 API）就不顯示這一列 */
  }
}

/** 開或關開機啟動：點了就存（畫面先換，失敗退回、紀錄標「沒存成」＋重試） */
export async function setAutostart(on: boolean, retryOf?: LogEntry): Promise<boolean> {
  const before = state.autostart;
  if (!before?.available) return false;
  set({ autostart: { ...before, enabled: on, other: on ? null : before.other }, autostartBusy: true });
  const what: Seg[] = [on ? "開機時啟動 → 開" : "開機時啟動 → 關"];
  const entry = retryOf ?? addLog({ what, st: "pending", sub: ["存檔中…"] });
  if (retryOf) patchLog(retryOf.id, { st: "pending", sub: ["重試中…"], at: Date.now(), fresh: true, retry: undefined });
  try {
    const r = await api.setDesktopAutostart(on);
    set({ autostart: r });
    patchLog(entry.id, { st: "ok", sub: [on ? "下次登入 Windows 時，桌面版會在背景起來（系統匣的勾也跟著變）" : "登入時不再自動啟動（系統匣的勾也跟著變）"] });
    return true;
  } catch (e) {
    set({ autostart: before });
    const reason = e instanceof ApiError ? (e.detail as { reason?: string } | undefined)?.reason : undefined;
    const why =
      reason === "unavailable"
        ? "這個服務不是桌面版起的，開機啟動只能在桌面版切"
        : reason === "write_failed"
          ? "寫不進 Windows 的開機項目（沒有權限，或被防毒軟體擋下）"
          : e instanceof ApiError && e.status
            ? `服務回 ${e.status}：${e.message}`
            : "服務沒回應（連不上）";
    const self: LogEntry = { ...entry };
    patchLog(entry.id, { st: "err", sub: [why, "。開關已退回原本的狀態。"], retry: reason === "unavailable" ? undefined : () => void setAutostart(on, self) });
    void loadAutostart();
    return false;
  } finally {
    set({ autostartBusy: false });
  }
}

/* ---------------- 寫入 ---------------- */
/** 自己送出的 PATCH 之後一小段時間內收到的 settings.changed 是自己的，不另記「別處改了」 */
let ownUntil = 0;
const offline = (): boolean => !!getBoardState().status?.offline;

/** 把 PATCH 先套在畫面上（等級別名、預設、開關）；失敗時退回 */
function optimistic(s: SettingsView, body: SettingsPatch): SettingsView {
  const next: SettingsView = { ...s, providers: { ...s.providers }, tiers: { ...s.tiers }, defaults: { ...s.defaults } };
  for (const [slot, p] of Object.entries(body.providers ?? {})) {
    const cur = next.providers[slot as Slot];
    if (cur && p && typeof p.enabled === "boolean") next.providers[slot as Slot] = { ...cur, enabled: p.enabled, enabled_source: "settings" };
  }
  for (const [t, v] of Object.entries(body.tiers ?? {})) {
    const cur = next.tiers[t];
    if (cur) next.tiers[t] = v ? { ...cur, model: v, source: "settings" } : cur;
  }
  for (const [k, v] of Object.entries(body.defaults ?? {})) {
    const cur = next.defaults[k as DefaultKind];
    if (cur) next.defaults[k as DefaultKind] = v ? { model: v, source: "settings" } : cur;
  }
  return next;
}

export interface SaveOpts {
  what: Seg[];
  /** 存好之後在哪一列蓋章 */
  target?: string;
  sub?: Seg[];
  /** 存 key：等背景重列（目錄的供應商名） */
  waitFor?: string;
}

/** 送一次 PATCH：回饋寫進紀錄；回傳成功與否 */
export async function saveSettings(body: SettingsPatch, opts: SaveOpts, retryOf?: LogEntry): Promise<boolean> {
  const before = state.settings;
  if (before) set({ settings: optimistic(before, body) });
  const entry = retryOf ?? addLog({ what: opts.what, st: "pending", sub: ["存檔中…"] });
  if (retryOf) patchLog(retryOf.id, { st: "pending", sub: ["重試中…"], at: Date.now(), fresh: true, retry: undefined });
  ownUntil = Date.now() + 4000;
  try {
    const r = await api.patchSettings(body);
    const { changed: _c, providers_changed, warnings, ...view } = r;
    // 「已存・已生效」章只蓋在最近改的那一列（舊的章不佔位置）
    set((s) => ({ settings: view, saved: opts.target ? { [opts.target]: Date.now() } : s.saved }));
    invalidateTextModels();
    void loadModels(true);
    ownUntil = Date.now() + 2500;
    if (warnings.length) {
      patchLog(entry.id, { st: "warn", sub: [warningText(warnings)] });
    } else if (opts.waitFor && providers_changed.length && !offline()) {
      patchLog(entry.id, { st: "pending", sub: ["背景重列模型中…"], waitFor: opts.waitFor });
      // 背景重列太久沒回：照樣標已生效（列模型還在跑，不影響這把 key 已經生效）
      window.setTimeout(() => {
        const e = state.log.find((x) => x.id === entry.id);
        if (e && e.st === "pending") patchLog(entry.id, { st: "ok", sub: ["已生效；模型清單還在背景查"], waitFor: undefined });
      }, 30_000);
    } else {
      patchLog(entry.id, { st: "ok", sub: opts.sub ?? (opts.waitFor && offline() ? ["離線沙盒：不重列模型"] : undefined) });
    }
    return true;
  } catch (e) {
    if (before) set({ settings: before });
    const why = e instanceof ApiError && e.status ? `服務回 ${e.status}：${safeMsg(e.message)}` : "服務沒回應（連不上）";
    const self: LogEntry = { ...entry };
    patchLog(entry.id, {
      st: "err",
      sub: [why, "。畫面上的值已退回原本的。"],
      retry: body.providers && Object.values(body.providers).some((p) => p && typeof p.api_key === "string") ? undefined : () => void saveSettings(body, opts, self),
    });
    return false;
  }
}

/** 錯誤訊息裡不可能有完整的 key（後端不回），保險起見把像 key 的長字串遮掉 */
const safeMsg = (m: string): string => m.replace(/[A-Za-z0-9_\-.]{16,}/g, (s) => `•••• ${s.slice(-4)}`);

function warningText(w: string[]): string {
  // 後端的提醒是英文的欄位描述：只翻常見的那一種
  const notIn = w.filter((x) => /is not in the model catalog/.test(x)).length;
  if (notIn && notIn === w.length) return "目錄裡沒有這個 id，照樣存了（之後列到了就會認得）";
  return `有 ${w.length} 個提醒：${w.join("；")}`;
}

/** 存 key：key 只在這次請求裡；紀錄只寫末四碼 */
export async function saveKey(slot: Slot, key: string, opts: { label: string; replaced: boolean }): Promise<boolean> {
  const last4 = key.slice(-4);
  const provider = state.settings?.providers[slot]?.provider ?? slot;
  return saveSettings(
    { providers: { [slot]: { api_key: key } } },
    { what: [`${opts.label} ${opts.replaced ? "換了" : "加了"} key `, { mask: last4 }], target: `pv:${slot}`, waitFor: provider },
  );
}

export async function removeKey(slot: Slot, label: string): Promise<boolean> {
  const sh = state.settings?.providers[slot]?.key.shadowed;
  return saveSettings(
    { providers: { [slot]: { api_key: null } } },
    { what: [`${label} 的設定頁 key 拿掉了`], target: `pv:${slot}`, sub: sh?.set ? ["回到 .env 的 ", { mask: sh.last4 ?? "" }] : ["這家現在沒有 key"] },
  );
}

export function setEnabled(slot: Slot, on: boolean, label: string): Promise<boolean> {
  return saveSettings({ providers: { [slot]: { enabled: on } } }, { what: [on ? `${label} 開回來了` : `${label} 暫時停用`], target: `pv:${slot}`, sub: on ? undefined : ["key 留著"] });
}

/** 測一把 key：送的是剛貼上的值（或不帶＝測現在生效的那把）；回應只拿 ok／reason／status／models／message */
export async function testKey(slot: Slot, key?: string): Promise<TestKeyResult> {
  const r = await api.testKey(slot, key);
  // 真的打到供應商的結果會記成那把 key 的 health：重抓，列上的「能不能用」跟上
  if (!r.simulated) void loadSettings();
  return { provider: r.provider, key: r.key, ok: r.ok, reason: r.reason, status: r.status, models: r.models, message: r.message, simulated: r.simulated, checked_at: r.checked_at, credits: r.credits };
}

/* ---------------- 匯流排 ---------------- */
function onBus(e: BusEvent): void {
  if (e.type === "settings.changed") {
    if (Date.now() > ownUntil) {
      const changed = (e as { changed?: string[] }).changed ?? [];
      addLog({ what: ["別的分頁或程式改了設定"], st: "ok", sub: [changed.length ? changed.map(fieldName).join("、") : "已重新讀取"] });
    }
    void loadSettings();
    invalidateTextModels();
    if (state.models) void loadModels(true);
  } else if (e.type === "catalog.refreshed") {
    const counts = ((e as { providers?: Record<string, number> }).providers ?? {}) as Record<string, number>;
    for (const x of state.log) {
      if (x.st === "pending" && x.waitFor && x.waitFor in counts) {
        const label = state.settings ? Object.values(state.settings.providers).find((p) => p.provider === x.waitFor)?.label ?? x.waitFor : x.waitFor;
        patchLog(x.id, { st: "ok", sub: [`背景重列模型：${label} 列到 ${counts[x.waitFor]} 個`], waitFor: undefined });
      }
    }
    void loadSettings();
    invalidateTextModels();
    if (state.models) void loadModels(true);
  } else if (e.type === "claude_mcp.changed") {
    void loadClaude();
  } else if (e.type === "desktop.autostart.changed") {
    void loadAutostart();
  }
}

/** settings.changed 的欄位名 → 人話（providers.openai.api_key → OpenAI 的 key） */
function fieldName(f: string): string {
  const [sec, a, b] = f.split(".");
  if (sec === "providers") {
    const label = state.settings?.providers[a as Slot]?.label ?? a;
    return b === "api_key" ? `${label} 的 key` : b === "enabled" ? `${label} 的啟用` : label;
  }
  if (sec === "tiers") return `等級別名 ${a}`;
  if (sec === "defaults") return `預設${({ chat: "聊天", dispatch: "派工", image: "生圖", speech: "語音", music: "音樂", transcript: "轉錄" } as Record<string, string>)[a] ?? a}`;
  return f;
}

let booted = false;
/** App 掛載時呼叫一次：讀設定（導覽的「缺 key」章、首次啟動的判斷要用）＋聽匯流排 */
export function bootSettings(): void {
  if (booted) return;
  booted = true;
  void loadSettings();
  onBusEvent(onBus);
  onResync(() => {
    void loadSettings();
    if (state.models) void loadModels(true);
  });
}

/* ---------------- 選擇器 ---------------- */
/** 七家都沒有 key（看板的「缺 key」條、導覽的章、首次啟動的判斷都看這個；設定還沒讀到＝null） */
export const selectNoKey = (s: SettingsState): boolean | null => (s.settings ? !Object.values(s.settings.providers).some((p) => p.key.set) : null);
