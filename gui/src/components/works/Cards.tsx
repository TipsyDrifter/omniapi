/* 作品牆的卡片（照 a-workbench 版）：
   圖＝無框，圖本身就是卡，底下一行（章、模型、改圖小章、費用）；
   音檔＝深一階的紙塊，播放鈕在卡上；回填的誠實標「當時沒留提示詞與模型」；
   文字＝紙底墨框，楷書摘錄前幾行；
   日期籤＝3px 墨線＋大號月／日＋星期＋件數，跟作品排在同一列。 */
import { useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import type { Artifact } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { Glyph, claimAudio, msShort } from "@/components/make";
import { mediaOf } from "@/lib/modalities";
import { KIND_ZH, fileName, isBare, sourceZh, verbOf } from "./wall";

/** 卡片底下說明列的高度：音檔卡、文字卡、日期籤要跟「圖＋說明列」等高 */
export const CAP = 25;

const fmtWd = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Taipei", weekday: "short" });
const todayYmd = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());

/** head：手機（S 段）的日期籤是整列寬的組頭「10 04 TODAY …… 5 件」（1.3-M3） */
export function DateSlug({ day, n, width, h, head }: { day: string; n: number; width: number; h: number; head?: boolean }) {
  const [, mm, dd] = day.split("-");
  const wd = day === todayYmd() ? "TODAY" : fmtWd.format(new Date(`${day}T12:00:00+08:00`)).toUpperCase();
  return (
    <div className={`wk-cell wk-slug${head ? " head" : ""}`} style={head ? { width } : { width, height: h + CAP }} aria-label={`${day}，${n} 件`}>
      <b className="n">{mm}</b>
      <b className="n">{dd}</b>
      <span>{wd}</span>
      <i>
        <span className="n">{n}</span> 件
      </i>
    </div>
  );
}

interface CardProps {
  w: Artifact;
  width: number;
  h: number;
  fresh: boolean;
  onOpen: (id: string) => void;
}

const openKeys = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.target !== e.currentTarget) return;
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fn();
  }
};

/** 卡片依作品是什麼（media）挑；不認得的種類（mediaOf 會警告）用文字卡保底：標出種類名，點開看詳情 */
export function WorkCard(p: CardProps) {
  const media = mediaOf(p.w.kind);
  if (media === "image") return <ImageCard {...p} />;
  if (media === "audio") return <AudioCard {...p} />;
  if (media === "video") return <VideoCard {...p} />;
  return <TextCard {...p} />;
}

/** 影片卡＝封面＋底下一條墨色「片條」（▶ 時長、聲／無聲），上緣打一排孔。
   封面：有 poster 用縮圖端點；沒有（電腦上沒有 ffmpeg，縮圖端點回 204）就讓 <video preload="metadata"> 自己顯示第一格。
   滑過不自動預覽（第 8 題）：點了開燈箱才播。 */
function VideoCard({ w, width, h, fresh, onOpen }: CardProps) {
  const poster = w.poster !== false && w.thumb_url ? `${w.thumb_url}?w=480` : null;
  const len = w.duration_s ? msShort(w.duration_s) : "…";
  const snd = w.has_audio === true ? "有聲" : w.has_audio === false ? "無聲" : "";
  return (
    <figure className="wk-cell wk-img wk-vcard" style={{ width }} data-id={w.id}>
      <button type="button" className="wk-media" style={{ height: h }} onClick={() => onOpen(w.id)} aria-label={`看影片：${len}${snd ? `，${snd}` : ""}，${dt(w.created_at)}`}>
        {fresh ? <NewMark /> : null}
        {w.source && w.source !== "backfill" ? <span className="wk-src">{sourceZh(w.source)}</span> : null}
        {w.exists === false ? (
          <span className="wk-gone">檔案不在了</span>
        ) : poster ? (
          <img src={poster} width={w.width ?? undefined} height={w.height ?? undefined} loading="lazy" decoding="async" alt="" />
        ) : (
          <video className="wk-vfirst" src={`${w.file_url}#t=0.1`} preload="metadata" muted playsInline tabIndex={-1} aria-hidden="true" />
        )}
        <span className="wk-vbar" aria-hidden="true">
          <span className="tri" />
          <span>{len}</span>
          {w.has_audio === true ? <span className="snd">聲</span> : w.has_audio === false ? <span className="snd no">無聲</span> : null}
        </span>
      </button>
      <figcaption className="wk-cap">
        <Glyph kind="video" size="sm" />
        <span className="code wk-md">{w.model ?? "沒有記錄模型"}</span>
        <span className="wk-c n">{w.cost_usd != null ? usd(w.cost_usd) : "未回報"}</span>
      </figcaption>
    </figure>
  );
}

function NewMark() {
  return <span className="wk-new">新</span>;
}

function ImageCard({ w, width, h, fresh, onOpen }: CardProps) {
  const edit = verbOf(w) === "改圖";
  const missing = w.exists === false || !w.thumb_url;
  return (
    <figure className="wk-cell wk-img" style={{ width }} data-id={w.id}>
      <button type="button" className="wk-media" style={{ height: h }} onClick={() => onOpen(w.id)} aria-label={`看大圖：${verbOf(w)} ${dt(w.created_at)}`}>
        {fresh ? <NewMark /> : null}
        {w.source && w.source !== "backfill" ? <span className="wk-src">{sourceZh(w.source)}</span> : null}
        {missing ? (
          <span className="wk-gone">檔案不在了</span>
        ) : (
          <img src={`${w.thumb_url}?w=480`} width={w.width ?? undefined} height={w.height ?? undefined} loading="lazy" decoding="async" alt="" />
        )}
      </button>
      <figcaption className="wk-cap">
        <Glyph kind="image" size="sm" />
        <span className="code wk-md">{w.model ?? "沒有記錄模型"}</span>
        {edit ? <span className="wk-tag" title="改圖">改</span> : null}
        <span className="wk-c n">{w.cost_usd != null ? usd(w.cost_usd) : "未回報"}</span>
      </figcaption>
    </figure>
  );
}

/** 卡上的小播放器：播放鈕＋細時間軸（全站同一時間只播一段） */
function MiniPlayer({ src, dur0 }: { src: string; dur0: number | null }) {
  const ref = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [t, setT] = useState(0);
  const [dur, setDur] = useState<number | null>(dur0);
  const toggle = (e: MouseEvent) => {
    e.stopPropagation();
    const a = ref.current;
    if (!a) return;
    if (a.paused) {
      claimAudio(a);
      void a.play().catch(() => setPlaying(false));
    } else a.pause();
  };
  return (
    <div className="wk-player" onClick={(e) => e.stopPropagation()} role="presentation">
      <audio
        ref={ref}
        src={src}
        preload="none"
        onLoadedMetadata={(e) => Number.isFinite(e.currentTarget.duration) && setDur(e.currentTarget.duration)}
        onTimeUpdate={(e) => setT(e.currentTarget.currentTime)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          setT(0);
        }}
      />
      <button type="button" className={`wk-play${playing ? " on" : ""}`} aria-label={playing ? "暫停" : "播放"} onClick={toggle} />
      <div className="wk-trk-w">
        <div className="wk-trk">
          <i style={{ width: dur ? `${Math.min(100, (t / dur) * 100)}%` : 0 }} />
        </div>
        <div className="wk-trkt n">
          <span>{msShort(t)}</span>
          <span>{msShort(dur)}</span>
        </div>
      </div>
    </div>
  );
}

function AudioCard({ w, width, h, fresh, onOpen }: CardProps) {
  const bare = isBare(w);
  const open = () => onOpen(w.id);
  return (
    <figure className="wk-cell" style={{ width }} data-id={w.id}>
      {fresh ? <NewMark /> : null}
      <div className="wk-au" style={{ height: h + CAP }} role="button" tabIndex={0} onClick={open} onKeyDown={openKeys(open)} aria-label={`看詳情：${KIND_ZH[w.kind] ?? w.kind} ${dt(w.created_at)}`}>
        <div className="wk-kh">
          <Glyph kind={String(w.kind)} size="sm" />
          <span>{KIND_ZH[w.kind] ?? w.kind}</span>
          {w.source === "backfill" ? <span className="wk-tag soft">回填</span> : w.source ? <span className="wk-tag">{sourceZh(w.source)}</span> : null}
          <span className="t n">{dt(w.created_at).slice(6)}</span>
        </div>
        {w.title ? <div className="wk-ttl">{w.title}</div> : null}
        <div className="wk-fn code">{fileName(w)}</div>
        {bare ? (
          <div className="wk-thin">當時沒留提示詞與模型</div>
        ) : (
          <div className="wk-thin">
            <span className="code">{w.model ?? "沒有記錄模型"}</span>
            {w.cost_usd != null ? <span className="n"> · {usd(w.cost_usd)}</span> : null}
          </div>
        )}
        {w.exists === false ? <div className="wk-thin">檔案不在了</div> : <MiniPlayer src={w.file_url} dur0={w.duration_s} />}
      </div>
    </figure>
  );
}

/** 摘錄前幾行：行數照卡片高度算，最後一行用「…」收，不切半行 */
function TextBody({ text, h }: { text: string; h: number }) {
  const lines = text
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean)
    .join("\n");
  // 卡高 h+CAP 扣掉：上下內距 21、卡頭 19、摘錄上距 8、底下一行約 29
  const clamp = Math.max(2, Math.floor((h + CAP - 77) / 20.8));
  return (
    <div className="wk-body" style={{ WebkitLineClamp: clamp }}>
      {lines}
    </div>
  );
}

function TextCard({ w, width, h, fresh, onOpen }: CardProps) {
  const text = w.text ?? "";
  const lyrics = w.kind === "lyrics";
  return (
    <figure className="wk-cell" style={{ width }} data-id={w.id}>
      {fresh ? <NewMark /> : null}
      <button type="button" className="wk-tx" style={{ height: h + CAP }} onClick={() => onOpen(w.id)} aria-label={`看全文：${KIND_ZH[w.kind] ?? w.kind} ${dt(w.created_at)}`}>
        <div className="wk-kh">
          <Glyph kind={String(w.kind)} size="sm" />
          <span>{KIND_ZH[w.kind] ?? w.kind}</span>
          <span className="t n">{dt(w.created_at).slice(6)}</span>
        </div>
        {text.trim() ? <TextBody text={text} h={h} /> : <div className="wk-body wk-empty">（沒有文字）</div>}
        <div className="wk-fn code">
          {lyrics ? `${text.split("\n").filter((x) => x.trim()).length} 行` : w.model ?? "沒有記錄模型"}
          {w.duration_s ? ` · ${msShort(w.duration_s)}` : ""}
        </div>
      </button>
    </figure>
  );
}
