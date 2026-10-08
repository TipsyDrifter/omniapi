/* 生成模態的單一來源（1.4-M1）。後端的對照表在 mcp/omniapi_mcp/modalities.py；
   mcp/tests/unit/test_modalities.py 會讀這個檔，兩邊不一致就失敗——所以下面幾張表的寫法（一筆一行、鍵與值的形狀）請保持。

   加一種模態：GEN_KINDS 加一個名字，GEN_KIND_INFO 與 WORK_KIND_INFO 各補一列；
   其餘以 Record<GenKind, …>／Record<ArtifactKind, …>／Covers<…> 寫的對照表會在 typecheck 時報缺，照著補。
   伺服器送來不認得的值（舊前端配新後端）不丟例外：呼叫端走保底呈現，這裡在 console 警告一次。 */

/** 生成的模態（＝/make/:kind、生成工作的 kind） */
export const GEN_KINDS = ["image", "speech", "music", "transcript", "video"] as const;
export type GenKind = (typeof GEN_KINDS)[number];

/** 不是生成模態、但作品有的種類：歌詞（作曲的副產品） */
export const EXTRA_WORK_KINDS = ["lyrics"] as const;
/** 作品的種類（順序＝後端 WORK_KINDS） */
export const WORK_KINDS = [...GEN_KINDS, ...EXTRA_WORK_KINDS] as const;
export type ArtifactKind = (typeof WORK_KINDS)[number];

/** 模型目錄的模態（畫面上的排列順序；文字不是生成模態，聊天用） */
export const CATALOG_MODALITIES = ["text", "image", "speech", "music", "transcription", "video"] as const;
export type Modality = (typeof CATALOG_MODALITIES)[number];

/** 檔案是什麼：決定縮圖、播放器、卡片樣式、當附件時算什麼 */
export type Media = "image" | "audio" | "text" | "video";

export interface GenKindInfo {
  /** 目錄裡的模態名（transcript ↔ transcription 只在這裡對照） */
  modality: Modality;
  /** 生成頁分頁鈕的字 */
  zh: string;
  en: string;
  /** 設定頁「預設模型」那一列的名字 */
  def: string;
}
// prettier-ignore
export const GEN_KIND_INFO = {
  image: { modality: "image", zh: "圖片", en: "IMAGE", def: "生圖" },
  speech: { modality: "speech", zh: "語音", en: "SPEECH", def: "語音" },
  music: { modality: "music", zh: "音樂", en: "MUSIC", def: "音樂" },
  transcript: { modality: "transcription", zh: "轉錄", en: "TRANSCRIBE", def: "轉錄" },
  video: { modality: "video", zh: "影片", en: "VIDEO", def: "影片" },
} as const satisfies Record<GenKind, GenKindInfo>;

export interface WorkKindInfo {
  media: Media;
  /** 模態章的字 */
  glyph: string;
  /** 作品牆、燈箱的名字 */
  zh: string;
  en: string;
}
// prettier-ignore
export const WORK_KIND_INFO = {
  image: { media: "image", glyph: "圖", zh: "圖片", en: "IMAGE" },
  speech: { media: "audio", glyph: "聲", zh: "語音", en: "SPEECH" },
  music: { media: "audio", glyph: "曲", zh: "音樂", en: "MUSIC" },
  transcript: { media: "text", glyph: "字", zh: "逐字稿", en: "TRANSCRIPT" },
  video: { media: "video", glyph: "影", zh: "影片", en: "VIDEO" },
  lyrics: { media: "text", glyph: "詞", zh: "歌詞", en: "LYRICS" },
} as const satisfies Record<ArtifactKind, WorkKindInfo>;

/** 型別層的完整性檢查：`const _ok: Covers<Drafts, GenKind> = true;`——T 少了 K 的哪個鍵，這行就編譯不過（錯誤訊息會寫出缺哪個） */
export type Covers<T, K extends PropertyKey> = [Exclude<K, keyof T>] extends [never] ? true : { missing: Exclude<K, keyof T> };

/* ---------------- 判斷與保底 ---------------- */

const has = <T extends string>(list: readonly T[], v: unknown): v is T => typeof v === "string" && (list as readonly string[]).includes(v);
export const isGenKind = (k: unknown): k is GenKind => has(GEN_KINDS, k);
export const isWorkKind = (k: unknown): k is ArtifactKind => has(WORK_KINDS, k);
export const isModality = (m: unknown): m is Modality => has(CATALOG_MODALITIES, m);

const warned = new Set<string>();
/** 伺服器送來這一版不認得的值：警告一次（同一個值不重複洗 console） */
export function warnUnknown(what: string, value: unknown): void {
  const key = `${what}\u0000${String(value)}`;
  if (warned.has(key)) return;
  warned.add(key);
  console.warn(`OmniAPI：這一版的畫面不認得${what}「${String(value)}」，先用保底的樣子顯示。`);
}

/** 作品種類 → 檔案是什麼；不認得＝null（並警告），呼叫端自己決定保底 */
export function mediaOf(kind: unknown): Media | null {
  if (isWorkKind(kind)) return WORK_KIND_INFO[kind].media;
  warnUnknown("作品種類", kind);
  return null;
}

/** 某一種 media 的作品種類（照 WORK_KINDS 的順序） */
export const kindsOfMedia = (media: Media): ArtifactKind[] => WORK_KINDS.filter((k) => WORK_KIND_INFO[k].media === media);

/** 目錄模態清單 → 認得的那些；不認得的警告後略過（畫面上的章只有認得的才畫得出來） */
export function knownModalities(list: readonly unknown[] | null | undefined): Modality[] {
  const out: Modality[] = [];
  for (const m of list ?? []) {
    if (isModality(m)) out.push(m);
    else warnUnknown("模態", m);
  }
  return out;
}
