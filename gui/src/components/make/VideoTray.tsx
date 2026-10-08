/* 出件口的影片卡（v1.4）：等待票、「不等了…」的那一步、不等了之後、做好（當場播放）、失敗。
   每張不是「做好」的卡都有一行把錢講清楚（照後端的 charged）。
   沒有「再試一次」：影片一送就計費，重送一律「帶回表單」、再過一次確認單。 */
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { Generation, VideoKindExtras } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { dismissGeneration, recheckVideo, setDraft, stopWaitingVideo, useMake } from "@/store/make";
import { Glyph, errMsg } from "./bits";
import { draftFromGeneration, firstLine, mmss } from "./draft";
import { VideoPlayer } from "./VideoPlayer";
import { VIDEO_UI, estOf, moneyLine, specOf } from "./video";

const titleOf = (g: Generation): string => {
  const full = String(g.params?.prompt ?? "");
  const t = g.title || firstLine(full) || "影片";
  return g.title && full.length > g.title.length && full.startsWith(g.title) ? t + "…" : t;
};
const modelOf = (g: Generation): string => g.model ?? (typeof g.params?.model === "string" ? g.params.model : "預設模型");
const REMOTE_ZH: Record<string, string> = { pending: "排隊中", queued: "排隊中", in_progress: "製作中", processing: "製作中", running: "製作中", completed: "做好了", failed: "失敗" };

/** 失敗的一句人話（影片自己的；其他照出件口的對照表） */
function reasonOf(g: Generation, limits: VideoKindExtras["limits"] | null): string {
  const k = g.error_kind ?? (g.status === "interrupted" ? "interrupted" : "other");
  // 等了多久：照這一支實際等的（設定之後可能改過，不拿現在的上限講）
  const w = g.video?.waited_s ?? g.video?.max_wait_s ?? limits?.max_wait_s ?? 1200;
  const wait = w >= 60 ? `${Math.round(w / 60)} 分鐘` : `${Math.round(w)} 秒`;
  const google = g.video?.provider === "google";
  switch (k) {
    case "gave_up":
      return `等了 ${wait}還沒做好，先停下來不等`;
    case "download":
      return "供應商那邊做好了，但下載不下來";
    case "lost":
      if (google && !g.video?.remote_id) return "服務在 Google 做這支影片的時候停了：已送出、多半已計費，但影片拿不回來";
      return `${g.video?.resumed?.length ? "服務重啟後" : ""}接不回這支：供應商那邊查不到這個工作`;
    case "rejected":
      return "內容被供應商擋下：改寫提示詞再試";
    case "quota":
      return google ? "Google 不收：專案還沒開付費層，或額度用完" : "額度不夠：OpenRouter 的餘額不足，加值後再試";
    case "auth":
      return google ? "Google 不接受這次請求：檢查 Google 的 API key（錯誤原話在下面）" : "金鑰不被接受：檢查 OpenRouter 的 API key";
    case "timeout":
      return "供應商太久沒有回應";
    case "unavailable":
      return "這個模型現在叫不動（供應商暫時無法服務）";
    case "offline":
      return "離線模式不呼叫供應商";
    case "interrupted":
      return "服務重新啟動，這支沒有送出去";
    case "invalid":
      return "參數不被接受（見下面的原始訊息）";
    default:
      return "生成失敗（見下面的原始訊息）";
  }
}

/** 0–6 分鐘的刻度尺：虛線框標「通常 1～5 分鐘」，黑點是現在（不是進度條） */
function WaitSpan({ el }: { el: number }) {
  const MAX = VIDEO_UI.rulerMax;
  const [a, b] = VIDEO_UI.typicalWait;
  const pct = (s: number) => (Math.min(s, MAX) / MAX) * 100;
  const marks = [0, MAX / 3, (MAX * 2) / 3, MAX];
  const label = `通常 ${a / 60}～${b / 60} 分鐘`;
  return (
    <div className="mk-span" role="img" aria-label={`已等 ${mmss(el)}；${label}`}>
      <span className="lab band-lab" style={{ left: `${pct(a)}%` }}>
        {label}
      </span>
      <div className="band" style={{ left: `${pct(a)}%`, width: `${pct(b) - pct(a)}%` }} />
      <div className="base" style={{ backgroundSize: `calc(100% / ${MAX / 60}) 6px` }} />
      <div className="mk-now" style={{ left: `${pct(el)}%` }} />
      {marks.map((s, i) => (
        <span key={s} className={`lab${i === 0 ? " first" : i === marks.length - 1 ? " last" : ""}`} style={{ left: `${pct(s)}%` }}>
          {Math.round(s / 60)}:00
        </span>
      ))}
      {el > MAX ? <span className="lab over">超過 {MAX / 60} 分鐘</span> : null}
    </div>
  );
}

export function VideoOutCard({ g, now }: { g: Generation; now: number }) {
  const navigate = useNavigate();
  const limits = useMake((s) => s.options?.kinds.video?.limits ?? null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const v = g.video;
  const act = async (fn: (id: string) => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn(g.id);
      setAsking(false);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const restore = () => {
    setDraft("video", draftFromGeneration(g));
    navigate("/make/video");
    window.setTimeout(() => window.scrollTo({ top: 0 }), 0);
  };
  const est = estOf(g.estimate);
  const spec = specOf(g);
  const waiting = g.status === "running" || g.status === "detached";
  const waited = v?.submitted_at ? (waiting ? now - v.submitted_at : v.waited_s ?? 0) : null;
  const keep = limits?.keep_collecting ?? v?.keep_collecting ?? true;
  const pollEvery = limits?.poll_s?.length ? limits.poll_s[limits.poll_s.length - 1] : 30;
  const resumed = v?.resumed ?? [];
  const head = (stamp: ReactNode, extra?: ReactNode) => (
    <div className="mk-oi-h">
      {stamp}
      {extra}
      <Glyph kind="video" size="sm" />
      {g.demo === true ? <span className="sample mk-smp">示意</span> : null}
      <span className="mk-rid n">{dt(g.created_at)} 送出</span>
    </div>
  );
  const meta = (extra?: ReactNode) => (
    <div className="mk-oi-m">
      <span className="code">{modelOf(g)}</span>
      {spec ? <span>{spec}</span> : null}
      {extra}
    </div>
  );
  const title = <div className="mk-oi-t">{titleOf(g)}</div>;
  const close = (
    <button type="button" className="mk-mini mk-close" onClick={() => dismissGeneration(g.id)} title="只從出件口拿掉，不刪檔">
      收起
    </button>
  );
  const back = (
    <button type="button" className="mk-mini" onClick={restore} title="帶回表單，不會自動送出：再送一樣要過確認單">
      帶回表單
    </button>
  );
  const recheck = v?.can_recheck ? (
    <button type="button" className="mk-mini strong" onClick={() => void act(recheckVideo)} disabled={busy} title="用原本的任務編號再問一次：不重送、不多收">
      {busy ? "問…" : "再去問一次"}
    </button>
  ) : null;
  const errLine = err ? <div className="warn mk-carderr">{err}</div> : null;
  const money = (m = moneyLine(g, limits)) => (
    <div className={`vd-money${m.soft ? " soft" : ""}`}>
      <i>費</i>
      <span>{m.text}</span>
    </div>
  );
  const pst = v ? (
    <div className="vd-pst">
      <span>
        <span className={`vd-dot${v.remote_status === "pending" || v.remote_status === "queued" || !v.remote_status ? " vd-q" : ""}`} />
        供應商：<b>{v.remote_status ? REMOTE_ZH[v.remote_status] ?? v.remote_status : "送出中"}</b>
      </span>
      <span>
        每 {pollEvery} 秒問一次{v.polled_at ? <> · 上次 {Math.max(0, Math.round(now - v.polled_at))} 秒前</> : null}
      </span>
    </div>
  ) : null;
  const resumedTag = resumed.length ? <span className="vd-resumed">服務重啟過・已接回</span> : null;

  /* ---- 等待中 ---- */
  if (g.status === "running") {
    const el = waited ?? now - g.created_at;
    const before = resumed.length ? resumed[resumed.length - 1].after_s : null;
    return (
      <article className="mk-oi pending vd-wait" data-gen={g.id}>
        {head(<span className="live-stamp">生成中</span>, <span className="drum" aria-hidden="true" />)}
        <div className="mk-oi-b">
          {resumedTag}
          {title}
          {meta(est ? <span>預估 {est.text}</span> : null)}
          <div className="mk-el">
            <span className="k">
              已等
              <br />
              WAIT
            </span>
            <span className="mk-elbig">{mmss(el)}</span>
          </div>
          <WaitSpan el={el} />
          {pst}
          {g.video?.provider === "google" && !g.video?.remote_id ? (
            <div className="vd-resline">
              Google 做好才一次交回影片：這段時間服務若停掉，這支就<b>拿不回來</b>（已送出、多半已計費）。
            </div>
          ) : null}
          {before != null ? (
            <div className="vd-resline">
              重啟前已等 {mmss(before)}。送出時就記下了供應商那邊的工作，重啟後接著等，<b>不用重送、不會多收</b>。
            </div>
          ) : null}
          {asking ? (
            <div className="vd-stop" role="alertdialog" aria-label="不等了？">
              <b>不等了？先說清楚錢</b>
              供應商<u>沒有取消的功能</u>。按「不等了」只是 OmniAPI 不再等這支：供應商多半會照樣做完、<u>照樣收費{est ? `（${est.text}）` : ""}</u>，
              {keep ? "做好之後還是會收進作品牆，費用照實際記。" : "做好的影片也不會收進作品牆，費用頁記一筆「費用不明」。"}
              <div className="mk-acts">
                <button type="button" className="mk-mini" onClick={() => void act(stopWaitingVideo)} disabled={busy}>
                  {busy ? "…" : "不等了，錢照樣可能收"}
                </button>
                <button type="button" className="mk-mini ghost" onClick={() => setAsking(false)} disabled={busy}>
                  繼續等
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="mk-leave">
                <b>可以離開這頁，關掉瀏覽器也沒關係。</b>做好會留在這裡；頂欄「生成」旁的件數會跟著減一。
              </div>
              {v?.can_stop_waiting ? (
                <div className="mk-acts">
                  <button type="button" className="mk-mini" onClick={() => setAsking(true)}>
                    {VIDEO_UI.stopLabel}
                  </button>
                </div>
              ) : null}
            </>
          )}
          {errLine}
        </div>
      </article>
    );
  }

  /* ---- 不等了・背景還在收 ---- */
  if (g.status === "detached") {
    return (
      <article className="mk-oi small off vd-detached" data-gen={g.id}>
        {head(<span className="mk-offstamp">不等了</span>)}
        <div className="mk-oi-b">
          {title}
          {meta(waited != null ? <span>已送出 <span className="n">{mmss(waited)}</span></span> : null)}
          {money()}
          {pst}
          <div className="mk-acts">
            {back}
            {close}
          </div>
          {errLine}
        </div>
      </article>
    );
  }

  /* ---- 做好 ---- */
  if (g.status === "done") {
    const a = (g.artifacts ?? [])[0];
    const cost = g.cost_usd != null ? <>實際 <span className="n">{usd(g.cost_usd)}</span>{est ? `（預估${est.text}）` : ""}</> : "費用未回報";
    return (
      <article className="mk-oi done vd-done" data-gen={g.id}>
        {a && a.exists !== false ? (
          <VideoPlayer src={a.file_url} poster={a.thumb_url ? `${a.thumb_url}?w=960` : null} hasAudio={a.has_audio} width={a.width} height={a.height} durationHint={a.duration_s} posKey={a.id} label={titleOf(g)} />
        ) : null}
        {head(<span className="live-stamp done">完成</span>, v?.late ? <span className="vd-tag dash">不等了之後收回來的</span> : resumedTag)}
        <div className="mk-oi-b">
          {title}
          {meta(
            <>
              <span>{cost}</span>
              {v?.waited_s != null ? (
                <span>
                  耗時 <span className="n">{mmss(v.waited_s)}</span>
                </span>
              ) : null}
            </>,
          )}
          {!a ? <div className="dp-note">沒有留下檔案。</div> : a.exists === false ? <div className="dp-note">檔案已經不在硬碟上。</div> : null}
          <div className="mk-acts mk-acts-end">
            {a ? (
              <Link className="mk-mini strong" to={`/works/${a.id}`}>
                在作品牆看 →
              </Link>
            ) : null}
            {a && a.exists !== false ? (
              <a className="mk-mini" href={`${a.file_url}?download=true`} download>
                下載
              </a>
            ) : null}
            {back}
            {close}
          </div>
          {errLine}
        </div>
      </article>
    );
  }

  /* ---- 不等了・沒收回（設定關掉背景收） ---- */
  if (g.status === "abandoned") {
    return (
      <article className="mk-oi small off vd-abandoned" data-gen={g.id}>
        {head(<span className="mk-offstamp">不等了・沒收回</span>)}
        <div className="mk-oi-b">
          {title}
          {meta()}
          {money()}
          <div className="mk-acts">
            {recheck}
            {back}
            {close}
          </div>
          {errLine}
        </div>
      </article>
    );
  }

  /* ---- 失敗（含等太久、接不回、下載失敗） ---- */
  const longWait = g.status === "gave_up";
  return (
    <article className="mk-oi fail vd-fail" data-gen={g.id} data-kind={g.error_kind ?? g.status}>
      {head(<span className="errstamp">{longWait ? (g.error_kind === "download" ? "下載失敗" : "等太久") : "失敗"}</span>)}
      <div className="mk-oi-b">
        {title}
        {meta(waited != null && waited > 0 ? <span>等了 <span className="n">{mmss(waited)}</span></span> : null)}
        <div className="mk-reason">{reasonOf(g, limits)}</div>
        {g.error && !longWait ? <div className="mk-raw code">{g.error}</div> : null}
        {money()}
        <div className="mk-acts">
          {recheck}
          {back}
          {close}
        </div>
        {errLine}
      </div>
    </article>
  );
}
