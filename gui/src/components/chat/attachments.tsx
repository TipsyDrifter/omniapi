import { useEffect, useRef, useState, type ClipboardEvent, type ReactNode } from "react";
import { ApiError, api } from "@/api/client";
import type { Artifact, AttachKind, ChatAttachment, ChatAttachmentRef, FileInfo, UploadLimits } from "@/api/types";
import { ImagePicker, TextWorkPicker, artName, fmtBytes, useFileDrop, uploadErrText } from "@/components/make";
import type { OutgoingAttachment } from "@/store/chat";
import { Facts, FileDoc, extOf, factsOf, howRead, isUnknown, unreadableNote, type ModelCaps } from "./files";

/* 聊天的附件（1.2-M2 只有圖，1.2-M5 改成什麼檔都收）：輸入列與編輯舊訊息（1.2-M3）共用。
   貼上、拖進、選檔、從作品牆挑（圖與文字作品）；選了就先上傳（purpose=chat），一排：圖是縮圖，檔案是折角紙
   （檔名、類型・大小・頁數或列數、這個模型會怎麼讀它）。上傳中、上傳失敗、太大都不能送；讀不了的照樣能送。
   一則幾個、一個檔多大以 /api/uploads/limits 為準。模型不會看圖（vision=false）時圖片的規則照舊（不收新的圖、
   已附的圖擋送出），檔案不受影響。 */

export const BLIND = "這個模型不會看圖";

/** 上限：整個分頁讀一次；讀不到就不在前端擋（後端照樣會擋，413） */
let limitsCache: UploadLimits | null = null;
let limitsPending: Promise<UploadLimits> | null = null;
function useLimits(): UploadLimits | null {
  const [l, setL] = useState<UploadLimits | null>(limitsCache);
  useEffect(() => {
    if (limitsCache) return;
    let alive = true;
    limitsPending ??= api.uploadLimits();
    limitsPending
      .then((r) => {
        limitsCache = r;
        if (alive) setL(r);
      })
      .catch(() => {
        limitsPending = null;
      });
    return () => {
      alive = false;
    };
  }, []);
  return l;
}

export interface AttachItem {
  key: string;
  /** big＝超過上限，不上傳、不會送出 */
  status: "uploading" | "ready" | "error" | "big";
  kind: AttachKind;
  name: string;
  /** 圖的縮圖：本機檔用 object URL（上傳前就看得到），作品用它的縮圖 */
  preview: string | null;
  /** 要 revoke 的 object URL */
  obj?: string;
  att?: OutgoingAttachment;
  error?: string;
  bytes?: number | null;
  /** 上傳進度（bytes） */
  loaded?: number;
  info?: FileInfo | null;
  /** 抽取還沒做完（後端 info_pending）：之後補拿 */
  pending?: boolean;
}

let itemSeq = 0;

const IMAGE_RE = /\.(png|jpe?g|webp|gif)$/i;
const AUDIO_RE = /\.(mp3|wav|ogg|opus|flac|aac|m4a|mp4|webm|mpga|mpeg)$/i;
/** 上傳前先猜種類（後端照副檔名判斷，同 daemon/app.py 的清單）：決定縮圖還是折角紙、套哪個大小上限 */
const guessKind = (f: File): AttachKind => (IMAGE_RE.test(f.name) ? "image" : AUDIO_RE.test(f.name) ? "audio" : f.type.startsWith("image/") && !f.name.includes(".") ? "image" : "file");

/** 剪貼簿的圖常常叫 image.png 或沒有副檔名：後端靠副檔名判斷型別，給它一個 */
function named(f: File): File {
  if (IMAGE_RE.test(f.name)) return f;
  const ext = f.type === "image/jpeg" ? "jpg" : f.type.replace(/^image\//, "") || "png";
  const t = new Date();
  const stamp = `${t.getFullYear()}${String(t.getMonth() + 1).padStart(2, "0")}${String(t.getDate()).padStart(2, "0")}-${String(t.getHours()).padStart(2, "0")}${String(t.getMinutes()).padStart(2, "0")}${String(t.getSeconds()).padStart(2, "0")}`;
  return new File([f], `pasted-${stamp}.${ext}`, { type: f.type });
}

/** 上傳失敗 → 一句人話（後端的原因是英文時翻成中文；其餘同生成頁的 uploadErrText） */
function chatUploadErr(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return `${e.message}，拿掉再附一次。`;
    if (e.status === 400 && e.message === "empty upload") return "檔案是空的（0 bytes），沒有內容可以傳。";
  }
  return uploadErrText(e);
}

const kindOf = (a: ChatAttachment): AttachKind => (a.kind === "audio" || a.kind === "file" ? a.kind : "image");

/** 訊息上已經有的附件 → 可以再送一次的附件（編輯舊訊息時沿用） */
function fromExisting(a: ChatAttachment): AttachItem {
  const kind = kindOf(a);
  const ref: ChatAttachmentRef = a.upload_id ? { upload_id: a.upload_id } : { artifact_id: a.artifact_id ?? "" };
  return {
    key: `i${++itemSeq}`,
    status: "ready",
    kind,
    name: a.name ?? "",
    preview: kind === "image" && a.exists ? (a.thumb_url ? `${a.thumb_url}?w=240` : a.file_url) : null,
    att: { ref, view: a },
    bytes: a.bytes ?? null,
    info: a.info ?? null,
  };
}

/** 作品的種類 → 當附件時是什麼（同後端 _WORK_ATTACH_KIND） */
const WORK_KIND: Record<string, AttachKind> = { image: "image", speech: "audio", music: "audio", transcript: "file", lyrics: "file" };

export function useAttachments({ caps, initial }: { caps: ModelCaps; initial?: ChatAttachment[] | null }) {
  const [items, setItems] = useState<AttachItem[]>(() => (initial ?? []).map(fromExisting));
  /** 一行暫時的說明（不收的圖、超過個數），幾秒後自己收掉 */
  const [note, setNote] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const limits = useLimits();
  const max = limits?.max_attachments ?? 10;
  const blind = caps.vision === false;
  const alive = useRef(true);

  // 暫時的說明 4 秒後收掉
  useEffect(() => {
    if (!note) return;
    const t = window.setTimeout(() => setNote(null), 4000);
    return () => window.clearTimeout(t);
  }, [note]);

  // 離開：本機縮圖的 object URL 還回去；之後回來的上傳結果不再寫
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      itemsRef.current.forEach((x) => x.obj && URL.revokeObjectURL(x.obj));
    };
  }, []);

  const patch = (key: string, p: Partial<AttachItem>) => alive.current && setItems((xs) => xs.map((x) => (x.key === key ? { ...x, ...p } : x)));
  const remove = (key: string) => {
    setTouched(true);
    setItems((xs) => {
      const x = xs.find((i) => i.key === key);
      if (x?.obj) URL.revokeObjectURL(x.obj);
      return xs.filter((i) => i.key !== key);
    });
  };
  const room = () => max - itemsRef.current.length;
  const push = (item: AttachItem) => {
    setTouched(true);
    itemsRef.current = [...itemsRef.current, item];
    setItems((xs) => [...xs, item]);
  };

  /** 抽取慢的檔（info_pending）：隔一下再問一次，最多約一分鐘 */
  const follow = (key: string, id: string, tries = 0) => {
    window.setTimeout(() => {
      if (!alive.current || !itemsRef.current.some((x) => x.key === key)) return;
      api
        .uploadRow(id)
        .then((u) => {
          if (u.info_pending && tries < 40) return follow(key, id, tries + 1);
          setItems((xs) =>
            xs.map((x) => (x.key === key && x.att ? { ...x, pending: false, info: u.info ?? null, att: { ...x.att, view: { ...x.att.view, info: u.info ?? null } } } : x)),
          );
        })
        .catch(() => tries < 40 && follow(key, id, tries + 1));
    }, 1500);
  };

  /** 本機檔 → 先放進附件列（上傳中），上傳完換成可送的附件 */
  const addFile = (raw: File) => {
    const guess = guessKind(raw);
    if (guess === "image" && blind) {
      setNote(`${BLIND}，圖沒有附上。換一個會看圖的模型再附（檔案照樣可以附）。`);
      return;
    }
    if (room() <= 0) {
      setNote(`一則最多附 ${max} 個（圖與檔案一起算）。`);
      return;
    }
    const f = guess === "image" && raw.type.startsWith("image/") ? named(raw) : raw;
    const key = `i${++itemSeq}`;
    const obj = guess === "image" ? URL.createObjectURL(f) : undefined;
    const cap = limits?.max_bytes?.[guess];
    if (cap && f.size > cap) {
      push({ key, status: "big", kind: guess, name: f.name, preview: obj ?? null, obj, bytes: f.size, error: `${f.name} 是 ${fmtBytes(f.size)}，一個${guess === "audio" ? "音檔" : guess === "image" ? "圖" : "檔"}最多 ${fmtBytes(cap)}。可以先剪短、壓縮，或只附需要的那一段。` });
      return;
    }
    push({ key, status: "uploading", kind: guess, name: f.name, preview: obj ?? null, obj, bytes: f.size, loaded: 0 });
    api
      .uploadChat(f, (loaded) => patch(key, { loaded }))
      .then((u) => {
        // 後端說了算：壞掉的圖收成一般檔案（1.2-M5-k），縮圖收起來
        const kind = u.kind;
        const file_url = u.file_url ?? api.uploadFileUrl(u.id);
        const view: ChatAttachment = {
          kind,
          upload_id: u.id,
          name: u.filename,
          file_url,
          thumb_url: null,
          exists: true,
          ...(kind === "image" ? {} : { mime: u.mime, bytes: u.bytes, info: u.info ?? null, download_url: u.download_url }),
        };
        patch(key, {
          status: "ready",
          kind,
          name: u.filename,
          bytes: u.bytes,
          info: u.info ?? null,
          pending: !!u.info_pending,
          ...(kind === "image" ? {} : { preview: null }),
          att: { ref: { upload_id: u.id, kind }, view },
        });
        if (u.info_pending) follow(key, u.id);
      })
      .catch((e: unknown) => patch(key, { status: "error", error: chatUploadErr(e) }));
  };

  /** 從作品牆挑（圖、文字作品）：已經挑過的再點一次＝拿掉 */
  const pickArtifact = (a: Artifact) => {
    const had = itemsRef.current.find((x) => x.att?.ref.artifact_id === a.id);
    if (had) return remove(had.key);
    const kind = WORK_KIND[a.kind] ?? "file";
    if (kind === "image" && blind) {
      setNote(`${BLIND}，圖沒有附上。`);
      return;
    }
    if (room() <= 0) {
      setNote(`一則最多附 ${max} 個（圖與檔案一起算）。`);
      return;
    }
    const name = artName(a);
    const key = `i${++itemSeq}`;
    if (kind === "image") {
      push({
        key,
        status: "ready",
        kind,
        name,
        preview: a.thumb_url ? `${a.thumb_url}?w=240` : a.file_url,
        att: { ref: { artifact_id: a.id }, view: { kind: "image", artifact_id: a.id, name, file_url: a.file_url, thumb_url: a.thumb_url, exists: true } },
      });
      return;
    }
    // 文字作品（逐字稿、歌詞）：清單裡的文字可能是截短的預覽，字數向單筆要
    const typeName = a.kind === "lyrics" ? "歌詞" : a.kind === "transcript" ? "逐字稿" : "文字";
    const info: FileInfo = { type: a.kind, type_name: typeName, readable: true, chars: (a.text ?? "").length };
    const view: ChatAttachment = { kind, artifact_id: a.id, name, file_url: a.file_url, thumb_url: null, exists: true, mime: a.mime, bytes: a.bytes, info, download_url: `${a.file_url}?download=true` };
    push({ key, status: "ready", kind, name, preview: null, bytes: a.bytes, info, att: { ref: { artifact_id: a.id }, view } });
    api
      .artifact(a.id)
      .then((full) => {
        const fi: FileInfo = { ...info, chars: (full.text ?? "").length };
        setItems((xs) => xs.map((x) => (x.key === key && x.att ? { ...x, info: fi, att: { ...x.att, view: { ...x.att.view, info: fi } } } : x)));
      })
      .catch(() => undefined);
  };

  const drop = useFileDrop(addFile, { multiple: true });

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files ?? []);
    if (!files.length) return;
    // 只有檔案（截圖、複製的檔）：不讓瀏覽器再貼一次檔名之類的字；同時有文字就照常貼文字
    if (!e.clipboardData.getData("text/plain")) e.preventDefault();
    files.forEach(addFile);
  };

  const clear = () => {
    itemsRef.current.forEach((x) => x.obj && URL.revokeObjectURL(x.obj));
    itemsRef.current = [];
    setItems([]);
  };

  const uploading = items.some((x) => x.status === "uploading");
  const failed = items.some((x) => x.status === "error" || x.status === "big");
  const ready = items.filter((x) => x.status === "ready" && x.att).map((x) => x.att as OutgoingAttachment);
  const blindWithImages = blind && items.some((x) => x.kind === "image");
  /** 不能送的原因（附件的部分）；null＝附件沒問題 */
  const why = uploading ? "檔案還在上傳，傳完才能送" : failed ? "先拿掉上傳失敗或太大的檔" : blindWithImages ? `${BLIND}：拿掉圖，或換一個會看圖的模型` : null;
  return { items, ready, uploading, failed, blind, blindWithImages, why, note, setNote, touched, room, max, limits, caps, addFile, pickArtifact, remove, clear, drop, onPaste };
}

export type Attachments = ReturnType<typeof useAttachments>;

/** 一個檔案（不是圖）：折角紙＋檔名、類型・大小・頁數、這個模型會怎麼讀它 */
function FileCard({ x, a }: { x: AttachItem; a: Attachments }) {
  const ext = extOf(x.name, x.info);
  let meta: ReactNode;
  let how: { text: string; soft?: boolean };
  if (x.status === "uploading") {
    meta = (
      <>
        上傳中 <span className="n">{fmtBytes(x.loaded ?? 0)}</span> / <span className="n">{fmtBytes(x.bytes ?? 0)}</span>
      </>
    );
    how = { text: x.kind === "audio" ? "傳完才看得到長度" : "傳完才看得到頁數、列數", soft: true };
  } else {
    meta = <Facts parts={factsOf(x.name, x.info, x.bytes, x.kind, true)} />;
    how = x.status === "error" ? { text: "上傳失敗" } : x.status === "big" ? { text: "太大，不會送出" } : x.pending ? { text: "正在看檔案內容…", soft: true } : howRead({ kind: x.kind, info: x.info, bytes: x.bytes }, a.caps);
  }
  const unk = x.status === "ready" && x.kind === "file" && x.info?.readable === false;
  // 「太大」用 over（.big 是看板的大數字）
  const cls = x.status === "uploading" ? " up" : x.status === "error" ? " err" : x.status === "big" ? " over" : unk ? " unk" : "";
  return (
    <div className={`cf${cls}`} title={x.status === "error" || x.status === "big" ? `${x.name}\n${x.error ?? ""}` : `${x.name}\n${factsOf(x.name, x.info, x.bytes, x.kind).join(" · ")}\n${how.text}`} data-kind={x.kind}>
      <FileDoc ext={ext} dash={x.status === "uploading" || unk} />
      <span className="cf-t">
        <span className="cf-n">{x.name}</span>
        <span className="cf-m">{meta}</span>
        <span className={`cf-h${how.soft ? " soft" : ""}`}>{how.text}</span>
      </span>
      <button type="button" className="cx-x" aria-label={`拿掉 ${x.name}`} title="拿掉" onClick={() => a.remove(x.key)}>
        ×
      </button>
    </div>
  );
}

/** 待送出的附件：圖是小縮圖、檔案是折角紙，右上角 × 拿掉；extra＝後面多放的東西（編輯框的「＋附件」） */
export function AttachTray({ a, className = "cx-tray", extra, always }: { a: Attachments; className?: string; extra?: ReactNode; always?: boolean }) {
  if (!a.items.length && !always) return null;
  return (
    <div className={className} aria-label="待送出的附件">
      {a.items.map((x) =>
        x.kind === "image" ? (
          <div key={x.key} className={`cx-th ${x.status === "big" ? "error" : x.status}`} title={x.status === "error" || x.status === "big" ? `${x.name}\n${x.error ?? ""}` : x.name}>
            {x.preview ? <img src={x.preview} alt="" /> : x.status === "ready" ? <span className="cx-st">檔案已不在</span> : null}
            {x.status === "uploading" ? <span className="cx-st">上傳中…</span> : null}
            {x.status === "error" ? <span className="cx-st">失敗</span> : null}
            {x.status === "big" ? <span className="cx-st">太大</span> : null}
            <button type="button" className="cx-x" aria-label={`拿掉 ${x.name}`} title="拿掉" onClick={() => a.remove(x.key)}>
              ×
            </button>
          </div>
        ) : (
          <FileCard key={x.key} x={x} a={a} />
        ),
      )}
      {extra}
      <span className="cx-count n">
        {a.items.length} / {a.max}
      </span>
    </div>
  );
}

/** 上傳失敗與太大的原因、讀不了的提醒、暫時的說明、不會看圖的提醒 */
export function AttachNotes({ a }: { a: Attachments }) {
  const errs = a.items.filter((x) => x.status === "error" || x.status === "big");
  const unk = a.items.filter((x) => x.status === "ready" && x.kind === "file" && isUnknown(x.info) && x.info);
  const broken = a.items.filter((x) => x.status === "ready" && x.kind === "file" && x.info?.readable === false && !isUnknown(x.info) && x.info.reason !== "no_text");
  return (
    <>
      {errs.length ? (
        <div className="cx-errs" role="alert">
          {errs.map((x) =>
            x.status === "big" ? (
              <div key={x.key}>
                <b>太大</b> {x.error}
              </div>
            ) : (
              <div key={x.key}>
                <b>上傳失敗</b> <span className="code">{x.name}</span>：{x.error}
              </div>
            ),
          )}
        </div>
      ) : null}
      {[...unk, ...broken].map((x) => (
        <p key={x.key} className="cx-note" role="status">
          {unreadableNote(x.name, x.info as FileInfo)}
        </p>
      ))}
      {a.blindWithImages ? (
        <p className="cx-note" role="status">
          {BLIND}：這幾張圖不會送出。拿掉圖，或換一個會看圖的模型。
        </p>
      ) : a.note ? (
        <p className="cx-note" role="status">
          {a.note}
        </p>
      ) : null}
    </>
  );
}

/** 附件展開區的內容：從電腦選＋作品牆的圖與文字作品（點一下附上、再點一下拿掉） */
export function AttachPicker({ a }: { a: Attachments }) {
  const fileCap = a.limits?.max_bytes?.file;
  const picked = a.items.flatMap((x) => (x.att?.ref.artifact_id ? [x.att.ref.artifact_id] : []));
  return (
    <>
      <div className="dp-fh">
        <span className="lbl">Attach</span>
        <span className="zh">附件</span>
        <span className="aside">
          已附 <span className="n">{a.items.length}</span> / {a.max} 個{fileCap ? <> · 一個檔最多 <span className="n">{fmtBytes(fileCap)}</span></> : null}
        </span>
      </div>
      <div className="cx-popacts">
        <button type="button" className="mk-mini strong" onClick={a.drop.open} disabled={a.room() <= 0}>
          從電腦選檔案…
        </button>
        <span className="cs-note">什麼檔都可以：文件、試算表、簡報、程式碼、音檔都會先試著讀，讀不了的會老實說。也可以直接貼上或拖進輸入框。</span>
      </div>
      <div className="cx-picksec">
        <span className="cx-pickk">作品牆的圖</span>
        {a.blind ? <p className="cs-note">{BLIND}，作品牆的圖先不列；換一個會看圖的模型再挑。</p> : <ImagePicker selected={picked} onPick={a.pickArtifact} />}
      </div>
      <div className="cx-picksec">
        <span className="cx-pickk">作品牆的文字（逐字稿、歌詞）</span>
        <TextWorkPicker selected={picked} onPick={a.pickArtifact} />
      </div>
    </>
  );
}
