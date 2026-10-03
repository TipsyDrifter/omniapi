import { useState, type ReactNode } from "react";
import type { ChatAttachment, FileDelivery, FileInfo, FileRead, ModelEntry } from "@/api/types";
import { AudioPlayer, fmtBytes, msShort } from "@/components/make";

/* 1.2-M5 檔案附件：輸入框、訊息、回覆共用的小元件與判斷（版面稿 prototypes/chat-upgrade/m4-m5-design，情境 9–14）。
   檔案沒有縮圖：一張「折角紙」寫副檔名（不是模態章，也不是事件章）。
   「這個模型會怎麼讀它」在輸入框當下推一句（跟著細列的模型變）；實際怎麼送的看回覆的 meta.files（後端 chat/delivery.py 為準）。 */

/* ---------------- 模型的能力 ---------------- */

/** 下一則的模型收什麼；null＝還不知道（模型清單沒讀到），不擋、不猜 */
export interface ModelCaps {
  vision: boolean | null;
  pdf: boolean | null;
  audio: boolean | null;
  tools: boolean | null;
}

/** 目錄旗標為準；目錄不認得的同後端當成都不收（1.2-M2-a、1.2-M5-d）；清單還沒讀到＝不知道 */
export function capsOf(entry: ModelEntry | null, loaded: boolean): ModelCaps {
  if (!loaded) return { vision: null, pdf: null, audio: null, tools: null };
  const c = entry?.capabilities ?? {};
  return { vision: c.vision === true, pdf: c.pdf === true, audio: c.audio === true, tools: c.tools === true };
}

/* ---------------- 同後端的常數（chat/delivery.py） ---------------- */
/** 一個檔整份放進訊息的字數上限（MAX_INLINE_FILE_CHARS） */
const INLINE_CHARS = 30_000;
/** 原樣送的單檔上限（base64 後，NATIVE_MAX_FILE_B64） */
const NATIVE_B64 = 10 * 1024 * 1024;
/** 原樣送的 PDF 頁數上限（NATIVE_PDF_MAX_PAGES） */
const NATIVE_PAGES = 100;
/** 原樣送得出去的音訊格式（NATIVE_AUDIO_FORMATS） */
const NATIVE_AUDIO = ["wav", "mp3"];
const b64 = (n: number | null | undefined) => Math.ceil((n ?? 0) / 3) * 4;

/** 打不開＝檔案本身壞了（不管哪個模型）；讀不了＝不認得的格式 */
const BROKEN = new Set(["corrupt", "encrypted", "empty", "too_large", "timeout", "missing"]);
export const isBroken = (info: FileInfo | null | undefined): boolean => info?.readable === false && BROKEN.has(info.reason ?? "");
export const isUnknown = (info: FileInfo | null | undefined): boolean => info?.readable === false && !BROKEN.has(info.reason ?? "") && info.reason !== "no_text";

/* ---------------- 檔案的事實 ---------------- */

/** 副檔名（折角紙上那幾個字）；沒有就用類型 */
export function extOf(name: string | null | undefined, info?: FileInfo | null): string {
  const m = /\.([A-Za-z0-9]{1,5})$/.exec(name ?? "");
  if (m) return m[1].toUpperCase();
  return info?.type === "transcript" ? "TXT" : info?.type === "lyrics" ? "TXT" : "FILE";
}

const n = (v: number) => v.toLocaleString("en-US");

/** 類型 · 大小 · 頁數或列數（音檔：類型 · 長度 · 大小）；回傳幾段字，第一段是類型。compact＝輸入框的窄卡（不寫欄數、工作表數） */
export function factsOf(name: string | null | undefined, info: FileInfo | null | undefined, bytes: number | null | undefined, kind?: string, compact = false): string[] {
  const type = info?.type_name ?? (kind === "audio" ? "音檔" : extOf(name, info));
  const size = bytes == null ? null : bytes === 0 ? "0 KB" : fmtBytes(bytes);
  if (kind === "audio" || info?.type === "audio") return [type, info?.duration_s != null ? msShort(info.duration_s) : null, size].filter((x): x is string => !!x);
  let extra: string | null = null;
  if (info?.pages) extra = `${n(info.pages)} 頁`;
  else if (info?.slides) extra = `${n(info.slides)} 張投影片`;
  else if (info?.sheets && info.sheets.length > 1 && !compact) extra = `${info.sheets.length} 個工作表 · ${n(info.rows ?? 0)} 列`;
  else if (info?.rows) extra = info.cols && !compact ? `${n(info.rows)} 列 · ${n(info.cols)} 欄` : `${n(info.rows)} 列`;
  else if (info?.readable && info.chars) extra = `${n(info.chars)} 字`;
  return [type, size, extra].filter((x): x is string => !!x);
}

/** 第一段（類型）一般字，其餘數字用等寬 */
export function Facts({ parts }: { parts: string[] }) {
  return (
    <>
      {parts.map((p, i) => (
        <span key={i}>
          {i ? " · " : ""}
          {i ? <span className="n">{p}</span> : p}
        </span>
      ))}
    </>
  );
}

/** 折角紙：寫副檔名；dash＝還沒成真（上傳中）或讀不了 */
export function FileDoc({ ext, dash }: { ext: string; dash?: boolean }) {
  return (
    <span className={`fdoc${dash ? " dash" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 30 38">
        <path className="pg" d="M1 1H20L29 10V37H1Z" />
        <path className="ear" d="M20 1V10H29Z" />
      </svg>
      <b>{ext.slice(0, 4)}</b>
    </span>
  );
}

/* ---------------- 這個模型會怎麼讀它（輸入框當下推的一句） ---------------- */

export interface HowRead {
  text: string;
  /** 墨 70（不是結論，只是還不知道或讀不了） */
  soft?: boolean;
}

/** 依模型的 pdf／audio／tools 與檔案的 info 推一句（同後端 delivery.decide 的順序）；caps 不知道時不猜 */
export function howRead(f: { kind: string; info?: FileInfo | null; bytes?: number | null }, caps: ModelCaps): HowRead {
  const info = f.info ?? null;
  if (f.kind === "audio") {
    if (caps.audio == null) return { text: "依模型決定怎麼送", soft: true };
    const fmt = (info?.format ?? "").toLowerCase();
    if (caps.audio && NATIVE_AUDIO.includes(fmt) && b64(f.bytes) <= NATIVE_B64) return { text: "原樣送出（模型直接聽）" };
    return { text: "要先轉錄，送出後會問你" };
  }
  if (!info) return { text: "正在看檔案內容…", soft: true };
  if (info.type === "pdf" && !["encrypted", "corrupt", "empty"].includes(info.reason ?? "")) {
    if (caps.pdf == null) return { text: "依模型決定怎麼送", soft: true };
    if (caps.pdf && (info.pages ?? 0) <= NATIVE_PAGES && b64(f.bytes) <= NATIVE_B64) return { text: "原樣送出（看得到版面）" };
  }
  if (info.readable === false) return { text: isBroken(info) ? "打不開" : "讀不了", soft: true };
  if ((info.chars ?? 0) > INLINE_CHARS) return { text: caps.tools ? "只送開頭，其餘用工具讀" : "太長，只送開頭" };
  return { text: "抽出文字" };
}

/** 讀不了的那一句（輸入框底下；不擋送出） */
export function unreadableNote(name: string, info: FileInfo): string {
  const why = info.reason === "binary" || !info.reason_text ? "這種格式讀不了" : info.reason_text;
  return `${name}：${why}。照樣可以送，模型只會知道有這個檔、看不到內容。`;
}

/* ---------------- 訊息裡的檔案單 ---------------- */

/** 訊息裡的一張檔案單（整張點下去就下載；音檔多播放鈕）。只放檔案本身的事實，不放「模型怎麼看」 */
export function FileSlip({ a }: { a: ChatAttachment }) {
  const name = a.name ?? "（沒有檔名）";
  const ext = extOf(a.name, a.info);
  const parts = factsOf(a.name, a.info, a.bytes, a.kind);
  const href = a.download_url ?? `${a.file_url}?download=true`;
  const text = (
    <span className="mf-t">
      <span className="mf-n">{name}</span>
      <span className="mf-m">
        <Facts parts={parts} />
      </span>
    </span>
  );
  if (!a.exists)
    return (
      <span className="mf bad" title={`${name}\n檔案已不在`}>
        <FileDoc ext={ext} dash />
        {text}
        <span className="errstamp">檔案已不在</span>
      </span>
    );
  if (a.kind === "audio")
    return (
      <div className="mf au" title={name}>
        <FileDoc ext={ext} />
        {text}
        <AudioPlayer src={a.file_url} slip />
        <a className="mf-dl" href={href} download title={`下載 ${name}`}>
          下載
        </a>
      </div>
    );
  if (isBroken(a.info))
    return (
      <a className="mf bad" href={href} download title={`下載 ${name}（原檔照樣可以下載）\n${a.info?.reason_text ?? ""}`}>
        <FileDoc ext={ext} />
        {text}
        <span className="errstamp">打不開</span>
      </a>
    );
  if (isUnknown(a.info))
    return (
      <a className="mf unk" href={href} download title={`下載 ${name}\n${a.info?.reason_text ?? "這種格式讀不了"}`}>
        <FileDoc ext={ext} dash />
        {text}
        <span className="mf-unk">讀不了</span>
      </a>
    );
  return (
    <a className="mf" href={href} download title={`下載 ${name}`}>
      <FileDoc ext={ext} />
      {text}
      <span className="mf-dl">下載</span>
    </a>
  );
}

/* ---------------- 回覆的「檔案」小字：模型怎麼看這個檔 ---------------- */

/** 打了折扣的（只送了一部分、沒送出、打不開）才加底線 */
function deliveryLine(e: FileDelivery): { body: ReactNode; cut: boolean } {
  const B = ({ children }: { children: ReactNode }) => <b>{children}</b>;
  if (e.mode === "native")
    return { body: e.format === "input_audio" ? "原樣送出，模型直接聽" : e.pages ? `原樣送出，看得到版面與圖表（${n(e.pages)} 頁）` : "原樣送出，看得到版面與圖表", cut: false };
  if (e.mode === "text") {
    const head = e.transcribed ? `轉成文字後送出（逐字稿 ${n(e.chars ?? 0)} 字）` : `抽出文字（${n(e.chars ?? 0)} 字）`;
    return { body: e.truncated ? <>{head}，<B>抽取時有截斷</B></> : head, cut: !!e.truncated };
  }
  if (e.mode === "excerpt") {
    const rest = e.tools ? "；後面的由模型自己用工具讀" : "；後面的這次沒有給模型";
    if (e.rows)
      return { body: <>抽出文字，<B>只送了前 {n(e.sent_rows ?? 0)} 列</B>（共 {n(e.rows)} 列）{rest}</>, cut: true };
    return { body: <>抽出文字，<B>只送了開頭 {n(e.sent_chars ?? 0)} 字</B>（共 {n(e.chars ?? 0)} 字）{rest}</>, cut: true };
  }
  // unreadable
  if (e.reason === "audio_unheard") return { body: <><B>沒有送出內容</B>（{e.reason_text ?? "這個模型聽不到音檔，也沒有轉成文字"}）</>, cut: true };
  if (e.reason === "missing") return { body: <><B>檔案已不在</B>，沒有送出</>, cut: true };
  if (BROKEN.has(e.reason ?? "")) return { body: <><B>打不開</B>（{e.reason_text ?? "檔案可能壞了"}），沒有送出</>, cut: true };
  return { body: <><B>讀不了</B>（{e.reason_text ?? "這種格式讀不了"}），沒有送出</>, cut: true };
}

/** 回覆資訊列下的「檔案」小字：這則問題附的檔一個一行；較早訊息的檔只列打了折扣的，其餘併一句 */
export function FileNotes({ files, question }: { files?: FileDelivery[]; question?: string | null }) {
  if (!files?.length) return null;
  const mine = files.filter((e) => !question || !e.message_id || e.message_id === question);
  const older = files.filter((e) => !mine.includes(e));
  const lines = mine.map((e) => ({ e, ...deliveryLine(e) }));
  const olderCut = older.map((e) => ({ e, ...deliveryLine(e) })).filter((x) => x.cut);
  const olderOk = older.length - olderCut.length;
  const rows: ReactNode[] = [];
  if (lines.length >= 3 && lines.every((x) => !x.cut && x.e.mode !== "text")) {
    rows.push(<li key="all">{lines.length} 個都整份原樣送出</li>);
  } else if (lines.length >= 3 && lines.every((x) => !x.cut)) {
    const pdf = lines.some((x) => x.e.mode === "native" && x.e.format !== "input_audio");
    rows.push(<li key="all">{lines.length} 個都整份送出{pdf ? "（PDF 看得到版面）" : ""}</li>);
  } else {
    for (const x of lines)
      rows.push(
        <li key={x.e.id + (x.e.message_id ?? "")}>
          <span className="code">{x.e.name}</span>
          {x.body}
        </li>,
      );
  }
  for (const x of olderCut)
    rows.push(
      <li key={`o-${x.e.id}-${x.e.message_id ?? ""}`}>
        <span className="code">{x.e.name}</span>
        （較早的訊息）{x.body}
      </li>,
    );
  if (olderOk > 0) rows.push(<li key="older">較早訊息的 {olderOk} 個檔也一起送出</li>);
  return (
    <div className="cm-fnote">
      <span className="k">檔案</span>
      <ul>{rows}</ul>
    </div>
  );
}

/* ---------------- 「讀」章：模型用工具看檔案（同「想」那一族的事件章，實線圈） ---------------- */

/** 一步讀檔 → 一句人話 */
function readText(r: FileRead): ReactNode {
  const name = r.name ? <span className="code">{r.name}</span> : null;
  if (r.tool === "list_files") return "看了這段聊天裡有哪些檔案";
  if (r.tool === "search_file")
    return (
      <>
        在{name ?? "全部檔案"}裡{r.what ?? "搜尋"}
      </>
    );
  return (
    <>
      讀{" "}
      {name}
      {r.what ? ` ${r.what}` : ""}
    </>
  );
}

const readRight = (r: FileRead): string => (r.error ? "沒讀到" : r.tool === "list_files" ? (/（(\d+) 個）/.exec(r.what ?? "")?.[1] ?? "") + " 個" : r.chars ? `約 ${n(r.chars)} 字` : "");

/** 摘要：讀了哪幾個檔的哪幾段（最多列三段） */
function readSummary(reads: FileRead[]): ReactNode {
  const got = reads.filter((r) => !r.error && r.tool === "read_file");
  if (!got.length) {
    const s = reads.find((r) => r.tool === "search_file");
    if (s) return <>{readText(s)}</>;
    return reads.some((r) => r.tool === "list_files") ? "看了這段聊天裡有哪些檔案" : "想讀檔，但沒讀到內容";
  }
  const head = got.slice(0, 3);
  return (
    <>
      讀了{" "}
      {head.map((r, i) => (
        <span key={i}>
          {i ? "、" : ""}
          {r.name ? <span className="code">{r.name}</span> : null}
          {r.what ? ` ${r.what}` : ""}
        </span>
      ))}
      {got.length > head.length ? ` 等 ${got.length} 段` : ""}
    </>
  );
}

/** 回覆上方一行：圓章「讀」＋摘要，點開才列每一步與大約字數；live＝回覆進行中（虛線圈＋滾筒「正在讀…」） */
export function ReadLine({ reads, reading }: { reads?: FileRead[] | null; reading?: FileRead | null }) {
  const [open, setOpen] = useState(false);
  const list = reads ?? [];
  if (reading)
    return (
      <div className="cm-read live" role="status">
        <span className="cm-read-gl" aria-hidden="true">
          讀
        </span>
        <div>
          <span className="cm-read-live">
            <span className="drum" aria-hidden="true" />
            {reading.tool === "list_files" ? (
              "正在看這段聊天裡有哪些檔案…"
            ) : reading.tool === "search_file" ? (
              <>
                正在{reading.name ? <> <span className="code">{reading.name}</span> 裡</> : "全部檔案裡"}{reading.what ?? "搜尋"}…
              </>
            ) : (
              <>
                正在讀 {reading.name ? <span className="code">{reading.name}</span> : null}
                {reading.what ? ` ${reading.what}` : ""}…
              </>
            )}
          </span>
        </div>
      </div>
    );
  if (!list.length) return null;
  return (
    <div className="cm-read">
      <span className="cm-read-gl" aria-hidden="true">
        讀
      </span>
      <div>
        <button type="button" className="cm-read-s" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <span>{readSummary(list)}</span>
          <span className="cs-car">{open ? "▾" : "▸"}</span>
        </button>
        {open ? (
          <>
            <ol className="cm-read-l">
              {list.map((r, i) => (
                <li key={i}>
                  <span className="n">{i + 1}</span>
                  <span>{readText(r)}</span>
                  <span className="r">{readRight(r)}</span>
                </li>
              ))}
            </ol>
            <p className="cm-read-f">讀到的內容算在這則的輸入 token 裡（上面的 token 數已經含在內）。</p>
          </>
        ) : null}
      </div>
    </div>
  );
}
