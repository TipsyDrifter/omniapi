import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { Slot, TierMap } from "@/api/types";
import { SLOTS } from "@/api/types";
import { GetKeyHint, KeyInput, KeyMetaLine, TestBox, type KeyInputHandle, type KeyMeta, type TestState } from "@/components/settings/KeyParts";
import ToolsList, { claudeLoggedIn } from "@/components/settings/ToolsList";
import { allModels, isModality, isRetired, MOD, MOD_ORDER, providerOfSlot, slotOf, TIER_NAMES, type Modality } from "@/lib/catalog";
import { useTier } from "@/lib/rwd";
import { loadModels, loadSettings, loadTools, saveKey, saveSettings, testKey, useSettings } from "@/store/settings";

/* 首次啟動引導 /welcome（1.3-M4，D50：獨立的一整頁，不是蓋在看板上的面板）。
   四步：選一家 → 貼上 key → 測試 → 完成。頂欄只留 wordmark＋「略過，先看看板 →」。
   什麼時候出現：七家都沒有 key、這個瀏覽器沒有略過或走完過，打開看板就導過來（App.tsx）；設定頁有入口可以再進。
   完成頁：這家有哪些模態可用；內建的等級別名指到還沒有 key 的供應商時，列對照表、問要不要一鍵改成這一家的（建議值用 API 的 suggested_tiers）。 */

export const WELCOME_KEY = "omniapi.welcome";
/** 略過或走完：記在這個瀏覽器，之後不再自動帶過來 */
export function markWelcome(v: "skipped" | "done"): void {
  try {
    localStorage.setItem(WELCOME_KEY, v);
  } catch {
    /* 無痕模式：這次照樣離開 */
  }
}

/** 每家一句話（事實，不是推薦排名）；徽章同 */
const PITCH: Record<Slot, string> = {
  openai: "文字、生圖、語音、轉錄都有。一把 key 能用到的功能最多。",
  anthropic: "Claude 系列的文字模型，走 API 計費。只想用訂閱派工給 Claude Code 的話，不用這把。",
  gemini: "文字、生圖、語音、音樂。",
  deepseek: "便宜的文字模型。內建的 cheap 等級就是它。",
  openrouter: "一把 key 接很多家的文字模型，名單是現查的。",
  elevenlabs: "語音與音樂。沒有文字模型。",
  kie: "Suno 音樂。沒有文字模型。",
};
const BADGE: Partial<Record<Slot, string>> = { openai: "功能最多", deepseek: "內建 cheap" };
const STEPS: [string, string][] = [
  ["選一家", "PROVIDER"],
  ["貼上 key", "PASTE"],
  ["測試", "TEST"],
  ["完成", "DONE"],
];

function Wg({ m }: { m: Modality }) {
  return (
    <i className={`mk-wg ${MOD[m].k} sm`} title={MOD[m].zh}>
      {MOD[m].g}
    </i>
  );
}

export default function WelcomePage() {
  const navigate = useNavigate();
  const settings = useSettings((s) => s.settings);
  const models = useSettings((s) => s.models);
  const tools = useSettings((s) => s.tools);
  const narrow = useTier() === "s";
  const [step, setStep] = useState(1);
  const [slot, setSlot] = useState<Slot | null>(null);
  const [meta, setMeta] = useState<KeyMeta | null>(null);
  /** 第 3 步顯示的末四碼（輸入框已清掉） */
  const [last4, setLast4] = useState("");
  const [t, setT] = useState<TestState>({ st: "untested" });
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [fixed, setFixed] = useState<TierMap | null>(null);
  const [fixBusy, setFixBusy] = useState(false);
  const inp = useRef<KeyInputHandle>(null);
  /** 第 2 步貼的 key：第 3 步看不到輸入框，只在這裡等到存起來；存好或離開就清掉 */
  const keyRef = useRef<string>("");

  useEffect(() => {
    document.title = "第一次使用 · OmniAPI";
    void loadModels();
    void loadTools();
    return () => {
      keyRef.current = "";
      document.title = "OmniAPI 派工中心";
    };
  }, []);
  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [step]);

  const list = useMemo(() => allModels(models), [models]);
  const L = slot ? settings?.providers[slot]?.label ?? slot : "";
  const modsOf = (s: Slot): Modality[] => ((models?.providers?.[providerOfSlot(s, settings)]?.modalities as string[] | undefined) ?? []).filter(isModality);
  const countOf = (s: Slot, mod?: Modality) => list.filter((m) => slotOf(m.provider, models) === s && !isRetired(m) && m.status !== "discovered" && (!mod || m.modality === mod)).length;

  const skip = () => {
    markWelcome("skipped");
    keyRef.current = "";
    navigate("/");
  };
  const goTest = async () => {
    const v = inp.current?.value() ?? "";
    if (!v || !slot) return;
    keyRef.current = v;
    setLast4(v.slice(-4));
    inp.current?.clear();
    setStep(3);
    setT({ st: "testing" });
    try {
      setT({ st: "done", res: await testKey(slot, v) });
    } catch {
      setT({ st: "done", res: { provider: slot, key: { set: true, last4: v.slice(-4) }, ok: false, reason: "network", message: "", checked_at: Date.now() / 1000 } });
    }
  };
  const save = async () => {
    if (!slot || !keyRef.current) return;
    setSaveErr(null);
    const ok = await saveKey(slot, keyRef.current, { label: L, replaced: !!settings?.providers[slot]?.key.set });
    if (!ok) {
      setSaveErr("沒存成：服務沒回應或拒絕了。可以再按一次。");
      return;
    }
    keyRef.current = "";
    markWelcome("done");
    await loadSettings();
    setStep(4);
  };
  const retype = () => {
    keyRef.current = "";
    setMeta(null);
    setT({ st: "untested" });
    setStep(2);
  };
  const again = () => {
    keyRef.current = "";
    setSlot(null);
    setMeta(null);
    setFixed(null);
    setT({ st: "untested" });
    setStep(1);
  };

  let main: ReactNode = null;
  if (step === 1) {
    main = (
      <>
        <h2 className="wl-q">先接上一家模型供應商</h2>
        <p className="wl-qs">OmniAPI 自己不提供模型，它幫你把工作派給各家的模型。挑一家你已經有帳號的，貼上它的 API key 就能開始。之後隨時能在「設定」加別家。</p>
        <div className="pc-grid" role="radiogroup" aria-label="供應商">
          {SLOTS.map((s) => (
            <button key={s} type="button" className="pc" role="radio" aria-checked={slot === s} aria-pressed={slot === s} onClick={() => setSlot(s)} data-slot={s}>
              <span className="h">
                <b>{settings?.providers[s]?.label ?? s}</b>
                {BADGE[s] ? <span className="sample">{BADGE[s]}</span> : null}
              </span>
              <p>{PITCH[s]}</p>
              <span className="ft">
                {modsOf(s).map((m) => (
                  <Wg key={m} m={m} />
                ))}
                <span className="n">{s === "openrouter" ? "模型現查" : models ? `目錄 ${countOf(s)} 個` : ""}</span>
              </span>
            </button>
          ))}
        </div>
        <div className="wl-nav">
          <button type="button" className="lnk" onClick={skip}>
            略過，先看看板
          </button>
          <span className="sp" />
          <button type="button" className="stamp-btn" aria-disabled={!slot} onClick={() => slot && setStep(2)}>
            下一步：貼 key →
          </button>
        </div>
      </>
    );
  } else if ((step === 2 || step === 3) && slot) {
    const p = settings?.providers[slot];
    const done = t.st === "done" ? t.res : null;
    let nav: ReactNode;
    if (step === 2)
      nav = (
        <>
          <button type="button" className="lnk" onClick={() => setStep(1)}>
            ← 換一家
          </button>
          <span className="sp" />
          <button type="button" className="stamp-btn" aria-disabled={!meta} onClick={() => void goTest()}>
            測試 →
          </button>
        </>
      );
    else if (t.st === "testing")
      nav = (
        <>
          <span className="x-dim" style={{ fontSize: "var(--fs-s)" }}>
            測試中，通常幾秒…
          </span>
          <span className="sp" />
          <button type="button" className="stamp-btn" aria-disabled="true">
            存起來 →
          </button>
        </>
      );
    else if (done && done.ok === false)
      nav = (
        <>
          <button type="button" className="lnk" onClick={() => void save()}>
            照樣存，之後再說
          </button>
          <span className="sp" />
          <button type="button" className="stamp-btn" onClick={retype}>
            ← 改貼一次
          </button>
        </>
      );
    else
      nav = (
        <>
          <button type="button" className="lnk" onClick={retype}>
            ← 改貼一次
          </button>
          <span className="sp" />
          <button type="button" className="stamp-btn" onClick={() => void save()}>
            存起來，下一步 →
          </button>
        </>
      );
    main = (
      <>
        <h2 className="wl-q">{step === 2 ? `貼上 ${L} 的 API key` : `測試 ${L} 的 key`}</h2>
        <p className="wl-qs">{step === 2 ? "key 只存在這台電腦，畫面上之後只會顯示末四碼。" : "測試只請對方列出模型清單，不叫模型、不花錢。"}</p>
        <div className="wl-box">
          <div className="pvh">
            <b>{L}</b>
            {modsOf(slot).map((m) => (
              <Wg key={m} m={m} />
            ))}
            {step === 2 ? (
              <button type="button" className="lnk" onClick={() => setStep(1)}>
                換一家
              </button>
            ) : null}
          </div>
          {step === 2 ? (
            <>
              <ol className="howto">
                <li>
                  <GetKeyHint gk={p?.get_key} />
                  {p?.get_key?.url ? "（要先登入）。" : "，登入後台找建立 API key 的地方。"}
                </li>
                <li>建一把新的 key，複製起來。</li>
                <li>貼到下面。</li>
              </ol>
              <KeyInput ref={inp} label={`${L} API key`} onMeta={setMeta} />
              <KeyMetaLine meta={meta} />
            </>
          ) : (
            <>
              <div className="kin-meta">
                <span>要測的 key</span>
                <span className="kmask">
                  <i>••••</i>
                  {last4}
                </span>
              </div>
              <TestBox t={t} label={L} />
              {saveErr ? (
                <div className="warn" role="alert">
                  <b>沒存成</b>：{saveErr}
                </div>
              ) : null}
            </>
          )}
        </div>
        <div className="wl-nav">{nav}</div>
      </>
    );
  } else if (step === 4 && slot) {
    main = <Done slot={slot} label={L} fixed={fixed} setFixed={setFixed} fixBusy={fixBusy} setFixBusy={setFixBusy} countOf={countOf} again={again} />;
  }

  const showNoKey = claudeLoggedIn(tools);
  return (
    <div className="welcome-page">
      <header className="wl-top wrap">
        <span className="logo">
          <span className="ovp" data-t="OmniAPI">
            OmniAPI
          </span>
          <small>派工中心</small>
        </span>
        <button type="button" className="skip" onClick={skip}>
          {step === 4 ? "去看板 →" : "略過，先看看板 →"}
        </button>
      </header>
      <main className="wl wrap">
        <div className="wl-hero">
          <h1 className="ovp" data-t="Hello">
            Hello
          </h1>
          <span className="zh">
            第一次使用<small>接上一家模型供應商就能開始。大約一分鐘。</small>
          </span>
        </div>
        <ol className="steps">
          {STEPS.map(([zh, la], i) => (
            <li key={zh} className={i + 1 < step ? "done" : ""} aria-current={i + 1 === step ? "step" : undefined}>
              <span className="no">{i + 1}</span>
              {zh}
              <small>{la}</small>
            </li>
          ))}
        </ol>
        <div className="wl-grid">
          <div className="wl-main">{main}</div>
          <aside className="wl-side">
            {showNoKey ? (
              <div className="nokey-card">
                <b>不填 key 也能做的事</b>
                <p>這台電腦的 Claude Code 已登入：可以直接派工給 Claude Code，用你的訂閱，不用 API key。按右上「略過」就能開始。</p>
              </div>
            ) : null}
            {tools ? (
              <details className="card" open={!narrow}>
                <summary>
                  <h4>
                    這台電腦上
                    <small>
                      找到 {tools.summary.found}・少 {tools.summary.missing}
                    </small>
                  </h4>
                </summary>
                <p>少了的不擋路，只是某些功能用不了。之後在「設定 › 服務資訊」看得到，裝好按「重新偵測」。</p>
                <ToolsList data={tools} />
              </details>
            ) : null}
          </aside>
        </div>
      </main>
    </div>
  );
}

/** 第 4 步：OK 大章、各模態解鎖幾個、等級別名檢查、外部工具不擋路 */
function Done(props: {
  slot: Slot;
  label: string;
  fixed: TierMap | null;
  setFixed: (t: TierMap) => void;
  fixBusy: boolean;
  setFixBusy: (b: boolean) => void;
  countOf: (s: Slot, mod?: Modality) => number;
  again: () => void;
}) {
  const { slot, label: L, fixed, setFixed, fixBusy, setFixBusy, countOf, again } = props;
  const navigate = useNavigate();
  const settings = useSettings((s) => s.settings);
  const models = useSettings((s) => s.models);
  const list = useMemo(() => allModels(models), [models]);
  const counts = MOD_ORDER.map((k) => [k, countOf(slot, k)] as [Modality, number]);
  const hasText = counts[0][1] > 0 || slot === "openrouter";
  const sug = settings?.providers[slot]?.suggested_tiers ?? null;
  // 現在的等級別名指到哪一家；那一家還沒有 key 的列出來
  const tierRows = TIER_NAMES.map((t) => {
    const id = settings?.tiers[t]?.model ?? "";
    const m = list.find((x) => x.id === id || x.aliases?.includes(id));
    const s = m ? slotOf(m.provider, models) : id.includes("/") ? "openrouter" : null;
    const keyed = s ? !!settings?.providers[s]?.key.set : true;
    return { t, id, s, label: s ? settings?.providers[s]?.label ?? s : "", keyed };
  });
  const off = tierRows.filter((r) => !r.keyed);
  const applyFix = async () => {
    if (!sug) return;
    setFixBusy(true);
    const body: Record<string, string | null> = {};
    for (const t of TIER_NAMES) body[t] = sug[t] === settings?.tiers[t]?.catalog ? null : sug[t];
    const ok = await saveSettings({ tiers: body }, { what: [`等級別名都改用 ${L} 的模型`], sub: ["從首次啟動引導"] });
    setFixBusy(false);
    if (ok) setFixed(sug);
  };

  let fix: ReactNode = null;
  if (fixed)
    fix = (
      <div className="tierfix is-fixed">
        <div>
          <b className="t">三個等級都改用 {L} 的模型了</b>
          <table>
            <tbody>
              {TIER_NAMES.map((t) => (
                <tr key={t}>
                  <td className="l">{t}</td>
                  <td>
                    <span className="code">{fixed[t]}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <span className="x-dim" style={{ fontSize: "var(--fs-xs)" }}>
            要改回來或換別的：設定 › 等級別名。
          </span>
        </div>
      </div>
    );
  else if (!hasText)
    fix = (
      <div className="tierfix">
        <div>
          <b className="t">{L} 沒有文字模型</b>
          <span style={{ fontSize: "var(--fs-xs)" }}>
            生成
            {counts
              .filter(([, n]) => n)
              .map(([k]) => MOD[k].zh)
              .join("、")}
            可以用了。聊天、派工還要再接一家有文字模型的（或用 Claude Code 的訂閱派工）。
          </span>
          <div className="acts">
            <button type="button" className="btn solid" onClick={again}>
              再接一家
            </button>
          </div>
        </div>
      </div>
    );
  else if (off.length && sug)
    fix = (
      <div className="tierfix">
        <div>
          <b className="t">內建的等級別名有 {off.length} 個指到你還沒有 key 的供應商</b>
          <table className="tf-map">
            <tbody>
              {tierRows.map((r) => (
                <tr key={r.t}>
                  <td className="l">{r.t}</td>
                  <td className="from">
                    <span className="code">{r.id}</span>{" "}
                    <span className="x-dim">
                      {r.label}
                      {r.keyed ? "" : "（沒有 key）"}
                    </span>
                  </td>
                  <td className="arr">→</td>
                  <td className="to">
                    <span className="code">{sug[r.t as keyof TierMap]}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <span className="x-dim" style={{ fontSize: "var(--fs-xs)" }}>
            不改的話，用 cheap 聊天、派工（內建預設）會失敗。右邊是 {L} 的便宜／一般／最強，之後隨時能在設定裡換。
          </span>
          <div className="acts">
            <button type="button" className="btn solid" onClick={() => void applyFix()} disabled={fixBusy}>
              {fixBusy ? "改中…" : `三個都改用 ${L} 的`}
            </button>
            <Link className="lnk" to="/settings?sec=s-tiers">
              我自己挑
            </Link>
          </div>
        </div>
      </div>
    );
  else if (off.length && !sug)
    // 這家沒有建議值（OpenRouter：名單現查、目錄沒有它的定價）：不列空的對照表，請使用者等清單回來自己挑
    fix = (
      <div className="tierfix">
        <div>
          <b className="t">等級別名還指著內建的模型</b>
          <span style={{ fontSize: "var(--fs-xs)" }}>
            有 {off.length} 個指到你還沒有 key 的供應商（
            {off.map((r) => `${r.t} → ${r.id}`).join("、")}）。{L} 的模型是現查的，等清單回來（約幾秒）再到「設定 › 等級別名」挑。
          </span>
          <div className="acts">
            <Link className="btn solid" to="/settings?sec=s-tiers">
              去挑等級別名
            </Link>
          </div>
        </div>
      </div>
    );

  return (
    <>
      <div className="wl-done">
        <span className="stampbig">OK</span>
        <div className="t">
          <b>{L} 可以用了</b>
          <span>已存起來、已生效，不用重啟。</span>
        </div>
      </div>
      <div className="unl" style={{ marginTop: 18 }}>
        {counts.map(([k, n]) => (
          <div key={k} className={n ? "" : "zero"}>
            <b>{slot === "openrouter" && k === "text" && !n ? "—" : n}</b>
            <span>
              <Wg m={k} />
              {MOD[k].zh}
            </span>
          </div>
        ))}
      </div>
      {slot === "openrouter" ? (
        <p className="x-dim" style={{ fontSize: "var(--fs-xs)", marginTop: 6 }}>
          OpenRouter 的文字模型是現查的，不在目錄裡：清單回來之後在模型頁看得到。
        </p>
      ) : null}
      {fix ? <div style={{ marginTop: 18 }}>{fix}</div> : null}
      <div className="wl-nav">
        <button type="button" className="lnk" onClick={again}>
          再接一家
        </button>
        <button type="button" className="lnk" onClick={() => navigate("/")}>
          去看板
        </button>
        <span className="sp" />
        {hasText ? (
          <button type="button" className="stamp-btn" onClick={() => navigate("/chat")}>
            → 開始聊天
          </button>
        ) : (
          <button type="button" className="stamp-btn" onClick={() => navigate("/make")}>
            → 去生成
          </button>
        )}
      </div>
    </>
  );
}
