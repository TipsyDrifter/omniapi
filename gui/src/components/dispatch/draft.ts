/* 新對話表單的狀態、草稿存取、模型清單小工具 */
import type { Harness, HarnessesResponse, ModelEntry, ModelsResponse, RunSpecInput } from "@/api/types";

/** 等級別名（D21）的名字與按鈕順序；等級對應到哪個模型一律讀 /api/models 的 tiers（設定頁改得動），
 *  這個常數只在清單還沒讀到時當退路 */
export const TIERS = ["cheap", "standard", "strong"] as const;
export type Tier = (typeof TIERS)[number];

export type ModelSel = { kind: "tier"; tier: string } | { kind: "id"; id: string };
/** auto＝跟著模型（不送 harness 欄） */
export type HarnessSel = "auto" | Harness | "replay";

export interface Draft {
  /** 工具箱開＝派工；關＝聊天（M6） */
  toolbox: boolean;
  prompt: string;
  title: string;
  /** 聊天側的 system prompt（派工不用） */
  system: string;
  model: ModelSel;
  longtail: boolean;
  harness: HarnessSel;
  /** 重播來源 run id；空＝最近一筆 */
  replayId: string;
  cwd: string;
  yolo: boolean;
  search: boolean;
  maxTurns: string;
  auth: "subscription" | "api";
}

export const DEFAULT_DRAFT: Draft = {
  toolbox: true,
  prompt: "",
  title: "",
  system: "",
  model: { kind: "tier", tier: "cheap" },
  longtail: false,
  harness: "auto",
  replayId: "",
  cwd: "",
  yolo: false,
  search: true,
  maxTurns: "",
  auth: "subscription",
};

const KEY = "omniapi.newrun.draft";

export function loadDraft(): Draft {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_DRAFT };
    const j = JSON.parse(raw) as Partial<Draft>;
    const d: Draft = { ...DEFAULT_DRAFT };
    // 逐欄檢查型別，壞掉的欄位退回預設
    for (const k of Object.keys(DEFAULT_DRAFT) as (keyof Draft)[]) {
      const v = j[k];
      if (v !== undefined && typeof v === typeof DEFAULT_DRAFT[k]) (d as unknown as Record<string, unknown>)[k] = v;
    }
    const m = d.model as Partial<{ kind: string; tier: string; id: string }>;
    if (!(m && ((m.kind === "tier" && typeof m.tier === "string") || (m.kind === "id" && typeof m.id === "string")))) d.model = DEFAULT_DRAFT.model;
    if (!["auto", "claude", "codex", "gemini", "replay"].includes(d.harness)) d.harness = "auto";
    if (d.auth !== "api") d.auth = "subscription";
    return d;
  } catch {
    return { ...DEFAULT_DRAFT };
  }
}

/** 這個瀏覽器存過派工草稿沒有（沒存過時，模型改用設定頁的「預設派工」） */
export function hasSavedDraft(): boolean {
  try {
    return localStorage.getItem(KEY) !== null;
  } catch {
    return false;
  }
}

/** 設定裡的模型字串（等級別名或模型 id）→ 表單的選擇 */
export function modelSelOf(model: string): ModelSel {
  return (TIERS as readonly string[]).includes(model) ? { kind: "tier", tier: model } : { kind: "id", id: model };
}

export function saveDraft(d: Draft): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(d));
  } catch {
    /* 無痕模式等：存不了就算了 */
  }
}

/** URL query 預填：?cwd= ?model= ?prompt= */
export function applyQuery(d: Draft, q: URLSearchParams): Draft {
  const out = { ...d };
  const cwd = q.get("cwd");
  const model = q.get("model");
  const prompt = q.get("prompt");
  if (cwd !== null) out.cwd = cwd;
  if (prompt !== null) out.prompt = prompt;
  if (model) {
    if (model.startsWith("replay")) {
      out.harness = "replay";
      out.replayId = model.startsWith("replay:") ? model.slice(7) : "";
    } else {
      out.model = (TIERS as readonly string[]).includes(model) ? { kind: "tier", tier: model } : { kind: "id", id: model };
      if (out.harness === "replay") out.harness = "auto";
    }
  }
  return out;
}

/** models 可能是陣列或 {text:[...]}，攤平成一條 */
export function flattenModels(r: ModelsResponse | null): ModelEntry[] {
  if (!r) return [];
  if (Array.isArray(r.models)) return r.models;
  return Object.values(r.models ?? {}).flat();
}

/** id 與別名 → 模型 */
export function indexModels(list: ModelEntry[]): Map<string, ModelEntry> {
  const m = new Map<string, ModelEntry>();
  for (const e of list) {
    if (!m.has(e.id)) m.set(e.id, e);
    for (const a of e.aliases ?? []) if (!m.has(a)) m.set(a, e);
  }
  return m;
}

/** 選到的模型：送出用的字串、實際 id、目錄上的那筆 */
export function resolveModel(sel: ModelSel, tiers: Record<string, string>, idx: Map<string, ModelEntry>) {
  const send = sel.kind === "tier" ? sel.tier : sel.id;
  const id = sel.kind === "tier" ? tiers[sel.tier] ?? null : sel.id;
  const entry = id ? idx.get(id) ?? null : null;
  return { send, id, entry };
}

/** 模型預設會走的 harness（D18）；目錄沒有但是 vendor/model 形式的走 OpenRouter → claude */
export function routeHarness(id: string | null, entry: ModelEntry | null): string | null {
  if (entry?.harness) return String(entry.harness);
  if (id && id.includes("/")) return "claude";
  return null;
}

export function providerOf(id: string | null, entry: ModelEntry | null): string | null {
  if (entry?.provider) return entry.provider;
  if (id && id.includes("/")) return "openrouter";
  return null;
}

/** claude harness 會用到的端點（對應 /api/harnesses 的 claude.endpoints） */
export function claudeEndpoint(provider: string | null, auth: Draft["auth"]): string | null {
  if (!provider) return null;
  if (provider === "anthropic") return auth === "api" ? "anthropic-api" : "anthropic";
  if (provider === "deepseek") return "deepseek";
  return "openrouter";
}

/** harness 不能用的原因（null＝可用或查不到） */
export function harnessBlock(h: string | null, hs: HarnessesResponse | null): string | null {
  if (!h || !hs) return null;
  const info = hs[h];
  if (!info || info.available !== false) return null;
  return info.cli === false ? "沒裝 CLI" : "沒設定 key";
}

/** USD／百萬 token：去掉多餘的 0 */
const price = (v: number) => `$${+v.toFixed(4)}`;
export function fmtPricing(p: ModelEntry["pricing"]): string | null {
  if (!p || (p.input == null && p.output == null)) return null;
  return `${p.input != null ? price(p.input) : "—"} / ${p.output != null ? price(p.output) : "—"}`;
}

/** 表單 → POST /api/runs 的 body */
export function buildSpec(d: Draft, eff: { harness: string | null; provider: string | null }): RunSpecInput {
  const spec: RunSpecInput = { prompt: d.prompt, dispatcher: "GUI" };
  if (d.harness === "replay") {
    const src = d.replayId.trim();
    spec.harness = "replay";
    spec.model = src ? `replay:${src}` : "replay";
  } else {
    spec.model = d.model.kind === "tier" ? d.model.tier : d.model.id;
    if (d.harness !== "auto") spec.harness = d.harness;
    if (eff.harness === "claude" && !d.search) spec.search = false;
    if (eff.provider === "anthropic" && d.auth === "api") spec.auth = "api";
  }
  const cwd = d.cwd.trim();
  if (cwd) spec.cwd = cwd;
  const title = d.title.trim();
  if (title) spec.title = title;
  if (d.yolo) spec.yolo = true;
  const mt = Number.parseInt(d.maxTurns, 10);
  if (Number.isFinite(mt) && mt > 0) spec.max_turns = mt;
  return spec;
}
