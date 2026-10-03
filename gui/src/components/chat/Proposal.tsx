/* 1.2-M4 聊天裡的生成：回覆裡的提議卡（版面稿 prototypes/chat-upgrade/m4-m5-design，情境 1–8）。
   卡是回覆的一部分：接在模型那段話後面、同一則回覆的虛線分隔之內，寬 700。狀態只用墨的語言分：
   提議中＝1.5px 墨框＋虛線「提議」章；生成中＝出件口的等待票（2px 墨框＋6px 實墨影＋滾筒）；
   做好＝作品標籤（A：圖左說明右）；失敗＝墨底反白章＋斜紋；已中止＝虛線框；不用了／略過＝收成一行。
   沒按「生成」就不花錢：只在預估旁邊講一次「按了才生成、才計費」。按下之後模型不會自動再回話。
   提議的原始值（模型擬的提示詞、推薦的模型）在 proposed；外層的 prompt／model 是最後一次實際用的。
   卡上的改動（提示詞、模型、聲音）與「重新打開」記在這個分頁（切換版本、捲走再回來都還在），重新整理就回到後端的樣子。
   1.2-M5 轉錄提問（kind: transcript、gate: true）是同一種卡，放在回覆的位置、回覆之前：
   「不轉錄，直接回覆」「→ 轉錄後回覆」；轉錄中＝等待票；轉好＝逐字稿卡，回覆自動接著寫進同一則（跟生成相反：主人的問題還等著答）。 */
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import type { ChatMessage, ChatProposal, GenModel, GenOptions, Generation, Voice } from "@/api/types";
import { dt, usd } from "@/lib/format";
import { acceptProposal, declineProposal } from "@/store/chat";
import { cancelGeneration, loadOptions, setDraft, useMake } from "@/store/make";
import { AudioPlayer, Glyph, basisText, errMsg, useEstimate, useNow } from "@/components/make/bits";
import { fmtBytes, mmss, msShort, priceText } from "@/components/make/draft";
import { ERROR_TEXT } from "@/components/make/Tray";
import { ImageViewer } from "@/components/works/Lightbox";
import CopyButton from "./CopyButton";

const KIND: Record<string, { verb: string; fld: string; en: string; usual: string; full: string }> = {
  image: { verb: "生一張圖", fld: "提示詞", en: "PROMPT", usual: "圖片通常 20～60 秒", full: "完整提示詞" },
  speech: { verb: "念成語音", fld: "要念的字", en: "TEXT", usual: "語音通常幾秒", full: "要念的字" },
};
const kindOf = (k: string) => KIND[k] ?? KIND.image;
/** 字數（不算空白；同生成頁按字計價的算法） */
const chars = (s: string) => [...s.replace(/\s/g, "")].length;
/** unix 秒 → HH:mm */
const hm = (s: number | null | undefined) => (s == null ? "—" : dt(s).slice(-5));
/** ERROR_TEXT 的前半句（「上次失敗：…」用） */
const shortErr = (kind: string | undefined) => (ERROR_TEXT[kind ?? "other"] ?? ERROR_TEXT.other).split("：")[0];

/* ---------------- 原始值與可選的模型 ---------------- */

/** 模型這一則擬的原始提議（後端的 proposed，按下生成之後也不變）；沒有就是外層的值 */
function originalOf(p: ChatProposal): { prompt: string; model: string | null } {
  const pr = p.proposed;
  if (!pr) return { prompt: p.prompt, model: p.model };
  return { prompt: pr.prompt?.trim() || p.prompt, model: pr.model?.trim() || null };
}

type GenKindKey = keyof GenOptions["kinds"];
/** 按下生成時後端會用的模型清單（同後端：現行、叫得動的） */
const usableModels = (opts: GenOptions | null, kind: string): GenModel[] =>
  (opts?.kinds[kind as GenKindKey]?.models ?? []).filter((m) => m.available && m.status === "current");
/** 想要的模型能用就用它，不然是這一類的預設（同後端 ProposeGeneration.request 的退回順序） */
function effective(opts: GenOptions | null, kind: string, want: string | null | undefined): string | null {
  const list = usableModels(opts, kind);
  if (want && (!opts || list.some((m) => m.id === want))) return want;
  return opts?.kinds[kind as GenKindKey]?.default_model ?? want ?? null;
}

/** 聲音編號 → 給人看的名字（ElevenLabs 的編號是一串亂碼，不上畫面） */
function voiceName(opts: GenOptions | null, id: unknown): string | null {
  if (typeof id !== "string" || !id) return null;
  for (const vs of Object.values(opts?.kinds.speech.voices ?? {})) {
    const v = vs.voices.find((x) => x.id === id);
    if (v) return v.name;
  }
  return /^[a-z][a-z0-9_-]{0,15}$/i.test(id) && !/\d{3,}/.test(id) ? id : null;
}

/* ---------------- 卡上的改動（這個分頁記著） ---------------- */
interface Local {
  prompt?: string;
  /** null＝照推薦的 */
  model?: string | null;
  voice?: string | null;
  /** reopen＝不用了／略過的重新打開；edit＝失敗或中止後改提示詞；fold＝中止後收起；peek＝一行打開看原提議 */
  mode?: "reopen" | "edit" | "fold" | "peek" | null;
  /** 第一次改之前的預估（舊值劃線留在數字旁邊） */
  firstEst?: number | null;
  /** 這些改動是在哪個狀態下做的：狀態變了（按下生成、別的分頁按了）就作廢 */
  at?: string;
}
const locals = new Map<string, Local>();
function useLocal(p: ChatProposal): [Local, (patch: Partial<Local>) => void] {
  const stamp = `${p.state}:${p.attempts ?? 0}`;
  const [, bump] = useState(0);
  let cur = locals.get(p.id);
  if (cur && cur.at !== stamp) {
    cur = { firstEst: cur.firstEst, at: stamp };
    locals.set(p.id, cur);
  }
  const set = (patch: Partial<Local>) => {
    locals.set(p.id, { ...(locals.get(p.id) ?? {}), ...patch, at: stamp });
    bump((n) => n + 1);
  };
  return [cur ?? { at: stamp }, set];
}

/* ---------------- 一則回覆的全部提議 ---------------- */
export interface ProposalsProps {
  msg: ChatMessage;
  /** 後面還有沒有訊息（略過的原因怎麼講） */
  hasNext: boolean;
  /** 這段聊天已封存：卡照樣看，但不能按 */
  archived?: boolean;
  /** gate＝只列轉錄提問（回覆之前）；其餘＝只列生成提議（回覆之後） */
  gate?: boolean;
}

export function Proposals({ msg, hasNext, archived, gate }: ProposalsProps) {
  const list = (msg.proposals ?? []).filter((p) => !!p.gate === !!gate);
  const opts = useMake((s) => s.options);
  useEffect(() => {
    if (list.length) void loadOptions();
  }, [list.length]);
  if (!list.length) return null;
  return (
    <div className={gate ? "gp-list gp-gate" : "gp-list"}>
      {list.map((p) =>
        p.kind === "transcript" ? (
          <Transcript key={p.id} msg={msg} p={p} opts={opts} hasNext={hasNext} archived={!!archived} />
        ) : (
          <Card key={p.id} msg={msg} p={p} opts={opts} hasNext={hasNext} archived={!!archived} />
        ),
      )}
    </div>
  );
}

interface CardProps {
  msg: ChatMessage;
  p: ChatProposal;
  opts: GenOptions | null;
  hasNext: boolean;
  archived: boolean;
}

function Card(props: CardProps) {
  const { p } = props;
  const [local, setLocal] = useLocal(p);
  const gen = useMake((s) => (p.generation_id ? s.gens[p.generation_id] : undefined));
  const mode = local.mode ?? null;
  if (p.state === "generating") return <RunCard {...props} gen={gen} />;
  if (p.state === "done") return <DoneCard {...props} gen={gen} />;
  const editing = p.state === "pending" || ((p.state === "declined" && mode === "reopen") || ((p.state === "failed" || p.state === "cancelled") && mode === "edit"));
  if (editing) return <PropCard {...props} local={local} setLocal={setLocal} />;
  if (p.state === "failed") return <FailCard {...props} gen={gen} setLocal={setLocal} />;
  if (p.state === "cancelled" && mode !== "fold") return <OffCard {...props} gen={gen} setLocal={setLocal} />;
  return <Line {...props} local={local} setLocal={setLocal} />;
}

/** 卡頭：模態章＋動詞＋（章）＋右邊小字 */
function Head({ p, stamp, aside, at }: { p: ChatProposal; stamp?: ReactNode; aside?: ReactNode; at?: ReactNode }) {
  const K = kindOf(p.kind);
  return (
    <div className="gp-h">
      {stamp}
      <Glyph kind={p.kind} size="sm" />
      <b>{K.verb}</b>
      {aside}
      {at}
    </div>
  );
}

/** 按「生成」：送卡上的值；回傳錯誤訊息（null＝成功） */
async function go(props: CardProps, body: { prompt: string; model: string | null; voice?: string | null }): Promise<string | null> {
  const { msg, p } = props;
  try {
    await acceptProposal(msg.conversation_id, msg.id, p.id, {
      prompt: body.prompt,
      ...(body.model ? { model: body.model } : {}),
      ...(p.kind === "speech" && body.voice ? { params: { voice: body.voice } } : {}),
    });
    return null;
  } catch (e) {
    return errMsg(e);
  }
}

const ARCHIVED = "這段聊天已封存，不能再生成";

/* ---------------- 提議中（含改過、重新打開、失敗後改提示詞） ---------------- */
function PropCard(props: CardProps & { local: Local; setLocal: (p: Partial<Local>) => void }) {
  const { msg, p, opts, local, setLocal, archived } = props;
  const navigate = useNavigate();
  const K = kindOf(p.kind);
  const orig = originalOf(p);
  const recommended = effective(opts, p.kind, orig.model);
  /** 起點：還沒按過＝原始提議；失敗、中止後改＝上次實際用的 */
  const basePrompt = p.state === "pending" ? orig.prompt : p.prompt;
  const prompt = local.prompt ?? basePrompt;
  const model = local.model ?? effective(opts, p.kind, p.state === "pending" ? orig.model : p.model);
  const modP = prompt.trim() !== orig.prompt.trim();
  const modM = !!model && !!recommended && model !== recommended;
  const recLabel = orig.model && recommended === orig.model ? `${msg.model ?? "聊天模型"} 推薦` : "預設";

  // 聲音（語音）：模型那家的聲音清單；提議帶的聲音能用就選它
  const mEntry = usableModels(opts, p.kind).find((m) => m.id === model) ?? null;
  const vs = p.kind === "speech" && mEntry ? opts?.kinds.speech.voices?.[mEntry.provider] ?? null : null;
  const okVoice = (v: Voice) => !v.only || !mEntry || v.only.includes(mEntry.id);
  const wantVoice = local.voice ?? p.voice ?? null;
  const voice = vs?.voices.find((v) => v.id === wantVoice && okVoice(v))?.id ?? vs?.default ?? wantVoice;

  const body = { kind: p.kind as "image" | "speech", params: { [p.kind === "speech" ? "text" : "prompt"]: prompt, ...(model ? { model } : {}), ...(p.kind === "speech" && voice ? { voice } : {}) } };
  const { est, err: estErr, pending: estPending } = useEstimate(body);
  const modified = modP || modM || (local.voice != null && local.voice !== p.voice);
  useEffect(() => {
    // 第一次改之前的預估：之後改了，舊值劃線留著對照
    if (est && est.usd != null && !modified && local.firstEst == null && !estPending) setLocal({ firstEst: est.usd });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [est, estPending, modified]);

  const [busy, setBusy] = useState<null | "go" | "no">(null);
  const [err, setErr] = useState<string | null>(null);
  const ta = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [prompt]);

  const onGo = async () => {
    if (busy || archived || !prompt.trim()) return;
    setBusy("go");
    setErr(null);
    const e = await go(props, { prompt: prompt.trim(), model, voice });
    if (e) setErr(`送不出去：${e}`);
    setBusy(null);
  };
  const onNo = async () => {
    if (busy || archived) return;
    // 重新打開的、失敗後改到一半的：收回去就好（後端那邊本來就是那個狀態）
    if (p.state !== "pending") {
      setLocal({ mode: null });
      return;
    }
    setBusy("no");
    setErr(null);
    try {
      await declineProposal(msg.conversation_id, msg.id, p.id);
    } catch (e) {
      setErr(`沒有成功：${errMsg(e)}`);
    }
    setBusy(null);
  };
  const toMake = () => {
    setDraft("image", { prompt, model, source: null });
    navigate("/make/image");
  };

  const curEst = est && est.usd != null ? est.usd : null;
  const was = modified && local.firstEst != null && curEst != null && Math.abs(local.firstEst - curEst) > 1e-9 ? local.firstEst : null;
  const noLabel = p.state === "pending" ? "不用了" : p.state === "declined" ? "收回" : "先不改";
  return (
    <section className="gp prop" aria-label={`提議：${K.verb}`} data-proposal={p.id} data-state={p.state}>
      <Head
        p={p}
        aside={
          <>
            <span className="gp-tag" title={p.note ?? undefined}>
              提議
            </span>
            <span className="gp-aside">{p.state === "declined" ? "重新打開的提議：按了生成才會做" : `可以先改${K.fld}或換模型`}</span>
          </>
        }
      />
      <div className="gp-b">
        {p.state === "failed" ? <div className="warn gp-was-note">上次失敗：{shortErr(p.error_kind)}。改完再按生成。</div> : null}
        {p.state === "cancelled" ? <div className="warn gp-was-note">上次中止了，沒有留下作品。改完再按生成。</div> : null}
        <div className="gp-row">
          <span className="gp-k">
            {K.fld}
            <small>{K.en}</small>
          </span>
          <div className="gp-v">
            <textarea
              ref={ta}
              className="gp-in"
              rows={1}
              aria-label={K.fld}
              value={prompt}
              disabled={archived}
              onChange={(e) => setLocal({ prompt: e.target.value })}
            />
            <div className="gp-sub">
              <span>
                <span className="n">{chars(prompt)}</span> 字
              </span>
              {modP ? (
                <>
                  <span className="gp-mod">你改過</span>
                  <button type="button" className="gp-undo" onClick={() => setLocal({ prompt: orig.prompt })}>
                    還原成模型擬的
                  </button>
                </>
              ) : null}
            </div>
          </div>
        </div>
        <div className="gp-row">
          <span className="gp-k">
            模型<small>MODEL</small>
          </span>
          <div className="gp-v">
            <ModelMenu
              opts={opts}
              kind={p.kind}
              model={model}
              recommended={recommended}
              recTag={recLabel}
              archived={archived}
              onPick={(id) => setLocal({ model: id })}
              aside={
                modM ? (
                  <>
                    <span className="gp-mod">你換過</span>
                    <button type="button" className="gp-undo" onClick={() => setLocal({ model: recommended })}>
                      還原成{recLabel === "預設" ? "預設的" : "推薦的"}
                    </button>
                  </>
                ) : recommended ? (
                  <span className="gp-rec" title={recLabel === "預設" ? "聊天模型沒有指定（或指定的現在不能用），用這一類的預設" : "聊天模型推薦的，預設就選它"}>
                    {recLabel === "預設" ? (
                      "預設"
                    ) : (
                      <>
                        <span className="code">{msg.model}</span> 推薦
                      </>
                    )}
                  </span>
                ) : null
              }
            />
          </div>
        </div>
        {p.kind === "speech" && vs && vs.voices.length ? (
          <div className="gp-row gp-voice">
            <span className="gp-k">
              聲音<small>VOICE</small>
            </span>
            <div className="gp-v">
              <div className="mk-seg" role="group" aria-label="聲音">
                {pickVoices(vs.voices.filter(okVoice), voice).map((v) => (
                  <button key={v.id} type="button" aria-pressed={v.id === voice} disabled={archived} onClick={() => setLocal({ voice: v.id })} title={v.note ?? undefined}>
                    {v.name}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : null}
        {p.kind === "image" ? (
          <div className="gp-row">
            <span className="gp-k">
              規格<small>SPEC</small>
            </span>
            <div className="gp-v">
              <span className="gp-spec">
                <span>照模型的預設規格 · 1 張</span>
                <button type="button" className="gp-undo" onClick={toMake} title="帶著這張提議（提示詞、模型）到生成頁，在那邊調比例、張數再送出">
                  更多設定到生成頁 →
                </button>
              </span>
            </div>
          </div>
        ) : null}
        <div className="gp-foot">
          <div className={`gp-est${modified ? " flash" : ""}`} key={modified ? JSON.stringify(body) : "first"} aria-live="polite">
            <span className="k">
              預估
              <br />
              EST
            </span>
            {curEst != null ? (
              <span className="gp-estn">
                <small>$</small>
                {curEst.toFixed(4)}
              </span>
            ) : (
              <span className="gp-estna">{est ? "估不出" : estErr ? "—" : "…"}</span>
            )}
            {was != null ? (
              <s className="gp-was" title="改之前的預估">
                ${was.toFixed(4)}
              </s>
            ) : null}
            <span className="gp-basis">
              {est ? basisText(est) : estErr ? `預估問不到：${estErr}` : "正在算…"}
              <br />
              <b>按了才生成、才計費</b>
            </span>
          </div>
          <button type="button" className="cs-act" onClick={() => void onNo()} disabled={!!busy || archived}>
            {busy === "no" ? "…" : noLabel}
          </button>
          <Gated off={archived ? ARCHIVED : !prompt.trim() ? `${K.fld}是空的` : null}>
            <button type="button" className="stamp-btn gp-go" onClick={() => void onGo()} disabled={!!busy} aria-disabled={archived || !prompt.trim() ? true : undefined}>
              {busy === "go" ? (
                "送出中…"
              ) : (
                <>
                  <b>→</b>生成
                </>
              )}
            </button>
          </Gated>
        </div>
        {err ? (
          <div className="warn gp-err" role="alert">
            {err}
          </div>
        ) : null}
      </div>
    </section>
  );
}

/** 聲音太多時只列幾個（選中的一定在） */
function pickVoices(list: Voice[], sel: string | null | undefined): Voice[] {
  const head = list.slice(0, 5);
  const chosen = list.find((v) => v.id === sel);
  return chosen && !head.includes(chosen) ? [...head.slice(0, 4), chosen] : head;
}

/** 卡上的模型鈕：按一下往下長一個清單（同生成頁的 mk-mrow）；aside＝鈕旁的「推薦」章或「你換過」 */
function ModelMenu({ opts, kind, model, recommended, recTag, archived, onPick, aside }: {
  opts: GenOptions | null;
  kind: string;
  model: string | null;
  recommended: string | null;
  recTag: string;
  archived: boolean;
  onPick: (id: string) => void;
  aside: ReactNode;
}) {
  const [pop, setPop] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!pop) return;
    const down = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setPop(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setPop(false);
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", esc);
    };
  }, [pop]);
  const list = usableModels(opts, kind);
  const entry = list.find((m) => m.id === model) ?? null;
  const price = entry ? priceText(entry) : null;
  return (
    <div className="gp-mwrap" ref={ref}>
      <button type="button" className="gp-mbtn" aria-expanded={pop} aria-haspopup="listbox" onClick={() => setPop((o) => !o)} disabled={archived}>
        <span className="code">{model ?? "預設模型"}</span>
        {entry?.name ? <span className="gp-mname">{entry.name}</span> : null}
        <span className={price ? "gp-mprice n" : "gp-mname"}>{price ?? "未定價"}</span>
        <span className="cs-car" aria-hidden="true">
          {pop ? "▴" : "▾"}
        </span>
      </button>
      {aside}
      {pop ? (
        <div className="gp-mpop" role="dialog" aria-label={kind === "transcript" ? "換轉錄模型" : "換生成模型"}>
          <div className="dp-fh">
            <span className="lbl">Model</span>
            <span className="zh">換模型</span>
            <span className="aside">{list.length} 個可用 · 價格取自模型目錄</span>
          </div>
          <div className="gp-mlist" role="listbox" aria-label={kind === "transcript" ? "轉錄模型" : "生成模型"}>
            {list.map((m) => {
              const pt = priceText(m);
              return (
                <button
                  key={m.id}
                  type="button"
                  role="option"
                  className="mk-mrow"
                  aria-selected={m.id === model}
                  onClick={() => {
                    onPick(m.id);
                    setPop(false);
                  }}
                >
                  <span className="code">{m.id}</span>
                  {m.id === recommended ? <span className="dp-tag">{recTag}</span> : <span />}
                  <span className={pt ? "mk-price n" : "mk-price none"}>{pt ?? "未定價"}</span>
                  <span className="mk-prov">
                    {m.provider}
                    {m.name ? ` · ${m.name}` : ""}
                  </span>
                </button>
              );
            })}
            {!opts ? <div className="dp-empty">讀取模型清單…</div> : null}
          </div>
          <p className="cs-note">換了模型，預估跟著重算；{kind === "transcript" ? "轉錄預設" : "推薦"}的那個一直標在清單裡，隨時換回來。</p>
        </div>
      ) : null}
    </div>
  );
}

/** 不能按的鈕：滑過或聚焦出原因（同 1.2-M3 的停用樣式） */
function Gated({ off, children }: { off: string | null; children: ReactNode }) {
  const tip = useId();
  if (!off) return <>{children}</>;
  return (
    <span className="tipw r" aria-describedby={tip}>
      {children}
      <span className="cx-tip" id={tip} role="tooltip">
        {off}
      </span>
    </span>
  );
}

/* ---------------- 生成中：出件口的等待票 ---------------- */
function RunCard({ p, gen, opts }: CardProps & { gen?: Generation }) {
  const K = kindOf(p.kind);
  const now = useNow(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const stop = async () => {
    if (!p.generation_id) return;
    setBusy(true);
    setErr(null);
    try {
      await cancelGeneration(p.generation_id);
    } catch (e) {
      setErr(`中止不了：${errMsg(e)}`);
      setBusy(false);
    }
  };
  const est = gen?.estimate?.usd;
  return (
    <section className="gp run" aria-busy="true" data-proposal={p.id} data-state={p.state}>
      <Head p={p} stamp={<span className="live-stamp">生成中</span>} aside={<span className="drum" aria-hidden="true" />} at={<span className="mk-rid">{hm(gen?.created_at)} 按下</span>} />
      <div className="gp-b">
        <div className="gp-t">{p.prompt}</div>
        <div className="mk-oi-m">
          <span className="code">{p.model ?? "預設模型"}</span>
          {p.kind === "speech" && voiceName(opts, gen?.params?.voice) ? <span>聲音 {voiceName(opts, gen?.params?.voice)}</span> : null}
          {est != null ? (
            <span>
              預估 <span className="n">{usd(est)}</span>
            </span>
          ) : null}
        </div>
        <div className="gp-wait">
          <span className="k">
            已等
            <br />
            WAIT
          </span>
          <span className="gp-elv">{gen ? mmss(now - gen.created_at) : "—"}</span>
          <span className="gp-usual">{K.usual}</span>
          <button type="button" className="mk-mini" onClick={() => void stop()} disabled={busy || !p.generation_id}>
            {busy ? "中止中…" : "中止"}
          </button>
        </div>
        {err ? <div className="warn gp-err">{err}</div> : null}
        <div className="gp-leave">
          <b>可以繼續聊。</b>做好會出現在這裡，也會進作品牆；送出下一則不會打斷它。
        </div>
      </div>
    </section>
  );
}

/* ---------------- 做好了：作品標籤（A 版面：圖左說明右） ---------------- */
function DoneCard({ msg, p, gen, opts }: CardProps & { gen?: Generation }) {
  const navigate = useNavigate();
  const K = kindOf(p.kind);
  const art = p.artifact;
  const orig = originalOf(p);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(false);
  const used = typeof gen?.params?.model === "string" ? gen.params.model : p.model;
  const modP = p.prompt.trim() !== orig.prompt.trim();
  const modM = !!orig.model && !!used && used !== orig.model;
  const modTxt = [modP ? (p.kind === "speech" ? "要念的字你改過" : "提示詞你改過") : "", modM ? "模型你換過" : ""].filter(Boolean).join("、");
  const took = gen?.finished_at ? gen.finished_at - gen.created_at : null;
  const full = gen?.artifacts?.find((a) => a.id === art?.id);
  const toEdit = () => {
    if (!art) return;
    setDraft("image", { source: { ref: { artifact_id: art.id }, name: art.name ?? art.id, url: art.thumb_url ? `${art.thumb_url}?w=480` : art.file_url, from: "image", bytes: full?.bytes ?? null, mime: full?.mime ?? null } });
    navigate("/make/image");
  };
  const head = (
    <div className="gp-dh">
      <span className="live-stamp done">完成</span>
      <Glyph kind={p.kind} size="sm" />
      <span className="gp-by">
        <span className="code">{msg.model ?? "聊天模型"}</span> 提議，你在 {hm(gen?.created_at)} 按了生成
      </span>
      <span className="mk-rid">{hm(gen?.finished_at)}</span>
    </div>
  );
  const meta = (
    <div className="mk-oi-m">
      <span className="code">{gen?.model ?? p.model ?? "—"}</span>
      {p.kind === "speech" && voiceName(opts, gen?.params?.voice) ? <span>聲音 {voiceName(opts, gen?.params?.voice)}</span> : null}
      <span>{gen?.cost_usd != null ? <>實際 <span className="n">{usd(gen.cost_usd)}</span></> : "費用未回報"}</span>
      {took != null ? (
        <span>
          耗時 <span className="n">{mmss(took)}</span>
        </span>
      ) : null}
      {modTxt ? <span>{modTxt}</span> : null}
    </div>
  );
  const promptToggle = (
    <>
      <button type="button" className="gp-prt" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {K.full} {open ? "▾" : "▸"}
      </button>
      {open ? (
        <div className="gp-prbox">
          {p.prompt}
          {modP ? (
            <small>
              模型擬的是：{orig.prompt}
            </small>
          ) : null}
          {modM ? (
            <small>
              <span className="code">{msg.model}</span> 推薦的是 <span className="code">{orig.model}</span>，你換成 <span className="code">{used}</span>
            </small>
          ) : null}
        </div>
      ) : null}
    </>
  );
  const acts = art ? (
    <div className="mk-acts mk-acts-end">
      <Link className="mk-mini strong" to={`/works/${art.id}`} title="作品牆上這件的來源標「聊天」">
        在作品牆看 →
      </Link>
      <a className="mk-mini" href={`${art.file_url}?download=true`} download>
        下載
      </a>
      {art.kind === "image" ? (
        <button type="button" className="mk-mini" onClick={toEdit} title="帶到生成頁的改圖">
          拿去改圖
        </button>
      ) : null}
    </div>
  ) : (
    <div className="dp-note">沒有留下檔案。</div>
  );
  if (art?.kind === "image") {
    return (
      <section className="gp done" data-proposal={p.id} data-state={p.state}>
        <button type="button" className="gp-fig" onClick={() => setView(true)} aria-label="看大圖">
          <img src={art.thumb_url ? `${art.thumb_url}?w=576` : art.file_url} alt={p.prompt} loading="lazy" />
        </button>
        <div className="gp-info">
          {head}
          <div className="gp-t">{p.prompt}</div>
          {meta}
          {promptToggle}
          <span className="gp-spacer" />
          {acts}
        </div>
        {view ? (
          <ImageViewer images={[{ file_url: art.file_url, src: art.thumb_url ? `${art.thumb_url}?w=1600` : art.file_url, name: art.name, artifact_id: art.id }]} start={0} onClose={() => setView(false)} />
        ) : null}
      </section>
    );
  }
  return (
    <section className="gp done au" data-proposal={p.id} data-state={p.state}>
      <div className="gp-info">
        {head}
        <div className="gp-t">{p.prompt}</div>
        {meta}
        {art ? <AudioPlayer src={art.file_url} /> : null}
        {promptToggle}
        {acts}
      </div>
    </section>
  );
}

/* ---------------- 失敗：墨底反白章＋斜紋 ---------------- */
function FailCard(props: CardProps & { gen?: Generation; setLocal: (p: Partial<Local>) => void }) {
  const { p, gen, setLocal, archived } = props;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const no = useDecline(props, setErr);
  const kind = p.error_kind ?? "other";
  const waited = gen?.finished_at ? gen.finished_at - gen.created_at : null;
  const retry = async () => {
    if (busy || archived) return;
    setBusy(true);
    setErr(null);
    const e = await go(props, { prompt: p.prompt, model: p.model, voice: typeof gen?.params?.voice === "string" ? gen.params.voice : p.voice });
    if (e) setErr(`送不出去：${e}`);
    setBusy(false);
  };
  return (
    <section className="gp fail" data-proposal={p.id} data-state={p.state}>
      <Head p={p} stamp={<span className="errstamp">失敗</span>} at={<span className="mk-rid">{hm(gen?.created_at)} 按下</span>} />
      <div className="gp-b">
        <div className="gp-t">{p.prompt}</div>
        <div className="mk-oi-m">
          <span className="code">{p.model ?? "預設模型"}</span>
          <span>{gen?.cost_usd != null ? <>實際 <span className="n">{usd(gen.cost_usd)}</span></> : "費用未回報"}</span>
          {waited != null ? (
            <span>
              等了 <span className="n">{mmss(waited)}</span>
            </span>
          ) : null}
        </div>
      </div>
      <div className="gp-fb">
        <div className="mk-reason">{ERROR_TEXT[kind] ?? ERROR_TEXT.other}</div>
        {p.error ? <div className="mk-raw code">{p.error}</div> : null}
        <div className="mk-acts">
          <Gated off={archived ? ARCHIVED : null}>
            <button type="button" className="mk-mini strong" onClick={() => void retry()} disabled={busy} aria-disabled={archived || undefined}>
              {busy ? "送出中…" : "再試一次"}
            </button>
          </Gated>
          <button type="button" className="mk-mini" onClick={() => setLocal({ mode: "edit" })} disabled={archived}>
            改提示詞
          </button>
          <button type="button" className="mk-mini" onClick={() => void no.run()} disabled={archived || no.busy} title="收成一行；之後還能重新打開">
            {no.busy ? "…" : "不用了"}
          </button>
        </div>
        {err ? <div className="warn gp-err">{err}</div> : null}
      </div>
    </section>
  );
}

/** 失敗、中止後按「不用了」（後端收成 declined，模型下一輪知道主人不要了） */
function useDecline(props: CardProps, setErr: (e: string | null) => void) {
  const { msg, p, archived } = props;
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (busy || archived) return;
    setBusy(true);
    setErr(null);
    try {
      await declineProposal(msg.conversation_id, msg.id, p.id);
    } catch (e) {
      setErr(`沒有成功：${errMsg(e)}`);
    }
    setBusy(false);
  };
  return { busy, run };
}

/* ---------------- 已中止：虛線框＋虛線章 ---------------- */
function OffCard(props: CardProps & { gen?: Generation; setLocal: (p: Partial<Local>) => void }) {
  const { p, gen, setLocal, archived } = props;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const no = useDecline(props, setErr);
  const at = gen?.finished_at ? gen.finished_at - gen.created_at : null;
  const again = async () => {
    if (busy || archived) return;
    setBusy(true);
    setErr(null);
    const e = await go(props, { prompt: p.prompt, model: p.model, voice: typeof gen?.params?.voice === "string" ? gen.params.voice : p.voice });
    if (e) setErr(`送不出去：${e}`);
    setBusy(false);
  };
  return (
    <section className="gp off" data-proposal={p.id} data-state={p.state}>
      <Head p={p} stamp={<span className="mk-offstamp">已中止</span>} at={<span className="mk-rid">{hm(gen?.created_at)} 按下</span>} />
      <div className="gp-b">
        <div className="gp-t">{p.prompt}</div>
        <div className="mk-oi-m">
          <span className="code">{p.model ?? "預設模型"}</span>
          {at != null ? (
            <span>
              你在 <span className="n">{mmss(at)}</span> 中止
            </span>
          ) : null}
          <span>沒有留下作品</span>
        </div>
        <div className="mk-acts">
          <Gated off={archived ? ARCHIVED : null}>
            <button type="button" className="mk-mini strong" onClick={() => void again()} disabled={busy} aria-disabled={archived || undefined}>
              {busy ? "送出中…" : "再生成一次"}
            </button>
          </Gated>
          <button type="button" className="mk-mini" onClick={() => setLocal({ mode: "edit" })} disabled={archived}>
            改提示詞
          </button>
          <button type="button" className="mk-mini" onClick={() => void no.run()} disabled={archived || no.busy} title="收成一行；之後還能重新打開">
            {no.busy ? "…" : "不用了"}
          </button>
          <button type="button" className="mk-mini" onClick={() => setLocal({ mode: "fold" })}>
            收起
          </button>
        </div>
        {err ? <div className="warn gp-err">{err}</div> : null}
      </div>
    </section>
  );
}

/* ---------------- 不用了／略過（與中止後收起）：收成一行，可以打開看原提議、重新打開 ---------------- */
function Line(props: CardProps & { local: Local; setLocal: (p: Partial<Local>) => void }) {
  const { msg, p, opts, hasNext, local, setLocal, archived } = props;
  const peek = local.mode === "peek";
  const auto = p.state === "declined" && !!p.auto;
  const k = p.state === "cancelled" ? "已中止" : auto ? "略過" : "不用了";
  const why =
    p.state === "cancelled"
      ? "中止後收起來了，沒有留下作品。"
      : auto
        ? hasNext
          ? "你直接送了下一則，這張提議就略過了，沒有生成。"
          : "你重新生成或改了前面的訊息，這張提議就略過了，沒有生成。"
        : "你按的，沒有生成。";
  return (
    <>
      <div className="gp-line" data-proposal={p.id} data-state={p.state}>
        <Glyph kind={p.kind} size="sm" />
        <span className={`gp-lk${auto ? " auto" : ""}`}>{k}</span>
        <span className="gp-lt">{why}</span>
        <span className="gp-lp">{p.prompt}</span>
        <button type="button" className="gp-undo" aria-expanded={peek} onClick={() => setLocal({ mode: peek ? null : "peek" })}>
          {peek ? "收起" : "看提議"}
        </button>
      </div>
      {peek ? <Peek msg={msg} p={p} opts={opts} archived={archived} onReopen={() => setLocal({ mode: p.state === "cancelled" ? "edit" : "reopen" })} /> : null}
    </>
  );
}

function Peek({ msg, p, opts, archived, onReopen }: { msg: ChatMessage; p: ChatProposal; opts: GenOptions | null; archived: boolean; onReopen: () => void }) {
  const model = effective(opts, p.kind, p.model);
  const m = usableModels(opts, p.kind).find((x) => x.id === model);
  const pt = m ? priceText(m) : null;
  const { est } = useEstimate({ kind: p.kind as "image" | "speech", params: { [p.kind === "speech" ? "text" : "prompt"]: p.prompt, ...(model ? { model } : {}) } });
  return (
    <div className="gp-peek">
      <div className="gp-prbox">
        {p.prompt}
        <small>
          <span className="code">{model ?? "預設模型"}</span>
          {pt ? ` · ${pt}` : ""}
          {est && est.usd != null ? ` · 預估 $${est.usd.toFixed(4)}` : ""}
          {msg.model ? ` · ${msg.model} 提議` : ""}
        </small>
      </div>
      <div className="mk-acts">
        <Gated off={archived ? ARCHIVED : null}>
          <button type="button" className="mk-mini strong" onClick={() => !archived && onReopen()} aria-disabled={archived || undefined}>
            重新打開提議
          </button>
        </Gated>
        <span className="dp-note">打開後還是要再按一次「生成」才會做。</span>
      </div>
    </div>
  );
}

/* ================ 1.2-M5 轉錄提問：回覆前先問主人（同款提議卡，模態章「字」） ================ */

const ARCHIVED_T = "這段聊天已封存，不能再轉錄";

/** 錄音：檔名（長度） */
const audioTitle = (p: ChatProposal) => {
  const d = p.duration_s ?? p.file?.duration_s;
  return `${p.file?.name ?? "錄音"}${d ? `（${msShort(d)}）` : ""}`;
};

function Transcript(props: CardProps) {
  const { p } = props;
  const gen = useMake((s) => (p.generation_id ? s.gens[p.generation_id] : undefined));
  if (p.state === "generating") return <TRun {...props} gen={gen} />;
  if (p.state === "done") return <TDone {...props} gen={gen} />;
  if (p.state === "failed" || p.state === "cancelled") return <TOff {...props} gen={gen} />;
  if (p.state === "declined") return <TLine {...props} />;
  return <TAsk {...props} />;
}

/** 按「轉錄後回覆」／「不轉錄，直接回覆」：回傳錯誤訊息（null＝成功） */
async function decide(props: CardProps, yes: boolean, model?: string | null): Promise<string | null> {
  const { msg, p } = props;
  try {
    if (yes) await acceptProposal(msg.conversation_id, msg.id, p.id, model ? { model } : {});
    else await declineProposal(msg.conversation_id, msg.id, p.id);
    return null;
  } catch (e) {
    return errMsg(e);
  }
}

/* ---------------- 提問中：錄音、轉錄模型、預估，「不轉錄，直接回覆」「→ 轉錄後回覆」 ---------------- */
function TAsk(props: CardProps) {
  const { msg, p, opts, archived } = props;
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "go" | "no">(null);
  const [err, setErr] = useState<string | null>(null);
  const recommended = effective(opts, "transcript", p.model);
  const model = pick ?? recommended;
  const dur = p.duration_s ?? p.file?.duration_s ?? undefined;
  const { est, err: estErr } = useEstimate({ kind: "transcript", params: model ? { model } : {}, ...(dur ? { duration_s: dur } : {}) });
  const curEst = est && est.usd != null ? est.usd : null;
  const run = async (yes: boolean) => {
    if (busy || archived) return;
    setBusy(yes ? "go" : "no");
    setErr(null);
    const e = await decide(props, yes, yes && model !== p.model ? model : null);
    if (e) setErr(`沒有成功：${e}`);
    setBusy(null);
  };
  const facts = ["音檔", dur ? msShort(dur) : null, p.file?.bytes != null ? fmtBytes(p.file.bytes) : null].filter(Boolean).join(" · ");
  return (
    <section className="gp prop" aria-label="提議：先把錄音轉成文字" data-proposal={p.id} data-state={p.state}>
      <div className="gp-h">
        <Glyph kind="transcript" size="sm" />
        <b>先把錄音轉成文字？</b>
        <span className="gp-tag">提議</span>
        <span className="gp-aside">
          <span className="code">{msg.model ?? "這個模型"}</span> 聽不到音檔，轉成文字它才知道錄音裡講了什麼
        </span>
      </div>
      <div className="gp-b">
        <div className="gp-row">
          <span className="gp-k">
            錄音<small>AUDIO</small>
          </span>
          <div className="gp-v">
            <span className="gp-spec">
              <span className="code">{p.file?.name ?? "錄音"}</span>
              <span>{facts}</span>
            </span>
          </div>
        </div>
        <div className="gp-row">
          <span className="gp-k">
            模型<small>MODEL</small>
          </span>
          <div className="gp-v">
            <ModelMenu
              opts={opts}
              kind="transcript"
              model={model}
              recommended={recommended}
              recTag="轉錄預設"
              archived={archived}
              onPick={setPick}
              aside={
                pick && pick !== recommended ? (
                  <>
                    <span className="gp-mod">你換過</span>
                    <button type="button" className="gp-undo" onClick={() => setPick(null)}>
                      還原成預設的
                    </button>
                  </>
                ) : recommended ? (
                  <span className="gp-rec" title="生成頁「轉錄」的預設模型">
                    轉錄預設
                  </span>
                ) : null
              }
            />
          </div>
        </div>
        <div className="gp-foot">
          <div className="gp-est" aria-live="polite">
            <span className="k">
              預估
              <br />
              EST
            </span>
            {curEst != null ? (
              <span className="gp-estn">
                <small>$</small>
                {curEst.toFixed(4)}
              </span>
            ) : (
              <span className="gp-estna">{est ? "估不出" : estErr ? "—" : "…"}</span>
            )}
            <span className="gp-basis">
              {est ? basisText(est) : estErr ? `預估問不到：${estErr}` : "正在算…"}
              <br />
              <b>按了才轉錄、才計費</b>
            </span>
          </div>
          <Gated off={archived ? ARCHIVED_T : null}>
            <button type="button" className="cs-act" onClick={() => void run(false)} disabled={!!busy} aria-disabled={archived || undefined} title="模型只會知道有這段錄音，聽不到內容">
              {busy === "no" ? "…" : "不轉錄，直接回覆"}
            </button>
          </Gated>
          <Gated off={archived ? ARCHIVED_T : null}>
            <button type="button" className="stamp-btn gp-go" onClick={() => void run(true)} disabled={!!busy} aria-disabled={archived || undefined}>
              {busy === "go" ? (
                "送出中…"
              ) : (
                <>
                  <b>→</b>轉錄後回覆
                </>
              )}
            </button>
          </Gated>
        </div>
        {err ? (
          <div className="warn gp-err" role="alert">
            {err}
          </div>
        ) : null}
      </div>
    </section>
  );
}

/* ---------------- 轉錄中：等待票 ---------------- */
function TRun({ msg, p, gen }: CardProps & { gen?: Generation }) {
  const waiting = msg.meta?.state === "awaiting";
  const now = useNow(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const stop = async () => {
    if (!p.generation_id) return;
    setBusy(true);
    setErr(null);
    try {
      await cancelGeneration(p.generation_id);
    } catch (e) {
      setErr(`中止不了：${errMsg(e)}`);
      setBusy(false);
    }
  };
  const est = gen?.estimate?.usd;
  return (
    <section className="gp run" aria-busy="true" data-proposal={p.id} data-state={p.state}>
      <div className="gp-h">
        <span className="live-stamp">轉錄中</span>
        <span className="drum" aria-hidden="true" />
        <Glyph kind="transcript" size="sm" />
        <b>把錄音轉成文字</b>
        <span className="mk-rid">{hm(gen?.created_at)} 按下</span>
      </div>
      <div className="gp-b">
        <div className="gp-t">{audioTitle(p)}</div>
        <div className="mk-oi-m">
          <span className="code">{p.model ?? "預設模型"}</span>
          {est != null ? (
            <span>
              預估 <span className="n">{usd(est)}</span>
            </span>
          ) : null}
        </div>
        <div className="gp-wait">
          <span className="k">
            已等
            <br />
            WAIT
          </span>
          <span className="gp-elv">{gen ? mmss(now - gen.created_at) : "—"}</span>
          <span className="gp-usual">錄音越長等越久</span>
          <button type="button" className="mk-mini" onClick={() => void stop()} disabled={busy || !p.generation_id}>
            {busy ? "中止中…" : "中止"}
          </button>
        </div>
        {err ? <div className="warn gp-err">{err}</div> : null}
        <div className="gp-leave">
          {waiting ? (
            <>
              <b>轉好就接著回覆。</b>逐字稿也會進作品牆。
            </>
          ) : (
            <>
              <b>你送了下一則，這則不再回覆。</b>逐字稿照樣做完、進作品牆。
            </>
          )}
        </div>
      </div>
    </section>
  );
}

/* ---------------- 轉好了：逐字稿卡（回覆接在後面） ---------------- */
function TDone({ p, gen }: CardProps & { gen?: Generation }) {
  const art = p.artifact;
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    if (!art?.id) return;
    let alive = true;
    api
      .artifact(art.id)
      .then((a) => alive && setText(a.text ?? ""))
      .catch(() => alive && setText(null));
    return () => {
      alive = false;
    };
  }, [art?.id]);
  const took = gen?.finished_at ? gen.finished_at - gen.created_at : null;
  const name = (p.file?.name ?? "錄音").replace(/\.[^.]+$/, "");
  const chars = art?.chars ?? text?.length ?? null;
  return (
    <section className="gp done tx" data-proposal={p.id} data-state={p.state}>
      <div className="gp-info">
        <div className="gp-dh">
          <span className="live-stamp done">完成</span>
          <Glyph kind="transcript" size="sm" />
          <span className="gp-by">回覆前先轉錄，你在 {hm(gen?.created_at)} 按下</span>
          <span className="mk-rid">{hm(gen?.finished_at)}</span>
        </div>
        <div className="gp-t">{name} 逐字稿</div>
        <div className="mk-oi-m">
          <span className="code">{gen?.model ?? p.model ?? "—"}</span>
          {chars != null ? (
            <span>
              <span className="n">{chars.toLocaleString("en-US")}</span> 字
            </span>
          ) : null}
          <span>{gen?.cost_usd != null ? <>實際 <span className="n">{usd(gen.cost_usd)}</span></> : "費用未回報"}</span>
          {took != null ? (
            <span>
              耗時 <span className="n">{mmss(took)}</span>
            </span>
          ) : null}
        </div>
        {text ? <div className="mk-text">{text}</div> : null}
        {art ? (
          <div className="mk-acts mk-acts-end">
            <Link className="mk-mini strong" to={`/works/${art.id}`} title="作品牆上這份逐字稿的來源標「聊天」">
              在作品牆看 →
            </Link>
            {text ? <CopyButton text={text} className="mk-mini" label="複製全文" /> : null}
            <a className="mk-mini" href={`${art.file_url}?download=true`} download>
              下載
            </a>
          </div>
        ) : (
          <div className="dp-note">沒有留下檔案。</div>
        )}
      </div>
    </section>
  );
}

/* ---------------- 轉錄失敗、中止：再試一次，或改成直接回覆 ---------------- */
function TOff(props: CardProps & { gen?: Generation }) {
  const { msg, p, gen, archived } = props;
  const [busy, setBusy] = useState<null | "go" | "no">(null);
  const [err, setErr] = useState<string | null>(null);
  const failed = p.state === "failed";
  // 主人已經送了下一則（這則略過了）：不能再決定
  const waiting = msg.meta?.state === "awaiting";
  const kind = p.error_kind ?? "other";
  const run = async (yes: boolean) => {
    if (busy || archived) return;
    setBusy(yes ? "go" : "no");
    setErr(null);
    const e = await decide(props, yes, yes ? p.model : null);
    if (e) setErr(`沒有成功：${e}`);
    setBusy(null);
  };
  const acts = !waiting ? null : (
    <div className="mk-acts">
      <Gated off={archived ? ARCHIVED_T : null}>
        <button type="button" className="mk-mini strong" onClick={() => void run(true)} disabled={!!busy} aria-disabled={archived || undefined}>
          {busy === "go" ? "送出中…" : failed ? "再試一次" : "再轉錄一次"}
        </button>
      </Gated>
      <Gated off={archived ? ARCHIVED_T : null}>
        <button type="button" className="mk-mini" onClick={() => void run(false)} disabled={!!busy} aria-disabled={archived || undefined} title="模型只會知道有這段錄音，聽不到內容">
          {busy === "no" ? "…" : "不轉錄，直接回覆"}
        </button>
      </Gated>
    </div>
  );
  const head = (
    <div className="gp-h">
      {failed ? <span className="errstamp">失敗</span> : <span className="mk-offstamp">已中止</span>}
      <Glyph kind="transcript" size="sm" />
      <b>把錄音轉成文字</b>
      <span className="mk-rid">{hm(gen?.created_at)} 按下</span>
    </div>
  );
  const meta = (
    <div className="mk-oi-m">
      <span className="code">{p.model ?? "預設模型"}</span>
      {failed ? <span>{gen?.cost_usd != null ? <>實際 <span className="n">{usd(gen.cost_usd)}</span></> : "費用未回報"}</span> : <span>沒有留下逐字稿</span>}
      <span>{waiting ? "回覆還在等你決定" : "你送了下一則，這則沒有回覆"}</span>
    </div>
  );
  if (failed)
    return (
      <section className="gp fail" data-proposal={p.id} data-state={p.state}>
        {head}
        <div className="gp-b">
          <div className="gp-t">{audioTitle(p)}</div>
          {meta}
        </div>
        <div className="gp-fb">
          <div className="mk-reason">{ERROR_TEXT[kind] ?? ERROR_TEXT.other}</div>
          {p.error ? <div className="mk-raw code">{p.error}</div> : null}
          {acts}
          {err ? <div className="warn gp-err">{err}</div> : null}
        </div>
      </section>
    );
  return (
    <section className="gp off" data-proposal={p.id} data-state={p.state}>
      {head}
      <div className="gp-b">
        <div className="gp-t">{audioTitle(p)}</div>
        {meta}
        {acts}
        {err ? <div className="warn gp-err">{err}</div> : null}
      </div>
    </section>
  );
}

/* ---------------- 不轉錄（你選的）／略過（送了下一則）：收成一行 ---------------- */
function TLine({ p }: CardProps) {
  const auto = !!p.auto;
  return (
    <div className="gp-line" data-proposal={p.id} data-state={p.state}>
      <Glyph kind="transcript" size="sm" />
      <span className={`gp-lk${auto ? " auto" : ""}`}>{auto ? "略過" : "不用了"}</span>
      <span className="gp-lt">{auto ? "你直接送了下一則，沒有轉錄，這則也就沒有回覆。" : "沒有轉錄：你選了直接回覆，模型只知道有這段錄音。"}</span>
      <span className="gp-lp">{p.file?.name ?? ""}</span>
    </div>
  );
}
