/* 設定頁、模型頁、首次啟動引導共用的目錄小工具（1.3-M4，版面稿 prototypes/settings-models-design/）。
   資料一律來自 API：/api/models（模型、定價、狀態、能力、各家 health／get_key／suggested_tiers）、/api/settings（key、等級別名、預設）。
   這裡只放「怎麼解讀」，不放任何示範值。 */
import type { ModelEntry, ModelsResponse, ProviderHealth, SettingsView, Slot } from "@/api/types";
import { SLOTS } from "@/api/types";

export type Modality = "text" | "image" | "speech" | "music" | "transcription";
export const MOD_ORDER: Modality[] = ["text", "image", "speech", "music", "transcription"];
/** 模態章：中文、拉丁、.mk-wg 的類、章上的字 */
export const MOD: Record<Modality, { zh: string; la: string; k: string; g: string }> = {
  text: { zh: "文字", la: "TEXT", k: "", g: "文" },
  image: { zh: "圖片", la: "IMAGE", k: "k-image", g: "圖" },
  speech: { zh: "語音", la: "SPEECH", k: "k-speech", g: "聲" },
  music: { zh: "音樂", la: "MUSIC", k: "k-music", g: "曲" },
  transcription: { zh: "轉錄", la: "TRANSCRIBE", k: "k-transcript", g: "字" },
};
export const isModality = (m: string): m is Modality => (MOD_ORDER as string[]).includes(m);

/** 目錄的供應商名 → 設定頁的 slot（google → gemini；其他同名） */
export function slotOf(provider: string, data?: ModelsResponse | null): Slot | null {
  const p = data?.providers?.[provider];
  const s = (p?.settings_key as string | undefined) ?? provider;
  return (SLOTS as readonly string[]).includes(s) ? (s as Slot) : null;
}
/** slot → 目錄的供應商名 */
export function providerOfSlot(slot: Slot, settings?: SettingsView | null): string {
  return settings?.providers[slot]?.provider ?? (slot === "gemini" ? "google" : slot);
}

/* ---------- 能不能用 ---------- */
export type Usable = "none" | "off" | "bad" | "untested" | "ok";
/** 稿的判斷順序：沒有 key → 停用 → 最近一次測試不通 → 能用（還沒測過的另外標） */
export function usableOf(p: { key: { set: boolean }; enabled: boolean; health?: ProviderHealth | null } | null | undefined): Usable {
  if (!p || !p.key.set) return "none";
  if (!p.enabled) return "off";
  if (p.health?.state === "failed") return "bad";
  if (p.health?.state === "ok") return "ok";
  return "untested";
}
/** 叫得動（能用或還沒測過——還沒測不代表不能用） */
export const callable = (u: Usable): boolean => u === "ok" || u === "untested";

export function slotUsable(settings: SettingsView | null, slot: Slot | null): Usable {
  if (!slot) return "ok"; // 不屬於任何 slot 的（開發沙盒的 echo）：不擋
  return usableOf(settings?.providers[slot]);
}

/** 失敗原因（API 的 reason）→ 人話 */
export function reasonText(reason: string | null | undefined, status?: number | null): string {
  switch (reason) {
    case "rejected":
      return `對方說這把 key 無效或已撤銷${status ? `（HTTP ${status}）` : ""}`;
    case "payment":
      return "帳戶沒有餘額或還沒綁付款方式";
    case "rate_limited":
      return "對方說請求太多，稍後再試";
    case "provider_error":
      return `對方的服務暫時出錯${status ? `（HTTP ${status}）` : ""}`;
    case "http_error":
      return `對方回了錯誤${status ? `（HTTP ${status}）` : ""}`;
    case "network":
      return "連不上對方（網路或防火牆）";
    case "format":
      return "格式不像 API key（太短、中間有空白或換行、或有不該出現的字元）";
    case "missing_key":
      return "沒有 key 可以測";
    default:
      return status ? `HTTP ${status}` : "不通";
  }
}

/* ---------- 下架 ---------- */
/** 快下架的門檻（D50：30 天） */
export const SUNSET_DAYS = 30;
export function daysLeft(d: string, now = new Date()): number {
  const t = Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10));
  const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((t - today) / 864e5);
}
export const isSunset = (m: ModelEntry): boolean => m.status === "deprecated";
export const isRetired = (m: ModelEntry): boolean => m.status === "retired";
export const soonSunset = (m: ModelEntry): boolean => isSunset(m) && !!m.shutdown && daysLeft(m.shutdown) <= SUNSET_DAYS;

/* ---------- 價格 ---------- */
export function money(v: number): string {
  if (Number.isInteger(v)) return "$" + v;
  let s = v.toFixed(4).replace(/0+$/, "");
  if ((s.split(".")[1] || "").length < 2) s = v.toFixed(2);
  return "$" + s;
}
type P = Record<string, unknown>;
const num = (p: P, k: string): number | null => (typeof p[k] === "number" ? (p[k] as number) : null);
/** 名單上的主價格：主數字＋單位＋排序鍵（只在同一個模態裡排才有意義） */
export function priceMain(m: ModelEntry): { main: string; sub: string; key: number } | null {
  const p = (m.pricing ?? null) as P | null;
  if (!p) return null;
  const inp = num(p, "input"), out = num(p, "output");
  if (inp != null || out != null) return { main: `${inp != null ? money(inp) : "—"} / ${out != null ? money(out) : "—"}`, sub: "每 1M token 入／出", key: out ?? inp ?? 0 };
  const unit = p.unit;
  if (unit === "per_1m_tokens") {
    const io = num(p, "image_output"), ao = num(p, "audio_output");
    if (io != null) return { main: money(io), sub: "每 1M 圖片輸出 token", key: io };
    if (ao != null) return { main: money(ao), sub: "每 1M 音訊輸出 token", key: ao };
  }
  if (unit === "per_image") {
    const k = num(p, "1K") != null ? "1K" : Object.keys(p).find((x) => /K$/.test(x) && typeof p[x] === "number");
    if (k) return { main: money(p[k] as number), sub: `每張（${k}）`, key: p[k] as number };
  }
  const one = (k: string, sub: string) => (num(p, k) != null ? { main: money(num(p, k)!), sub, key: num(p, k)! } : null);
  if (unit === "per_minute") return one("audio", "每分鐘");
  if (unit === "per_1m_chars") return one("text", "每 1M 字元");
  if (unit === "per_1k_chars") return one("text", "每千字元");
  if (unit === "per_song") return one("song", "每首");
  if (unit === "credits") return one("credit_usd", "每 credit（依長度扣）");
  return null;
}
export const PRICE_LABEL: Record<string, string> = {
  input: "輸入", cached_input: "快取輸入", output: "輸出", text_input: "文字輸入", image_input: "圖片輸入", image_output: "圖片輸出",
  audio_input: "音訊輸入", audio_output: "音訊輸出", audio: "音訊（每分鐘）", text: "文字", song: "每首", credit_usd: "每 credit",
  text_input_per_1m: "文字輸入（每 1M）", "0.5K": "每張 0.5K", "1K": "每張 1K", "2K": "每張 2K", "4K": "每張 4K",
};
export const UNIT_NOTE: Record<string, string> = {
  per_1m_tokens: "每 1M token", per_image: "每張", per_minute: "每分鐘", per_1m_chars: "每 1M 字元", per_1k_chars: "每千字元", per_song: "每首", credits: "credits",
};

/* ---------- 能力 ---------- */
/** 名單上每個模態的三格能力章（沒有的那一格顯示淡框，欄位才對得齊） */
export const CAPS: Record<Modality, [string, string, string][]> = {
  text: [["vision", "看", "看圖"], ["tools", "工", "用工具"], ["reasoning", "想", "推理"]],
  image: [["edit", "改", "改圖"], ["inpaint", "補", "局部重繪"], ["multi_reference", "參", "多張參考圖"]],
  speech: [["instructions", "指", "可下語氣指示"], ["expressive", "情", "情緒表現"], ["low_latency", "快", "低延遲"]],
  music: [],
  transcription: [["diarization", "分", "分辨說話者"], ["timestamps", "時", "時間碼"], ["srt", "幕", "輸出字幕檔"]],
};
/** 篩選欄多的能力（文字：收 PDF、收音訊——/api/models 算出來的布林） */
export const EXTRA_CAPS: Partial<Record<Modality, [string, string][]>> = {
  text: [["pdf", "收 PDF"], ["audio", "收音訊"]],
};
export const CAP_ZH: Record<string, string> = {
  vision: "看圖", tools: "用工具", reasoning: "推理", pdf: "收 PDF", audio: "收音訊", structured_output: "結構化輸出", anthropic_endpoint: "Anthropic 相容端點",
  edit: "改圖", inpaint: "局部重繪", multi_reference: "多張參考圖", max_edge: "指定最長邊", input_fidelity: "保留原圖細節", sizes: "多種尺寸",
  timestamps: "時間碼", diarization: "分辨說話者", logprobs: "logprobs", srt: "SRT 字幕", vtt: "VTT 字幕", translate: "翻成英文",
  instructions: "可下語氣指示", expressive: "情緒表現", realtime: "即時", low_latency: "低延遲",
};
/** 能力有沒有（值可能是布林、數字、陣列） */
export function hasCap(m: ModelEntry, k: string): boolean {
  const v = (m.capabilities ?? {})[k];
  if (Array.isArray(v)) return v.length > 0;
  return !!v;
}
export function capList(m: ModelEntry): string[] {
  return Object.keys(m.capabilities ?? {}).filter((k) => hasCap(m, k));
}

/* ---------- 設定用到哪些 ---------- */
export const TIER_NAMES = ["cheap", "standard", "strong"] as const;
export const DEF_LABEL: Record<string, string> = { chat: "聊天", dispatch: "派工", image: "生圖", speech: "語音", music: "音樂", transcript: "轉錄" };
/** 等級別名 → 實際 id */
export function resolveTier(settings: SettingsView | null, v: string | null): string | null {
  if (!v) return null;
  return settings?.tiers[v]?.model ?? v;
}
/** 一個模型被設定的哪些地方用到：tier＝等級別名（反白章）、def＝某個預設（虛線章） */
export function usedBy(settings: SettingsView | null, id: string): { kind: "tier" | "def"; label: string; via?: string }[] {
  if (!settings) return [];
  const out: { kind: "tier" | "def"; label: string; via?: string }[] = [];
  for (const [t, v] of Object.entries(settings.tiers)) if (v.model === id) out.push({ kind: "tier", label: t });
  for (const [k, d] of Object.entries(settings.defaults)) {
    if (!d.model) continue;
    if (resolveTier(settings, d.model) === id) out.push({ kind: "def", label: `${DEF_LABEL[k] ?? k}預設`, via: settings.tiers[d.model] ? d.model : undefined });
  }
  return out;
}

/** 整份模型（攤平，去掉不屬於任何供應商的開發用模型） */
export function allModels(data: ModelsResponse | null): ModelEntry[] {
  if (!data) return [];
  const list = Array.isArray(data.models) ? data.models : Object.values(data.models ?? {}).flat();
  const provs = data.providers ?? {};
  return list.filter((m) => m.provider in provs);
}
export function indexById(list: ModelEntry[]): Map<string, ModelEntry> {
  const m = new Map<string, ModelEntry>();
  for (const e of list) {
    if (!m.has(e.id)) m.set(e.id, e);
    for (const a of e.aliases ?? []) if (!m.has(a)) m.set(a, e);
  }
  return m;
}

/** 末四碼的遮罩字（畫面上唯一能出現的 key 片段） */
export const masked = (last4: string | null | undefined): string => (last4 ? `•••• ${last4}` : "••••");

/** 秒 → 「10:42」（今天）或「10-03 10:42」 */
export function hhmm(sec: number | null | undefined): string {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  const now = new Date();
  const two = (n: number) => String(n).padStart(2, "0");
  const t = `${two(d.getHours())}:${two(d.getMinutes())}`;
  return d.toDateString() === now.toDateString() ? t : `${two(d.getMonth() + 1)}-${two(d.getDate())} ${t}`;
}
