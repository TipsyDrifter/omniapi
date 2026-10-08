/* 影片（v1.4）的純函式：主人還沒定版的幾題集中在 VIDEO_UI、名單定價的解讀、草稿 → 請求、預估 → 給人看的字、錢的那一行。
   定價的規則照後端 mcp/omniapi_mcp/catalog/openrouter_videos.py（parse_skus／price）逐條移過來：
   送出鈕旁的大數字一律用後端的 /api/generate/estimate；這裡算的只用在按鈕上的小字（每秒單價）與確認單「省錢的換法」。 */
import type { Estimate, GenModel, GenRequest, Generation, VideoKindExtras, VideoParams } from "@/api/types";
import type { Built, PickedSource, VideoDraft } from "./draft";

/* ================= 主人還沒定版的題目：改主意時改這裡 ================= */
export const VIDEO_UI = {
  /** 第 1 題：確認單每支都出（null）；改成金額（美元）＝預估確定低於它的免確認。離線沙盒與算不出金額的一律要確認 */
  skipConfirmBelowUsd: null as number | null,
  /** 第 2 題：預估超過這個金額（美元）加重確認單（斜紋票頭、金額放大、省錢的換法） */
  highUsd: 1,
  /** 第 3 題：等待中的按鈕字（不叫「取消」「中止」：供應商沒有取消） */
  stopLabel: "不等了…",
  /** 第 7 題：有影片在等時，頂欄「生成」的進行中章滑過出詳情卡 */
  hoverCard: true,
  /** 等待票的刻度尺：通常要等多久（秒）與尺的長度 */
  typicalWait: [60, 300] as const,
  rulerMax: 360,
} as const;
/* 其他幾題落在別處：第 4、5、11 題是服務的設定（settings.json 的 video 一節，設定頁「預設模型」那段可改）；
   第 6 題＝lib/modalities.ts 的 GEN_KINDS 順序；第 8 題（牆上滑過不預覽）、第 9 題（不做「拿這一格去…」）＝沒有寫那段功能；
   第 10 題＝選項一律讀每個模型的 video_params。 */

/* ================= 名單上的定價（pricing.skus） ================= */
type SkuKind = "per_second" | "per_frame_image" | "per_reference_image" | "minimum" | "per_token" | "per_megapixel_second" | "continuation" | "flag";
interface Sku {
  key: string;
  kind: SkuKind;
  usd: number;
  mode: string | null;
  audio: boolean | null;
  resolution: string | null;
}
const RES = "(\\d{3,4}p|\\d(?:\\.\\d)?k)";
const PATTERNS: [RegExp, SkuKind, number, "full" | "res" | "none"][] = [
  [new RegExp(`^(?:(text_to_video|image_to_video|reference)_)?duration_seconds(?:_(with_audio|without_audio))?(?:_${RES})?$`), "per_second", 1, "full"],
  [new RegExp(`^cents_per_second_output(?:_${RES})?$`), "per_second", 0.01, "res"],
  [new RegExp(`^cents_per_video_output_second(?:_${RES})?$`), "per_second", 0.01, "res"],
  [new RegExp(`^cents_per_second_video_continuation(?:_${RES})?$`), "continuation", 0.01, "res"],
  [new RegExp(`^per-video-second(?:-${RES})?$`), "per_second", 1, "res"],
  [/^cents_per_image_input$/, "per_frame_image", 0.01, "none"],
  [/^reference_images$/, "per_reference_image", 1, "none"],
  [/^minimum_cents_per_generation$/, "minimum", 0.01, "none"],
  [/^video_tokens(?:_[a-z0-9_]+)?$/, "per_token", 1, "none"],
  [/^cents_per_megapixel_second(?:_[a-z0-9_]+)?$/, "per_megapixel_second", 0.01, "none"],
  // 不是價：目錄自己寫的標記（Gemini Omni 直連）——只有列出來的解析度算得出每秒價，其他解析度＝算不出（不是「每種價都可能」）
  [/^resolution_lines_only$/, "flag", 1, "none"],
];
const normRes = (r: string | null | undefined): string | null => (r ? String(r).trim().toLowerCase() : null);

function parseSkus(skus: unknown): { rules: Sku[]; unknown: string[] } {
  const rules: Sku[] = [];
  const unknown: string[] = [];
  if (!skus || typeof skus !== "object") return { rules, unknown };
  for (const [key, raw] of Object.entries(skus as Record<string, unknown>)) {
    const amount = Number(raw);
    if (!Number.isFinite(amount)) {
      unknown.push(key);
      continue;
    }
    const k = key.trim().toLowerCase();
    let hit = false;
    for (const [re, kind, scale, shape] of PATTERNS) {
      const m = k.match(re);
      if (!m) continue;
      hit = true;
      if (shape === "full") rules.push({ key, kind, usd: amount * scale, mode: m[1] ?? null, audio: m[2] == null ? null : m[2] === "with_audio", resolution: normRes(m[3]) });
      else if (shape === "res") rules.push({ key, kind, usd: amount * scale, mode: kind === "continuation" ? "continuation" : null, audio: null, resolution: normRes(m[1]) });
      else rules.push({ key, kind, usd: amount * scale, mode: null, audio: null, resolution: null });
      break;
    }
    if (!hit) unknown.push(key);
  }
  return { rules, unknown };
}

export type VideoPrice =
  | { kind: "exact"; usd: number; low: number; high: number; perSecond: number | null }
  | { kind: "range"; usd: null; low: number; high: number; perSecond: number | null }
  | { kind: "unknown"; reason: "per_token" | "per_megapixel" | "unknown_sku" | "no_price" | "no_length" };

/** 一支影片照名單的價（同後端 openrouter_videos.price） */
export function videoPrice(skus: unknown, o: { seconds: number | null; resolution?: string | null; audio?: boolean | null; firstFrame?: boolean; frames?: number }): VideoPrice {
  const { rules, unknown } = parseSkus(skus);
  if (unknown.length) return { kind: "unknown", reason: "unknown_sku" };
  if (!rules.length) return { kind: "unknown", reason: "no_price" };
  if (!o.seconds) return { kind: "unknown", reason: "no_length" };
  const want = o.firstFrame ? "image_to_video" : "text_to_video";
  let per = rules.filter((r) => r.kind === "per_second" && (r.mode === null || r.mode === want));
  if (!per.length) {
    if (rules.some((r) => r.kind === "per_token")) return { kind: "unknown", reason: "per_token" };
    if (rules.some((r) => r.kind === "per_megapixel_second")) return { kind: "unknown", reason: "per_megapixel" };
    return { kind: "unknown", reason: "no_price" };
  }
  const audio = o.audio ?? null;
  if (per.some((r) => r.audio !== null) && audio !== null) {
    const named = per.filter((r) => r.audio === audio);
    per = named.length ? named : per.filter((r) => r.audio === null).length ? per.filter((r) => r.audio === null) : per;
  }
  const moded = per.filter((r) => r.mode === want);
  if (moded.length && !per.some((r) => r.audio !== null)) per = moded;
  const res = normRes(o.resolution);
  if (res) {
    const exact = per.filter((r) => r.resolution === res);
    const plain = per.filter((r) => r.resolution === null);
    if (exact.length) per = exact;
    else if (plain.length) per = plain;
    else if (rules.some((r) => r.kind === "flag" && r.key.trim().toLowerCase() === "resolution_lines_only")) return { kind: "unknown", reason: "per_token" };
  }
  const lowS = Math.min(...per.map((r) => r.usd));
  const highS = Math.max(...per.map((r) => r.usd));
  let low = lowS * o.seconds;
  let high = highS * o.seconds;
  const frames = o.frames ?? 0;
  for (const r of rules) {
    if (r.kind === "per_frame_image" && frames > 0) {
      low += r.usd * frames;
      high += r.usd * frames;
    } else if (r.kind === "per_reference_image" && frames > 0) high += r.usd * frames;
  }
  const floor = rules.filter((r) => r.kind === "minimum").map((r) => r.usd);
  if (floor.length) {
    const f = Math.max(...floor);
    low = Math.max(low, f);
    high = Math.max(high, f);
  }
  const perSecond = lowS === highS ? lowS : null;
  return Math.abs(high - low) < 1e-9 ? { kind: "exact", usd: low, low, high, perSecond } : { kind: "range", usd: null, low, high, perSecond };
}

/** 清單上的每秒價區間（模型列、比較表）：全部每秒的那幾條（不含續寫、參考圖） */
export function perSecondSpan(m: GenModel | null): { low: number; high: number } | null {
  const skus = (m?.pricing?.skus ?? null) as Record<string, unknown> | null;
  if (!skus) return null;
  const { rules } = parseSkus(skus);
  const per = rules.filter((r) => r.kind === "per_second" && (r.mode === null || r.mode === "text_to_video" || r.mode === "image_to_video"));
  if (!per.length) return null;
  return { low: Math.min(...per.map((r) => r.usd)), high: Math.max(...per.map((r) => r.usd)) };
}
/** 按 token 計價（名單上只有 video_tokens） */
export const tokenPriced = (m: GenModel | null): boolean => !perSecondSpan(m) && Object.keys((m?.pricing?.skus ?? {}) as object).some((k) => k.startsWith("video_tokens"));
/** 按 token 計價的每 token 單價（名單上第一條） */
export function tokenUnit(m: GenModel | null): number | null {
  const skus = (m?.pricing?.skus ?? {}) as Record<string, unknown>;
  const k = Object.keys(skus).find((x) => x.startsWith("video_tokens"));
  const v = k ? Number(skus[k]) : NaN;
  return Number.isFinite(v) ? v : null;
}
/** 有聲、無聲有沒有分開定價 */
export const audioPricedApart = (m: GenModel | null): boolean => parseSkus(m?.pricing?.skus).rules.some((r) => r.kind === "per_second" && r.audio !== null);

/* ================= 錢的寫法 ================= */
/** 單價：至少兩位小數（$0.10），名單給到四位就照四位（$0.0817） */
export function perTxt(v: number): string {
  const s = String(Math.round(v * 10000) / 10000);
  const d = (s.split(".")[1] ?? "").length;
  return "$" + (d < 2 ? v.toFixed(2) : s);
}
/** 一支的金額：一塊以上兩位小數；以下最多四位、至少兩位（$0.25、$0.052、$6.00） */
export function vUsd(v: number): string {
  if (v >= 1) return "$" + v.toFixed(2);
  const s = String(Math.round(v * 10000) / 10000);
  return "$" + ((s.split(".")[1] ?? "").length < 2 ? v.toFixed(2) : s);
}

/* ================= 模型的名單欄位 ================= */
const NO_PARAMS: VideoParams = { durations: [], resolutions: [], aspect_ratios: [], sizes: [], frames: [], audio: null, seed: null, passthrough: [] };
export const vparams = (m: GenModel | null): VideoParams => ((m?.video_params as VideoParams | undefined) ?? NO_PARAMS);
const resNum = (r: string) => {
  const s = r.toLowerCase();
  const k = s.match(/^(\d(?:\.\d)?)k$/);
  return k ? Number(k[1]) * 1000 : Number.parseInt(s, 10) || 0;
};
/** 解析度照大小排（480p → 4K） */
export const sortRes = (rs: readonly string[]): string[] => [...rs].sort((a, b) => resNum(a) - resNum(b));
/** 沒指定秒數時送的長度：5 秒，模型不收就取最接近 5 的（同後端 default_duration） */
export function defaultDuration(vp: VideoParams): number | null {
  if (!vp.durations.length) return null;
  return [...vp.durations].sort((a, b) => Math.abs(a - 5) - Math.abs(b - 5) || a - b)[0];
}
/** 秒數：連續的一段（2–30）畫成步進＋尺；跳著的（4、6、8）畫成按鈕 */
export const durationsContiguous = (d: readonly number[]): boolean => d.length > 4 && d[d.length - 1] - d[0] === d.length - 1;
export const durTxt = (vp: VideoParams): string => (vp.durations.length ? (vp.durations.length === 1 ? `${vp.durations[0]} 秒` : durationsContiguous(vp.durations) || vp.durations.length > 3 ? `${vp.durations[0]}–${vp.durations[vp.durations.length - 1]} 秒` : `${vp.durations.join("、")} 秒`) : "—");
export const resTxt = (vp: VideoParams): string => {
  const r = sortRes(vp.resolutions);
  return r.length ? (r.length > 1 ? `${r[0]}–${r[r.length - 1]}` : r[0]) : "—";
};
/** 比例：橫的、方的、直的；不認得的接在後面 */
const RATIO_ORDER = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21", "3:2", "2:3", "5:4", "4:5"];
export const orderRatios = (rs: readonly string[]): string[] => [...RATIO_ORDER.filter((r) => rs.includes(r)), ...rs.filter((r) => !RATIO_ORDER.includes(r))];

/** 模型的狀態：能選、快下架（還能選，提醒日期）、原廠已關閉、這一版沒接上 */
export type VState = "ok" | "sunset" | "retired" | "off";
export function vstate(m: GenModel): VState {
  if (m.status === "retired" || m.unavailable?.reason === "retired") return "retired";
  if (!m.available) return "off";
  if (m.status === "deprecated") return "sunset";
  return "ok";
}
/** 模型清單的排序：能用的照每秒最低價、按 token 計價的排在後面、快下架的再後、用不了的最後 */
export function modelRank(m: GenModel): number {
  const st = vstate(m);
  if (st === "retired") return 1e6;
  if (st === "off") return 1e5;
  const span = perSecondSpan(m);
  const base = span ? span.low : tokenPriced(m) ? 1e3 : 1e4;
  return (st === "sunset" ? 1e4 : 0) + base;
}

/* ================= 草稿 → 請求 ================= */
export interface VideoPick {
  duration: number | null;
  resolution: string | null;
  ratio: string | null;
  /** 有沒有聲音：true／false；null＝這個模型沒有開關（名單沒標或不做聲音） */
  audio: boolean | null;
  first: PickedSource | null;
  last: PickedSource | null;
  /** 放了但這個模型不收、不會送出的 */
  firstDropped: boolean;
  lastDropped: boolean;
}
/** 草稿的值這個模型收就用，不收就用它的預設（秒數取最接近 5、解析度取最低、比例 16:9 或第一個） */
export function videoPick(d: VideoDraft, m: GenModel | null): VideoPick {
  const vp = vparams(m);
  const duration = vp.durations.includes(d.duration) ? d.duration : defaultDuration(vp);
  const res = sortRes(vp.resolutions);
  const resolution = !res.length ? null : res.includes(d.resolution) ? d.resolution : vp.default_resolution && res.includes(vp.default_resolution) ? vp.default_resolution : res[0];
  const ratio = !vp.aspect_ratios.length ? null : vp.aspect_ratios.includes(d.aspect_ratio) ? d.aspect_ratio : vp.aspect_ratios.includes("16:9") ? "16:9" : vp.aspect_ratios[0];
  // 一定有聲音的模型（audio_fixed）沒有開關：不送 generate_audio
  const audio = vp.audio === true && !vp.audio_fixed ? d.audio !== "off" : null;
  const takesFirst = vp.frames.includes("first_frame");
  const takesLast = vp.frames.includes("last_frame");
  return {
    duration,
    resolution,
    ratio,
    audio,
    first: takesFirst ? d.first : null,
    last: takesLast ? d.last : null,
    firstDropped: !!d.first && !takesFirst,
    lastDropped: !!d.last && !takesLast,
  };
}

const PROMPT_MAX = 4000;
export function buildVideo(d: VideoDraft, m: GenModel | null): Built {
  const v = videoPick(d, m);
  const p: Record<string, unknown> = {};
  if (d.prompt) p.prompt = d.prompt;
  if (m) p.model = m.id;
  if (v.duration != null) p.duration = v.duration;
  if (v.resolution) p.resolution = v.resolution;
  if (v.ratio) p.aspect_ratio = v.ratio;
  if (v.audio !== null) p.generate_audio = v.audio;
  const frames: { first?: PickedSource["ref"]; last?: PickedSource["ref"] } = {};
  if (v.first) frames.first = v.first.ref;
  if (v.last) frames.last = v.last.ref;
  const problem = !d.prompt.trim()
    ? "先寫提示詞。"
    : [...d.prompt].length > PROMPT_MAX
      ? `提示詞超過 ${PROMPT_MAX} 字。`
      : !m
        ? "還沒有可用的影片模型。"
        : vstate(m) === "retired"
          ? `${m.name ?? m.id} 原廠已經關閉，送不出去：換一個模型。`
          : !m.available
            ? `${m.name ?? m.id} 這一版還沒接上，送不出去：換一個模型。`
            : v.last && !v.first && vparams(m).last_frame_needs_first
              ? `${m.name ?? m.id} 的尾幀要跟首幀一起給：補一張首幀，或拿掉尾幀。`
              : null;
  const body: GenRequest = { kind: "video", params: p, sources: frames.first || frames.last ? { frames } : undefined };
  return { body, req: problem ? null : body, problem, verb: "生影片" };
}

/* ================= 預估 → 給人看的 ================= */
export interface VideoEst {
  /** exact＝一個數字；range＝區間；token＝按 token 計價（history 有就給參考區間）；none＝算不出 */
  kind: "exact" | "range" | "token" | "none";
  usd: number | null;
  low: number | null;
  high: number | null;
  /** 有過去的實際花費可參考（按 token 計價的模型做過幾支之後） */
  samples: number | null;
  /** 離線沙盒：不花錢；金額是「真的送出」照名單的價 */
  sandbox: boolean;
  /** 依據（一句話） */
  basis: string;
  /** 拿來比「偏貴」門檻的數字（區間取上緣） */
  top: number | null;
}
const WHY_NONE: Record<string, string> = {
  per_megapixel: "這個模型按每百萬像素秒計價，送出前算不出；送出後記實際費用",
  unknown_sku: "名單上的定價寫法這一版還看不懂，送出前算不出；送出後記實際費用",
  no_price: "OpenRouter 沒有公布這個模型的定價；送出後記實際費用",
};
export function videoEst(e: Estimate | null, m: GenModel | null, pick: VideoPick): VideoEst | null {
  if (!e) return null;
  const sandbox = e.basis === "sandbox";
  const x = (sandbox ? e.listed : e) ?? null;
  const basisKey = x?.basis ?? "no_price";
  const per = m ? videoPrice(m.pricing?.skus, { seconds: 1, resolution: pick.resolution, audio: pick.audio ?? vparams(m).audio, firstFrame: !!pick.first, frames: 0 }) : null;
  const perS = per && per.kind !== "unknown" ? per.perSecond : null;
  const frames = (pick.first ? 1 : 0) + (pick.last ? 1 : 0);
  const secs = (x?.seconds as number | undefined) ?? pick.duration ?? 0;
  const sfx = sandbox ? "。離線沙盒不花錢，這是真的送出時的價" : "。實際以供應商回報為準";
  const out = (o: Omit<VideoEst, "sandbox" | "top">): VideoEst => ({ ...o, sandbox, top: o.usd ?? o.high ?? null });
  if (x && x.usd != null && (basisKey === "per_second" || x.approx)) {
    const fz = pick.first && pick.last ? "首尾幀" : pick.first ? "首幀" : "尾幀";
    const tail = frames && x.usd > (perS ?? 0) * secs + 1e-9 ? `＋${fz} ${vUsd(x.usd - (perS ?? 0) * secs)}` : "";
    const res = pick.resolution ? `（${pick.resolution}${audioPricedApart(m) ? (pick.audio === false ? "，不含聲音" : "，含聲音") : ""}）` : "";
    return out({ kind: "exact", usd: x.usd, low: x.usd, high: x.usd, samples: null, basis: perS != null ? `每秒 ${perTxt(perS)}${res} × ${secs} 秒${tail}${sfx}` : `照名單算${sfx}` });
  }
  if (x && x.low != null && x.high != null && basisKey === "range") {
    return out({ kind: "range", usd: null, low: x.low, high: x.high, samples: null, basis: `名單上這組設定可能是 ${vUsd(x.low)}–${vUsd(x.high)}（有幾種價都可能適用）${sfx}` });
  }
  if (basisKey === "history_per_second" && x && x.low != null && x.high != null) {
    return out({ kind: "token", usd: null, low: x.low, high: x.high, samples: (x.samples as number) ?? null, basis: `按 token 計價，送出前算不出；依這個模型過去 ${x.samples ?? 0} 支的實際花費，${secs} 秒約 ${vUsd(x.low)}–${vUsd(x.high)}，僅供參考${sandbox ? "" : "；送出後記實際費用"}` });
  }
  if (basisKey === "per_token") {
    const t = tokenUnit(m);
    return out({ kind: "token", usd: null, low: null, high: null, samples: null, basis: `按 token 計價${t != null ? `（每 token $${t.toFixed(7).replace(/0+$/, "")}）` : ""}：每秒用多少 token 名單上沒寫，送出前算不出；送出後記實際費用` });
  }
  return out({ kind: "none", usd: null, low: null, high: null, samples: null, basis: WHY_NONE[basisKey] ?? "送出前算不出金額；送出後記實際費用" });
}

/** 確認單「想省一點」：換較低的解析度、改短一點（只列確定比現在便宜的） */
export function cheaperOptions(m: GenModel | null, pick: VideoPick, now: number): { label: string; usd: number; patch: Partial<VideoDraft> }[] {
  if (!m) return [];
  const vp = vparams(m);
  const frames = (pick.first ? 1 : 0) + (pick.last ? 1 : 0);
  const at = (o: { res?: string | null; dur?: number }) => videoPrice(m.pricing?.skus, { seconds: o.dur ?? pick.duration, resolution: o.res ?? pick.resolution, audio: pick.audio ?? vp.audio, firstFrame: !!pick.first, frames });
  const out: { label: string; usd: number; patch: Partial<VideoDraft> }[] = [];
  for (const r of sortRes(vp.resolutions)) {
    if (r === pick.resolution) continue;
    const p = at({ res: r });
    if (p.kind === "exact" && p.usd < now - 1e-9) out.push({ label: `換 ${r}`, usd: p.usd, patch: { resolution: r } });
  }
  if (vp.audio === true && pick.audio && audioPricedApart(m)) {
    const p = videoPrice(m.pricing?.skus, { seconds: pick.duration, resolution: pick.resolution, audio: false, firstFrame: !!pick.first, frames });
    if (p.kind === "exact" && p.usd < now - 1e-9) out.push({ label: "改無聲", usd: p.usd, patch: { audio: "off" } });
  }
  for (const s of [10, 5]) {
    if (pick.duration == null || s >= pick.duration || !vp.durations.includes(s)) continue;
    const p = at({ dur: s });
    if (p.kind === "exact" && p.usd < now - 1e-9) out.push({ label: `改 ${s} 秒`, usd: p.usd, patch: { duration: s } });
  }
  return out.slice(0, 5);
}

/* ================= 生成工作的幾句話 ================= */
/** 一支的設定（「480p · 5 秒 · 有聲 · 首幀」） */
export function specOf(g: Pick<Generation, "params" | "sources">): string {
  const p = g.params ?? {};
  const bits: string[] = [];
  if (typeof p.resolution === "string") bits.push(p.resolution);
  if (typeof p.aspect_ratio === "string") bits.push(p.aspect_ratio);
  if (typeof p.duration === "number") bits.push(`${p.duration} 秒`);
  if (p.generate_audio === true) bits.push("有聲");
  else if (p.generate_audio === false) bits.push("無聲");
  const f = g.sources?.frames;
  if (f?.first && f?.last) bits.push("首幀＋尾幀");
  else if (f?.first) bits.push("首幀");
  else if (f?.last) bits.push("尾幀");
  return bits.join(" · ");
}
/** 送出時的預估（給「約 $x」） */
export function estOf(e: Estimate | null | undefined): { text: string; usd: number | null } | null {
  if (!e) return null;
  const x = e.basis === "sandbox" ? e.listed ?? null : e;
  if (!x) return null;
  if (x.usd != null) return { text: `約 ${vUsd(x.usd)}`, usd: x.usd };
  if (x.low != null && x.high != null) return { text: `約 ${vUsd(x.low)}–${vUsd(x.high)}`, usd: x.high };
  return null;
}
/** 錢的那一行（失敗、不等了、沒收回的卡）：照後端的 charged */
export function moneyLine(g: Generation, limits: VideoKindExtras["limits"] | null): { text: string; soft: boolean } {
  const v = g.video;
  const e = estOf(g.estimate);
  const amt = e ? `（${e.text}）` : "";
  const sandbox = g.estimate?.basis === "sandbox";
  const sb = sandbox ? "離線沙盒不花錢；真的送出時：" : "";
  const charged = v?.charged ?? "unknown";
  const recheck = v?.can_recheck ? "按「再去問一次」拿得到就收進作品牆，不會重送、不會多收。" : "";
  if (g.status === "detached")
    return {
      text: `${sb}供應商那邊多半照樣做完、照樣收費${amt}。背景還在收：做好會收進作品牆，費用記實際的。`,
      soft: false,
    };
  if (g.status === "abandoned")
    return { text: `${sb}這支已經送出，供應商多半照樣收費${amt}，但沒有收回來；費用頁記一筆「費用不明」，實際有沒有收看 OpenRouter 的帳單。${recheck}`, soft: false };
  if (g.status === "gave_up" && g.error_kind === "download")
    return { text: `${sb}供應商那邊做好了，多半已經收費${amt}，只是下載失敗。${recheck}`, soft: false };
  if (g.status === "gave_up")
    return { text: `${sb}供應商那邊可能還在做，做好照樣收費${amt}。${recheck}`, soft: false };
  if (g.error_kind === "lost" && v?.provider === "google" && !v?.remote_id)
    return { text: `${sb}這支已經送出，Google 多半已經做好、也已經收費${amt}，但服務在它做的時候停了，影片拿不回來。再生一次會再花一次錢。`, soft: false };
  if (g.error_kind === "lost")
    return { text: `${sb}這支已經送出，供應商可能照樣收費${amt}，但拿不回影片。再生一次會再花一次錢。`, soft: false };
  if (charged === "no") return { text: "供應商沒有收下這支，不會收費。", soft: true };
  if (charged === "yes") return { text: g.cost_usd != null ? `供應商收了 ${vUsd(g.cost_usd)}。` : `供應商有收費${amt}。`, soft: false };
  if (charged === "likely") return { text: `${sb}這支已經送出，供應商多半照樣收費${amt}。`, soft: false };
  if (g.error_kind === "rejected") return { text: "被擋下的多半不收費；有沒有收看供應商的回報，費用頁照實記。", soft: true };
  if (g.error_kind === "quota") return { text: "供應商沒有收下這支，多半不會收費。", soft: true };
  void limits;
  return { text: `有沒有收費不確定${amt}；費用頁照供應商的回報記，記不到就寫「費用不明」。`, soft: true };
}
