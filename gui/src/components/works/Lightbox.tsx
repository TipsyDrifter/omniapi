/* 作品詳情：燈箱，壓在頂欄下面（頂欄導覽照常可用），有自己的網址 /works/:id。
   左＝舞台（圖 w=1600、點了開原圖；音檔＝大播放器；文字＝可捲動的全文），右＝資訊欄：
   章＋標題、主要動作兩顆、次要動作、提示詞全文（可複製）、設定與費用表、來源與衍生。
   鍵盤：← 比較新的一件、→ 比較舊的一件（照後端給的 newer／older）、Esc 關。 */
import { useEffect, useRef, useState, type ReactNode, type RefObject, type TouchEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import type { Artifact, ArtifactDetail, WallQuery } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { WORK_KIND_INFO, isWorkKind, mediaOf } from "@/lib/modalities";
import { AudioPlayer, Glyph, VideoPlayer, canRegenerate, draftFromArtifact, fmtBytes, mmss, msShort, musicFamily, sourceFromArtifact, takeLastStop, vUsd, type PlayerHandle } from "@/components/make";
import { carryIn, getMakeState, loadOptions } from "@/store/make";
import { setHidden } from "@/store/works";
import { KIND_EN, KIND_ZH, fileName, isAudio, isBare, sourceZh, verbOf } from "./wall";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fullTime = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const when = (s: number) => fullTime.format(new Date(s * 1000)).replace(",", "");

/** 參數表不列的：路徑（頁面不顯示路徑）、已經另外列的 */
const SKIP_PARAM = /(^|_)(path|paths)$|^(prompt|text|model)$/;
const PARAM_ZH: Record<string, string> = {
  size: "尺寸",
  quality: "品質",
  background: "背景",
  output_format: "格式",
  compression: "壓縮",
  n: "張數",
  style: "風格",
  moderation: "審查",
  input_fidelity: "還原度",
  image_size: "解析度",
  aspect_ratio: "比例",
  voice: "聲音",
  instructions: "語氣",
  speed: "語速",
  language: "語言",
  language_code: "語言",
  instrumental: "純音樂",
  custom_mode: "照歌詞",
  title: "標題",
  vocal_gender: "唱的人",
  negative_tags: "避開",
  music_length_ms: "長度",
  response_format: "輸出",
};
function paramText(k: string, v: unknown): string {
  if (typeof v === "boolean") return v ? "是" : "否";
  if (k === "music_length_ms" && typeof v === "number") return msShort(v / 1000);
  if (typeof v === "string" || typeof v === "number") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function titleOf(a: Artifact): string {
  // 影片：標題是提示詞截的前幾十字，燈箱頭照「影片・時間」（提示詞全文在下面）
  if (mediaOf(a.kind) === "video") return `${verbOf(a)}・${dt(a.created_at)}`;
  return a.title || `${verbOf(a)}・${dt(a.created_at)}`;
}

export interface LightboxProps {
  id: string;
  query: WallQuery;
  /** 位置（第幾件／共幾件）；不知道就不顯示 */
  pos: { i: number; n: number } | null;
  onClose: () => void;
  onGo: (id: string) => void;
}

export function Lightbox({ id, query, pos, onClose, onGo }: LightboxProps) {
  const navigate = useNavigate();
  const qkey = JSON.stringify(query);
  const [st, setSt] = useState<{ id: string; a: ArtifactDetail | null; err: string | null }>({ id: "", a: null, err: null });
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setConfirm(false);
    setMsg(null);
    api
      .artifactDetail(id, JSON.parse(qkey) as WallQuery)
      .then((a) => alive && setSt({ id, a, err: null }))
      .catch((e) => alive && setSt({ id, a: null, err: errMsg(e) }));
    return () => {
      alive = false;
    };
  }, [id, qkey]);

  const a = st.id === id ? st.a : null;
  const newer = a?.newer ?? null;
  const older = a?.older ?? null;
  const player = useRef<PlayerHandle>(null);
  const isVideo = !!a && mediaOf(a.kind) === "video";

  // 換一件時：上一支影片若播到一半，說一句停在哪（回去會從那裡接著播；下一支停在封面、不自動播）
  useEffect(() => {
    const s = takeLastStop();
    if (s && s.key !== id) {
      const m = `上一支影片停在 ${msShort(s.t)}，聲音一起停；回去會從 ${msShort(s.t)} 接著播`;
      setMsg(m);
      const t = window.setTimeout(() => setMsg((x) => (x === m ? null : x)), 4000);
      return () => window.clearTimeout(t);
    }
  }, [id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      // 影片：空白鍵＝播放／暫停；⇧＋← →＝跳一秒（← → 照舊是換一件）
      if (isVideo && player.current && (e.key === " " || e.key === "Spacebar") && !(t && (t.tagName === "BUTTON" || t.tagName === "A"))) {
        e.preventDefault();
        player.current.toggle();
        return;
      }
      if (isVideo && player.current && e.shiftKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
        e.preventDefault();
        player.current.skip(e.key === "ArrowLeft" ? -1 : 1);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowLeft" && newer) {
        e.preventDefault();
        onGo(newer);
      } else if (e.key === "ArrowRight" && older) {
        e.preventDefault();
        onGo(older);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newer, older, onClose, onGo, isVideo]);

  const flash = (m: string) => {
    setMsg(m);
    window.setTimeout(() => setMsg((x) => (x === m ? null : x)), 2600);
  };
  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      flash(`已複製${what}`);
    } catch (e) {
      flash(`複製不了：${errMsg(e)}`);
    }
  };
  const toggleHidden = async (hidden: boolean) => {
    if (!a) return;
    setBusy(true);
    try {
      await setHidden(a.id, hidden);
      setSt((s) => (s.a ? { ...s, a: { ...s.a, hidden } } : s));
      setConfirm(false);
      flash(hidden ? "已從牆上移除。檔案還在，「已移除」裡找得到。" : "已放回牆上。");
    } catch (e) {
      flash(`沒有成功：${errMsg(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const thumbOf = (x: Artifact) => (x.kind === "image" && x.thumb_url ? `${x.thumb_url}?w=240` : null);
  const carryBase = (x: Artifact) => ({ id: x.id, artKind: String(x.kind), thumb: thumbOf(x) });

  const again = async () => {
    if (!a) return;
    const d = draftFromArtifact(a, a.parent, getMakeState().drafts.speech.voice);
    if (!d) return;
    setBusy(true);
    await loadOptions();
    setBusy(false);
    // 當時的模型不在現在的清單裡（下架、改名、沙盒的假模型）：表單會退回預設模型，先講清楚
    const model = (d.patch as { model?: string | null }).model;
    const list = getMakeState().options?.kinds[d.kind]?.models;
    const gone = model && list && !list.some((m) => m.id === model) ? `當時的模型 ${model} 不在現在的模型清單裡，表單改用預設模型。` : null;
    carryIn(d.kind, d.patch, { ...carryBase(a), what: d.what, note: [d.note, gone].filter(Boolean).join(" ") || null });
    navigate(`/make/${d.kind}`);
  };
  const toEdit = () => {
    if (!a) return;
    carryIn("image", { source: sourceFromArtifact(a) }, { ...carryBase(a), what: "這張圖設為來源圖（改圖）；寫下要怎麼改", note: null });
    navigate("/make/image");
  };
  /** 圖片 → 影片表單的首幀（不會自動送出） */
  const toVideo = () => {
    if (!a) return;
    carryIn("video", { first: sourceFromArtifact(a) }, { ...carryBase(a), what: "這張圖設為首幀；寫下要怎麼動", note: null });
    navigate("/make/video");
  };
  const toTranscribe = () => {
    if (!a) return;
    carryIn("transcript", { source: sourceFromArtifact(a) }, { ...carryBase(a), what: `這段${KIND_ZH[a.kind] ?? "音檔"}設為要轉錄的音檔`, note: null });
    navigate("/make/transcript");
  };
  const toMusic = async () => {
    if (!a) return;
    setBusy(true);
    await loadOptions();
    setBusy(false);
    const ms = getMakeState();
    const models = ms.options?.kinds.music.models ?? [];
    const cur = models.find((m) => m.id === ms.drafts.music.model) ?? null;
    // 照歌詞作曲只有 Suno 系列：現在選的不是就換成第一個能用的 Suno 模型
    const suno = musicFamily(cur) === "suno" ? cur : models.find((m) => musicFamily(m) === "suno" && m.available) ?? models.find((m) => musicFamily(m) === "suno") ?? null;
    carryIn(
      "music",
      { mode: "lyrics", lyrics: a.text ?? "", lyricsFrom: a.id, title: a.title ?? ms.drafts.music.title, ...(suno ? { model: suno.id } : {}) },
      { ...carryBase(a), what: "這份歌詞（照歌詞作曲；風格與標題要自己填）", note: suno ? null : "模型清單裡沒有 Suno 系列：照歌詞作曲要 Suno，這一版先帶歌詞進去。" },
    );
    navigate("/make/music");
  };

  // 1.3-M3 觸控：在舞台上左右滑換一件（手指往左＝下一件，比較舊的）
  const swipe = useRef<{ x: number; y: number } | null>(null);
  const onTouchStart = (e: TouchEvent) => {
    const t = e.touches[0];
    swipe.current = t ? { x: t.clientX, y: t.clientY } : null;
  };
  const onTouchEnd = (e: TouchEvent) => {
    const s = swipe.current;
    const t = e.changedTouches[0];
    swipe.current = null;
    if (!s || !t) return;
    const dx = t.clientX - s.x;
    if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(t.clientY - s.y) * 1.5) return;
    if (dx < 0 && older) onGo(older);
    else if (dx > 0 && newer) onGo(newer);
  };

  return (
    <div className="wk-lb" role="dialog" aria-modal="true" aria-label="作品詳情">
      <div className="wk-lb-bar">
        <button type="button" className="wk-back" onClick={onClose} aria-label="回作品牆">
          ← <span className="x-s">作品</span>牆
        </button>
        {a ? (
          <>
            <Glyph kind={String(a.kind)} />
            <span className="lbl wk-en">{KIND_EN[a.kind] ?? a.kind}</span>
          </>
        ) : null}
        {pos ? (
          <span className="wk-pos n">
            {pos.i} / {pos.n}
          </span>
        ) : null}
        <div className="wk-nav2">
          <span className="wk-esc x-touch">Esc 關閉 · ← → 換一件{isVideo ? " · 空白鍵播放" : ""}</span>
          <span className="wk-esc only-touch">左右滑換一件</span>
          <button type="button" className="mk-mini" disabled={!newer} onClick={() => newer && onGo(newer)} title="比較新的一件" aria-label="上一件">
            ←<span className="x-s"> 上一件</span>
          </button>
          <button type="button" className="mk-mini" disabled={!older} onClick={() => older && onGo(older)} title="比較舊的一件" aria-label="下一件">
            <span className="x-s">下一件 </span>→
          </button>
        </div>
      </div>

      {!a ? (
        <div className="wk-lb-msg">{st.id === id && st.err ? <div className="warn">讀不到這件作品：{st.err}</div> : <span className="dp-note">讀取作品…</span>}</div>
      ) : (
        <div className="wk-lb-body">
          <div className="wk-stage" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
            <Stage a={a} player={player} />
          </div>
          <div className="wk-info">
            <div className="wk-info-h">
              <Glyph kind={String(a.kind)} size="lg" />
              <h3>{titleOf(a)}</h3>
              {a.hidden ? <span className="wk-tag soft">已移除</span> : null}
            </div>

            <Actions a={a} busy={busy} again={() => void again()} toEdit={toEdit} toVideo={toVideo} toTranscribe={toTranscribe} toMusic={() => void toMusic()} copy={copy} />

            <div className="wk-acts2">
              <a className="mk-mini" href={`${a.file_url}?download=true`} download>
                下載
              </a>
              {a.prompt ? (
                <button type="button" className="mk-mini" onClick={() => void copy(a.prompt ?? "", "提示詞")}>
                  複製提示詞
                </button>
              ) : null}
              {a.hidden ? (
                <button type="button" className="mk-mini wk-rm" onClick={() => void toggleHidden(false)} disabled={busy}>
                  放回牆上
                </button>
              ) : (
                <button type="button" className="mk-mini wk-rm" onClick={() => setConfirm(true)} disabled={busy || confirm}>
                  從牆上移除
                </button>
              )}
            </div>
            {confirm ? (
              <div className="wk-confirm" role="alertdialog" aria-label="確認移除">
                <b>從牆上移除這件？</b>
                <span>只是不在牆上顯示，檔案不刪；之後在「已移除」找得回來、可以放回。</span>
                <button type="button" className="mk-mini strong" onClick={() => void toggleHidden(true)} disabled={busy}>
                  {busy ? "移除中…" : "移除"}
                </button>
                <button type="button" className="mk-mini" onClick={() => setConfirm(false)}>
                  算了
                </button>
              </div>
            ) : null}
            {msg ? (
              <div className="wk-flash" role="status">
                {msg}
              </div>
            ) : null}

            <PromptBlock a={a} copy={copy} />
            <DetailTable a={a} />
            <Lineage a={a} onGo={onGo} />
          </div>
        </div>
      )}
    </div>
  );
}

/** 舞台上的一張大圖：點了開原檔（新分頁）。作品詳情與聊天裡的看圖共用 */
function BigImage({ href, src, width, height, alt, note }: { href: string; src: string; width?: number | null; height?: number | null; alt: string; note: ReactNode }) {
  return (
    <>
      <a className="wk-big" href={href} target="_blank" rel="noreferrer" title="開原圖（新分頁）">
        <img src={src} width={width ?? undefined} height={height ?? undefined} alt={alt} />
      </a>
      <span className="wk-sz n">{note}</span>
    </>
  );
}

export interface ViewerImage {
  /** 原檔 */
  file_url: string;
  /** 舞台上顯示的（作品用 1600px 預覽；上傳檔沒有縮圖，就是原檔） */
  src: string;
  name: string | null;
  /** 是作品才有：可以去作品牆看詳情 */
  artifact_id?: string;
}

/** 只看圖的燈箱（聊天訊息裡的附件）：沒有資訊欄，一則訊息裡有幾張就能 ← → 換。
   上傳的圖不是作品，沒有詳情；作品附一個「在作品牆打開」。 */
export function ImageViewer({ images, start, onClose }: { images: ViewerImage[]; start: number; onClose: () => void }) {
  const navigate = useNavigate();
  const [i, setI] = useState(start);
  const img = images[Math.min(i, images.length - 1)];
  const n = images.length;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowLeft" && n > 1) {
        e.preventDefault();
        setI((x) => (x + n - 1) % n);
      } else if (e.key === "ArrowRight" && n > 1) {
        e.preventDefault();
        setI((x) => (x + 1) % n);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [n, onClose]);
  if (!img) return null;
  return (
    <div className="wk-lb" role="dialog" aria-modal="true" aria-label="看圖">
      <div className="wk-lb-bar">
        <button type="button" className="wk-back" onClick={onClose}>
          ← 回聊天
        </button>
        <span className="code wk-vname">{img.name ?? "（沒有檔名）"}</span>
        {n > 1 ? (
          <span className="wk-pos n">
            {i + 1} / {n}
          </span>
        ) : null}
        <div className="wk-nav2">
          <span className="wk-esc x-touch">{n > 1 ? "Esc 關閉 · ← → 換一張" : "Esc 關閉"}</span>
          {img.artifact_id ? (
            <button type="button" className="mk-mini" onClick={() => navigate(`/works/${encodeURIComponent(img.artifact_id ?? "")}`)}>
              在作品牆打開
            </button>
          ) : null}
          {n > 1 ? (
            <>
              <button type="button" className="mk-mini" onClick={() => setI((x) => (x + n - 1) % n)}>
                ← 上一張
              </button>
              <button type="button" className="mk-mini" onClick={() => setI((x) => (x + 1) % n)}>
                下一張 →
              </button>
            </>
          ) : null}
        </div>
      </div>
      <div className="wk-lb-body solo">
        <div className="wk-stage">
          <BigImage key={img.file_url} href={img.file_url} src={img.src} alt={img.name ?? ""} note={img.src === img.file_url ? "原圖 · 點圖在新分頁打開" : "這裡是 1600px 預覽，點圖開原檔"} />
        </div>
      </div>
    </div>
  );
}

function Stage({ a, player }: { a: ArtifactDetail; player: RefObject<PlayerHandle | null> }) {
  if (a.exists === false) return <div className="wk-astage">這件作品的檔案已經不在硬碟上。</div>;
  if (a.kind === "image") {
    return (
      <BigImage
        href={a.file_url}
        src={a.thumb_url ? `${a.thumb_url}?w=1600` : a.file_url}
        width={a.width}
        height={a.height}
        alt={a.prompt ?? ""}
        note={`${a.width && a.height ? `${a.width}×${a.height} 原圖 · ` : ""}這裡是 1600px 預覽，點圖開原檔`}
      />
    );
  }
  if (mediaOf(a.kind) === "video") {
    // 自己的控制列（拖時間軸靠 /file 的 Range）；空白鍵與 ⇧＋← → 由燈箱接到 player
    const fps = a.meta?.fps;
    return (
      <>
        <VideoPlayer
          key={a.id}
          handleRef={player}
          big
          src={a.file_url}
          poster={a.thumb_url ? `${a.thumb_url}?w=1600` : null}
          hasAudio={a.has_audio}
          width={a.width}
          height={a.height}
          durationHint={a.duration_s}
          posKey={a.id}
          label={a.prompt ?? "影片"}
        />
        <span className="vd-sz n">
          {[a.width && a.height ? `${a.width}×${a.height}` : null, fps ? `${Math.round(fps)} fps` : null, a.duration_s ? `${a.duration_s.toFixed(2)} 秒` : null].filter(Boolean).join(" · ")}
          <span className="x-touch">
            {" · "}
            <kbd>空白</kbd> 播放／暫停 · <kbd>⇧</kbd>＋<kbd>←</kbd>
            <kbd>→</kbd> 跳一秒
          </span>
        </span>
      </>
    );
  }
  if (isAudio(a)) {
    return (
      <div className="wk-astage">
        <div className="wk-kh">
          <Glyph kind={String(a.kind)} />
          <span className="lbl">
            {KIND_EN[a.kind]} {KIND_ZH[a.kind]}
          </span>
        </div>
        {a.title ? <div className="wk-ast-t">{a.title}</div> : null}
        <div className="wk-ast-fn code">{fileName(a)}</div>
        <AudioPlayer key={a.id} src={a.file_url} big />
        {isBare(a) ? <div className="warn">回填：當時沒留提示詞與模型，只知道檔名、時間、長度與大小。</div> : null}
      </div>
    );
  }
  const lines = (a.text ?? "").split("\n");
  return (
    <div className={`wk-tstage${a.kind === "lyrics" ? " lyr" : ""}`}>
      {a.text?.trim()
        ? lines.map((l, i) => {
            const m = l.trim().match(/^\[(.+?)\]$/);
            return m ? (
              <span key={i} className="wk-sec">
                {m[1].toUpperCase()}
              </span>
            ) : (
              <span key={i} className="wk-line">
                {l || " "}
              </span>
            );
          })
        : "（沒有文字）"}
    </div>
  );
}

function Actions({
  a,
  busy,
  again,
  toEdit,
  toVideo,
  toTranscribe,
  toMusic,
  copy,
}: {
  a: ArtifactDetail;
  busy: boolean;
  again: () => void;
  toEdit: () => void;
  toVideo: () => void;
  toTranscribe: () => void;
  toMusic: () => void;
  copy: (t: string, what: string) => Promise<void>;
}) {
  const can = canRegenerate(a);
  const btns: ReactNode[] = [];
  const againBtn = (
    <button key="again" type="button" className="stamp-btn" onClick={again} disabled={busy} title="帶到生成頁的表單，不會自動送出">
      <b>↻</b>用同樣設定再生一次
    </button>
  );
  let why: ReactNode = null;
  let tail: ReactNode = null;
  if (a.kind === "image") {
    if (can) btns.push(againBtn);
    btns.push(
      <button key="edit" type="button" className="stamp-btn" onClick={toEdit}>
        <b>→</b>拿去改圖
      </button>,
      <button key="video" type="button" className="stamp-btn vd-new" onClick={toVideo}>
        <b>→</b>拿去做影片
      </button>,
    );
    tail = (
      <p className="wk-why">
        「拿去做影片」把這張圖放進影片表單的<b>首幀</b>，不會自動送出：寫下要怎麼動、看過費用再送。
      </p>
    );
    if (can && verbOf(a) === "改圖" && !a.parent)
      why = (
        <>
          這張是<b>改圖</b>，當時的來源圖沒有留存：「再生一次」會帶提示詞與設定，但送出會變成生圖。
        </>
      );
  } else if (isAudio(a)) {
    if (can) btns.push(againBtn);
    btns.push(
      <button key="tr" type="button" className="stamp-btn" onClick={toTranscribe}>
        <b>→</b>拿去轉錄
      </button>,
    );
    if (!can)
      why = (
        <>
          回填的{KIND_ZH[a.kind]}<b>沒有留提示詞與模型</b>，沒有原設定可以照著再生。
        </>
      );
  } else if (mediaOf(a.kind) === "video") {
    if (can) btns.push(againBtn);
    else
      why = (
        <>
          這支影片<b>沒有留下提示詞</b>，沒有原設定可以照著再生。
        </>
      );
  } else if (a.kind === "lyrics") {
    btns.push(
      <button key="mu" type="button" className="stamp-btn" onClick={toMusic} disabled={busy}>
        <b>→</b>照這份歌詞作曲
      </button>,
      <button key="cp" type="button" className="stamp-btn" onClick={() => void copy(a.text ?? "", "歌詞")}>
        <b>⧉</b>複製歌詞
      </button>,
    );
  } else {
    if (can) btns.push(againBtn);
    btns.push(
      <button key="cp" type="button" className="stamp-btn" onClick={() => void copy(a.text ?? "", "全文")} disabled={!a.text}>
        <b>⧉</b>複製全文
      </button>,
    );
    if (can && !a.parent)
      why = (
        <>
          當時的<b>音檔沒有留存</b>：「再生一次」只帶設定，要先放一段音檔才能送出。
        </>
      );
  }
  const vid = mediaOf(a.kind) === "video";
  const withFrame = vid && !!(a.parent || a.meta?.frames?.first || a.meta?.frames?.last);
  return (
    <div className={`wk-acts${btns.length === 1 ? " one" : btns.length === 3 ? " three" : ""}`}>
      {btns}
      {why ? <p className="wk-why">{why}</p> : null}
      {tail}
      {can ? (
        <p className="wk-why">
          {vid
            ? `「再生一次」把提示詞與設定${withFrame ? "（含首尾幀）" : ""}帶到生成頁，不會自動送出：送出前一樣會確認費用。`
            : "「再生一次」只把提示詞與設定帶到生成頁，不會自動送出：看一眼預估費用再按。"}
        </p>
      ) : null}
    </div>
  );
}

function PromptBlock({ a, copy }: { a: ArtifactDetail; copy: (t: string, what: string) => Promise<void> }) {
  const zh = a.kind === "speech" ? "要念的文字" : a.kind === "music" && a.params?.custom_mode === true ? "歌詞" : "提示詞";
  if (!a.prompt) {
    if (isText(a)) return null;
    return (
      <div className="wk-blk">
        <h4>
          <span className="lbl">Prompt</span>
          <span className="zh">{zh}</span>
        </h4>
        <div className="warn">{isBare(a) ? "當時沒留提示詞與模型：這件是從檔案目錄補進牆上的。" : "這件作品沒有提示詞。"}</div>
      </div>
    );
  }
  return (
    <div className="wk-blk">
      <h4>
        <span className="lbl">Prompt</span>
        <span className="zh">{zh}</span>
        <button type="button" className="mk-mini" onClick={() => void copy(a.prompt ?? "", zh)}>
          複製
        </button>
      </h4>
      <div className="wk-ptext">{a.prompt}</div>
    </div>
  );
}
const isText = (a: Artifact) => isWorkKind(a.kind) && WORK_KIND_INFO[a.kind].media === "text";

/** 影片的設定與費用：秒數、解析度、比例、聲音、首尾幀、長度、像素、費用（實際＋送出時的預估）、耗時；不列內部編號 */
function VideoDetail({ a }: { a: ArtifactDetail }) {
  const nil = (s: string) => <span className="wk-nil">{s}</span>;
  const p = a.params ?? {};
  const meta = a.meta ?? {};
  const est = meta.estimate;
  const estTxt = est ? (est.usd != null ? `約 ${vUsd(est.usd)}` : est.low != null && est.high != null ? `約 ${vUsd(est.low)}–${vUsd(est.high)}` : null) : null;
  const sandbox = est?.basis === "sandbox";
  const fr = meta.frames ?? null;
  const frameTxt = (r: { artifact_id?: string; upload_id?: string } | undefined) => (!r ? null : r.artifact_id ? "有（見下面的來源）" : "有（從電腦選的圖）");
  const audio = p.generate_audio === true ? "有" : p.generate_audio === false ? "無" : null;
  const fileAudio = a.has_audio === true ? "檔案裡有聲音" : a.has_audio === false ? "檔案裡沒有聲音" : null;
  const code = (v: unknown) => <span className="code">{String(v)}</span>;
  return (
    <div className="wk-blk">
      <h4>
        <span className="lbl">Detail</span>
        <span className="zh">設定與費用</span>
      </h4>
      <dl className="wk-dl">
        <dt>模型</dt>
        <dd>{a.model ? <span className="code">{a.model}</span> : nil("沒有記錄")}</dd>
        <dt>工具</dt>
        <dd>{a.tool ? <span className="code">{a.tool}</span> : nil("沒有記錄")}</dd>
        {p.duration != null ? (
          <>
            <dt>秒數</dt>
            <dd>{code(p.duration)}</dd>
          </>
        ) : null}
        {p.resolution ? (
          <>
            <dt>解析度</dt>
            <dd>{code(p.resolution)}</dd>
          </>
        ) : null}
        {p.aspect_ratio ? (
          <>
            <dt>比例</dt>
            <dd>{code(p.aspect_ratio)}</dd>
          </>
        ) : null}
        <dt>聲音</dt>
        <dd>
          {audio ? <span className="code">{audio}</span> : nil("沒有指定")}
          {fileAudio ? <span className="wk-dim"> · {fileAudio}</span> : null}
        </dd>
        <dt>首幀</dt>
        <dd>{frameTxt(fr?.first) ?? (a.parent ? "有（見下面的來源）" : nil("沒有（文生影片）"))}</dd>
        {fr?.last ? (
          <>
            <dt>尾幀</dt>
            <dd>{frameTxt(fr.last)}</dd>
          </>
        ) : null}
        {a.duration_s ? (
          <>
            <dt>長度</dt>
            <dd className="n">{msShort(a.duration_s)}</dd>
          </>
        ) : null}
        {a.width && a.height ? (
          <>
            <dt>像素</dt>
            <dd className="n">
              {a.width}×{a.height}
              {meta.fps ? ` · ${Math.round(meta.fps)} fps` : ""}
            </dd>
          </>
        ) : null}
        <dt>費用</dt>
        <dd>
          {a.cost_usd != null ? <span className="n">{usd(a.cost_usd)}</span> : nil("未回報")}
          {sandbox ? <span className="wk-dim"> · 離線沙盒，沒有計費</span> : estTxt ? <span className="wk-dim"> · 送出時預估{estTxt}</span> : null}
        </dd>
        {meta.waited_s != null ? (
          <>
            <dt>耗時</dt>
            <dd className="n">{mmss(meta.waited_s)}</dd>
          </>
        ) : null}
        {meta.late ? (
          <>
            <dt>收回</dt>
            <dd>不等了之後在背景收回來的</dd>
          </>
        ) : meta.resumed ? (
          <>
            <dt>收回</dt>
            <dd>服務重啟過，接回來後收的</dd>
          </>
        ) : null}
        <dt>來源</dt>
        <dd>{sourceZh(a.source)}</dd>
        <dt>時間</dt>
        <dd className="n">{when(a.created_at)}</dd>
        <dt>檔案</dt>
        <dd>
          <span className="code">{fileName(a)}</span>
          <span className="wk-dim">
            {" · "}
            <span className="n">{fmtBytes(a.bytes)}</span>
            {a.mime ? ` · ${a.mime}` : ""}
          </span>
        </dd>
      </dl>
    </div>
  );
}

function DetailTable({ a }: { a: ArtifactDetail }) {
  if (mediaOf(a.kind) === "video") return <VideoDetail a={a} />;
  const nil = (s: string) => <span className="wk-nil">{s}</span>;
  const params = Object.entries(a.params ?? {}).filter(([k, v]) => !SKIP_PARAM.test(k) && v !== null && v !== undefined && v !== "");
  return (
    <div className="wk-blk">
      <h4>
        <span className="lbl">Detail</span>
        <span className="zh">設定與費用</span>
      </h4>
      <dl className="wk-dl">
        <dt>模型</dt>
        <dd>{a.model ? <span className="code">{a.model}</span> : nil("沒有記錄")}</dd>
        <dt>工具</dt>
        <dd>{a.tool ? <span className="code">{a.tool}</span> : nil("沒有記錄")}</dd>
        {params.map(([k, v]) => (
          <Row key={k} k={PARAM_ZH[k] ?? k} code={!PARAM_ZH[k]}>
            <span className="code">{paramText(k, v)}</span>
          </Row>
        ))}
        {a.kind === "image" && a.width && a.height && a.params?.size !== `${a.width}x${a.height}` ? (
          <>
            <dt>像素</dt>
            <dd className="n">
              {a.width}×{a.height}
            </dd>
          </>
        ) : null}
        {a.duration_s ? (
          <>
            <dt>長度</dt>
            <dd className="n">{msShort(a.duration_s)}</dd>
          </>
        ) : null}
        <dt>費用</dt>
        <dd>{a.cost_usd != null ? <span className="n">{usd(a.cost_usd)}</span> : nil("未回報")}</dd>
        <dt>來源</dt>
        <dd>
          {sourceZh(a.source)}
          {a.source === "backfill" ? "（歷史紀錄補進來的）" : ""}
        </dd>
        <dt>時間</dt>
        <dd className="n">{when(a.created_at)}</dd>
        <dt>檔案</dt>
        <dd>
          <span className="code">{fileName(a)}</span>
          <span className="wk-dim">
            {" · "}
            <span className="n">{fmtBytes(a.bytes)}</span>
            {a.mime ? ` · ${a.mime}` : ""}
          </span>
        </dd>
      </dl>
    </div>
  );
}
function Row({ k, code, children }: { k: string; code?: boolean; children: ReactNode }) {
  return (
    <>
      <dt className={code ? "raw" : undefined}>{k}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Lineage({ a, onGo }: { a: ArtifactDetail; onGo: (id: string) => void }) {
  const kids = a.children ?? [];
  const vid = mediaOf(a.kind) === "video";
  const fr = vid ? a.meta?.frames ?? null : null;
  // 影片：首幀＝parent；尾幀（作品）另外接回那一件（點了換到那件）
  const lastId = fr?.last?.artifact_id && fr.last.artifact_id !== a.parent?.id ? fr.last.artifact_id : null;
  if (!a.parent && !kids.length && !a.parent_id && !lastId) return null;
  const firstIsParent = !!a.parent && (!fr?.first?.artifact_id || fr.first.artifact_id === a.parent.id);
  const tile = (x: Artifact) => (
    <button key={x.id} type="button" className="wk-tile" onClick={() => onGo(x.id)} title={vid || mediaOf(x.kind) === "video" ? titleOf(x) : `${titleOf(x)}\n${x.id}`}>
      {x.kind === "image" && x.thumb_url ? <img src={`${x.thumb_url}?w=240`} alt="" loading="lazy" /> : <Glyph kind={String(x.kind)} size="lg" />}
      <span>
        <b>{verbOf(x)}</b>
        <span className="code">{x.title || fileName(x)}</span>
      </span>
    </button>
  );
  return (
    <div className="wk-blk">
      <h4>
        <span className="lbl">Lineage</span>
        <span className="zh">來源與衍生</span>
      </h4>
      {a.parent ? (
        <div className="wk-lin">
          <span className="k">{vid ? (firstIsParent ? "首幀・從這件做出來" : "尾幀・從這件做出來") : "從這件做出來"}</span>
          {tile(a.parent)}
        </div>
      ) : a.parent_id ? (
        <div className="wk-lin">
          <span className="k">從這件做出來</span>
          {vid ? <span className="dp-note">來源的圖已經不在作品庫</span> : <span className="dp-note">來源作品已經不在作品庫（<span className="code">{a.parent_id}</span>）</span>}
        </div>
      ) : null}
      {lastId ? (
        <div className="wk-lin">
          <span className="k">尾幀・從這件做出來</span>
          <button type="button" className="wk-tile" onClick={() => onGo(lastId)}>
            <img src={`/api/artifacts/${encodeURIComponent(lastId)}/thumb?w=240`} alt="" loading="lazy" />
            <span>
              <b>圖片</b>
              <span className="code">尾幀用的那張圖</span>
            </span>
          </button>
        </div>
      ) : null}
      {kids.length ? (
        <div className="wk-lin">
          <span className="k">
            用它做出來的 <span className="n">{kids.length}</span> 件
          </span>
          <div className="wk-tiles">{kids.map(tile)}</div>
        </div>
      ) : null}
    </div>
  );
}
