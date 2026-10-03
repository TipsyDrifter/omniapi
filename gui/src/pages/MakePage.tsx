import { useEffect, useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import type { GenKind, GenRequest } from "@/api/types";
import { clearCarry, injectDemoFailures, loadGenerations, loadOptions, selectTrayIds, startGeneration, useMake } from "@/store/make";
import { Glyph, ImageForm, KINDS, KIND_META, MusicForm, SpeechForm, TranscriptForm, Tray, isGenKind, lastKind, rememberKind } from "@/components/make";
import { ApiError } from "@/api/client";

/* 生成 `/make/:kind`（1.1-M3，D32–D35）：不開 Claude Code 也能生圖、改圖、生語音、作曲、轉錄。
   沿用新對話的排法：主要輸入在左、模型與參數在中、出件口在右；出件口沒東西時收起，寬度還給左邊兩欄。
   `/make` 轉到上次用的那一種。 */
export default function MakePage() {
  const { kind } = useParams();
  if (!isGenKind(kind)) return <Navigate to={`/make/${lastKind()}`} replace />;
  return <Make kind={kind} />;
}

const SUBS: Record<GenKind, string> = {
  image: "送出後留在這頁，成品從右邊出件；放一張來源圖就變成改圖",
  speech: "念好從右邊出件，當場可播",
  music: "要等 1～3 分鐘；送出後可以離開，回來還在",
  transcript: "放一段音檔，逐字稿從右邊出件",
};

function Make({ kind }: { kind: GenKind }) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const opts = useMake((s) => s.options);
  const optsError = useMake((s) => s.optionsError);
  const trayIds = useMake(selectTrayIds);
  const carry = useMake((s) => s.carry);

  useEffect(() => {
    void loadOptions();
    void loadGenerations().catch(() => {});
  }, []);
  useEffect(() => rememberKind(kind), [kind]);

  // 開發／沙盒才有的示意開關：?demo=fail 塞幾張假的失敗卡（截圖、看排版用）
  const demo = params.get("demo");
  const demoOk = import.meta.env.DEV || opts?.sandbox === true;
  useEffect(() => {
    if (demo === "fail" && demoOk) injectDemoFailures();
  }, [demo, demoOk]);

  const [busy, setBusy] = useState<GenKind | null>(null);
  const [errors, setErrors] = useState<Partial<Record<GenKind, string>>>({});
  async function send(req: GenRequest) {
    if (busy) return;
    setBusy(req.kind);
    setErrors((o) => ({ ...o, [req.kind]: undefined }));
    try {
      await startGeneration(req);
      clearCarry(); // 送出了：「從作品牆帶入」那一行任務完成
    } catch (e) {
      const msg = e instanceof ApiError || e instanceof Error ? e.message : String(e);
      setErrors((o) => ({ ...o, [req.kind]: msg }));
    } finally {
      setBusy(null);
    }
  }

  const formProps = { opts, optsError, busy: busy === kind, error: errors[kind] ?? null, onSend: (r: GenRequest) => void send(r) };
  const open = trayIds.length > 0;

  return (
    <section className="wrap mk">
      <div className="sechead mk-head">
        <h2>
          <span className="ovp" data-t="Make">
            Make
          </span>
        </h2>
        <span className="zh">生成</span>
        <span className="dp-sub">{SUBS[kind]}</span>
        <div className="mk-tabs" role="tablist" aria-label="模態">
          {KINDS.map((k) => (
            <button key={k} type="button" role="tab" aria-selected={k === kind} onClick={() => navigate(`/make/${k}`)}>
              <Glyph kind={k} />
              <b>{KIND_META[k].zh}</b>
              <small>{KIND_META[k].en}</small>
            </button>
          ))}
        </div>
      </div>
      {opts?.sandbox ? (
        <div className="warn mk-sandbox">
          <b>離線沙盒</b>：不呼叫供應商、不花錢，做出來的是示範檔（圖是色塊、聲音是嗶聲）。
        </div>
      ) : opts?.offline ? (
        <div className="warn mk-sandbox">
          <b>離線模式</b>：這台 daemon 不呼叫供應商，送出會失敗。
        </div>
      ) : null}
      {carry && carry.kind === kind ? (
        <div className="mk-carry" role="status">
          {carry.thumb ? <img className="mk-carry-th" src={carry.thumb} alt="" /> : <Glyph kind={carry.artKind} size="lg" />}
          <span className="mk-carry-t">
            <span>
              <b>從作品牆帶入：</b>
              {carry.what}
              <span className="code">{carry.id}</span>
              <span className="mk-carry-why">還沒送出——看一眼預估費用再按。</span>
            </span>
            {carry.note ? <span className="mk-carry-note">{carry.note}</span> : null}
          </span>
          <span className="mk-carry-acts">
            <Link className="mk-mini" to={`/works/${carry.id}`}>
              回到這件作品
            </Link>
            <button type="button" className="mk-mini" onClick={clearCarry}>
              清掉這行
            </button>
          </span>
        </div>
      ) : null}

      <div className={`mk-grid${open ? " with-tray" : ""}`} data-kind={kind}>
        {kind === "image" ? <ImageForm {...formProps} /> : kind === "speech" ? <SpeechForm {...formProps} /> : kind === "music" ? <MusicForm {...formProps} /> : <TranscriptForm {...formProps} />}
        {open ? <Tray ids={trayIds} /> : null}
      </div>
    </section>
  );
}
