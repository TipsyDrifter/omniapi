/* 作品牆（1.1-M4）的純函式：網址 query ⇄ 篩選、篩選 → API query、接觸印樣的排列、顯示用的字。 */
import type { Artifact, WallQuery } from "@/api/types";
import { day } from "@/lib/format";

/* ---------------- 篩選（網址 query 是唯一真相） ---------------- */

export type DateRange = "" | "today" | "7" | "30";
export interface WallFilter {
  /** "" ＝全部；image／speech／music,lyrics／transcript */
  kind: string;
  /** "" ＝全部來源 */
  src: string;
  /** "" ＝全部模型；"-"＝沒有記錄 */
  model: string;
  d: DateRange;
  q: string;
  /** 看「已移除」那一面 */
  hidden: boolean;
}

/** 模態分頁：全部＋圖／聲／曲＋詞／字（歌詞是作曲的副產品，跟音樂同一頁） */
export const KIND_TABS: { v: string; zh: string; glyphs: string[] }[] = [
  { v: "", zh: "全部", glyphs: [] },
  { v: "image", zh: "圖片", glyphs: ["image"] },
  { v: "speech", zh: "語音", glyphs: ["speech"] },
  { v: "music,lyrics", zh: "音樂＋歌詞", glyphs: ["music", "lyrics"] },
  { v: "transcript", zh: "逐字稿", glyphs: ["transcript"] },
];
export const DATE_OPTS: { v: DateRange; zh: string }[] = [
  { v: "", zh: "全部" },
  { v: "today", zh: "今天" },
  { v: "7", zh: "7 天" },
  { v: "30", zh: "30 天" },
];
export const SOURCE_ZH: Record<string, string> = { gui: "生成頁", chat: "聊天", mcp: "MCP", cli: "CLI", backfill: "回填" };
export const sourceZh = (s: string | null | undefined): string => (s ? SOURCE_ZH[s] ?? s : "—");

const KIND_OK = new Set(KIND_TABS.map((t) => t.v));
const DATE_OK = new Set<string>(DATE_OPTS.map((d) => d.v));

export function parseFilter(sp: URLSearchParams): WallFilter {
  const kind = sp.get("kind") ?? "";
  const d = sp.get("d") ?? "";
  return {
    kind: KIND_OK.has(kind) ? kind : "",
    src: sp.get("src") ?? "",
    model: sp.get("model") ?? "",
    d: (DATE_OK.has(d) ? d : "") as DateRange,
    q: sp.get("q") ?? "",
    hidden: sp.get("hidden") === "1",
  };
}

/** 篩選 → 網址 query（空的不寫，網址乾淨） */
export function filterParams(f: WallFilter): URLSearchParams {
  const p = new URLSearchParams();
  if (f.kind) p.set("kind", f.kind);
  if (f.src) p.set("src", f.src);
  if (f.model) p.set("model", f.model);
  if (f.d) p.set("d", f.d);
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.hidden) p.set("hidden", "1");
  return p;
}
export const isFiltered = (f: WallFilter): boolean => !!(f.kind || f.src || f.model || f.d || f.q.trim() || f.hidden);

/** 台北時間某天 00:00 的 unix 秒（往前 back 天） */
function taipeiMidnight(back = 0): number {
  const ymd = day(Date.now() / 1000 - back * 86400);
  return Math.floor(Date.parse(`${ymd}T00:00:00+08:00`) / 1000);
}

/** 篩選 → API query；日期換成 since（今天＝今天 0 點；7 天＝含今天往前 7 個日曆天） */
export function toQuery(f: WallFilter): WallQuery {
  const q: WallQuery = {};
  if (f.kind) q.kind = f.kind;
  if (f.src) q.source = f.src;
  if (f.model) q.model = f.model;
  if (f.q.trim()) q.q = f.q.trim();
  if (f.d === "today") q.since = taipeiMidnight(0);
  else if (f.d === "7") q.since = taipeiMidnight(6);
  else if (f.d === "30") q.since = taipeiMidnight(29);
  if (f.hidden) q.only_hidden = true;
  return q;
}

/* ---------------- 顯示用 ---------------- */

export const KIND_ZH: Record<string, string> = { image: "圖片", speech: "語音", music: "音樂", lyrics: "歌詞", transcript: "逐字稿" };
export const KIND_EN: Record<string, string> = { image: "IMAGE", speech: "SPEECH", music: "MUSIC", lyrics: "LYRICS", transcript: "TRANSCRIPT" };
export const isAudio = (a: Pick<Artifact, "kind">) => a.kind === "speech" || a.kind === "music";
export const isText = (a: Pick<Artifact, "kind">) => a.kind === "lyrics" || a.kind === "transcript";
export const fileName = (a: Artifact): string => (a.file_path ? a.file_path.split(/[\\/]/).pop() ?? a.id : a.id);
/** 回填的語音與音樂：當時沒留提示詞與模型 */
export const isBare = (a: Artifact): boolean => isAudio(a) && !a.prompt && !a.model;

/** 動作的中文（燈箱標題沒有 title 時用：「生圖・09-05 16:42」） */
export function verbOf(a: Artifact): string {
  if (a.kind === "image") return a.tool === "edit_image" || a.parent_id ? "改圖" : "生圖";
  if (a.kind === "speech") return "語音";
  if (a.kind === "music") return "音樂";
  if (a.kind === "lyrics") return "歌詞";
  if (a.kind === "transcript") return "逐字稿";
  return String(a.kind);
}

/** 生成類工具的中文名（費用頁「最近的生成」） */
export const TOOL_ZH: Record<string, string> = {
  generate_image: "生圖",
  edit_image: "改圖",
  generate_speech: "語音",
  generate_music: "作曲",
  edit_music: "改曲",
  compose_music: "編曲",
  music_utility: "音樂工具",
  music_lyrics: "寫歌詞",
  transcribe_audio: "轉錄",
};
export const GEN_TOOLS = Object.keys(TOOL_ZH);

/* ---------------- 接觸印樣：兩端對齊的列 ---------------- */

export type Cell = { t: "slug"; day: string; n: number; a: number } | { t: "work"; w: Artifact; a: number };
export interface Row {
  cells: Cell[];
  /** 這列卡片的高度（圖的高度；文字卡、音檔卡、日期籤再加一行說明的高度） */
  h: number;
  /** 手機（S 段）：這列只有一張日期籤，排成整列寬的組頭 */
  head?: boolean;
}

/** 卡片的寬高比（寬／高）：圖照原比例；音檔卡、文字卡近方形（A 版的緊湊卡）；日期籤很窄 */
export function aspectOf(w: Artifact): number {
  if (w.kind === "image") return w.width && w.height ? Math.max(0.3, Math.min(3.2, w.width / w.height)) : 1;
  if (isAudio(w)) return 0.92;
  return 0.86;
}
export const SLUG_ASPECT = 0.34;

/** 新到舊排、日子之間插一張日期籤 */
export function cellsOf(items: Artifact[], boxScale = 1): Cell[] {
  const out: Cell[] = [];
  const perDay = new Map<string, number>();
  for (const w of items) perDay.set(day(w.created_at), (perDay.get(day(w.created_at)) ?? 0) + 1);
  let cur = "";
  for (const w of items) {
    const d = day(w.created_at);
    if (d !== cur) {
      out.push({ t: "slug", day: d, n: perDay.get(d) ?? 0, a: SLUG_ASPECT });
      cur = d;
    }
    // boxScale：手機上音檔卡、文字卡塞不下字，調寬一點（圖照原比例）
    out.push({ t: "work", w, a: w.kind === "image" ? aspectOf(w) : aspectOf(w) * boxScale });
  }
  return out;
}

/** 依容器寬排成等高的列；最後一列不拉伸；日期籤不落單在列尾。
    列滿的那一格放不放進這列：看放進去與挪到下一列，哪個的列高比較接近 target（不讓一張寬圖把整列壓得太矮） */
export function layoutRows(cells: Cell[], width: number, gap = 14, target = 232, maxH = 300, slugRow = false): Row[] {
  const rows: Row[] = [];
  if (width <= 0) return rows;
  if (slugRow) {
    // 手機：日期籤自己一列（組頭），每組作品各自排
    const out: Row[] = [];
    let group: Cell[] = [];
    const flushGroup = () => {
      if (group.length) out.push(...layoutRows(group, width, gap, target, maxH));
      group = [];
    };
    for (const c of cells) {
      if (c.t === "slug") {
        flushGroup();
        out.push({ cells: [c], h: 0, head: true });
      } else group.push(c);
    }
    flushGroup();
    return out;
  }
  const hOf = (n: number, s: number) => (width - gap * (n - 1)) / s;
  let cur: Cell[] = [];
  let sum = 0;
  const flush = (last: boolean) => {
    if (!cur.length) return;
    let h = hOf(cur.length, sum);
    if (last && h > target) h = target;
    rows.push({ cells: cur, h: Math.min(h, maxH) });
    cur = [];
    sum = 0;
  };
  for (const c of cells) {
    const hIn = hOf(cur.length + 1, sum + c.a);
    if (hIn > target) {
      cur.push(c);
      sum += c.a;
      continue;
    }
    // 這一格會讓列滿：日期籤一律挪到下一列開頭；作品看哪邊列高比較接近 target
    const hOut = cur.length ? hOf(cur.length, sum) : Infinity;
    if (cur.length && (c.t === "slug" || (hOut <= maxH && hOut - target < target - hIn))) {
      // 列尾剛好是日期籤：跟著這一格一起挪到下一列
      const carry: Cell[] = cur.length > 1 && cur[cur.length - 1].t === "slug" ? [cur.pop()!] : [];
      if (carry.length) sum -= carry[0].a;
      flush(false);
      for (const x of [...carry, c]) {
        cur.push(x);
        sum += x.a;
      }
    } else {
      cur.push(c);
      sum += c.a;
      flush(false);
    }
  }
  flush(true);
  return rows;
}
