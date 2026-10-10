/* 生成頁共用的小元件：模態章、切換鈕、模型清單、播放器、送出列（含預估費用）、拖放上傳、從作品挑 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type MouseEvent, type ReactNode } from "react";
import { ApiError, api } from "@/api/client";
import type { Artifact, Estimate, GenModel, GenOptions, GenRequest } from "@/api/types";
import { dt } from "@/lib/format";
import { onSendKey, sendHint, sendKeyName, useSendKey } from "@/lib/sendKey";
import { Field } from "@/components/dispatch";
import { ModelFilterBar, RankBadge, VendorHead, needsFilterBar, useModelFilter, type CapFilter } from "@/components/ModelFilterBar";
import { WORK_KIND_INFO, isWorkKind, kindsOfMedia, warnUnknown } from "@/lib/modalities";
import { GLYPH, baseName, firstLine, msShort, priceText, type Built } from "./draft";

/** 能當轉錄音檔的作品（語音、音樂）；文字作品（逐字稿、歌詞） */
const AUDIO_KINDS = kindsOfMedia("audio");
const TEXT_KINDS = kindsOfMedia("text");

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
  if (!(kind in GLYPH)) warnUnknown("作品種類", kind); // 保底：章上畫「·」
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

/** 送出鍵照設定（Ctrl+Enter，或 Enter：只在標了 data-enter-sends 的 prompt 欄單按 Enter 送出），見 lib/sendKey */
export const sendKeys = onSendKey;

/* ---------------- 模型清單（同新對話頁 dp-mrow 的語彙；反灰＝斜紋＋原因） ---------------- */
const statusRank = (m: GenModel) => (m.status === "current" ? 0 : m.status === "deprecated" ? 1 : 2);
const byStatus = (a: GenModel, b: GenModel) => statusRank(a) - statusRank(b);
const isUsable = (m: GenModel) => m.available;


/** 生成頁的模型清單：篩選列同新對話頁（components/ModelFilterBar），預設熱門序；caps＝這一種表單的能力 chips */
export function ModelList({ models, sel, onSel, loading, error, caps }: { models: GenModel[]; sel: string | null; onSel: (id: string) => void; loading: boolean; error: string | null; caps?: CapFilter<GenModel>[] }) {
  const f = useModelFilter(models, { caps, usable: isUsable, keep: sel, baseOrder: byStatus });
  const rows = f.shown;
  const listRef = useRef<HTMLDivElement>(null);
  // 進頁時把選中的那列捲進可視範圍
  useEffect(() => {
    const l = listRef.current;
    const on = l?.querySelector<HTMLElement>("[aria-selected=true]");
    if (l && on && (on.offsetTop + on.offsetHeight > l.scrollTop + l.clientHeight || on.offsetTop < l.scrollTop)) l.scrollTop = Math.max(0, on.offsetTop - 8);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models.length]);
  const usable = models.filter((m) => m.available).length;
  const dupNames = useMemo(() => sameNames(models), [models]);
  return (
    <Field lbl="Model" zh="模型" aside={models.length ? `${models.length} 個 · 可用 ${usable} · 價格取自模型目錄` : null}>
      {error ? (
        <div className="warn">
          <b>讀不到模型清單</b>：{error}
        </div>
      ) : null}
      {needsFilterBar(models) ? <ModelFilterBar f={f} placeholder="搜尋模型 id、名稱、原廠" /> : null}
      <div className="mk-mlist" role="listbox" aria-label="模型清單" ref={listRef}>
        {loading && !error ? <div className="dp-empty">讀取模型清單…</div> : null}
        {models.length && !rows.length ? <div className="dp-empty">{f.q.trim() ? `沒有符合「${f.q.trim()}」的模型。` : "沒有符合的模型；試著拿掉一個篩選。"}</div> : null}
        {rows.map((m, i) => {
          const p = priceText(m);
          const head = f.head(rows, i);
          return (
            <Fragment key={m.id}>
              {head ? <VendorHead label={head.label} count={head.count} /> : null}
              <button type="button" role="option" className="mk-mrow" aria-selected={m.id === sel} disabled={!m.available} onClick={() => onSel(m.id)} aria-label={genRowLabel(m, dupNames)} title={m.note ?? m.name ?? m.id} data-model={m.id}>
                <span className="mf-id">
                  <span className="code">{m.id}</span>
                  <RankBadge m={m} />
                </span>
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
                  {m.vendor_label ? ` · 原廠 ${m.vendor_label}` : ""}
                  {m.name ? ` · ${m.name}` : ""}
                </span>
                {!m.available ? <span className="mk-why">{whyNode(m)}</span> : null}
              </button>
            </Fragment>
          );
        })}
      </div>
    </Field>
  );
}

/** 模型清單一列的無障礙名稱：模型名＋狀態（用「，」接）。生成頁四種表單、影片表單、聊天的換模型小窗都用這一個寫法 */
export function rowLabel(name: string, status: (string | null | undefined | false)[] = []): string {
  const st = status.filter((x): x is string => !!x);
  return st.length ? `${name}，${st.join("，")}` : name;
}

/** 清單裡重複出現的模型名（直連與經 OpenRouter 的同一個模型）：這些列的名字要帶上走哪條路 */
export function sameNames(models: { id: string; name?: string }[]): Set<string> {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const m of models) {
    const n = m.name ?? m.id;
    if (seen.has(n)) dup.add(n);
    seen.add(n);
  }
  return dup;
}

/** 走哪條路：經 OpenRouter／某某直連 */
export const viaText = (provider: string) => (provider === "openrouter" ? "經 OpenRouter" : `${PROVIDER_ZH[provider] ?? provider} 直連`);
const PROVIDER_ZH: Record<string, string> = { openai: "OpenAI", google: "Google", elevenlabs: "ElevenLabs", kie: "kie.ai", deepseek: "DeepSeek", anthropic: "Anthropic" };

/** ModelList 一列的名字：模型名（同名的帶上走哪條路）＋關閉日／未整理／送不出的原因（目錄的英文備註留在 title 當補充） */
export function genRowLabel(m: GenModel, dup?: Set<string>): string {
  const name = m.name ?? m.id;
  const dep = m.status === "deprecated" ? (m.shutdown ? `${m.shutdown} 關閉` : "即將關閉") : m.status === "discovered" ? "未整理" : null;
  return rowLabel(dup?.has(name) ? `${name}（${viaText(m.provider)}）` : name, [dep, !m.available && whyText(m)]);
}

/** whyNode 的純文字版（無障礙名稱用） */
function whyText(m: GenModel): string {
  const u = m.unavailable;
  if (u?.reason === "missing_key") return `缺 ${u.env ?? "API key"}，這個模型送不出去`;
  const n = whyNode(m);
  return typeof n === "string" ? n : "現在不能用";
}

function whyNode(m: GenModel): ReactNode {
  const u = m.unavailable;
  if (u?.reason === "missing_key")
    return (
      <>
        缺 <u className="code">{u.env ?? "API key"}</u>，這個模型送不出去
      </>
    );
  if (u?.reason === "not_implemented" && m.provider === "openrouter") {
    const f = m.image_params?.output_formats ?? [];
    return f.length && f.every((x) => x === "svg") ? "只出 SVG 向量圖，作品牆這一版還不收" : "OpenRouter 上現在沒有供應商提供它";
  }
  if (u?.reason === "not_implemented") return "這一版還沒接上";
  if (u?.reason === "offline") return "離線模式不呼叫供應商";
  return u?.reason ?? "現在不能用";
}

/* ---------------- 播放器：播放鈕＋時間尺＋長度（不畫假波形） ---------------- */
let playingNow: HTMLMediaElement | null = null;
/** 全站同一時間只播一段：要播之前先叫這個（生成頁、作品牆的卡片與燈箱共用；影片也算） */
export function claimAudio(a: HTMLMediaElement): void {
  if (playingNow && playingNow !== a) playingNow.pause();
  playingNow = a;
}

/** slip＝聊天訊息裡音檔單的小播放器（播放鈕＋細時間軸，1.2-M5）；其餘照生成頁、作品牆 */
export function AudioPlayer({ src, onDuration, big, slip }: { src: string; onDuration?: (s: number) => void; big?: boolean; slip?: boolean }) {
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
  const seek = (e: MouseEvent<HTMLElement>) => {
    const a = ref.current;
    if (!a || !dur) return;
    const r = e.currentTarget.getBoundingClientRect();
    a.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * dur;
  };
  const audio = (
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
  );
  const play = <button type="button" className={`mk-play${playing ? " on" : ""}`} aria-label={playing ? "暫停" : "播放"} onClick={toggle} />;
  if (slip)
    return (
      <span className="mf-play">
        {audio}
        {play}
        <span className="mf-trk" onClick={seek} role="presentation" title={dur ? `${msShort(t)} / ${msShort(dur)}` : undefined}>
          <i style={{ width: dur ? `${Math.min(100, (t / dur) * 100)}%` : 0 }} />
        </span>
      </span>
    );
  return (
    <div className={`mk-aud${big ? " mk-aud-big" : ""}`}>
      {audio}
      {play}
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
/** 參考圖另計的那半句（OpenRouter） */
const refText = (e: Estimate): string =>
  e.refs ? (e.refs_unpriced ? `，${e.refs} 張參考圖另依用量計` : e.ref_usd ? `，含 ${e.refs} 張參考圖 ${pu(e.ref_usd)}` : "") : "";

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
    case "sandbox": {
      const l = e.listed;
      const real = l ? (l.usd != null ? pu(l.usd) : l.low != null && l.high != null ? `${pu(l.low)}–${pu(l.high)}` : null) : null;
      return `離線沙盒：不呼叫供應商，不花錢${real ? `（真的送出照名單約 ${real}）` : ""}`;
    }
    case "variant":
      return `OpenRouter 名單：每張 ${pu(e.unit_price)}${e.variant ? `（${String(e.variant).replace("_", " ")}）` : ""} × ${e.n ?? 1}${refText(e)}；帳上記實際費用`;
    case "range":
      return `OpenRouter 名單上這組設定可能是 ${pu(e.low)}–${pu(e.high)}（對不到確切的分級）${refText(e)}；帳上記實際費用`;
    case "per_megapixel":
      return `每百萬像素 ${pu(e.mp_price)}，以約 ${e.megapixels ?? 1} 百萬像素估${refText(e)}；實際依輸出尺寸計`;
    case "per_token":
      return "OpenRouter 依 token 計價，送出前算不出；帳上記實際費用";
    case "no_price":
      return "OpenRouter 沒有公布這個模型的定價；帳上記實際費用";
    default:
      return "按 token 計價，還沒有可參考的紀錄；送出後記實際費用";
  }
}

export function SubmitBar({ kind, recap, built, busy, error, onSend }: { kind: string; recap: ReactNode; built: Built; busy: boolean; error: string | null; onSend: () => void }) {
  const { est, err } = useEstimate(built.body);
  const [sendKey] = useSendKey();
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
        <button type="button" className="stamp-btn mk-go" disabled={!built.req || busy} title={built.problem ?? sendKeyName(sendKey)} onClick={onSend}>
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
              <small>{est.approx ? "約 $" : "$"}</small>
              {est.usd.toFixed(4)}
            </span>
          ) : est && est.low != null && est.high != null && est.basis === "range" ? (
            <span className="mk-estbig">
              <small>$</small>
              {+est.low.toFixed(4)}–{+est.high.toFixed(4)}
            </span>
          ) : (
            <span className="mk-estna">{est ? "估不出" : err ? "—" : "…"}</span>
          )}
          <span className="mk-basis">{est ? basisText(est) : err ? `預估問不到：${err}` : "正在算…"}</span>
        </div>
        <div className="mk-hint">{built.problem ?? `${sendHint(sendKey)}。送出後留在這頁，等待與成品從右邊出件。`}</div>
      </div>
    </div>
  );
}

/* ---------------- 拖放上傳 ---------------- */
/** multiple：一次收多個檔（聊天附件）；預設只取第一個（生成頁的來源圖、音檔） */
export function useFileDrop(onFile: (f: File) => void, opts: { multiple?: boolean } = {}) {
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const take = (files: FileList | null | undefined) => {
    const list = Array.from(files ?? []);
    for (const f of opts.multiple ? list : list.slice(0, 1)) onFile(f);
  };
  const handlers = {
    onDragOver: (e: DragEvent) => {
      e.preventDefault();
      setOver(true);
    },
    onDragLeave: () => setOver(false),
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      setOver(false);
      take(e.dataTransfer.files);
    },
  };
  const open = () => inputRef.current?.click();
  const input = (accept: string) => (
    <input
      ref={inputRef}
      type="file"
      accept={accept}
      multiple={opts.multiple}
      hidden
      onChange={(e) => {
        take(e.target.files);
        e.target.value = "";
      }}
    />
  );
  return { over, handlers, open, input };
}

/** 上傳失敗 → 給人看的一句（413 太大、415 型別不收；其餘照後端的原因） */
export function uploadErrText(e: unknown): string {
  const status = e instanceof ApiError ? e.status : 0;
  return status === 413 ? `檔案太大：${errMsg(e)}` : status === 415 ? `這種檔案不收：${errMsg(e)}` : errMsg(e);
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
      setErr(uploadErrText(e));
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

/** selected 可以是多個（聊天一則能附好幾張） */
export function ImagePicker({ selected, onPick }: { selected: string | readonly string[] | null; onPick: (a: Artifact) => void }) {
  const { items, err, hasMore, more } = useArtifacts(["image"]);
  if (err) return <div className="warn">讀不到作品：{err}</div>;
  if (!items) return <div className="dp-empty">讀取作品…</div>;
  if (!items.length) return <div className="dp-empty">作品庫裡還沒有圖。</div>;
  const on = (id: string) => (typeof selected === "string" || selected == null ? id === selected : selected.includes(id));
  return (
    <div className="mk-pickwrap">
      <div className="mk-pick">
        {items.map((a) => (
          <button
            key={a.id}
            type="button"
            aria-pressed={on(a.id)}
            disabled={a.exists === false}
            title={a.exists === false ? `${artName(a)}\n檔案已經不在硬碟上` : `${artName(a)}\n${a.prompt ?? ""}`}
            onClick={() => onPick(a)}
          >
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
  const { items, err, hasMore, more } = useArtifacts(AUDIO_KINDS);
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

/** 1.2-M5：作品牆的文字作品（歌詞、逐字稿），聊天可以當附件；selected 可以是多個 */
export function TextWorkPicker({ selected, onPick }: { selected: readonly string[]; onPick: (a: Artifact) => void }) {
  const { items, err, hasMore, more } = useArtifacts(TEXT_KINDS, 12);
  if (err) return <div className="warn">讀不到作品：{err}</div>;
  if (!items) return <div className="dp-empty">讀取作品…</div>;
  if (!items.length) return <div className="dp-empty">作品庫裡還沒有逐字稿或歌詞。</div>;
  return (
    <>
      <div className="mk-alist">
        {items.map((a) => (
          <button key={a.id} type="button" className="mk-arow" aria-pressed={selected.includes(a.id)} onClick={() => onPick(a)} title={firstLine(a.text) || undefined}>
            <Glyph kind={a.kind} size="sm" />
            <span className="code">{a.title || firstLine(a.text) || artName(a)}</span>
            <span className="n">{isWorkKind(a.kind) ? WORK_KIND_INFO[a.kind].zh : String(a.kind)}</span>
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
