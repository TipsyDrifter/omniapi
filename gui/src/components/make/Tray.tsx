/* 出件口（生成頁右欄）：最近的生成工作，跨四種模態，新到舊。
   進行中＝紙底墨框＋滾筒＋已等 mm:ss（音樂另配 0–4 分鐘刻度尺）；做好＝成品＋動作；失敗＝墨底反白章＋斜紋＋人話＋原始訊息。 */
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { Artifact, Generation } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { mediaOf } from "@/lib/modalities";
import { cancelGeneration, dismissGeneration, isRunning, setDraft, startGeneration, useMake } from "@/store/make";
import { VideoOutCard } from "./VideoTray";
import { KIND_META, draftFromGeneration, firstLine, mmss, retryRequest } from "./draft";
import { AudioPlayer, Glyph, artName, errMsg, useNow } from "./bits";

/** error_kind → 一句人話 */
export const ERROR_TEXT: Record<string, string> = {
  quota: "額度不夠或被限流：到供應商後台確認帳單與額度",
  auth: "金鑰不被接受：檢查這個供應商的 API key",
  rejected: "內容被供應商擋下：改寫提示詞再試",
  timeout: "等太久沒有回應：再試一次，或換一個模型",
  too_large: "檔案太大：換小一點的檔",
  unavailable: "這個模型現在叫不動（沒設定金鑰，或供應商暫時無法服務）",
  offline: "離線模式不呼叫供應商",
  interrupted: "服務重新啟動，這一件被中斷了",
  invalid: "參數不被接受（見下面的原始訊息）",
  other: "生成失敗（見下面的原始訊息）",
};

export function Tray({ ids }: { ids: string[] }) {
  const gens = useMake((s) => ids.map((id) => s.gens[id]));
  const anyRunning = gens.some(isRunning);
  const now = useNow(anyRunning);
  return (
    <aside className="mk-tray" aria-label="出件">
      <div className="mk-tray-h">
        <span className="lbl">OUT</span>
        <b>出件</b>
        <span className="mk-tray-n">
          最近 <span className="n">{gens.length}</span> 件
        </span>
      </div>
      {gens.map((g) => (g ? g.kind === "video" ? <VideoOutCard key={g.id} g={g} now={now} /> : <OutCard key={g.id} g={g} now={now} /> : null))}
    </aside>
  );
}

function titleOf(g: Generation): string {
  const full = String(g.params?.prompt ?? g.params?.text ?? "");
  const t = g.title || firstLine(full) || KIND_META[g.kind]?.zh || g.kind;
  // 後端的標題截在 60 字：原文比它長就補「…」（標題是摘要，不是代號，可以截）
  return g.title && full.length > g.title.length && full.startsWith(g.title) ? t + "…" : t;
}
const modelOf = (g: Generation): string => g.model ?? (typeof g.params?.model === "string" ? g.params.model : "預設模型");

function OutCard({ g, now }: { g: Generation; now: number }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const demo = g.demo === true;

  const restore = () => {
    setDraft(g.kind, draftFromGeneration(g));
    navigate(`/make/${g.kind}`);
  };
  const retry = async () => {
    setBusy(true);
    setErr(null);
    try {
      await startGeneration(retryRequest(g));
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    setBusy(true);
    setErr(null);
    try {
      await cancelGeneration(g.id);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const toEdit = (a: Artifact) => {
    setDraft("image", { source: { ref: { artifact_id: a.id }, name: artName(a), url: a.thumb_url ? `${a.thumb_url}?w=480` : a.file_url, from: "image", bytes: a.bytes, mime: a.mime } });
    navigate("/make/image");
  };
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (e) {
      setErr(`複製不了：${errMsg(e)}`);
    }
  };

  const head = (stamp: ReactNode, extra?: ReactNode) => (
    <div className="mk-oi-h">
      {stamp}
      {extra}
      <Glyph kind={g.kind} size="sm" />
      {demo ? <span className="sample mk-smp">示意</span> : null}
      <span className="mk-rid n">{dt(g.created_at)} 送出</span>
    </div>
  );
  const close = (
    <button type="button" className="mk-mini mk-close" onClick={() => dismissGeneration(g.id)} title="只從出件口拿掉，不刪檔">
      收起
    </button>
  );
  const meta = (
    <div className="mk-oi-m">
      <span className="code">{modelOf(g)}</span>
      {g.kind === "image" ? <span>{g.tool === "edit_image" || g.sources?.images?.length ? "改圖" : "生圖"}</span> : null}
      {g.status === "done" ? <span>{g.cost_usd != null ? <>實際 <span className="n">{usd(g.cost_usd)}</span></> : "費用未回報"}</span> : null}
      {g.status === "done" && g.finished_at ? (
        <span>
          耗時 <span className="n">{mmss(g.finished_at - g.created_at)}</span>
        </span>
      ) : null}
    </div>
  );
  const errLine = err ? <div className="warn mk-carderr">{err}</div> : null;

  if (g.status === "running") {
    const el = now - g.created_at;
    return (
      <article className="mk-oi pending">
        {head(<span className="live-stamp">生成中</span>, <span className="drum" aria-hidden="true" />)}
        <div className="mk-oi-b">
          <div className="mk-oi-t">{titleOf(g)}</div>
          {meta}
          <div className="mk-el">
            <span className="k">
              已等
              <br />
              WAIT
            </span>
            <span className="mk-elbig">{mmss(el)}</span>
          </div>
          {g.kind === "music" ? (
            <>
              <WaitSpan el={el} />
              <div className="mk-leave">
                <b>可以離開這頁。</b>做好會留在這裡；頂欄「生成」旁的件數會跟著減一。
              </div>
            </>
          ) : null}
          <div className="mk-acts">
            <button type="button" className="mk-mini" onClick={cancel} disabled={busy}>
              {busy ? "中止中…" : "中止"}
            </button>
          </div>
          {errLine}
        </div>
      </article>
    );
  }

  if (g.status === "cancelled") {
    return (
      <article className="mk-oi small off">
        {head(<span className="mk-offstamp">已中止</span>)}
        <div className="mk-oi-b">
          <div className="mk-oi-t">{titleOf(g)}</div>
          {meta}
          <div className="mk-acts">
            <button type="button" className="mk-mini" onClick={restore}>
              帶回表單
            </button>
            {close}
          </div>
          {errLine}
        </div>
      </article>
    );
  }

  if (g.status !== "done") {
    const kind = g.status === "interrupted" ? "interrupted" : g.error_kind ?? "other";
    return (
      <article className="mk-oi fail">
        {head(<span className="errstamp">失敗</span>)}
        <div className="mk-oi-b">
          <div className="mk-oi-t">{titleOf(g)}</div>
          {meta}
          <div className="mk-reason">{ERROR_TEXT[kind] ?? ERROR_TEXT.other}</div>
          {g.error ? <div className="mk-raw code">{g.error}</div> : null}
          <div className="mk-acts">
            <button type="button" className="mk-mini strong" onClick={retry} disabled={busy}>
              {busy ? "送出中…" : "再試一次"}
            </button>
            <button type="button" className="mk-mini" onClick={restore}>
              帶回表單
            </button>
            {close}
          </div>
          {errLine}
        </div>
      </article>
    );
  }

  const arts = g.artifacts ?? [];
  // 依作品是什麼分三格；不認得的種類（mediaOf 會警告）放進文字那一格保底，不再整件消失
  const imgs = arts.filter((a) => mediaOf(a.kind) === "image");
  const auds = arts.filter((a) => mediaOf(a.kind) === "audio");
  const texts = arts.filter((a) => { const m = mediaOf(a.kind); return m === "text" || m === null; });
  return (
    <article className="mk-oi done">
      {imgs.length ? (
        <div className={`mk-imgs n${Math.min(imgs.length, 2)}`}>
          {imgs.map((a) => (
            <figure key={a.id}>
              <a href={a.file_url} target="_blank" rel="noreferrer" title="開原圖（新分頁）">
                <img src={a.thumb_url ? `${a.thumb_url}?w=${imgs.length > 1 ? 480 : 960}` : a.file_url} alt={a.prompt ?? ""} loading="lazy" />
              </a>
              <figcaption>
                <button type="button" className="mk-mini strong" onClick={() => toEdit(a)}>
                  拿去改圖
                </button>
                <a className="mk-mini" href={`${a.file_url}?download=true`} download>
                  下載
                </a>
              </figcaption>
            </figure>
          ))}
        </div>
      ) : null}
      {head(<span className="live-stamp done">完成</span>)}
      <div className="mk-oi-b">
        <div className="mk-oi-t">{titleOf(g)}</div>
        {meta}
        {auds.map((a) => (
          <div key={a.id} className="mk-audbox">
            <AudioPlayer src={a.file_url} />
            <div className="mk-acts">
              <a className="mk-mini" href={`${a.file_url}?download=true`} download>
                下載
              </a>
            </div>
          </div>
        ))}
        {texts.map((a) => (
          <div key={a.id} className="mk-textbox">
            <div className="mk-text">{a.text ?? ""}</div>
            <div className="mk-acts">
              <button type="button" className="mk-mini strong" onClick={() => copy(a.text ?? "")}>
                {copied ? "已複製" : "複製"}
              </button>
              <a className="mk-mini" href={`${a.file_url}?download=true`} download>
                下載
              </a>
            </div>
          </div>
        ))}
        {!arts.length ? <div className="dp-note">沒有留下檔案。</div> : null}
        <div className="mk-acts mk-acts-end">
          {arts.length ? (
            <Link className="mk-mini strong" to={`/works/${arts[0].id}`} title={arts.length > 1 ? `共 ${arts.length} 件，先看第一件` : undefined}>
              在作品牆看 →
            </Link>
          ) : null}
          <button type="button" className="mk-mini" onClick={restore}>
            帶回表單
          </button>
          {close}
        </div>
        {errLine}
      </div>
    </article>
  );
}

/** 0–4 分鐘的刻度尺：虛線框標「通常 1～3 分鐘」，黑點是現在（不是進度條） */
function WaitSpan({ el }: { el: number }) {
  const MAX = 240;
  const pct = (s: number) => (Math.min(s, MAX) / MAX) * 100;
  return (
    <div className="mk-span" role="img" aria-label={`已等 ${mmss(el)}；通常 1～3 分鐘`}>
      <span className="lab band-lab" style={{ left: `${pct(60)}%` }}>
        通常 1～3 分鐘
      </span>
      <div className="band" style={{ left: `${pct(60)}%`, width: `${pct(180) - pct(60)}%` }} />
      <div className="base" />
      <div className="mk-now" style={{ left: `${pct(el)}%` }} />
      {[0, 60, 120, 180, 240].map((s, i, a) => (
        <span key={s} className={`lab${i === 0 ? " first" : i === a.length - 1 ? " last" : ""}`} style={{ left: `${pct(s)}%` }}>
          {Math.floor(s / 60)}:00
        </span>
      ))}
      {el > MAX ? <span className="lab over">超過 4 分鐘</span> : null}
    </div>
  );
}
