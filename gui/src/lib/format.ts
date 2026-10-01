/* 顯示用的小工具，從 d3-final/index.html 移植；所有元件共用同一套（THEME.md §2.2「金額小數位固定」等規則在這裡落實）。 */
import type { EventPayload, EventType, Run } from "@/api/types";

export const TZ = "Asia/Taipei";

/** harness 代號 → 人話 */
export const HARNESS_NAME: Record<string, string> = { claude: "Claude Code", codex: "Codex", gemini: "Gemini CLI" };
export const harnessName = (h: string | null | undefined): string => (h ? HARNESS_NAME[h] ?? h : "—");
/** harness 油墨 class（THEME.md §1.3）：未知 harness 退回 .h-unknown */
export const harnessClass = (h: string | null | undefined): string => (h && h in HARNESS_NAME ? `h-${h}` : "h-unknown");

const fmtDT = new Intl.DateTimeFormat("zh-TW", { timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtDTs = new Intl.DateTimeFormat("zh-TW", { timeZone: TZ, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const fmtDay = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const fmtHead = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short" });

/** unix 秒 → `MM-DD HH:mm`（Asia/Taipei） */
export const dt = (s: number | null | undefined): string => (s == null ? "—" : fmtDT.format(new Date(s * 1000)).replace(/\//g, "-"));
/** unix 秒 → `MM-DD HH:mm:ss` */
export const dts = (s: number | null | undefined): string => (s == null ? "—" : fmtDTs.format(new Date(s * 1000)).replace(/\//g, "-"));
/** unix 秒 → `YYYY-MM-DD`（Asia/Taipei） */
export const day = (s: number): string => fmtDay.format(new Date(s * 1000));
/** 頂欄日期：`2026 · 09 · 28　MON` */
export const headDate = (d = new Date()): string => `${fmtDay.format(d).replace(/-/g, " · ")}　${fmtHead.format(d).toUpperCase()}`;

/** 金額：票券／表格／帳本 4 位；終值與結果卡 6 位（THEME.md §2.2）。null → null（呼叫端顯示「未回報」） */
export const usd = (v: number | null | undefined, digits = 4): string | null => (v == null ? null : `$${v.toFixed(digits)}`);

/** 秒數 → `3分07秒`／`42秒` */
export const dur = (s: number): string => {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return m ? `${m}分${String(s % 60).padStart(2, "0")}秒` : `${s}秒`;
};

/** 標題開頭的 ↩ ＝ resume 次數，轉成「續」標記 */
export const splitTitle = (t: string | null | undefined): { n: number; t: string } => {
  const s = t ?? "";
  const m = s.match(/^((?:↩\s*)+)/);
  const n = m ? (m[1].match(/↩/g) ?? []).length : 0;
  return { n, t: m ? s.slice(m[1].length) : s };
};

/** `mcp__srv__tool` → { srv, n } */
export const toolParts = (name: string | null | undefined): { srv: string | null; n: string } => {
  const s = name ?? "?";
  const m = s.match(/^mcp__([^_]+(?:_[^_]+)*)__(.+)$/);
  return m ? { srv: m[1], n: m[2] } : { srv: null, n: s };
};

/** tool_call 的一行摘要（與後端 short_tool_summary 同口徑，但不截斷） */
export const summarizeInput = (inp: EventPayload["input"]): string => {
  if (inp == null) return "";
  if (typeof inp === "string") return inp;
  const i = inp as Record<string, unknown>;
  const v =
    i.command ?? i.file_path ?? i.query ?? i.url ?? (Array.isArray(i.urls) ? (i.urls as string[]).join("  ") : null) ?? i.pattern ?? i.path ?? i.prompt ?? i.description;
  if (v != null) return String(v);
  try {
    return JSON.stringify(inp);
  } catch {
    return String(inp);
  }
};

/** 事件章的字（THEME.md §5.2） */
export const GLYPH: Record<EventType, string> = {
  session_start: "始",
  text: "說",
  thinking: "想",
  tool_call: "工",
  tool_result: "回",
  status: "況",
  result: "結",
  error: "錯",
};
export const glyph = (t: string): string => (GLYPH as Record<string, string>)[t] ?? "·";

/** 狀態 → 中文章 */
export const STATE_LABEL: Record<string, string> = {
  starting: "啟動中",
  running: "執行中",
  done: "完成",
  error: "錯誤",
  cancelled: "取消",
  dead: "失聯",
};
export const stateLabel = (s: string): string => STATE_LABEL[s] ?? s;
export const isLive = (r: Pick<Run, "state" | "live">): boolean => r.live || r.state === "running" || r.state === "starting";

/** `#007` 序號 */
export const seq3 = (n: number): string => `#${String(n).padStart(3, "0")}`;

/** 計數器翻牌：420ms 內只換 6 格（steps(6)），不做連續補間（THEME.md §4） */
export function tween(from: number, to: number, apply: (v: number) => void, ms = 420, steps = 6): () => void {
  let t0: number | null = null;
  let raf = 0;
  let cancelled = false;
  const tick = (now: number) => {
    if (cancelled) return;
    if (t0 === null) t0 = now;
    const k = Math.max(0, Math.min(1, (now - t0) / ms));
    const e = Math.ceil(k * steps) / steps;
    apply(from + (to - from) * e);
    if (k < 1) raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => {
    cancelled = true;
    cancelAnimationFrame(raf);
  };
}
