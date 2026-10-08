/* 影片送出前的確認單（v1.4）：一張打孔的票。寬版直接蓋在送出列的位置；手機從底部升起（Sheet）。
   三種：一般、偏貴（超過 VIDEO_UI.highUsd：斜紋票頭＋大金額＋省錢的換法）、算不出金額（按 token 計價等）。
   「確定送出」是影片唯一的送出路徑。 */
import { useEffect, useRef, type ReactNode } from "react";
import type { GenModel, VideoKindExtras } from "@/api/types";
import { isSendKey } from "@/lib/sendKey";
import { Glyph } from "./bits";
import type { VideoDraft } from "./draft";
import { VIDEO_UI, cheaperOptions, vparams, vUsd, type VideoEst, type VideoPick } from "./video";

const aud = (m: GenModel, pick: VideoPick) => {
  const a = vparams(m).audio;
  return a === true ? (pick.audio === false ? "無聲" : "有聲") : a === false ? "無聲（這個模型不做聲音）" : "名單沒標有沒有聲音";
};

export function ConfirmTicket({
  m,
  pick,
  est,
  pending,
  busy,
  error,
  limits,
  onConfirm,
  onBack,
  onApply,
}: {
  m: GenModel;
  pick: VideoPick;
  est: VideoEst | null;
  pending: boolean;
  busy: boolean;
  error: string | null;
  limits: VideoKindExtras["limits"] | null;
  onConfirm: () => void;
  onBack: () => void;
  onApply: (patch: Partial<VideoDraft>) => void;
}) {
  const high = !!est && est.top != null && est.top > VIDEO_UI.highUsd;
  const token = !!est && (est.kind === "token" || est.kind === "none");
  const go = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const ready = !!est && !pending && !busy;
  // 開單時焦點放在單子本身（不放在「確定送出」上：按住 Enter 的連發不會直接按到它）
  useEffect(() => {
    card.current?.focus({ preventScroll: true });
  }, []);
  // 鍵盤：Ctrl+Enter（與設定成 Enter 送出時的 Enter）＝確定送出；Esc＝回去改
  useEffect(() => {
    // 開單的那一下（表單裡的 Ctrl+Enter）還在往上傳：React 會在同一次事件裡就掛上這個監聽，
    // 那一下傳到 window 時不能再被當成「確定送出」——只認單子開了之後才發生的按鍵
    const armedAt = performance.now();
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat || e.timeStamp <= armedAt) return;
      if (e.key === "Escape") {
        e.preventDefault();
        onBack();
        return;
      }
      const t = e.target as HTMLElement | null;
      const inField = !!t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT" || t.isContentEditable);
      const onButton = !!t && t.tagName === "BUTTON" && t !== go.current;
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey || (!inField && !onButton && isSendKey(e)))) {
        e.preventDefault();
        if (ready) onConfirm();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBack, onConfirm, ready]);

  const frames = pick.first ? (pick.last ? "首幀＋尾幀" : "首幀（圖生影片）") : pick.last ? "尾幀" : "沒有（文生影片）";
  const spec = [pick.duration != null ? `${pick.duration} 秒` : null, pick.resolution, pick.ratio, aud(m, pick)].filter(Boolean).join(" · ");
  const alts = high && est?.usd != null ? cheaperOptions(m, pick, est.usd) : [];
  const waitMin = Math.round((limits?.max_wait_s ?? 1200) / 60);

  let sum: ReactNode;
  if (!est) sum = <div className="vd-ok-sum"><div className="vd-ok-calc">{pending ? "正在算…" : "預估問不到"}</div><div className="vd-ok-big word">…</div></div>;
  else if (est.kind === "exact" || est.kind === "range")
    sum = (
      <div className="vd-ok-sum">
        <div className="vd-ok-calc">{est.basis}</div>
        <div className="vd-ok-big">
          <span className="vd-approx">約</span>
          {est.kind === "exact" ? (
            <>
              <small>$</small>
              {vUsd(est.usd!).slice(1)}
            </>
          ) : (
            <span className="vd-range">
              {vUsd(est.low!)}–{vUsd(est.high!)}
            </span>
          )}
        </div>
      </div>
    );
  else
    sum = (
      <div className="vd-ok-sum">
        <div className="vd-ok-calc">{est.basis}</div>
        <div className="vd-ok-big word">{est.kind === "token" ? "依實際用量" : "算不出來"}</div>
      </div>
    );
  const money = !est ? "…" : est.kind === "exact" ? `約 ${vUsd(est.usd!)}` : est.kind === "range" ? `約 ${vUsd(est.low!)}–${vUsd(est.high!)}` : est.kind === "token" ? "依實際用量" : "金額算不出來";

  return (
    <div ref={card} tabIndex={-1} className={`vd-ok${high ? " high" : ""}${token ? " token" : ""}`} role="alertdialog" aria-label="送出前確認" aria-describedby="vd-ok-facts">
      <div className="vd-ok-h">
        <Glyph kind="video" />
        <b>{high ? "這支偏貴，再確認一次" : "送出前確認"}</b>
        <span className="lbl x-s">CONFIRM</span>
        {high ? <span className="vd-tag solid">超過 {vUsd(VIDEO_UI.highUsd)}</span> : token ? <span className="vd-tag dash">金額算不出來</span> : null}
      </div>
      <dl className="vd-ok-l">
        <dt>模型</dt>
        <dd>
          <span className="code">{m.id}</span>
        </dd>
        <dt>影片</dt>
        <dd>{spec}</dd>
        <dt>首尾幀</dt>
        <dd>{frames}</dd>
      </dl>
      <div className="vd-perf" aria-hidden="true" />
      {sum}
      {alts.length ? (
        <div className="vd-alt">
          <span>想省一點：</span>
          {alts.map((a) => (
            <button key={a.label} type="button" className="mk-mini" onClick={() => onApply(a.patch)}>
              {a.label}
              <span className="n">約 {vUsd(a.usd)}</span>
            </button>
          ))}
        </div>
      ) : null}
      <ul className="vd-ok-facts" id="vd-ok-facts">
        <li>
          <span>
            按下去就<b>開始計費</b>。
          </span>
        </li>
        <li>
          <span>
            通常要等 <b>1～5 分鐘</b>（最多等 {waitMin} 分鐘）；可以離開這頁，做好會留在出件口。
          </span>
        </li>
        <li>
          <span>
            供應商<b>沒有「取消」</b>：中途不等了，這支多半照樣收費。
          </span>
        </li>
      </ul>
      {error ? (
        <div className="warn mk-err vd-ok-err" role="alert">
          <b>送不出去</b>：{error}
        </div>
      ) : null}
      <div className="vd-ok-acts">
        <button ref={go} type="button" className="stamp-btn vd-ok-go" disabled={!ready} onClick={onConfirm}>
          {busy ? (
            "送出中…"
          ) : (
            <>
              <b>→</b>確定送出・{money}
            </>
          )}
        </button>
        <button type="button" className="mk-mini" onClick={onBack}>
          回去改
        </button>
        <span className="dp-note x-touch">Esc 也是回去改</span>
      </div>
    </div>
  );
}
