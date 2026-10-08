/* 影片表單（v1.4，版面稿 prototypes/video-design/）：提示詞、首幀與尾幀（一條時間條的兩端）、模型（篩選＋比較全部）、
   輸出（秒數、解析度、比例、聲音——全部照每個模型的 video_params）、預估。
   「→ 生影片」不會直接送出：先出確認單（寬版蓋在送出列的位置、手機從底部升起），在確認單按「確定送出」才送。
   送出鍵（Ctrl+Enter，或設定成 Enter）在表單＝開確認單、在確認單＝確定送出。 */
import { Fragment, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api } from "@/api/client";
import type { GenModel, VideoKindExtras } from "@/api/types";
import { Field } from "@/components/dispatch";
import { ModelFilterBar, RankBadge, VendorHead, useModelFilter, type CapFilter } from "@/components/ModelFilterBar";
import { SendKeyHint } from "@/components/SendKeyMenu";
import Sheet from "@/components/Sheet";
import { useTier, useTouch } from "@/lib/rwd";
import { sendKeyName, useSendKey } from "@/lib/sendKey";
import { daysLeft } from "@/lib/catalog";
import { setDraft, useMake } from "@/store/make";
import { Glyph, ImagePicker, Knob, Seg, artName, sendKeys, useEstimate, useFileDrop, useUploader, type FormProps } from "./bits";
import { effModel, msShort, type PickedSource, type VideoDraft } from "./draft";
import { Caps } from "./VideoCaps";
import { ConfirmTicket } from "./VideoConfirm";
import { VideoCompare } from "./VideoCompare";
import {
  VIDEO_UI,
  audioPricedApart,
  buildVideo,
  durTxt,
  durationsContiguous,
  modelRank,
  orderRatios,
  perSecondSpan,
  perTxt,
  resTxt,
  sortRes,
  tokenPriced,
  videoEst,
  videoPick,
  videoPrice,
  vparams,
  vstate,
  vUsd,
  type VideoPick,
} from "./video";

const MAX_PROMPT = 4000;
const IMG_ACCEPT = "image/png,image/jpeg,image/webp";

export function VideoForm({ opts, optsError, busy, error, onSend }: FormProps) {
  const d = useMake((s) => s.drafts.video);
  const set = (p: Partial<VideoDraft>) => setDraft("video", p);
  const vk = opts?.kinds.video ?? null;
  const models = vk?.models ?? [];
  const m = effModel(opts, "video", d.model);
  const pick = videoPick(d, m);
  const built = buildVideo(d, m);
  const { est: raw, err: estErr, pending } = useEstimate(built.body);
  const est = videoEst(raw, m, pick);
  const sheet = useTier() === "s";
  const [confirm, setConfirm] = useState(false);
  const [cmp, setCmp] = useState(false);
  const sending = useRef(false);
  const [sent, setSent] = useState(false);

  // 送出後：成功就收起確認單；失敗留著，錯誤寫在單子上
  useEffect(() => {
    if (!sent || busy) return;
    sending.current = false;
    setSent(false);
    if (!error) setConfirm(false);
  }, [sent, busy, error]);
  // 表單變得送不出去（提示詞清空、換成用不了的模型）：確認單收起
  useEffect(() => {
    if (confirm && !built.req && !sent) setConfirm(false);
  }, [confirm, built.req, sent]);

  const send = () => {
    if (sending.current || busy || !built.req || pending || !est) return;
    sending.current = true;
    setSent(true);
    onSend(built.req);
  };
  /** 「→ 生影片」與送出鍵：開確認單（第 1 題：設了門檻、預估確定低於它才直接送） */
  const open = () => {
    if (!built.req || busy) return;
    const t = VIDEO_UI.skipConfirmBelowUsd;
    if (t != null && est && !est.sandbox && est.kind === "exact" && est.usd != null && est.usd < t && !pending) return send();
    setConfirm(true);
  };
  const back = () => {
    if (busy) return;
    setConfirm(false);
    window.setTimeout(() => document.getElementById("mk-prompt")?.focus({ preventScroll: true }), 0);
  };
  const ticket = m ? (
    <ConfirmTicket m={m} pick={pick} est={est} pending={pending} busy={busy} error={error} limits={vk?.limits ?? null} onConfirm={send} onBack={back} onApply={(p) => set(p)} />
  ) : null;

  return (
    <>
      <div className="mk-main" onKeyDown={sendKeys(open)}>
        <PromptField d={d} set={set} first={!!pick.first} />
        <FramesField d={d} set={set} m={m} pick={pick} />
        {confirm && !sheet && ticket ? (
          <div className="mk-submitwrap vd-okwrap">{ticket}</div>
        ) : (
          <VideoSubmit m={m} pick={pick} built={built} est={est} estErr={estErr} busy={busy} error={confirm ? null : error} onOpen={open} />
        )}
      </div>
      <aside className="mk-side" onKeyDown={sendKeys(open)}>
        <VideoModels models={models} sel={m?.id ?? null} onSel={(id) => set({ model: id })} loading={!opts} error={optsError} unlisted={vk?.unlisted ?? []} onCompare={() => setCmp(true)} />
        {m ? <OutputField m={m} d={d} set={set} pick={pick} /> : null}
      </aside>
      {sheet && ticket ? (
        <Sheet open={confirm} onClose={back} label="送出前確認" className="vd-oksheet">
          {ticket}
        </Sheet>
      ) : null}
      {cmp ? <VideoCompare models={models} sel={m?.id ?? null} unlisted={vk?.unlisted ?? []} onPick={(id) => set({ model: id })} onClose={() => setCmp(false)} /> : null}
    </>
  );
}

/* ---------------- 提示詞 ---------------- */
function PromptField({ d, set, first }: { d: VideoDraft; set: (p: Partial<VideoDraft>) => void; first: boolean }) {
  const chars = [...d.prompt].length;
  return (
    <Field
      lbl="Prompt"
      zh="提示詞"
      htmlFor="mk-prompt"
      aside={
        <>
          <span className="n">{chars}</span> / <span className="n">{MAX_PROMPT}</span> 字<SendKeyHint sep />
        </>
      }
    >
      <textarea
        id="mk-prompt"
        data-enter-sends=""
        className="dp-prompt mk-prompt vd-prompt"
        value={d.prompt}
        maxLength={MAX_PROMPT}
        onChange={(e) => set({ prompt: e.target.value })}
        placeholder={first ? "要怎麼動：主體做什麼、鏡頭怎麼走、最後停在哪。" : "想要什麼畫面、怎麼動：主體、動作、鏡頭、光線。"}
        spellCheck={false}
        autoFocus
      />
    </Field>
  );
}

/* ---------------- 首幀與尾幀：一條時間條的兩端 ---------------- */
type Which = "first" | "last";
/** 圖的寬高 → 常見的比例名（對不到就寫小數）：縮圖的像素不是原圖的，只用來看比例 */
const NAMED = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "9:21", "5:4", "4:5"];
function ratioName(w: number, h: number): string {
  const r = w / h;
  const hit = NAMED.find((n) => {
    const [a, b] = n.split(":").map(Number);
    return Math.abs(r - a / b) / (a / b) < 0.03;
  });
  return hit ?? `約 ${r.toFixed(2)}:1`;
}
const ZH: Record<Which, string> = { first: "首", last: "尾" };

function FramesField({ d, set, m, pick }: { d: VideoDraft; set: (p: Partial<VideoDraft>) => void; m: GenModel | null; pick: VideoPick }) {
  const vp = vparams(m);
  const name = m?.name ?? m?.id ?? "這個模型";
  const [picking, setPicking] = useState<Which | null>(null);
  const [firstSize, setFirstSize] = useState<{ w: number; h: number } | null>(null);
  const dur = pick.duration ?? 5;
  const tk = `${(100 / Math.max(1, dur)).toFixed(3)}%`;
  const takes = { first: vp.frames.includes("first_frame"), last: vp.frames.includes("last_frame") };
  const put = (w: Which, s: PickedSource | null) => set(w === "first" ? { first: s } : { last: s });
  // 首幀的比例跟影片不同：照實說「可能」，不宣稱供應商一定怎麼做
  const ratioOff = (() => {
    if (!pick.first || !firstSize || !pick.ratio) return false;
    const [rw, rh] = pick.ratio.split(":").map(Number);
    if (!(rw > 0 && rh > 0)) return false;
    return Math.abs(firstSize.w / firstSize.h - rw / rh) / (rw / rh) > 0.03;
  })();
  let note: ReactNode = null;
  if (pick.firstDropped || pick.lastDropped) {
    const which = pick.firstDropped && pick.lastDropped ? "首幀與尾幀" : pick.firstDropped ? "首幀" : "尾幀";
    note = (
      <div className="warn">
        <b>
          {name} 不收{which}：剛才放的{which}不會送出
        </b>
        。圖還留在這裡，換回收{which === "首幀" ? "首幀" : "尾幀"}的模型就會再用上；不要了就按「拿掉」。
      </div>
    );
  } else if (!takes.first && !takes.last) note = <p className="dp-note vd-frames-note">{name} 只做文生影片，不收首幀與尾幀。</p>;
  else if (pick.first && pick.last) note = <p className="dp-note vd-frames-note">首幀與尾幀都放好了：影片會從左邊那格開始、停在右邊那格。</p>;
  else if (pick.first)
    note = (
      <p className="dp-note vd-frames-note">
        首幀放好了：<b>圖生影片</b>。{takes.last ? "要指定最後一格的話，右邊再放一張尾幀。" : ""}
      </p>
    );
  else
    note = (
      <p className="dp-note vd-frames-note">
        有首幀＝<b>圖生影片</b>；沒有＝<b>文生影片</b>。可以從電腦選（png、jpg、webp），也可以從作品牆挑。
        {takes.first && !takes.last ? `${name} 只收首幀；要尾幀的話，模型清單點「要尾幀」篩出來。` : ""}
      </p>
    );
  return (
    <Field lbl="Frames" zh="首幀與尾幀" aside={takes.first ? "選填 · 有首幀＝圖生影片" : "這個模型不收"}>
      <div className="vd-frames">
        <div className="vd-fr">
          <div className="vd-fr-k">
            首幀<small>FIRST</small>
            <span className="n">0:00</span>
          </div>
          <FrameSlot which="first" src={d.first} can={takes.first} name={name} picking={picking === "first"} onPick={() => setPicking(picking === "first" ? null : "first")} onSet={(s) => put("first", s)} onSize={setFirstSize} />
        </div>
        <div className="vd-span" aria-hidden="true">
          <span className="what">影片長度</span>
          <div className="base" style={{ "--tk": tk } as CSSProperties} />
          <span className="sec">{pick.duration != null ? `${pick.duration} 秒` : "照模型"}</span>
        </div>
        <div className="vd-fr">
          <div className="vd-fr-k">
            尾幀<small>LAST</small>
            <span className="n">{msShort(dur)}</span>
          </div>
          <FrameSlot which="last" src={d.last} can={takes.last} name={name} picking={picking === "last"} onPick={() => setPicking(picking === "last" ? null : "last")} onSet={(s) => put("last", s)} />
        </div>
      </div>
      {picking ? (
        <ImagePicker
          selected={(() => {
            const s = picking === "first" ? d.first : d.last;
            return s && "artifact_id" in s.ref ? s.ref.artifact_id ?? null : null;
          })()}
          onPick={(a) => {
            put(picking, { ref: { artifact_id: a.id }, name: artName(a), url: a.thumb_url ? `${a.thumb_url}?w=480` : a.file_url, from: "image", bytes: a.bytes, mime: a.mime });
            setPicking(null);
          }}
        />
      ) : null}
      {note}
      {ratioOff ? (
        <p className="dp-note vd-frames-note">
          首幀與影片的比例不同（首幀 {ratioName(firstSize!.w, firstSize!.h)}、影片 {pick.ratio}），供應商可能裁切或補邊。
        </p>
      ) : null}
    </Field>
  );
}

function FrameSlot({ which, src, can, name, picking, onPick, onSet, onSize }: { which: Which; src: PickedSource | null; can: boolean; name: string; picking: boolean; onPick: () => void; onSet: (s: PickedSource | null) => void; onSize?: (s: { w: number; h: number } | null) => void }) {
  const up = useUploader();
  const drop = useFileDrop(async (f) => {
    const u = await up.run(f);
    if (!u) return;
    if (u.kind !== "image") {
      up.setErr("這不是圖：首幀與尾幀要放 png、jpg 或 webp。");
      return;
    }
    onSet({ ref: { upload_id: u.id }, name: u.filename, url: api.uploadFileUrl(u.id), from: "upload", bytes: u.bytes, mime: u.mime });
  });
  useEffect(() => {
    if (!src) onSize?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);
  const img = src ? <img src={src.url} alt={src.name ?? ""} onLoad={(e) => onSize?.({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} /> : null;
  if (!can && src)
    return (
      <>
        <div className="vd-slot drop" aria-label={`${name} 不收${ZH[which]}幀，這張不會送出`}>
          {img}
          <b>不會送出</b>
        </div>
        <div className="mk-row">
          <button type="button" className="mk-mini" onClick={() => onSet(null)}>
            拿掉
          </button>
        </div>
      </>
    );
  if (!can)
    return (
      <>
        <div className="vd-slot no" aria-disabled="true">
          <span>
            {name} 不收{ZH[which]}幀
          </span>
        </div>
        <div className="mk-row">
          <span className="dp-note">換一個收{ZH[which]}幀的模型才能放</span>
        </div>
      </>
    );
  return (
    <>
      <div className={`vd-slot${src ? " has" : ""}${drop.over ? " over" : ""}`} {...drop.handlers} onClick={drop.open} role="button" tabIndex={0} aria-label={src ? `換一張${ZH[which]}幀` : `放一張${ZH[which]}幀`} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && e.target === e.currentTarget && (e.preventDefault(), drop.open())}>
        {src ? (
          img
        ) : up.busy ? (
          "上傳中…"
        ) : (
          <>
            <span className="x-touch">
              拖一張圖進來
              <br />
              或點這裡選
            </span>
            <span className="only-touch">點這裡選一張圖</span>
          </>
        )}
      </div>
      {drop.input(IMG_ACCEPT)}
      <div className="mk-row">
        <button type="button" className="mk-mini" onClick={drop.open} disabled={up.busy}>
          {src ? "換一張…" : "選圖…"}
        </button>
        <button type="button" className="mk-mini" aria-pressed={picking} onClick={onPick}>
          {picking ? "收起作品" : "從作品挑"}
        </button>
        {src ? (
          <button type="button" className="mk-mini" onClick={() => onSet(null)}>
            拿掉
          </button>
        ) : null}
      </div>
      {up.err ? <div className="warn">{up.err}</div> : null}
    </>
  );
}

/* ---------------- 模型：篩選列＋清單＋「比較全部」 ---------------- */
function PriceCell({ m }: { m: GenModel }) {
  const s = perSecondSpan(m);
  if (!s)
    return tokenPriced(m) ? (
      <span className="mk-price n">
        依用量<small>token</small>
      </span>
    ) : (
      <span className="mk-price none">未定價</span>
    );
  return (
    <span className="mk-price n">
      {perTxt(s.low)}
      {s.high !== s.low ? "起" : ""}
      <small>/秒</small>
    </span>
  );
}

const goneNote = (m: GenModel): string | null => {
  const st = vstate(m);
  if (st === "retired") return `原廠已關閉${m.shutdown ? `（${m.shutdown}）` : ""}，送不出去`;
  if (st === "off") return m.unavailable?.reason === "not_implemented" ? "這一版還沒接上" : m.unavailable?.reason === "missing_key" ? `缺 ${m.unavailable.env ?? "API key"}，送不出去` : m.unavailable?.reason === "offline" ? "離線模式不呼叫供應商" : "現在送不出去";
  return null;
};

/** 不經 OpenRouter、直連原廠的影片模型標的廠名 */
const DIRECT_LABEL: Record<string, string> = { google: "Google" };

/** 影片的能力 chips（同模型頁）：要首幀、要尾幀、要聲音 */
const VIDEO_CAPS: CapFilter<GenModel>[] = [
  { key: "ff", label: "要首幀", test: (m) => vparams(m).frames.includes("first_frame") },
  { key: "lf", label: "要尾幀", test: (m) => vparams(m).frames.includes("last_frame") },
  { key: "audio", label: "要聲音", test: (m) => vparams(m).audio === true },
];
const byPrice = (a: GenModel, b: GenModel) => modelRank(a) - modelRank(b);
const retired = (m: GenModel) => vstate(m) === "retired";
const videoUsable = (m: GenModel) => { const st = vstate(m); return st !== "retired" && st !== "off"; };
const videoPriceKey = (m: GenModel) => perSecondSpan(m)?.low ?? null;
const videoHay = (m: GenModel) => `${m.id} ${m.name ?? ""} ${m.vendor_label ?? ""} ${m.vendor ?? ""}`.toLowerCase();

function VideoModels({ models, sel, onSel, loading, error, unlisted, onCompare }: { models: GenModel[]; sel: string | null; onSel: (id: string) => void; loading: boolean; error: string | null; unlisted: VideoKindExtras["unlisted"]; onCompare: () => void }) {
  // 篩選列同新對話頁（components/ModelFilterBar）：預設熱門序（文生影片、圖生影片兩張榜取好的那個）；原廠已關閉的一律墊底
  const f = useModelFilter(models, { caps: VIDEO_CAPS, usable: videoUsable, keep: sel, baseOrder: byPrice, sink: retired, priceKey: videoPriceKey, haystack: videoHay });
  const rows = f.shown;
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const l = listRef.current;
    const on = l?.querySelector<HTMLElement>("[aria-selected=true]");
    if (l && on) l.scrollTop = Math.max(0, l.scrollTop + on.getBoundingClientRect().top - l.getBoundingClientRect().top - 8);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models.length]);
  const usable = models.filter((m) => m.available).length;
  const cur = models.find((m) => m.id === sel) ?? null;
  const sun = cur && vstate(cur) === "sunset" ? cur : null;
  return (
    <Field lbl="Model" zh="模型" aside={models.length ? `${rows.length} 個${f.filtered ? "（篩過）" : ""} · 可用 ${usable} · 價格取自 OpenRouter 名單${models.some((m) => m.provider !== "openrouter") ? "與原廠定價頁" : ""}` : null}>
      {error ? (
        <div className="warn">
          <b>讀不到模型清單</b>：{error}
        </div>
      ) : null}
      <ModelFilterBar
        f={f}
        placeholder="搜尋模型 id、名稱、原廠"
        tail={
          <button type="button" className="mk-mini vd-cmpbtn" onClick={onCompare} disabled={!models.length}>
            比較全部 →
          </button>
        }
      />
      <div className="mk-mlist vd-mlist" role="listbox" aria-label="影片模型" ref={listRef}>
        {loading && !error ? <div className="dp-empty">讀取模型清單…</div> : null}
        {!loading && !models.length && !error ? <div className="dp-empty">還沒有影片模型：影片經 OpenRouter 生，或用 Google 的 key 直連 Gemini Omni；先到設定頁貼其中一把 key。</div> : null}
        {models.length && !rows.length ? <div className="dp-empty">沒有符合的模型。</div> : null}
        {rows.map((m, i) => {
          const head = f.head(rows, i);
          const st = vstate(m);
          const dead = st === "retired" || st === "off";
          const vp = vparams(m);
          const why = goneNote(m);
          return (
            <Fragment key={m.id}>
            {head ? <VendorHead label={head.label} count={head.count} /> : null}
            <button type="button" role="option" className={`mk-mrow vd-mrow${dead ? " gone" : ""}`} aria-selected={m.id === sel} disabled={dead} onClick={() => onSel(m.id)} title={m.note ?? m.name ?? m.id} data-model={m.id}>
              <span className="mf-id">
                <span className="code">{m.id}</span>
                <RankBadge m={m} />
              </span>
              <PriceCell m={m} />
              <span className="mk-prov">
                {m.name ?? ""}
                {m.vendor_label ? ` · ${m.vendor_label}` : m.provider !== "openrouter" ? ` · ${DIRECT_LABEL[m.provider] ?? m.provider} 直連` : ""}
                {st === "sunset" ? <span className="dp-tag vd-sun">{m.shutdown ? `${m.shutdown} 下架` : "即將下架"}</span> : null}
              </span>
              <span className="vd-caps">
                <span>{durTxt(vp)}</span>
                <span>{resTxt(vp)}</span>
                <Caps m={m} />
              </span>
              {why ? <span className="mk-why">{why}</span> : null}
            </button>
            </Fragment>
          );
        })}
      </div>
      {sun ? (
        <div className="warn vd-sunwarn">
          <b>{sun.name ?? sun.id} 原廠公告下架</b>：{sun.shutdown ? `${sun.shutdown}${daysLeft(sun.shutdown) >= 0 ? `（剩 ${daysLeft(sun.shutdown)} 天）` : ""}` : "日期未定"}，到期之後送不出。
          {sun.replacement ? (
            <>
              官方建議改用 <span className="code">{sun.replacement}</span>。
            </>
          ) : null}
        </div>
      ) : null}
      {unlisted.length ? (
        <div className="mk-mmore">
          <span className="dp-note">
            另有 <span className="n">{unlisted.length}</span> 個不是生影片的（編輯、放大、數位人），這一版不收
          </span>
        </div>
      ) : null}
    </Field>
  );
}

/* ---------------- 輸出：全部照這個模型的 video_params ---------------- */
function ratioLabel(r: string) {
  const [w, h] = r.split(":").map(Number);
  if (!(w > 0 && h > 0)) return r;
  const k = 14 / Math.max(w, h);
  return (
    <>
      <span className="mk-ar" style={{ width: Math.round(w * k), height: Math.round(h * k) }} />
      {r}
    </>
  );
}

function OutputField({ m, d, set, pick }: { m: GenModel; d: VideoDraft; set: (p: Partial<VideoDraft>) => void; pick: VideoPick }) {
  const vp = vparams(m);
  const name = m.name ?? m.id;
  const per = (o: { res?: string | null; audio?: boolean | null }) => {
    const p = videoPrice(m.pricing?.skus, { seconds: 1, resolution: o.res ?? pick.resolution, audio: o.audio === undefined ? pick.audio ?? vp.audio : o.audio, firstFrame: !!pick.first, frames: 0 });
    return p.kind === "unknown" ? null : p.perSecond;
  };
  const apart = audioPricedApart(m);
  const res = sortRes(vp.resolutions);
  const ratios = orderRatios(vp.aspect_ratios);
  const lo = vp.durations[0];
  const hi = vp.durations[vp.durations.length - 1];
  const cur = pick.duration;
  void d;
  return (
    <Field lbl="Output" zh="輸出" aside={`${name}：${durTxt(vp)}${res.length ? ` · ${res.join("／")}` : ""}`}>
      <div>
        <Knob zh="秒數" en="SEC">
          {!vp.durations.length ? (
            <span className="dp-note">名單沒寫可選的長度：照模型預設。</span>
          ) : vp.durations.length === 1 ? (
            <span className="vd-fixed">
              <span className="vd-tag">{vp.durations[0]} 秒</span>這個模型只有一種長度
            </span>
          ) : durationsContiguous(vp.durations) && cur != null ? (
            <>
              <div className="vd-dur">
                <button type="button" className="vd-step" aria-label="少一秒" disabled={cur <= lo} onClick={() => set({ duration: cur - 1 })}>
                  −
                </button>
                <span className="vd-durbig" aria-live="polite">
                  {cur}
                  <small>秒</small>
                </span>
                <button type="button" className="vd-step" aria-label="多一秒" disabled={cur >= hi} onClick={() => set({ duration: cur + 1 })}>
                  +
                </button>
                <div className="vd-rng" aria-hidden="true">
                  <div className="ln" />
                  <div className="pt" style={{ left: `${((cur - lo) / (hi - lo)) * 100}%` }} />
                  <span className="lab l">{lo}</span>
                  <span className="lab r">{hi} 秒</span>
                </div>
              </div>
              <div className="vd-quick">
                <Seg label="常用秒數" opts={[5, 10, 15, 30].filter((s) => vp.durations.includes(s)).map((s) => ({ v: s, label: `${s} 秒` }))} value={cur} onChange={(v) => set({ duration: v })} />
              </div>
            </>
          ) : (
            <Seg label="秒數" opts={vp.durations.map((s) => ({ v: s, label: `${s} 秒` }))} value={cur ?? -1} onChange={(v) => set({ duration: v })} />
          )}
        </Knob>
        <Knob zh="解析度" en="RES">
          {!res.length ? (
            <span className="dp-note">名單沒寫可選的解析度：照模型預設。</span>
          ) : res.length === 1 ? (
            <span className="vd-fixed">
              <span className="vd-tag">{res[0]}</span>這個模型只有一種
            </span>
          ) : (
            <Seg
              label="解析度"
              opts={res.map((r) => {
                const p = per({ res: r });
                return {
                  v: r,
                  label: (
                    <>
                      {r}
                      {p != null ? <span className="vd-sub">{perTxt(p)}/秒</span> : null}
                    </>
                  ),
                };
              })}
              value={pick.resolution ?? ""}
              onChange={(v) => set({ resolution: v })}
            />
          )}
        </Knob>
        <Knob zh="比例" en="RATIO">
          {!ratios.length ? (
            <span className="dp-note">名單沒寫可選的比例：照模型預設{pick.first ? "（多半跟著首幀）" : ""}。</span>
          ) : ratios.length === 1 ? (
            <span className="vd-fixed">
              <span className="vd-tag">{ratios[0]}</span>這個模型只有一種
            </span>
          ) : (
            <Seg label="比例" opts={ratios.map((r) => ({ v: r, label: ratioLabel(r) }))} value={pick.ratio ?? ""} onChange={(v) => set({ aspect_ratio: v })} />
          )}
        </Knob>
        <Knob zh="聲音" en="AUDIO">
          {vp.audio === true && vp.audio_fixed ? (
            <span className="vd-fixed">
              <span className="vd-tag">有聲</span>
              {name} 一定帶聲音，沒有開關；不要聲音就在提示詞寫「no dialogue」「no music」
            </span>
          ) : vp.audio === true ? (
            <>
              <Seg
                label="聲音"
                opts={[
                  {
                    v: "on",
                    label: (
                      <>
                        有聲{apart && per({ audio: true }) != null ? <span className="vd-sub">{perTxt(per({ audio: true })!)}/秒</span> : null}
                      </>
                    ),
                  },
                  {
                    v: "off",
                    label: (
                      <>
                        無聲{apart && per({ audio: false }) != null ? <span className="vd-sub">{perTxt(per({ audio: false })!)}/秒</span> : null}
                      </>
                    ),
                  },
                ]}
                value={pick.audio === false ? "off" : "on"}
                onChange={(v) => set({ audio: v })}
              />
              {!apart ? <div className="dp-note">這個模型有沒有聲音同價。</div> : null}
            </>
          ) : vp.audio === false ? (
            <span className="vd-fixed">
              <span className="vd-tag dash">無聲</span>
              {name} 只做沒有聲音的影片
            </span>
          ) : (
            <span className="vd-fixed">
              <span className="vd-tag dash">名單沒標</span>不知道有沒有聲音，做出來才知道；也沒有開關可以送
            </span>
          )}
        </Knob>
      </div>
    </Field>
  );
}

/* ---------------- 送出列：「→ 生影片」＝開確認單 ---------------- */
function audTxt(m: GenModel | null, pick: VideoPick): string {
  const a = vparams(m).audio;
  return a === true ? (pick.audio === false ? "無聲" : "有聲") : a === false ? "無聲" : "聲音未知";
}

function VideoSubmit({ m, pick, built, est, estErr, busy, error, onOpen }: { m: GenModel | null; pick: VideoPick; built: ReturnType<typeof buildVideo>; est: ReturnType<typeof videoEst>; estErr: string | null; busy: boolean; error: string | null; onOpen: () => void }) {
  const [sendKey] = useSendKey();
  const touch = useTouch();
  const spec = [pick.resolution, pick.ratio, pick.duration != null ? `${pick.duration} 秒` : null, audTxt(m, pick)].filter(Boolean).join(" · ");
  let big: ReactNode;
  if (!est) big = <span className="mk-estna">{estErr ? "—" : "…"}</span>;
  else if (est.kind === "exact")
    big = (
      <span className="mk-estbig">
        <span className="vd-approx">約</span>
        <small>$</small>
        {vUsd(est.usd!).slice(1)}
      </span>
    );
  else if (est.kind === "range")
    big = (
      <span className="mk-estbig vd-estrange">
        <span className="vd-approx">約</span>
        {vUsd(est.low!)}–{vUsd(est.high!).slice(1)}
      </span>
    );
  else big = <span className="vd-estna">{est.kind === "token" ? "依實際用量" : "算不出來"}</span>;
  return (
    <div className="mk-submitwrap">
      {error ? (
        <div className="warn mk-err" role="alert">
          <b>送不出去</b>：{error}
        </div>
      ) : null}
      <div className="mk-submit">
        <div className="mk-recap">
          <Glyph kind="video" size="sm" />
          <b>生影片</b>
          <span className="code">{m?.id ?? "—"}</span>
          {spec ? <span>{spec}</span> : null}
          {pick.first ? <span>{pick.last ? "首幀＋尾幀" : "首幀"}</span> : pick.last ? <span>尾幀</span> : null}
        </div>
        <button type="button" className="stamp-btn mk-go" disabled={!built.req || busy} title={built.problem ?? "先出確認單，再按一次才送出"} onClick={onOpen}>
          <b>→</b>生影片
        </button>
        <div className="mk-est" aria-live="polite">
          <span className="k">
            預估
            <br />
            EST
          </span>
          {big}
          <span className="mk-basis">{est ? est.basis : estErr ? `預估問不到：${estErr}` : "正在算…"}</span>
        </div>
        <div className="mk-hint">
          {built.problem ?? (
            <>
              按了會先出一張確認單，再按一次才送出{touch ? "" : `（${sendKeyName(sendKey)} 也一樣）`}。要等 1～5 分鐘，送出後可以離開這頁。
            </>
          )}
        </div>
      </div>
    </div>
  );
}
