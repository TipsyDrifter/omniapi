/* 生成頁（1.1-M3）的表單狀態：四種模態各一份草稿（各自存 localStorage）、
   草稿 → POST /api/generations 的 body、生成工作 → 草稿（「帶回表單」）。
   參數名照後端 ALLOWED_PARAMS（mcp/omniapi_mcp/generate/manager.py），空字串與 null 不送。 */
import type { Artifact, GenKind, GenModel, GenOptions, GenRequest, Generation, ImageCaps, OrImageParams, SourceRef } from "@/api/types";
import { orLines, orPriceMain, orVideoPerSecond } from "@/lib/catalog";
import { GEN_KINDS, GEN_KIND_INFO, WORK_KINDS, WORK_KIND_INFO, isGenKind, mediaOf, warnUnknown, type Covers } from "@/lib/modalities";

export const KINDS: readonly GenKind[] = GEN_KINDS;
export { isGenKind };

/** 模態章（方形楷字章）與分頁鈕的字 */
export const KIND_META = Object.fromEntries(
  GEN_KINDS.map((k) => [k, { g: WORK_KIND_INFO[k].glyph, zh: GEN_KIND_INFO[k].zh, en: GEN_KIND_INFO[k].en }]),
) as Record<GenKind, { g: string; zh: string; en: string }>;
/** 作品種類 → 章字（多一個歌詞「詞」）；不認得的種類查不到，Glyph 畫「·」 */
export const GLYPH: Record<string, string> = Object.fromEntries(WORK_KINDS.map((k) => [k, WORK_KIND_INFO[k].glyph]));

/* ---------------- 草稿 ---------------- */

/** 選好的來源檔：送出只送 ref（id），其他欄位給畫面用 */
export interface PickedSource {
  ref: SourceRef;
  name: string | null;
  /** 看得到的網址：圖＝縮圖（或上傳檔），音檔＝檔案本身 */
  url: string;
  /** 從作品挑的種類（speech／music／image）；上傳檔＝upload */
  from: string;
  bytes?: number | null;
  mime?: string | null;
  /** 音檔長度（秒），從 <audio> metadata 讀 */
  duration?: number | null;
}

export interface ImageDraft {
  prompt: string;
  model: string | null;
  size: string;
  quality: string;
  background: string;
  output_format: string;
  n: number;
  image_size: string;
  aspect_ratio: string;
  source: PickedSource | null;
  /** 1.4-M2：第一張來源圖之外的參考圖（只有收多張參考圖的 OpenRouter 模型會送） */
  extra: PickedSource[];
}
export interface SpeechDraft {
  text: string;
  model: string | null;
  /** 每個 provider 各記一個聲音（換模型不會把 ElevenLabs 的 voice id 送給 OpenAI） */
  voice: Record<string, string>;
  instructions: string;
  speed: string;
  language_code: string;
}
export type MusicMode = "describe" | "lyrics";
export interface MusicDraft {
  model: string | null;
  /** Suno 才有兩種寫法；其他模型一律照描述 */
  mode: MusicMode;
  prompt: string;
  lyrics: string;
  /** 從作品挑來的歌詞（id） */
  lyricsFrom: string | null;
  style: string;
  title: string;
  vocal_gender: "" | "m" | "f";
  negative_tags: string;
  instrumental: boolean;
  length_ms: number;
}
export interface TranscriptDraft {
  model: string | null;
  source: PickedSource | null;
  language: string;
  prompt: string;
  response_format: string;
}
/** 1.4-M3：影片（表單在下一個里程碑；草稿形狀先照後端的參數，帶回表單時不丟資料） */
export interface VideoDraft {
  prompt: string;
  model: string | null;
  /** 秒；0＝模型的預設（5 秒，或它收的長度裡最接近 5 的） */
  duration: number;
  resolution: string;
  aspect_ratio: string;
  /** ""＝照模型預設；"on"／"off" */
  audio: string;
  first: PickedSource | null;
  last: PickedSource | null;
}
export interface Drafts {
  image: ImageDraft;
  speech: SpeechDraft;
  music: MusicDraft;
  transcript: TranscriptDraft;
  video: VideoDraft;
}
/** 每一種生成模態都要有一份草稿（加了模態沒加草稿＝這行編譯不過） */
const _draftsCover: Covers<Drafts, GenKind> = true;
void _draftsCover;

export const DEFAULT_DRAFTS: Drafts = {
  image: { prompt: "", model: null, size: "auto", quality: "auto", background: "auto", output_format: "png", n: 1, image_size: "2K", aspect_ratio: "1:1", source: null, extra: [] },
  speech: { text: "", model: null, voice: {}, instructions: "", speed: "", language_code: "" },
  music: { model: null, mode: "describe", prompt: "", lyrics: "", lyricsFrom: null, style: "", title: "", vocal_gender: "", negative_tags: "", instrumental: false, length_ms: 60000 },
  transcript: { model: null, source: null, language: "auto", prompt: "", response_format: "text" },
  video: { prompt: "", model: null, duration: 0, resolution: "", aspect_ratio: "", audio: "", first: null, last: null },
};

const KEY = (k: GenKind) => `omniapi.make.draft.${k}`;

/** 逐欄檢查型別，壞掉的欄位退回預設 */
export function loadDraft<K extends GenKind>(kind: K): Drafts[K] {
  const def = DEFAULT_DRAFTS[kind];
  try {
    const raw = localStorage.getItem(KEY(kind));
    if (!raw) return { ...def };
    const j = JSON.parse(raw) as Record<string, unknown>;
    const out = { ...def } as Record<string, unknown>;
    for (const [k, v] of Object.entries(def)) {
      const got = j[k];
      if (got === undefined) continue;
      if (v === null) {
        if (got === null || typeof got === "string" || (typeof got === "object" && got)) out[k] = got;
      } else if (typeof got === typeof v) out[k] = got;
    }
    return out as unknown as Drafts[K];
  } catch {
    return { ...def };
  }
}

export function saveDraft<K extends GenKind>(kind: K, d: Drafts[K]): void {
  try {
    localStorage.setItem(KEY(kind), JSON.stringify(d));
  } catch {
    /* 無痕模式等：存不了就算了 */
  }
}

const LAST = "omniapi.make.kind";
export function lastKind(): GenKind {
  try {
    const k = localStorage.getItem(LAST);
    return isGenKind(k) ? k : "image";
  } catch {
    return "image";
  }
}
export function rememberKind(k: GenKind): void {
  try {
    localStorage.setItem(LAST, k);
  } catch {
    /* ignore */
  }
}

/* ---------------- 模型 ---------------- */

/** 草稿的模型不在清單（或還沒選）→ 後端給的預設 */
export function effModel(opts: GenOptions | null, kind: GenKind, want: string | null): GenModel | null {
  if (!opts) return null;
  const list = opts.kinds[kind].models;
  return list.find((m) => m.id === want) ?? list.find((m) => m.id === opts.kinds[kind].default_model) ?? list.find((m) => m.available) ?? null;
}

export const OPENAI_SIZES = ["1024x1024", "1536x1024", "1024x1536", "auto"];
export const OPENAI_QUALITIES = ["low", "medium", "high", "auto"];
export const OPENAI_FORMATS = ["png", "jpeg", "webp"];
export const GOOGLE_SIZES = ["1K", "2K", "4K"];
export const GOOGLE_RATIOS = ["1:1", "3:2", "2:3", "4:3", "3:4", "4:5", "5:4", "16:9", "9:16", "21:9"];

/** 圖片模型的參數表：capabilities 沒有這個模型（沙盒）就依 provider 用預設值 */
export function imageCaps(opts: GenOptions | null, m: GenModel | null): ImageCaps {
  // 1.4-M2：OpenRouter 的模型自己宣告收哪些參數（第三種形狀：依模型出選項）
  if (m?.provider === "openrouter" && m.image_params) {
    const ip = m.image_params;
    return { provider: "openrouter", sizes: [], qualities: ip.qualities, formats: ip.output_formats, max_images: Math.max(1, Math.min(4, ip.max_n)), supports_background: false, or: ip };
  }
  const c = m && opts ? opts.kinds.image.capabilities?.[m.id] : undefined;
  // 後端的 capabilities 用設定檔的鍵名（Google 那家叫 "gemini"），模型目錄叫 "google"：統一成 "google"，
  // 否則設好 Gemini 金鑰後，Gemini 生圖的比例／解析度既不顯示也不會送出
  const raw = c?.provider ?? m?.provider ?? "openai";
  const provider = raw === "gemini" ? "google" : raw;
  return {
    provider,
    sizes: c?.sizes?.length ? c.sizes : OPENAI_SIZES,
    qualities: c?.qualities?.length ? c.qualities : OPENAI_QUALITIES,
    formats: c?.formats?.length ? c.formats : OPENAI_FORMATS,
    max_images: Math.max(1, Math.min(4, c?.max_images ?? 4)),
    supports_background: c?.supports_background ?? provider === "openai",
  };
}

/** 語音：哪些參數對這個模型有效 */
export const speechKnobs = (m: GenModel | null) => ({
  instructions: m?.id === "gpt-4o-mini-tts",
  speed: m?.id === "tts-1" || m?.id === "tts-1-hd",
  language: m?.provider === "elevenlabs" || m?.provider === "google",
});

/** 音樂：模型屬於哪一系 */
export const musicFamily = (m: GenModel | null): "elevenlabs" | "suno" | "other" =>
  m?.provider === "elevenlabs" ? "elevenlabs" : m?.provider === "kie" ? "suno" : "other";

export const MUSIC_LENGTHS: [number, string][] = [
  [30000, "0:30"],
  [60000, "1:00"],
  [90000, "1:30"],
  [150000, "2:30"],
  [180000, "3:00"],
];

/* ---------------- 草稿 → 請求 ---------------- */

export interface Built {
  /** 永遠有（預估費用拿半填的表單也能問） */
  body: GenRequest;
  /** 能送才有 */
  req: GenRequest | null;
  /** 不能送的原因（給人看） */
  problem: string | null;
  /** 送出鈕的字 */
  verb: string;
}

const put = (o: Record<string, unknown>, k: string, v: unknown) => {
  if (v === undefined || v === null || v === "") return;
  o[k] = v;
};

/** OpenRouter 模型實際會用的解析度／比例／品質：草稿的值這個模型收就用，不收就用它的預設
   （解析度與品質取名單第一個——通常最便宜；比例 1:1，沒有就第一個）。null＝這個模型沒有這個參數 */
export function orPick(d: ImageDraft, ip: OrImageParams): { res: string | null; ratio: string | null; quality: string | null } {
  const pick = (want: string, list: string[], dflt: string | undefined) => (!list.length ? null : list.includes(want) ? want : dflt ?? list[0]);
  return {
    res: pick(d.image_size, ip.resolutions, ip.resolutions.includes("1K") ? "1K" : undefined),
    ratio: pick(d.aspect_ratio, ip.aspect_ratios, ip.aspect_ratios.includes("1:1") ? "1:1" : undefined),
    quality: pick(d.quality, ip.qualities, ip.qualities.find((q) => q !== "auto")),
  };
}

/** 一起送出的參考圖（第一張來源圖＋其他參考圖）；只有 OpenRouter 收多張的模型才帶其他的 */
export function sourceList(d: ImageDraft, caps: ImageCaps): PickedSource[] {
  if (!d.source) return [];
  const extra = caps.or && caps.or.max_references > 1 ? (d.extra ?? []).filter((s) => s && s.ref) : [];
  return [d.source, ...extra];
}

export function buildImage(d: ImageDraft, m: GenModel | null, caps: ImageCaps): Built {
  const edit = !!d.source;
  const verb = edit ? "改圖" : "生圖";
  const p: Record<string, unknown> = {};
  put(p, "prompt", d.prompt);
  put(p, "model", m?.id);
  let orProblem: string | null = null;
  const refs = sourceList(d, caps);
  if (caps.or) {
    const ip = caps.or;
    const v = orPick(d, ip);
    put(p, "image_size", v.res);
    put(p, "aspect_ratio", v.ratio);
    put(p, "quality", v.quality);
    if (!edit && caps.max_images > 1) p.n = Math.min(d.n, caps.max_images);
    if (edit && ip.max_references === 0) orProblem = "這個模型不收參考圖，不能改圖：換一個能改圖的模型，或拿掉來源圖。";
    else if (refs.length > ip.max_references) orProblem = `這個模型最多收 ${ip.max_references} 張參考圖，現在放了 ${refs.length} 張。`;
    else if (refs.length < ip.min_references) orProblem = "這個模型一定要有參考圖（它照著給的圖改風格）：先放一張來源圖。";
  } else if (caps.provider === "google") {
    if (!edit) {
      put(p, "image_size", d.image_size);
      put(p, "aspect_ratio", d.aspect_ratio);
    }
  } else if (caps.provider === "openai") {
    put(p, "size", caps.sizes.includes(d.size) ? d.size : undefined);
    put(p, "quality", caps.qualities.includes(d.quality) ? d.quality : undefined);
    if (caps.supports_background) put(p, "background", d.background);
    put(p, "output_format", caps.formats.includes(d.output_format) ? d.output_format : undefined);
    if (!edit) p.n = Math.min(d.n, caps.max_images);
  }
  const problem = !d.prompt.trim() ? "提示詞空白不能送出。" : [...d.prompt].length > 4000 ? "提示詞超過 4000 字。" : unavailable(m) ?? orProblem;
  const body: GenRequest = { kind: "image", params: p, sources: edit ? { images: refs.map((s) => s.ref) } : undefined };
  return { body, req: problem ? null : body, problem, verb };
}

export function buildSpeech(d: SpeechDraft, m: GenModel | null, voice: string | null): Built {
  const k = speechKnobs(m);
  const p: Record<string, unknown> = {};
  put(p, "text", d.text);
  put(p, "model", m?.id);
  put(p, "voice", voice);
  if (k.instructions) put(p, "instructions", d.instructions.trim());
  if (k.speed) {
    const s = Number.parseFloat(d.speed);
    if (Number.isFinite(s)) p.speed = s;
  }
  if (k.language) put(p, "language_code", d.language_code.trim());
  const problem = !d.text.trim() ? "沒有要念的文字。" : unavailable(m);
  const body: GenRequest = { kind: "speech", params: p };
  return { body, req: problem ? null : body, problem, verb: "生語音" };
}

export function buildMusic(d: MusicDraft, m: GenModel | null): Built {
  const fam = musicFamily(m);
  const lyrics = fam === "suno" && d.mode === "lyrics";
  const p: Record<string, unknown> = {};
  put(p, "model", m?.id);
  p.instrumental = d.instrumental;
  let problem: string | null = null;
  if (lyrics) {
    p.custom_mode = true;
    put(p, "prompt", d.lyrics);
    put(p, "style", d.style.trim());
    put(p, "title", d.title.trim());
    put(p, "vocal_gender", d.vocal_gender);
    put(p, "negative_tags", d.negative_tags.trim());
    if (!d.lyrics.trim()) problem = "歌詞空白不能送出。";
    else if (!d.style.trim() || !d.title.trim()) problem = "照歌詞作曲要填風格與標題。";
  } else {
    put(p, "prompt", d.prompt);
    if (fam === "elevenlabs") p.music_length_ms = d.length_ms;
    if (!d.prompt.trim()) problem = "描述空白不能送出。";
  }
  problem = problem ?? unavailable(m);
  const body: GenRequest = { kind: "music", params: p };
  return { body, req: problem ? null : body, problem, verb: "作曲" };
}

export function buildTranscript(d: TranscriptDraft, m: GenModel | null): Built {
  const p: Record<string, unknown> = {};
  put(p, "model", m?.id);
  if (d.language !== "auto") put(p, "language", d.language);
  put(p, "prompt", d.prompt.trim());
  const fmtOk = d.response_format === "text" || m?.id === "whisper-1";
  put(p, "response_format", fmtOk ? d.response_format : "text");
  const problem = !d.source ? "先放一段音檔。" : unavailable(m);
  const dur = d.source?.duration;
  const body: GenRequest = { kind: "transcript", params: p, sources: d.source ? { audio: d.source.ref } : undefined, duration_s: dur && Number.isFinite(dur) ? Math.round(dur * 10) / 10 : undefined };
  return { body, req: problem ? null : body, problem, verb: "轉錄" };
}

function unavailable(m: GenModel | null): string | null {
  if (!m) return "還沒有可用的模型。";
  if (!m.available) return `${m.id} 現在送不出去：${unavailableText(m)}。`;
  return null;
}

/** 反灰模型的原因（純文字版；畫面上 env 會包 .code） */
export function unavailableText(m: GenModel): string {
  const u = m.unavailable;
  if (!u) return "不可用";
  if (u.reason === "missing_key") return `缺 ${u.env ?? "API key"}`;
  if (u.reason === "not_implemented") return "這一版還沒接上";
  if (u.reason === "offline") return "離線模式不呼叫供應商";
  if (u.reason === "retired") return "原廠已關閉";
  return u.reason;
}

/* ---------------- 生成工作 → 草稿（「帶回表單」） ---------------- */

const str = (v: unknown, dflt = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : dflt);

/** 回傳要合併進該模態草稿的欄位 */
type Params = Record<string, unknown>;
/** 每一種模態：生成工作的 params → 草稿欄位 */
const FROM_GENERATION: { [K in GenKind]: (p: Params, model: string | null, g: Generation) => Partial<Drafts[K]> } = {
  image: (p, model, g) => {
    const src = g.sources?.images?.[0];
    const more = (g.sources?.images ?? []).slice(1).map((v) => sourceFromView(v, "image"));
    const out: Partial<ImageDraft> = { prompt: str(p.prompt), model, source: src ? sourceFromView(src, "image") : null, extra: more };
    if (typeof p.size === "string") out.size = p.size;
    if (typeof p.quality === "string") out.quality = p.quality;
    if (typeof p.background === "string") out.background = p.background;
    if (typeof p.output_format === "string") out.output_format = p.output_format;
    if (typeof p.n === "number") out.n = p.n;
    if (typeof p.image_size === "string") out.image_size = p.image_size;
    if (typeof p.aspect_ratio === "string") out.aspect_ratio = p.aspect_ratio;
    return out;
  },
  speech: (p, model) => ({ text: str(p.text), model, instructions: str(p.instructions), speed: str(p.speed), language_code: str(p.language_code) }),
  music: (p, model) => {
    const custom = p.custom_mode === true;
    const out: Partial<MusicDraft> = { model, instrumental: p.instrumental === true };
    if (custom) Object.assign(out, { mode: "lyrics", lyrics: str(p.prompt), style: str(p.style), title: str(p.title), vocal_gender: p.vocal_gender === "m" || p.vocal_gender === "f" ? p.vocal_gender : "", negative_tags: str(p.negative_tags) });
    else Object.assign(out, { mode: "describe", prompt: str(p.prompt) });
    if (typeof p.music_length_ms === "number") out.length_ms = p.music_length_ms;
    return out;
  },
  transcript: (p, model, g) => {
    const src = g.sources?.audio;
    return {
      model,
      source: src ? sourceFromView(src, "audio") : null,
      language: str(p.language, "auto") || "auto",
      prompt: str(p.prompt),
      response_format: str(p.response_format, "text") || "text",
    };
  },
  video: (p, model, g) => videoPatch(p, model, g.sources?.frames?.first ? sourceFromView(g.sources.frames.first, "image") : null, g.sources?.frames?.last ? sourceFromView(g.sources.frames.last, "image") : null),
};

function videoPatch(p: Params, model: string | null, first: PickedSource | null, last: PickedSource | null): Partial<VideoDraft> {
  return {
    prompt: str(p.prompt),
    model,
    duration: typeof p.duration === "number" ? p.duration : 0,
    resolution: str(p.resolution),
    aspect_ratio: str(p.aspect_ratio),
    audio: p.generate_audio === true ? "on" : p.generate_audio === false ? "off" : "",
    first,
    last,
  };
}

/** 回傳要合併進該模態草稿的欄位 */
export function draftFromGeneration(g: Generation): Partial<Drafts[GenKind]> {
  const p = g.params ?? {};
  const model = typeof p.model === "string" ? p.model : null;
  if (!isGenKind(g.kind)) {
    warnUnknown("生成模態", g.kind);
    return {};
  }
  return FROM_GENERATION[g.kind](p, model, g);
}

function sourceFromView(v: { artifact_id?: string; upload_id?: string; name: string | null; file_url: string; thumb_url: string | null }, slot: "image" | "audio"): PickedSource {
  const ref: SourceRef = v.artifact_id ? { artifact_id: v.artifact_id } : { upload_id: v.upload_id! };
  return { ref, name: v.name, url: slot === "image" ? (v.thumb_url ? `${v.thumb_url}?w=480` : v.file_url) : v.file_url, from: v.artifact_id ? "artifact" : "upload" };
}

/* ---------------- 作品 → 草稿（作品牆的「用同樣設定再生一次」，1.1-M4） ---------------- */

/** 作品當成來源檔（圖＝改圖的來源，音檔＝轉錄的音檔） */
export function sourceFromArtifact(a: Artifact): PickedSource {
  const name = a.title || baseName(a.file_path) || a.id;
  if (mediaOf(a.kind) === "image") return { ref: { artifact_id: a.id }, name, url: a.thumb_url ? `${a.thumb_url}?w=480` : a.file_url, from: "image", bytes: a.bytes, mime: a.mime };
  return { ref: { artifact_id: a.id }, name, url: a.file_url, from: String(a.kind), bytes: a.bytes, mime: a.mime, duration: a.duration_s };
}

export interface ArtifactDraft {
  kind: GenKind;
  patch: Partial<Drafts[GenKind]>;
  /** 一句話：帶入了什麼 */
  what: string;
  /** 資料不全的說明 */
  note: string | null;
}

/** 作品有沒有可以照著再生一次的設定（回填的語音、音樂沒有；歌詞是作曲的副產品，不能單獨再生） */
const CAN_REGENERATE: Record<GenKind, (a: Artifact, p: Params) => boolean> = {
  image: (a, p) => !!(a.prompt ?? p.prompt),
  speech: (a, p) => !!(a.prompt ?? p.text),
  music: (a, p) => !!(a.prompt ?? p.prompt),
  transcript: (a, p) => !!(a.tool || a.model || Object.keys(p).length), // 逐字稿：有模型或設定就算
  video: (a, p) => !!(a.prompt ?? p.prompt),
};
export function canRegenerate(a: Artifact): boolean {
  if (!isGenKind(a.kind)) return false;
  return CAN_REGENERATE[a.kind](a, a.params ?? {});
}

/** 作品＋（它的來源）→ 對應表單的草稿欄位。prevVoice＝語音草稿現有的聲音表（換 provider 不洗掉別家的） */
export function draftFromArtifact(a: Artifact, parent: Artifact | null, prevVoice: Record<string, string> = {}): ArtifactDraft | null {
  if (!canRegenerate(a) || !isGenKind(a.kind)) return null;
  const p = a.params ?? {};
  const model = a.model ?? (typeof p.model === "string" ? p.model : null);
  return FROM_ARTIFACT[a.kind](a, p, model, parent, prevVoice);
}

type FromArtifact = (a: Artifact, p: Params, model: string | null, parent: Artifact | null, prevVoice: Record<string, string>) => ArtifactDraft;
/** 每一種模態：作品＋它的來源 → 草稿 */
const FROM_ARTIFACT: Record<GenKind, FromArtifact> = {
  image: (a, p, model, parent) => {
    const edit = a.tool === "edit_image" || !!a.parent_id;
    const src = edit && parent && parent.kind === "image" && parent.exists !== false ? sourceFromArtifact(parent) : null;
    const out: Partial<ImageDraft> = { prompt: str(a.prompt ?? p.prompt), model, source: src, extra: [] };
    if (typeof p.size === "string") out.size = p.size;
    if (typeof p.quality === "string") out.quality = p.quality;
    if (typeof p.background === "string") out.background = p.background;
    if (typeof p.output_format === "string") out.output_format = p.output_format;
    if (typeof p.n === "number") out.n = p.n;
    if (typeof p.image_size === "string") out.image_size = p.image_size;
    if (typeof p.aspect_ratio === "string") out.aspect_ratio = p.aspect_ratio;
    return {
      kind: "image",
      patch: out,
      what: src ? "提示詞、設定與當時的來源圖" : "提示詞與設定",
      note: edit && !src ? "這張是改圖，當時的來源圖沒有留存：送出會變成生圖。" : null,
    };
  },
  speech: (a, p, model, _parent, prevVoice) => {
    const voice = typeof p.voice === "string" && a.provider ? { ...prevVoice, [a.provider]: p.voice } : prevVoice;
    const out: Partial<SpeechDraft> = { text: str(a.prompt ?? p.text), model, voice, instructions: str(p.instructions), speed: str(p.speed), language_code: str(p.language_code) };
    return { kind: "speech", patch: out, what: "要念的文字與設定", note: null };
  },
  music: (a, p, model) => {
    const custom = p.custom_mode === true;
    const out: Partial<MusicDraft> = { model, instrumental: p.instrumental === true };
    if (custom)
      Object.assign(out, {
        mode: "lyrics",
        lyrics: str(a.prompt ?? p.prompt),
        lyricsFrom: null,
        style: str(p.style),
        title: str(p.title ?? a.title),
        vocal_gender: p.vocal_gender === "m" || p.vocal_gender === "f" ? p.vocal_gender : "",
        negative_tags: str(p.negative_tags),
      });
    else Object.assign(out, { mode: "describe", prompt: str(a.prompt ?? p.prompt) });
    if (typeof p.music_length_ms === "number") out.length_ms = p.music_length_ms;
    return { kind: "music", patch: out, what: custom ? "歌詞、風格與設定" : "描述與設定", note: null };
  },
  transcript: (_a, p, model, parent) => {
    // 逐字稿：音檔就是它的 parent
    const src = parent && mediaOf(parent.kind) === "audio" && parent.exists !== false ? sourceFromArtifact(parent) : null;
    const out: Partial<TranscriptDraft> = {
      model,
      source: src,
      language: str(p.language, "auto") || "auto",
      prompt: str(p.prompt),
      response_format: str(p.response_format, "text") || "text",
    };
    return { kind: "transcript", patch: out, what: src ? "設定與當時的音檔" : "設定", note: src ? null : "當時的音檔沒有留存：要先放一段音檔才能送出。" };
  },
  video: (a, p, model, parent) => {
    // 首幀＝parent（作品）；從電腦選的首幀、尾幀記在 meta.frames（上傳檔用編號取）
    const fr = a.meta?.frames ?? null;
    const fromRef = (r: { artifact_id?: string; upload_id?: string } | undefined, isParent: boolean): PickedSource | null => {
      if (!r) return null;
      if (r.artifact_id) {
        if (isParent && parent && parent.id === r.artifact_id) return parent.exists !== false && mediaOf(parent.kind) === "image" ? sourceFromArtifact(parent) : null;
        return { ref: { artifact_id: r.artifact_id }, name: null, url: `/api/artifacts/${encodeURIComponent(r.artifact_id)}/thumb?w=480`, from: "image" };
      }
      return r.upload_id ? { ref: { upload_id: r.upload_id }, name: null, url: `/api/uploads/${encodeURIComponent(r.upload_id)}/file`, from: "upload" } : null;
    };
    const first = fr?.first ? fromRef(fr.first, true) : parent && mediaOf(parent.kind) === "image" && parent.exists !== false ? sourceFromArtifact(parent) : null;
    const last = fromRef(fr?.last, false);
    const what = first && last ? "提示詞、設定與首幀、尾幀" : first ? "提示詞、設定與首幀" : "提示詞與設定";
    const lost = fr?.first && !first ? "當時的首幀已經不在：送出會變成文生影片。" : null;
    return { kind: "video", patch: videoPatch({ ...p, prompt: a.prompt ?? p.prompt }, model, first, last), what, note: lost };
  },
};

/** 生成工作 → 再送一次的請求（同一組 params＋sources，只送 id） */
export function retryRequest(g: Generation): GenRequest {
  const sources: GenRequest["sources"] = {};
  const ref = (s: { artifact_id?: string; upload_id?: string }): SourceRef => (s.artifact_id ? { artifact_id: s.artifact_id } : { upload_id: s.upload_id! });
  if (g.sources?.images?.length) sources.images = g.sources.images.map(ref);
  if (g.sources?.audio) sources.audio = ref(g.sources.audio);
  if (g.sources?.frames && (g.sources.frames.first || g.sources.frames.last)) {
    sources.frames = {};
    if (g.sources.frames.first) sources.frames.first = ref(g.sources.frames.first);
    if (g.sources.frames.last) sources.frames.last = ref(g.sources.frames.last);
  }
  return { kind: g.kind, params: { ...g.params }, sources: Object.keys(sources).length ? sources : undefined };
}

/* ---------------- 顯示 ---------------- */

const p4 = (v: unknown) => (typeof v === "number" ? `$${+v.toFixed(4)}` : null);

/** 模型清單右欄的價格（依 pricing.unit） */
export function priceText(m: GenModel): string | null {
  const p = m.pricing;
  if (!p) return null;
  switch (p.unit) {
    case "per_image": {
      const v = p["2K"] ?? p["1K"];
      return p4(v) ? `${p4(v)}／張` : null;
    }
    case "per_1m_tokens":
      if (p.image_output != null) return `${p4(p.image_output)}／1M 出`;
      if (p.audio_output != null) return `${p4(p.audio_output)}／1M 音`;
      if (p.video_output != null) return `${p4(p.video_output)}／1M 影`;
      return p4(p.text_input) ? `${p4(p.text_input)}／1M` : null;
    case "per_1k_chars":
      return p4(p.text) ? `${p4(p.text)}／千字` : null;
    case "per_1m_chars":
      return p4(p.text) ? `${p4(p.text)}／百萬字` : null;
    case "per_minute":
      return p4(p.audio) ? `${p4(p.audio)}／分` : null;
    case "per_song":
      return p4(p.song) ? `${p4(p.song)}／首` : null;
    case "credits":
      return "點數";
    case "openrouter_video": {
      // 1.4-M3：名單的 SKU；每秒的那幾條取最低價（按 token 計價的寫「依用量」）
      const v = orVideoPerSecond(p);
      if (!v) return null;
      if (v.low == null) return "依用量";
      return `${p4(v.low)}${v.high !== v.low ? " 起" : ""}／秒`;
    }
    case "openrouter": {
      // OpenRouter 名單上的價：有分級就寫起價
      const pm = orPriceMain(orLines(p));
      if (!pm) return null;
      const lo = pm.main.split("–")[0];
      if (pm.sub.startsWith("每張")) return pm.main.includes("–") ? `${lo} 起／張` : `${lo}／張`;
      if (pm.sub.startsWith("每百萬像素")) return `${lo}／MP`;
      return `${lo}／1M 出`;
    }
    default:
      return null;
  }
}

/** 秒 → `m:ss` */
export const msShort = (s: number | null | undefined): string => {
  if (s == null || !Number.isFinite(s)) return "…";
  const t = Math.max(0, Math.round(s));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
};
/** 秒 → `mm:ss`（已等） */
export const mmss = (s: number): string => {
  const t = Math.max(0, Math.floor(s));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};
export const fmtBytes = (b: number | null | undefined): string => (b == null ? "—" : b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
/** 路徑或檔名 → 檔名 */
export const baseName = (p: string | null | undefined): string | null => (p ? p.split(/[\\/]/).pop() ?? p : null);
/** 標題第一行（歌詞跳過 [Verse] 這類段落標記） */
export const firstLine = (t: string | null | undefined): string =>
  (t ?? "").split("\n").map((x) => x.trim()).find((x) => x && !/^\[.*\]$/.test(x)) ?? (t ?? "").trim();
