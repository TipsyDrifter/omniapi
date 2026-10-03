/* 生成頁共用的小元件：模態章、切換鈕、模型清單、播放器、送出列（含預估費用）、拖放上傳、從作品挑 */
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { ApiError, api } from "@/api/client";
import type { Artifact, Estimate, GenModel, GenOptions, GenRequest } from "@/api/types";
import { dt } from "@/lib/format";
import { Field } from "@/components/dispatch";
import { GLYPH, baseName, firstLine, msShort, priceText, type Built } from "./draft";

/** 四種表單共用的外部狀態（送出由生成頁統一處理） */
export interface FormProps {
  opts: GenOptions | null;
  optsError: string | null;
  busy: boolean;
  error: string | null;
  onSend: (req: GenRequest) => void;
}

export const errMsg = (e: unknown): string => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

/* ---------------- 模態章：方形楷字章，只用墨的填法區分 ---------------- */
export function Glyph({ kind, size }: { kind: string; size?: "sm" | "lg" }) {
  return (
    <span className={`mk-wg k-${kind}${size ? " " + size : ""}`} aria-hidden="true">
      {GLYPH[kind] ?? "·"}
    </span>
  );
}

/* ---------------- 切換鈕群 ---------------- */
export interface SegOpt<T extends string | number | boolean> {
  v: T;
  label: ReactNode;
  disabled?: boolean;
  title?: string;
}
export function Seg<T extends string | number | boolean>({ opts, value, onChange, label }: { opts: SegOpt<T>[]; value: T; onChange: (v: T) => void; label?: string }) {
  return (
    <div className="mk-seg" role="group" aria-label={label}>
      {opts.map((o) => (
        <button key={String(o.v)} type="button" aria-pressed={o.v === value} disabled={o.disabled} title={o.title} onClick={() => onChange(o.v)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** 參數列：左邊中文＋拉丁小標，右邊控制項 */
export function Knob({ zh, en, children }: { zh: string; en: string; children: ReactNode }) {
  return (
    <div className="mk-prow">
      <span className="mk-pk">
        {zh}
        <small>{en}</small>
      </span>
      <div className="mk-pv">{children}</div>
    </div>
  );
}

/* ---------------- 每秒跳一次的「現在」 ---------------- */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now() / 1000);
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return now;
}

/** Ctrl／⌘＋Enter 送出 */
export const ctrlEnter = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    fn();
  }
};

/* ---------------- 模型清單（同新對話頁 dp-mrow 的語彙；反灰＝斜紋＋原因） ---------------- */
const rank = (m: GenModel) => (m.status === "current" ? 0 : m.status === "deprecated" ? 1 : 2);

export function ModelList({ models, sel, onSel, loading, error }: { models: GenModel[]; sel: string | null; onSel: (id: string) => void; loading: boolean; error: string | null }) {
  const [more, setMore] = useState(false);
  const rest = models.filter((m) => m.status === "discovered");
  const rows = useMemo(() => (more ? models : models.filter((m) => m.status !== "discovered")).slice().sort((a, b) => rank(a) - rank(b)), [models, more]);
  const listRef = useRef<HTMLDivElement>(null);
  // 進頁時把選中的那列捲進可視範圍
  useEffect(() => {
    const l = listRef.current;
    const on = l?.querySelector<HTMLElement>("[aria-selected=true]");
    if (l && on && (on.offsetTop + on.offsetHeight > l.scrollTop + l.clientHeight || on.offsetTop < l.scrollTop)) l.scrollTop = Math.max(0, on.offsetTop - 8);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models.length]);
  const usable = models.filter((m) => m.available).length;
  return (
    <Field lbl="Model" zh="模型" aside={models.length ? `${models.length} 個 · 可用 ${usable} · 價格取自模型目錄` : null}>
      {error ? (
        <div className="warn">
          <b>讀不到模型清單</b>：{error}
        </div>
      ) : null}
      <div className="mk-mlist" role="listbox" aria-label="模型清單" ref={listRef}>
        {loading && !error ? <div className="dp-empty">讀取模型清單…</div> : null}
        {rows.map((m) => {
          const p = priceText(m);
          return (
            <button key={m.id} type="button" role="option" className="mk-mrow" aria-selected={m.id === sel} disabled={!m.available} onClick={() => onSel(m.id)} title={m.note ?? m.name ?? m.id}>
              <span className="code">{m.id}</span>
              {m.status === "deprecated" ? (
                <span className="dp-tag" title={m.replacement ? `官方建議改用 ${m.replacement}` : undefined}>
                  {m.shutdown ? `${m.shutdown} 關閉` : "即將關閉"}
                </span>
              ) : m.status === "discovered" ? (
                <span className="dp-tag">未整理</span>
              ) : (
                <span />
              )}
              <span className={p ? "mk-price n" : "mk-price none"}>{p ?? "未定價"}</span>
              <span className="mk-prov">
                {m.provider}
                {m.name ? ` · ${m.name}` : ""}
              </span>
              {!m.available ? <span className="mk-why">{whyNode(m)}</span> : null}
            </button>
          );
        })}
      </div>
      {rest.length ? (
        <div className="mk-mmore">
          <span className="dp-note">
            另有 <span className="n">{rest.length}</span> 個偵測到但還沒整理的模型
          </span>
          <button type="button" className="mk-mini" onClick={() => setMore(!more)}>
            {more ? "收起" : "全部列出"}
          </button>
        </div>
      ) : null}
    </Field>
  );
}

function whyNode(m: GenModel): ReactNode {
  const u = m.unavailable;
  if (u?.reason === "missing_key")
    return (
      <>
        缺 <u className="code">{u.env ?? "API key"}</u>，這個模型送不出去
      </>
    );
  if (u?.reason === "not_implemented") return "這一版還沒接上";
  if (u?.reason === "offline") return "離線模式不呼叫供應商";
  return u?.reason ?? "現在不能用";
}

/* ---------------- 播放器：播放鈕＋時間尺＋長度（不畫假波形） ---------------- */
let playingNow: HTMLAudioElement | null = null;
/** 全站同一時間只播一段：要播之前先叫這個（生成頁、作品牆的卡片與燈箱共用） */
export function claimAudio(a: HTMLAudioElement): void {
  if (playingNow && playingNow !== a) playingNow.pause();
  playingNow = a;
}

export function AudioPlayer({ src, onDuration, big }: { src: string; onDuration?: (s: number) => void; big?: boolean }) {
  const ref = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [dur, setDur] = useState<number | null>(null);
  const [t, setT] = useState(0);
  const toggle = () => {
    const a = ref.current;
    if (!a) return;
    if (a.paused) {
      claimAudio(a);
      void a.play().catch(() => setPlaying(false));
    } else a.pause();
  };
  const seek = (e: MouseEvent<HTMLDivElement>) => {
    const a = ref.current;
    if (!a || !dur) return;
    const r = e.currentTarget.getBoundingClientRect();
    a.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur;
  };
  return (
    <div className={`mk-aud${big ? " mk-aud-big" : ""}`}>
      <audio
        ref={ref}
        src={src}
        preload="metadata"
        onLoadedMetadata={(e) => {
          const d = e.currentTarget.duration;
          if (Number.isFinite(d)) {
            setDur(d);
            onDuration?.(d);
          }
        }}
        onTimeUpdate={(e) => setT(e.currentTarget.currentTime)}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          setT(0);
        }}
      />
      <button type="button" className={`mk-play${playing ? " on" : ""}`} aria-label={playing ? "暫停" : "播放"} onClick={toggle} />
      <div className="mk-ruler" onClick={seek} role="presentation">
        <div className="ends">
          <span>{msShort(t)}</span>
          <span>{msShort(dur)}</span>
        </div>
        <div className="ticks" />
        <div className="fill" style={{ width: dur ? `${Math.min(100, (t / dur) * 100)}%` : 0 }} />
      </div>
      <span className="mk-dur">{msShort(dur)}</span>
    </div>
  );
}

/* ---------------- 送出列：模態章＋摘要＋預估費用＋送出鈕 ---------------- */
export function useEstimate(body: GenRequest): { est: Estimate | null; err: string | null; pending: boolean } {
  const key = JSON.stringify(body);
  const [st, setSt] = useState<{ key: string; est: Estimate | null; err: string | null }>({ key: "", est: null, err: null });
  useEffect(() => {
    let alive = true;
    const t = window.setTimeout(() => {
      api
        .estimate(JSON.parse(key) as GenRequest)
        .then((est) => alive && setSt({ key, est, err: null }))
        .catch((e) => alive && setSt({ key, est: null, err: errMsg(e) }));
    }, 300);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [key]);
  return { est: st.est, err: st.err, pending: st.key !== key };
}

const pu = (v: unknown) => (typeof v === "number" ? `$${+v.toFixed(4)}` : "$—");

/** 依 basis 寫一句依據（M3 派工說明的對照表） */
export function basisText(e: Estimate): string {
  switch (e.basis) {
    case "per_image":
      return `每張 ${pu(e.unit_price)}（${e.tier ?? "—"}）× ${e.n ?? 1}`;
    case "per_1k_chars":
      return `每千字 ${pu(e.unit_price)} × ${e.chars ?? 0} 字`;
    case "per_minute":
      return `每分鐘 ${pu(e.unit_price)} × ${msShort(e.seconds ?? 0)}`;
    case "per_song":
      return `每首 ${pu(e.unit_price)}`;
    case "history":
      return `按 token 計價；依同模型同設定的 ${e.samples ?? 0} 件作品實際花費平均（${pu(e.low)}–${pu(e.high)}）`;
    case "history_model":
      return `按 token 計價；這組設定沒生過，取同模型 ${e.samples ?? 0} 件的平均，僅供參考`;
    case "history_chars":
      return `依這個模型過去每字的實際花費 × ${e.chars ?? 0} 字`;
    case "credits":
      return "用點數計價，每首扣幾點沒有公開；生完記實際費用";
    case "needs_length":
      return `每分鐘 ${pu(e.unit_price)}；填了長度（或選了音檔）才算得出來`;
    case "sandbox":
      return "離線沙盒：不呼叫供應商，不花錢";
    default:
      return "按 token 計價，還沒有可參考的紀錄；送出後記實際費用";
  }
}

export function SubmitBar({ kind, recap, built, busy, error, onSend }: { kind: string; recap: ReactNode; built: Built; busy: boolean; error: string | null; onSend: () => void }) {
  const { est, err } = useEstimate(built.body);
  return (
    <div className="mk-submitwrap">
      {error ? (
        <div className="warn mk-err" role="alert">
          <b>送不出去</b>：{error}
        </div>
      ) : null}
      <div className="mk-submit">
        <div className="mk-recap">
          <Glyph kind={kind} size="sm" />
          <b>{built.verb}</b>
          {recap}
        </div>
        <button type="button" className="stamp-btn mk-go" disabled={!built.req || busy} title={built.problem ?? "Ctrl＋Enter"} onClick={onSend}>
          {busy ? (
            "送出中…"
          ) : (
            <>
              <b>→</b>
              {built.verb}
            </>
          )}
        </button>
        <div className="mk-est" aria-live="polite">
          <span className="k">
            預估
            <br />
            EST
          </span>
          {est && est.usd != null ? (
            <span className="mk-estbig">
              <small>$</small>
              {est.usd.toFixed(4)}
            </span>
          ) : (
            <span className="mk-estna">{est ? "估不出" : err ? "—" : "…"}</span>
          )}
          <span className="mk-basis">{est ? basisText(est) : err ? `預估問不到：${err}` : "正在算…"}</span>
        </div>
        <div className="mk-hint">{built.problem ?? "Ctrl＋Enter 送出。送出後留在這頁，等待與成品從右邊出件。"}</div>
      </div>
    </div>
  );
}

/* ---------------- 拖放上傳 ---------------- */
export function useFileDrop(onFile: (f: File) => void) {
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const handlers = {
    onDragOver: (e: DragEvent) => {
      e.preventDefault();
      setOver(true);
    },
    onDragLeave: () => setOver(false),
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      setOver(false);
      const f = e.dataTransfer.files?.[0];
      if (f) onFile(f);
    },
  };
  const open = () => inputRef.current?.click();
  const input = (accept: string) => (
    <input
      ref={inputRef}
      type="file"
      accept={accept}
      hidden
      onChange={(e) => {
        const f = e.target.files?.[0];
        if (f) onFile(f);
        e.target.value = "";
      }}
    />
  );
  return { over, handlers, open, input };
}

/** 上傳一個檔：回傳上傳結果，錯誤丟出給人看的訊息 */
export function useUploader() {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = useCallback(async (f: File) => {
    setBusy(true);
    setErr(null);
    try {
      return await api.upload(f);
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setErr(status === 413 ? `檔案太大：${errMsg(e)}` : status === 415 ? `這種檔案不收：${errMsg(e)}` : errMsg(e));
      return null;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, err, setErr, run };
}

/* ---------------- 從作品挑 ---------------- */
function useArtifacts(kinds: string[], limit = 24) {
  const [items, setItems] = useState<Artifact[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [next, setNext] = useState<Record<string, number | null>>({});
  const key = kinds.join(",");
  const load = useCallback(
    async (more: boolean) => {
      try {
        const res = await Promise.all(kinds.filter((k) => !more || next[k] != null).map((k) => api.artifacts({ kind: k, limit, before: more ? next[k] : undefined }).then((r) => [k, r] as const)));
        const got = res.flatMap(([, r]) => r.items);
        setNext((o) => ({ ...o, ...Object.fromEntries(res.map(([k, r]) => [k, r.next_before])) }));
        setItems((o) => [...(more ? o ?? [] : []), ...got].filter((a, i, all) => all.findIndex((x) => x.id === a.id) === i).sort((a, b) => b.created_at - a.created_at));
        setErr(null);
      } catch (e) {
        setErr(errMsg(e));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, limit, next],
  );
  useEffect(() => {
    void load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const hasMore = Object.values(next).some((v) => v != null);
  return { items, err, hasMore, more: () => void load(true) };
}

export const artName = (a: Artifact): string => a.title || baseName(a.file_path) || a.id;

export function ImagePicker({ selected, onPick }: { selected: string | null; onPick: (a: Artifact) => void }) {
  const { items, err, hasMore, more } = useArtifacts(["image"]);
  if (err) return <div className="warn">讀不到作品：{err}</div>;
  if (!items) return <div className="dp-empty">讀取作品…</div>;
  if (!items.length) return <div className="dp-empty">作品庫裡還沒有圖。</div>;
  return (
    <div className="mk-pickwrap">
      <div className="mk-pick">
        {items.map((a) => (
          <button key={a.id} type="button" aria-pressed={a.id === selected} title={`${artName(a)}\n${a.prompt ?? ""}`} onClick={() => onPick(a)}>
            {a.thumb_url ? <img src={`${a.thumb_url}?w=240`} alt="" loading="lazy" /> : null}
          </button>
        ))}
      </div>
      {hasMore ? (
        <button type="button" className="mk-mini mk-more" onClick={more}>
          更早的
        </button>
      ) : null}
    </div>
  );
}

export function AudioPicker({ selected, onPick }: { selected: string | null; onPick: (a: Artifact) => void }) {
  const { items, err, hasMore, more } = useArtifacts(["speech", "music"]);
  if (err) return <div className="warn">讀不到作品：{err}</div>;
  if (!items) return <div className="dp-empty">讀取作品…</div>;
  if (!items.length) return <div className="dp-empty">作品庫裡還沒有語音或音樂。</div>;
  return (
    <>
      <div className="mk-alist">
        {items.map((a) => (
          <button key={a.id} type="button" className="mk-arow" aria-pressed={a.id === selected} onClick={() => onPick(a)} disabled={a.exists === false} title={a.exists === false ? "檔案已不在硬碟上" : undefined}>
            <Glyph kind={a.kind} size="sm" />
            <span className="code">{artName(a)}</span>
            <span className="n">{msShort(a.duration_s)}</span>
            <span className="n">{dt(a.created_at)}</span>
          </button>
        ))}
      </div>
      {hasMore ? (
        <button type="button" className="mk-mini mk-more" onClick={more}>
          更早的
        </button>
      ) : null}
    </>
  );
}

export function LyricsPicker({ selected, onPick }: { selected: string | null; onPick: (a: Artifact) => void }) {
  const { items, err } = useArtifacts(["lyrics"]);
  if (err) return <div className="mk-pickfoot">讀不到歌詞：{err}</div>;
  if (!items) return <div className="mk-pickfoot">讀取歌詞…</div>;
  return (
    <div className="mk-pickfoot">
      <span>從作品挑歌詞</span>
      {items.length ? (
        items.slice(0, 8).map((a) => (
          <button key={a.id} type="button" className="mk-mini" aria-pressed={a.id === selected} onClick={() => onPick(a)} title={dt(a.created_at)}>
            {firstLine(a.text) || artName(a)}
          </button>
        ))
      ) : (
        <span className="mk-none">作品庫裡還沒有歌詞</span>
      )}
    </div>
  );
}
