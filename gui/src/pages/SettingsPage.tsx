import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { DefaultKind, DesktopUpdate, ModelEntry, ModelsResponse, SettingsPatch, SettingsProvider, SettingsView, Slot, VideoSettings } from "@/api/types";
import { loadOptions } from "@/store/make";
import { SLOTS } from "@/api/types";
import { GetKeyHint, KeyInput, KeyMetaLine, TestBox, type KeyInputHandle, type KeyMeta, type TestState } from "@/components/settings/KeyParts";
import { ModelChooser, ModelLine } from "@/components/settings/ModelBits";
import ToolsList from "@/components/settings/ToolsList";
import { allModels, callable, daysLeft, DEF_LABEL, defLabel, hhmm, indexById, isSunset, knownModalities, MOD, reasonText, resolveTier, slotOf, slotUsable, TIER_NAMES, usableOf, type Modality } from "@/lib/catalog";
import { GEN_KINDS, GEN_KIND_INFO } from "@/lib/modalities";
import { useTouch } from "@/lib/rwd";
import { useSendKey } from "@/lib/sendKey";
import { THEMES, useTheme } from "@/themes";
import { useBoard } from "@/store/board";
import {
  connectClaude,
  loadAutostart,
  loadClaude,
  loadModels,
  loadTools,
  logLocal,
  removeKey,
  saveKey,
  saveSettings,
  setAutostart,
  setEnabled,
  testKey,
  useSettings,
  type LogEntry,
  type Seg,
} from "@/store/settings";

/* 設定頁 /settings（1.3-M4，版面稿 prototypes/settings-models-design/ settings-*）。
   一頁五段：01 供應商與 API key、02 等級別名、03 預設模型、04 外觀與操作、05 服務資訊。
   寬版左欄是目錄＋變更紀錄；中寬與手機目錄收成頂部的段落列、紀錄換成貼底的回饋列（rwd.css）。
   畫面能顯示與能改的東西以 API 為準（/api/settings、/api/models、/api/status 的 service、/api/tools、/api/claude-mcp、/api/desktop/autostart）。 */

const SECS: [string, string][] = [
  ["s-keys", "供應商與 key"],
  ["s-tiers", "等級別名"],
  ["s-defs", "預設模型"],
  ["s-look", "外觀與操作"],
  ["s-svc", "服務資訊"],
];
const TIER_NOTE: Record<string, string> = { cheap: "便宜、快。聊天與派工的內建預設", standard: "一般工作", strong: "難的、要想很久的" };
/** 預設模型的每一列：用哪個模態的模型、能不能選等級別名、一句說明（每一種 DefaultKind 都要有，少了編譯不過） */
const DEF_ROWS: Record<DefaultKind, [Modality, boolean, string]> = {
  chat: ["text", true, "新聊天一開始用的"],
  dispatch: ["text", true, "派工沒指定模型時"],
  image: [GEN_KIND_INFO.image.modality, false, "生成頁、MCP 的生圖"],
  speech: [GEN_KIND_INFO.speech.modality, false, "文字轉語音"],
  music: [GEN_KIND_INFO.music.modality, false, "生成頁、MCP 的音樂"],
  transcript: [GEN_KIND_INFO.transcript.modality, false, "語音轉文字"],
  video: [GEN_KIND_INFO.video.modality, false, "生成頁、MCP 的影片"],
};
const DEFS: [DefaultKind, string, Modality, boolean, string][] = (["chat", "dispatch", ...GEN_KINDS] as DefaultKind[]).map((k) => [k, DEF_LABEL[k], ...DEF_ROWS[k]]);

/* ---------------- 共用小件 ---------------- */
export function Segs({ s }: { s: Seg[] | undefined }) {
  if (!s) return null;
  return (
    <>
      {s.map((x, i) =>
        typeof x === "string" ? (
          <Fragment key={i}>{x}</Fragment>
        ) : "code" in x ? (
          <span key={i} className="code">
            {x.code}
          </span>
        ) : (
          <span key={i} className="kmask">
            <i>••••</i>
            {x.mask}
          </span>
        ),
      )}
    </>
  );
}
function Stamp({ st }: { st: LogEntry["st"] }) {
  if (st === "ok") return <span className="done-stamp solid">已存・已生效</span>;
  if (st === "warn") return <span className="done-stamp">已存・有提醒</span>;
  if (st === "pending") return <span className="done-stamp">已存・生效中…</span>;
  if (st === "local") return <span className="done-stamp">記在這個瀏覽器</span>;
  return <span className="done-stamp">沒存成</span>;
}
const clock = (ms: number) => {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
function SrcStamp({ s }: { s: string }) {
  if (s === "settings") return <span className="srcst">設定頁</span>;
  if (s === "env") return <span className="srcst env">.env</span>;
  return <span className="srcst def">內建</span>;
}
function Wg({ m }: { m: Modality }) {
  return (
    <i className={`mk-wg ${MOD[m].k} sm`} title={MOD[m].zh}>
      {MOD[m].g}
    </i>
  );
}
async function copyText(t: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = t;
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

/* ---------------- 頁 ---------------- */
export default function SettingsPage() {
  const settings = useSettings((s) => s.settings);
  const error = useSettings((s) => s.error);
  const models = useSettings((s) => s.models);
  const log = useSettings((s) => s.log);
  const status = useBoard((s) => s.status);
  const loc = useLocation();
  const navigate = useNavigate();
  const [sec, setSec] = useState(0);
  const [edit, setEdit] = useState<Slot | null>(null);
  const [pick, setPick] = useState<string | null>(null);
  const [focusSlot, setFocusSlot] = useState<Slot | null>(null);

  useEffect(() => {
    void loadModels();
    void loadTools();
    void loadClaude();
    void loadAutostart();
    // 系統匣那邊可能改了開機啟動：視窗或分頁拿回焦點時重讀
    const back = () => {
      if (document.visibilityState === "visible") void loadAutostart();
    };
    window.addEventListener("focus", back);
    document.addEventListener("visibilitychange", back);
    return () => {
      window.removeEventListener("focus", back);
      document.removeEventListener("visibilitychange", back);
    };
  }, []);

  const list = useMemo(() => allModels(models), [models]);
  const idx = useMemo(() => indexById(list), [list]);

  // 深連結：?p=<slot>（模型頁「去設定貼 key」）、?sec=<段落>
  const q = new URLSearchParams(loc.search);
  const wantP = q.get("p") as Slot | null;
  const wantSec = q.get("sec");
  const handled = useRef("");
  useEffect(() => {
    if (!settings) return;
    const k = loc.search;
    if (handled.current === k) return;
    handled.current = k;
    if (wantP && (SLOTS as readonly string[]).includes(wantP)) {
      const p = settings.providers[wantP];
      if (p.key.set && !p.enabled) setFocusSlot(wantP);
      else setEdit(wantP);
      scrollToId(`pv-${wantP}`);
    } else if (wantSec) scrollToId(wantSec);
  }, [settings, loc.search, wantP, wantSec]);

  // 目錄跟著捲動標出目前段落
  useEffect(() => {
    const on = () => {
      let i = 0;
      SECS.forEach(([id], k) => {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top < 200) i = k;
      });
      setSec(i);
    };
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);

  const goSlot = (slot: Slot) => {
    const p = settings?.providers[slot];
    if (p && p.key.set && !p.enabled) void setEnabled(slot, true, p.label);
    else {
      setEdit(slot);
      setPick(null);
    }
    scrollToId(`pv-${slot}`);
  };

  if (!settings)
    return (
      <div className="smp wrap">
        <Head path={null} />
        <p className="x-dim">{error ? `讀不到設定：${error}` : "讀取設定…"}</p>
      </div>
    );

  const nOk = SLOTS.filter((s) => callable(usableOf(settings.providers[s]))).length;
  const tierOver = TIER_NAMES.filter((t) => settings.tiers[t]?.source === "settings").length;
  const fresh = log.find((l) => l.fresh);

  return (
    <div className="smp wrap">
      <Head path={settings.path} />
      {settings.restart_required.length ? (
        <div className="warn" role="status" style={{ marginBottom: 18 }}>
          <b>有 {settings.restart_required.length} 項要重啟服務才生效</b>：{settings.restart_required.join("、")}。
        </div>
      ) : null}
      <div className="sp-grid">
        <aside className="sp-rail">
          <nav className="sp-toc" aria-label="設定的段落">
            {SECS.map(([id, zh], i) => (
              <a
                key={id}
                href={`#${id}`}
                aria-current={i === sec ? "true" : undefined}
                onClick={(e) => {
                  e.preventDefault();
                  scrollToId(id);
                }}
              >
                <span className="no">0{i + 1}</span>
                <b>{zh}</b>
                <small>
                  {id === "s-keys" ? (
                    <>
                      <b>{nOk}</b>／{SLOTS.length} 家能用
                    </>
                  ) : id === "s-tiers" ? (
                    tierOver ? (
                      <>
                        改了 <b>{tierOver}</b> 個
                      </>
                    ) : (
                      "照內建"
                    )
                  ) : id === "s-defs" ? (
                    `${DEFS.length} 種用途`
                  ) : id === "s-look" ? (
                    "存在瀏覽器"
                  ) : (
                    "版本、資料夾、外部工具"
                  )}
                </small>
              </a>
            ))}
          </nav>
          <div className="sp-log" aria-live="polite">
            <h4>
              變更紀錄<small>這個分頁</small>
            </h4>
            {log.length ? (
              <ol>
                {log.slice(0, 6).map((l) => (
                  <li key={l.id} className={l.st === "err" ? "err" : ""}>
                    <span className="t">{clock(l.at)}</span>
                    <span className="w">
                      <Segs s={l.what} />
                    </span>
                    <div className="r">
                      <Stamp st={l.st} />
                      {l.retry ? (
                        <button type="button" className="lnk" onClick={l.retry}>
                          重試
                        </button>
                      ) : null}
                    </div>
                    {l.sub ? (
                      <div className="x-dim" style={{ marginTop: 3 }}>
                        <Segs s={l.sub} />
                      </div>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : (
              <div className="empty">還沒有改過。改了的東西會一筆一筆記在這裡。</div>
            )}
          </div>
          <div className="sp-file">
            存在 <span className="code">settings.json</span>，蓋在 <span className="code">.env</span> 之上。<span className="code">.env</span> 檔本身不會被改。
          </div>
        </aside>
        <div className="sp-main">
          <KeysSection settings={settings} edit={edit} setEdit={setEdit} focusSlot={focusSlot} />
          <TiersSection settings={settings} idx={idx} models={models} list={list} pick={pick} setPick={setPick} goSlot={goSlot} />
          <DefsSection settings={settings} idx={idx} models={models} list={list} pick={pick} setPick={setPick} goSlot={goSlot} />
          <LookSection />
          <SvcSection />
          {fresh ? <Toast l={fresh} key={fresh.id} /> : null}
        </div>
      </div>
      {status?.offline ? <p className="sp-file" style={{ marginTop: 24 }}>這是離線沙盒：測試 key 只檢查格式，不會真的送給供應商。</p> : null}
      <p className="sp-file" style={{ marginTop: 12 }}>
        第一次使用的引導：
        <a
          className="lnk"
          href="/welcome"
          onClick={(e) => {
            e.preventDefault();
            navigate("/welcome");
          }}
        >
          再走一次引導 →
        </a>
      </p>
    </div>
  );
}

function scrollToId(id: string) {
  window.setTimeout(() => {
    const el = document.getElementById(id);
    if (!el) return;
    const css = getComputedStyle(document.documentElement);
    const top = el.getBoundingClientRect().top + window.scrollY - (parseInt(css.getPropertyValue("--top-h")) || 64) - (css.getPropertyValue("--tier").trim() === "xl" || css.getPropertyValue("--tier").trim() === "l" ? 16 : 64);
    window.scrollTo({ top: Math.max(0, top) });
  }, 30);
}

function Head({ path }: { path: string | null }) {
  return (
    <div className="sechead sm-head">
      <h2 className="ovp" data-t="Settings">
        Settings
      </h2>
      <span className="zh">設定</span>
      <span className="sub">改了就生效，不用重啟服務</span>
      {path ? (
        <span className="aside x-s">
          設定檔 <span className="code">{path}</span>
        </span>
      ) : null}
    </div>
  );
}

/** 窄畫面貼底的回饋列：只放這次操作產生的那一筆；成功的過一陣子收起 */
function Toast({ l }: { l: LogEntry }) {
  const [gone, setGone] = useState(false);
  useEffect(() => {
    if (l.st === "err" || l.st === "pending") return;
    const t = window.setTimeout(() => setGone(true), 9000);
    return () => window.clearTimeout(t);
  }, [l.st]);
  if (gone) return null;
  return (
    <div className={`sp-toast${l.st === "err" ? " err" : ""}`} aria-live="polite">
      <span className="t">{clock(l.at)}</span>
      <Stamp st={l.st} />
      <span className="w">
        <Segs s={l.what} />
        {l.sub ? (
          <>
            {" · "}
            <span className="x-dim">
              <Segs s={l.sub} />
            </span>
          </>
        ) : null}
      </span>
      {l.retry ? (
        <button type="button" className="lnk" onClick={l.retry}>
          重試
        </button>
      ) : null}
    </div>
  );
}

/* ================= 01 供應商與 key ================= */
function KeysSection(props: { settings: SettingsView; edit: Slot | null; setEdit: (s: Slot | null) => void; focusSlot: Slot | null }) {
  const { settings, edit, setEdit, focusSlot } = props;
  const models = useSettings((s) => s.models);
  return (
    <section className="sx" id="s-keys">
      <div className="sx-h">
        <span className="no">01</span>
        <b>供應商與 API key</b>
        <span className="la">Providers</span>
        <span className="aside">測試不花錢：只列模型、不叫模型</span>
      </div>
      <p className="sx-lead">
        貼上 key → 按「測試」→ 存起來，<b>不用重啟</b>。key 只存在這台電腦（<span className="code">settings.json</span>，明文，跟 <span className="code">.env</span> 同一個安全等級），畫面上只顯示<b>末四碼</b>。
      </p>
      <div className="pv-legend">
        <span>供應商</span>
        <span>key</span>
        <span>啟用</span>
        <span>能不能用</span>
        <span />
      </div>
      {SLOTS.map((slot) => (
        <ProviderRow
          key={slot}
          slot={slot}
          p={settings.providers[slot]}
          mods={knownModalities(models?.providers?.[settings.providers[slot].provider]?.modalities as string[] | undefined)}
          editing={edit === slot}
          focused={focusSlot === slot}
          onEdit={() => setEdit(slot)}
          onClose={() => setEdit(null)}
        />
      ))}
    </section>
  );
}

function ProviderRow({ slot, p, mods, editing, focused, onEdit, onClose }: { slot: Slot; p: SettingsProvider; mods: Modality[]; editing: boolean; focused: boolean; onEdit: () => void; onClose: () => void }) {
  const saved = useSettings((s) => s.saved[`pv:${slot}`]);
  const [rm, setRm] = useState(false);
  const [rowTest, setRowTest] = useState<TestState>({ st: "untested" });
  const u = usableOf(p);
  const k = p.key;
  const L = p.label;

  const runRowTest = async () => {
    setRowTest({ st: "testing" });
    try {
      setRowTest({ st: "done", res: await testKey(slot) });
    } catch (e) {
      setRowTest({ st: "done", res: { provider: slot, key: { set: true, last4: k.last4 }, ok: false, reason: "network", message: String(e), checked_at: Date.now() / 1000 } });
    }
  };
  // 換了 key（末四碼變了）：上一次的列上測試結果作廢
  useEffect(() => {
    setRowTest({ st: "untested" });
  }, [k.last4, k.source]);

  let state: ReactNode;
  const rt = rowTest.st === "done" ? rowTest.res : null;
  if (rowTest.st === "testing")
    state = (
      <span className="st unk">
        <span className="drum" style={{ width: 12, height: 12, borderWidth: 1.5 }} />
        測試中…
      </span>
    );
  else if (u === "none")
    state = (
      <>
        <span className="st none">還不能用</span>
        <span className="st-sub">要先貼 key</span>
      </>
    );
  else if (u === "off")
    state = (
      <>
        <span className="st off">停用中</span>
        <span className="st-sub">key 留著，按「開」就回來</span>
      </>
    );
  else if (rt && rt.ok === false)
    state = (
      <>
        <span className="st error">不通</span>
        <span className="st-sub">{reasonText(rt.reason, rt.status)}</span>
      </>
    );
  else if (u === "bad")
    state = (
      <>
        <span className="st error">不通</span>
        <span className="st-sub">
          {reasonText(p.health?.reason, p.health?.status)} · {hhmm(p.health?.checked_at)}
        </span>
      </>
    );
  else if (rt && rt.ok === null)
    state = (
      <>
        <span className="st unk">已設定・沒辦法測</span>
        <span className="st-sub">這家沒有不花錢的檢查方式</span>
      </>
    );
  else if (u === "ok" || (rt && rt.ok))
    state = (
      <>
        <span className="st ok">能用</span>
        <span className="st-sub">
          {rt?.ok ? (
            rt.simulated ? (
              "剛剛測過（離線沙盒：只檢查格式）"
            ) : (
              "剛剛測過"
            )
          ) : (
            <>
              {p.health?.listed != null ? (
                <>
                  列到 <span className="n">{p.health.listed}</span> 個模型 ·{" "}
                </>
              ) : null}
              {hhmm(p.health?.checked_at)} {p.health?.source === "test" ? "測過" : "列過"}
            </>
          )}
        </span>
      </>
    );
  else
    state = (
      <>
        <span className="st unk">已設定・還沒測過</span>
        <span className="st-sub">按「測試」確認通不通（不花錢）</span>
      </>
    );

  let acts: ReactNode;
  if (rm)
    acts = (
      <>
        <span className="x-dim" style={{ fontSize: "var(--fs-m11)", flexBasis: "100%", textAlign: "right" }}>
          {k.shadowed?.set ? `拿掉後回到 .env 的 •••• ${k.shadowed.last4 ?? ""}` : `拿掉後 ${L} 就沒有 key 了`}
        </span>
        <button
          type="button"
          className="btn solid"
          onClick={() => {
            setRm(false);
            void removeKey(slot, L);
          }}
        >
          拿掉
        </button>
        <button type="button" className="btn soft" onClick={() => setRm(false)}>
          算了
        </button>
      </>
    );
  else if (!k.set)
    acts = (
      <button type="button" className="btn solid" onClick={onEdit}>
        貼上 key
      </button>
    );
  else if (k.source === "env")
    acts = (
      <>
        <button type="button" className="btn" onClick={onEdit}>
          用新 key 蓋過
        </button>
        <button type="button" className="btn soft" onClick={() => void runRowTest()} disabled={rowTest.st === "testing"}>
          測試
        </button>
      </>
    );
  else
    acts = (
      <>
        <button type="button" className="btn" onClick={onEdit}>
          換 key
        </button>
        <button type="button" className="btn soft" onClick={() => void runRowTest()} disabled={rowTest.st === "testing"}>
          測試
        </button>
        <button type="button" className="btn soft" onClick={() => setRm(true)}>
          拿掉
        </button>
      </>
    );

  const cls = ["pv", u === "off" ? "is-off" : "", u === "none" ? "is-none" : "", editing ? "is-editing" : "", focused && !editing ? "is-focus" : ""].filter(Boolean).join(" ");
  return (
    <div className={cls} id={`pv-${slot}`} data-slot={slot}>
      <div className="pv-name">
        <b>{L}</b>
        <span className="pv-mods">
          {mods.map((m) => (
            <Wg key={m} m={m} />
          ))}
        </span>
      </div>
      <div className="pv-key">
        {!k.set ? (
          <div className="kline">
            <span className="st none">沒有 key</span>
          </div>
        ) : (
          <>
            <div className="kline">
              <span className="kmask">
                <i>••••</i>
                {k.last4 ?? ""}
              </span>
              <SrcStamp s={k.source ?? "env"} />
            </div>
            {k.source === "env" ? (
              <div className="knote">
                來自 <span className="code">.env</span>。在這裡貼新的會蓋過它；要刪掉得改 <span className="code">.env</span> 檔。
              </div>
            ) : k.shadowed?.set ? (
              <div className="knote">
                蓋過了 <span className="code">.env</span> 的{" "}
                <span className="kmask">
                  <i>••••</i>
                  {k.shadowed.last4 ?? ""}
                </span>
                。拿掉這把就回到它。
              </div>
            ) : null}
          </>
        )}
      </div>
      <div className="pv-on">
        <div className={`sw${p.enabled ? "" : " is-off"}`} role="group" aria-label={`${L} 啟用`}>
          <button type="button" aria-pressed={k.set && p.enabled} disabled={!k.set} onClick={() => !p.enabled && void setEnabled(slot, true, L)}>
            開
          </button>
          <button type="button" aria-pressed={k.set && !p.enabled} disabled={!k.set} onClick={() => p.enabled && void setEnabled(slot, false, L)}>
            停
          </button>
        </div>
      </div>
      <div className="pv-state">
        {state}
        {saved ? (
          <div style={{ marginTop: 4 }}>
            <span className="done-stamp solid">已存・已生效 {clock(saved)}</span>
          </div>
        ) : null}
      </div>
      <div className="pv-acts">{acts}</div>
      {editing ? <KeyEditor slot={slot} p={p} onClose={onClose} /> : null}
    </div>
  );
}

/** 那一列原地展開：輸入框（左）＋測試結果框（右）。不通也可以「照樣存」，沒測也可以「不測，直接存」。 */
function KeyEditor({ slot, p, onClose }: { slot: Slot; p: SettingsProvider; onClose: () => void }) {
  const inp = useRef<KeyInputHandle>(null);
  const [meta, setMeta] = useState<KeyMeta | null>(null);
  const [t, setT] = useState<TestState>({ st: "untested" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const L = p.label;
  const has = !!meta;
  useEffect(() => {
    inp.current?.focus();
    return () => inp.current?.clear();
  }, []);
  const onMeta = (m: KeyMeta | null) => {
    setMeta(m);
    setErr(null);
    setT({ st: "untested" });
  };
  const test = async () => {
    const v = inp.current?.value() ?? "";
    if (!v) return;
    setT({ st: "testing" });
    try {
      setT({ st: "done", res: await testKey(slot, v) });
    } catch {
      setT({ st: "done", res: { provider: slot, key: { set: true, last4: v.slice(-4) }, ok: false, reason: "network", message: "", checked_at: Date.now() / 1000 } });
    }
  };
  const save = async () => {
    const v = inp.current?.value() ?? "";
    if (!v) return;
    setBusy(true);
    const ok = await saveKey(slot, v, { label: L, replaced: p.key.set });
    setBusy(false);
    if (ok) {
      inp.current?.clear();
      onClose();
    } else setErr("沒存成：服務沒回應或拒絕了。key 還在輸入框裡，可以再按一次「存起來」。");
  };
  const retype = () => {
    inp.current?.clear();
    inp.current?.focus();
  };
  const lbl = p.key.source === "env" ? `用新 key 蓋過 .env 的 •••• ${p.key.last4 ?? ""}` : p.key.set ? `換掉 •••• ${p.key.last4 ?? ""}` : `貼上 ${L} 的 key`;
  const done = t.st === "done" ? t.res : null;
  let main: ReactNode;
  if (done && done.ok === false)
    main = (
      <>
        <button type="button" className="btn solid" onClick={retype}>
          改貼一次
        </button>
        <button type="button" className="btn" onClick={() => void save()} disabled={busy}>
          照樣存
        </button>
      </>
    );
  else if (done)
    main = (
      <button type="button" className="btn solid" onClick={() => void save()} disabled={busy}>
        {busy ? "存檔中…" : "存起來"}
      </button>
    );
  else
    main = (
      <>
        <button type="button" className="btn solid" onClick={() => void test()} disabled={!has || t.st === "testing" || busy}>
          測試
        </button>
        <button type="button" className="btn" onClick={() => void save()} disabled={!has || t.st === "testing" || busy}>
          不測，直接存
        </button>
      </>
    );
  return (
    <div className="pv-ed">
      <div className="kin-wrap">
        <div className="kin-lbl">
          <b>{lbl}</b>
          <span className="aside">
            <GetKeyHint gk={p.get_key} compact />
          </span>
        </div>
        <KeyInput ref={inp} label={`${L} API key`} disabled={t.st === "testing" || busy} onMeta={onMeta} />
        <KeyMetaLine meta={meta} />
        <div className="kin-acts">
          {main}
          <span className="sp" />
          <button
            type="button"
            className="btn soft"
            onClick={() => {
              inp.current?.clear();
              onClose();
            }}
          >
            取消
          </button>
        </div>
        {err ? (
          <div className="warn" role="alert">
            <b>沒存成</b>：{err}
          </div>
        ) : null}
        <div className="kin-where">
          {p.key.source === "env" ? (
            <>
              設定頁的 key 優先；<span className="code">.env</span> 檔不會被改。之後拿掉這把，就回到 .env 的那把。
            </>
          ) : (
            <>
              存在 <span className="code">settings.json</span>。沒有「顯示 key」的按鈕：存了之後畫面上只剩末四碼。
            </>
          )}
        </div>
      </div>
      <TestBox t={t} label={L} />
    </div>
  );
}

/* ================= 02 等級別名、03 預設模型 ================= */
type PickProps = {
  settings: SettingsView;
  idx: Map<string, ModelEntry>;
  models: ModelsResponse | null;
  list: ModelEntry[];
  pick: string | null;
  setPick: (s: string | null) => void;
  goSlot: (s: Slot) => void;
};

/** 交叉檢查：用到的模型叫不動（沒 key、停用、key 不通）或快下架 → 虛線提醒＋一顆能直接處理的鈕 */
function ModelWarn({ value, ctx, target, p }: { value: string | null; ctx: string; target: string; p: PickProps }) {
  const id = resolveTier(p.settings, value);
  if (!id) return null;
  const m = p.idx.get(id);
  if (!m) return null;
  const slot = slotOf(m.provider, p.models);
  const u = slotUsable(p.settings, slot);
  const out: ReactNode[] = [];
  if (slot && !callable(u)) {
    const L = p.settings.providers[slot].label;
    const why = u === "none" ? `${L} 沒有 key` : u === "off" ? `${L} 停用中` : `${L} 的 key 不通`;
    out.push(
      <div className="tr-warn" key="u">
        <div className="warn">
          <b>{why}</b>：用到 {ctx} 的地方現在會失敗。
        </div>
        <button type="button" className="btn" onClick={() => p.goSlot(slot)}>
          {u === "off" ? `去開 ${L}` : u === "none" ? "去補 key" : `去看 ${L} 的 key`}
        </button>
      </div>,
    );
  }
  if (isSunset(m)) {
    const d = m.shutdown ? `${m.shutdown} 下架（${daysLeft(m.shutdown) >= 0 ? `剩 ${daysLeft(m.shutdown)} 天` : "已過下架日"}）` : "官方已公告淘汰";
    out.push(
      <div className="tr-warn" key="s">
        <div className="warn">
          <b>{m.id} 快下架</b>：{d}。{m.replacement ? (
            <>
              官方建議改用 <span className="code">{m.replacement}</span>。
            </>
          ) : null}
        </div>
        {m.replacement ? (
          <button type="button" className="btn" onClick={() => void choose(target, m.replacement!, p.settings)}>
            換成 {m.replacement}
          </button>
        ) : null}
      </div>,
    );
  }
  return <>{out}</>;
}

const targetLabel = (target: string) => (target.startsWith("d:") ? `預設${defLabel(target.slice(2))}` : target);
function choose(target: string, v: string, settings: SettingsView) {
  if (target.startsWith("d:")) {
    const k = target.slice(2) as DefaultKind;
    return saveSettings({ defaults: { [k]: v } }, { what: [`${targetLabel(target)} → `, { code: v }], target, sub: ["進行中的工作用舊的跑完"] });
  }
  // 選回內建的那個＝拿掉覆寫（來源回到「內建」）
  const builtin = settings.tiers[target]?.catalog;
  return saveSettings({ tiers: { [target]: v === builtin ? null : v } }, { what: [`${target} → `, { code: v }], target, sub: ["進行中的工作用舊的跑完"] });
}
function reset(target: string) {
  if (target.startsWith("d:")) return saveSettings({ defaults: { [target.slice(2)]: null } }, { what: [`${targetLabel(target)} 還原成內建`], target });
  return saveSettings({ tiers: { [target]: null } }, { what: [`${target} 還原成內建`], target });
}

function TiersSection(p: PickProps) {
  const { settings } = p;
  const saved = useSettings((s) => s.saved);
  return (
    <section className="sx" id="s-tiers">
      <div className="sx-h">
        <span className="no">02</span>
        <b>等級別名</b>
        <span className="la">Tiers</span>
        <span className="aside">派工、聊天、MCP 的 cheap／standard／strong 都照這張表</span>
      </div>
      <p className="sx-lead">選了就存、立刻生效。進行中的工作用舊的跑完。改過的可以「還原內建」，內建值跟著目錄更新。</p>
      {Object.keys(settings.tiers).map((t) => {
        const v = settings.tiers[t];
        const picking = p.pick === t;
        const sv = saved[t];
        return (
          <div key={t} className={`tr${picking ? " is-picking" : ""}${sv ? " is-saved" : ""}`} id={`tr-${t}`}>
            <div className="tr-lab">
              <b>{t}</b>
              <small>{TIER_NOTE[t] ?? ""}</small>
            </div>
            <div className="tr-model">
              <ModelLine id={v.model} entry={p.idx.get(v.model) ?? null} data={p.models} />
              {v.source !== "catalog" ? (
                <div className="via">
                  內建是 <span className="code">{v.catalog}</span>
                </div>
              ) : null}
            </div>
            <div className="tr-src">
              <SrcStamp s={v.source} />
              {sv ? <span className="done-stamp solid">已存・已生效 {clock(sv)}</span> : null}
            </div>
            <div className="tr-acts">
              {picking ? (
                <button type="button" className="btn soft" onClick={() => p.setPick(null)}>
                  收起
                </button>
              ) : (
                <button type="button" className="btn" onClick={() => p.setPick(t)}>
                  改
                </button>
              )}
              {v.source === "settings" ? (
                <button type="button" className="btn soft" onClick={() => void reset(t)}>
                  還原內建
                </button>
              ) : null}
            </div>
            {picking ? (
              <ModelChooser
                modality="text"
                current={v.model}
                withTiers={false}
                data={p.models}
                list={p.list}
                settings={settings}
                onChoose={(id) => {
                  p.setPick(null);
                  void choose(t, id, settings);
                }}
              />
            ) : null}
            <ModelWarn value={v.model} ctx={t} target={t} p={p} />
          </div>
        );
      })}
    </section>
  );
}

function DefsSection(p: PickProps) {
  const { settings } = p;
  const saved = useSettings((s) => s.saved);
  return (
    <section className="sx" id="s-defs">
      <div className="sx-h">
        <span className="no">03</span>
        <b>預設模型</b>
        <span className="la">Defaults</span>
        <span className="aside">沒指定模型時用哪個</span>
      </div>
      <p className="sx-lead">聊天與派工可以填等級別名（跟著 02 的表走），也可以指定一個模型。生成頁上自己挑的模型不受這裡影響。</p>
      {DEFS.map(([k, zh, modality, withTiers, note]) => {
        const d = settings.defaults[k];
        const target = `d:${k}`;
        const picking = p.pick === target;
        const sv = saved[target];
        const isTier = !!(d.model && settings.tiers[d.model]);
        return (
          <div key={k} className={`tr${picking ? " is-picking" : ""}${sv ? " is-saved" : ""}`} id={`tr-d-${k}`}>
            <div className="tr-lab">
              <b className="zh">{zh}</b>
              <small>{note}</small>
            </div>
            <div className="tr-model">
              {!d.model ? (
                <div className="ml">
                  <span className="prov">沒指定：用第一家有 key 的供應商的預設</span>
                </div>
              ) : isTier ? (
                <>
                  <div className="ml">
                    <span className="code">
                      <b>{d.model}</b>
                    </span>
                    <span className="prov">等級別名</span>
                  </div>
                  <div className="via">
                    現在是 <span className="code">{settings.tiers[d.model].model}</span>
                  </div>
                </>
              ) : (
                <ModelLine id={d.model} entry={p.idx.get(d.model) ?? null} data={p.models} />
              )}
            </div>
            <div className="tr-src">
              <SrcStamp s={d.source} />
              {sv ? <span className="done-stamp solid">已存・已生效 {clock(sv)}</span> : null}
            </div>
            <div className="tr-acts">
              {picking ? (
                <button type="button" className="btn soft" onClick={() => p.setPick(null)}>
                  收起
                </button>
              ) : (
                <button type="button" className="btn" onClick={() => p.setPick(target)}>
                  改
                </button>
              )}
              {d.source === "settings" ? (
                <button type="button" className="btn soft" onClick={() => void reset(target)}>
                  還原內建
                </button>
              ) : null}
            </div>
            {picking ? (
              <ModelChooser
                modality={modality}
                current={d.model}
                withTiers={withTiers}
                data={p.models}
                list={p.list}
                settings={settings}
                onChoose={(id) => {
                  p.setPick(null);
                  void choose(target, id, settings);
                }}
              />
            ) : null}
            <ModelWarn value={d.model} ctx={`${zh}預設`} target={target} p={p} />
            {k === "video" ? (
              <div className="tr-warn">
                <div className="warn">
                  影片按秒計費、一支幾毛到幾塊美金。這裡選的模型也是 <b>Claude Code 叫 generate_video 沒指定模型時</b>用的；MCP 送出前沒有確認單，只靠這裡的預設與下面的單支上限。
                </div>
              </div>
            ) : null}
          </div>
        );
      })}
      {settings.video ? <VideoSettings v={settings.video} /> : null}
    </section>
  );
}

/* ---------------- 影片的三個設定（settings.json 的 video 一節） ---------------- */
function VideoSettings({ v }: { v: NonNullable<SettingsView["video"]> }) {
  const saved = useSettings((s) => s.saved);
  const unlimited = v.mcp_unlimited.value === true;
  const max = Number(v.mcp_max_usd.value);
  const wait = Number(v.max_wait_minutes.value);
  const keep = v.keep_collecting.value === true;
  const [maxIn, setMaxIn] = useState(String(max));
  const [waitIn, setWaitIn] = useState(String(wait));
  useEffect(() => setMaxIn(String(max)), [max]);
  useEffect(() => setWaitIn(String(wait)), [wait]);
  const save = async (patch: NonNullable<SettingsPatch["video"]>, what: Seg[], target: string) => {
    const ok = await saveSettings({ video: patch }, { what, target, sub: ["之後送出的影片照新的設定；已經在等的不受影響"] });
    if (ok) void loadOptions(true);
  };
  const maxNum = Number(maxIn);
  const maxOk = Number.isFinite(maxNum) && maxNum > 0 && maxNum <= 1000;
  const waitNum = Number(waitIn);
  const waitOk = Number.isFinite(waitNum) && waitNum >= 1 && waitNum <= 240;
  const reset = (k: keyof VideoSettings, zh: string) =>
    v[k].source === "settings" ? (
      <button type="button" className="btn soft" onClick={() => void save({ [k]: null }, [`${zh} 還原成內建`], `v:${k}`)}>
        還原內建
      </button>
    ) : null;
  const stamp = (k: string) => (saved[`v:${k}`] ? <span className="done-stamp solid">已存・已生效 {clock(saved[`v:${k}`])}</span> : null);
  return (
    <div className="vd-set" id="s-video">
      <div className="sx-sub">
        <b>影片</b>
        <small>Claude Code（MCP）送影片的上限、我們這邊等多久、不等了之後要不要繼續收</small>
      </div>
      <div className="tr" id="tr-v-mcp">
        <div className="tr-lab">
          <b className="zh">MCP 單支上限</b>
          <small>Claude Code 叫 generate_video 時，預估超過就不送</small>
        </div>
        <div className="tr-model">
          {unlimited ? (
            <span className="code">
              <b>不限</b>
            </span>
          ) : (
            <>
              <label className="x-dim" htmlFor="vd-mcp-max">
                每支最多 $
              </label>
              <input id="vd-mcp-max" className="dp-in" inputMode="decimal" value={maxIn} onChange={(e) => setMaxIn(e.target.value)} aria-invalid={!maxOk} />
              <button type="button" className="btn" disabled={!maxOk || maxNum === max} onClick={() => void save({ mcp_max_usd: maxNum }, [`MCP 單支上限 → $${maxNum}`], "v:mcp_max_usd")}>
                存
              </button>
            </>
          )}
          <div className="x-dim">
            {unlimited
              ? "不限：MCP 送影片不看金額（按 token 計價、算不出金額的也照送）。"
              : "超過就不送出，回一句預估與上限；算不出金額的（按 token 計價）要 Claude Code 自己帶上限才送。生成頁有確認單，不受這個上限影響。"}
          </div>
        </div>
        <div className="tr-src">
          <SrcStamp s={v.mcp_unlimited.source === "settings" ? "settings" : v.mcp_max_usd.source} />
          {stamp("mcp_max_usd") ?? stamp("mcp_unlimited")}
        </div>
        <div className="tr-acts">
          <div className="sseg" role="group" aria-label="MCP 單支上限">
            <button type="button" aria-pressed={!unlimited} onClick={() => unlimited && void save({ mcp_unlimited: false }, ["MCP 單支上限 → 有上限"], "v:mcp_unlimited")}>
              有上限
            </button>
            <button type="button" aria-pressed={unlimited} onClick={() => !unlimited && void save({ mcp_unlimited: true }, ["MCP 單支上限 → 不限"], "v:mcp_unlimited")}>
              不限
            </button>
          </div>
          {reset("mcp_max_usd", "MCP 單支上限")}
        </div>
      </div>
      <div className="tr" id="tr-v-wait">
        <div className="tr-lab">
          <b className="zh">最長等待</b>
          <small>我們這邊等多久就先停下來</small>
        </div>
        <div className="tr-model">
          <label className="x-dim" htmlFor="vd-max-wait">
            最多等幾分鐘
          </label>
          <input id="vd-max-wait" className="dp-in" inputMode="numeric" value={waitIn} onChange={(e) => setWaitIn(e.target.value)} aria-invalid={!waitOk} />
          <button type="button" className="btn" disabled={!waitOk || waitNum === wait} onClick={() => void save({ max_wait_minutes: waitNum }, [`最長等待 → ${waitNum} 分鐘`], "v:max_wait_minutes")}>
            存
          </button>
          <div className="x-dim">超過就標「等太久」，留著供應商那邊的編號；之後按「再去問一次」拿得到就收進作品牆，不會重送、不會多收。</div>
        </div>
        <div className="tr-src">
          <SrcStamp s={v.max_wait_minutes.source} />
          {stamp("max_wait_minutes")}
        </div>
        <div className="tr-acts">{reset("max_wait_minutes", "最長等待")}</div>
      </div>
      <div className="tr" id="tr-v-keep">
        <div className="tr-lab">
          <b className="zh">不等了之後</b>
          <small>按了「不等了」，背景要不要繼續收</small>
        </div>
        <div className="tr-model">
          <div className="x-dim">
            {keep
              ? "繼續收：畫面上不再等，背景照樣問；做好照樣收進作品牆，費用記實際的。"
              : "不收：不再問也不下載；做好的影片不會收進作品牆，費用頁記「費用不明」（之後還能按「再去問一次」）。"}
          </div>
        </div>
        <div className="tr-src">
          <SrcStamp s={v.keep_collecting.source} />
          {stamp("keep_collecting")}
        </div>
        <div className="tr-acts">
          <div className="sseg" role="group" aria-label="不等了之後">
            <button type="button" aria-pressed={keep} onClick={() => !keep && void save({ keep_collecting: true }, ["不等了之後 → 繼續收"], "v:keep_collecting")}>
              繼續收
            </button>
            <button type="button" aria-pressed={!keep} onClick={() => keep && void save({ keep_collecting: false }, ["不等了之後 → 不收"], "v:keep_collecting")}>
              不收
            </button>
          </div>
          {reset("keep_collecting", "不等了之後")}
        </div>
      </div>
    </div>
  );
}

/* ================= 04 外觀與操作（存在這個瀏覽器） ================= */
function LookSection() {
  const [theme, setTheme] = useTheme();
  const [sendKey, setSendKey] = useSendKey();
  const touch = useTouch();
  return (
    <section className="sx" id="s-look">
      <div className="sx-h">
        <span className="no">04</span>
        <b>外觀與操作</b>
        <span className="la">This browser</span>
        <span className="aside">存在這個瀏覽器，不經過服務</span>
      </div>
      <div className="lk">
        <div className="lk-item">
          <div className="h">
            <b>主題</b>
            <small>頂欄右上也能切</small>
          </div>
          <div className="thm">
            {THEMES.map((t) => (
              <button
                key={t.id}
                type="button"
                aria-pressed={theme === t.id}
                onClick={() => {
                  if (theme === t.id) return;
                  setTheme(t.id);
                  logLocal([`主題 → ${t.short}`]);
                }}
              >
                <span className="sw3" data-theme={t.id} />
                <span>
                  <b>
                    {t.short} {t.name}
                  </b>
                  <small>{t.id === "d3" ? "雙色疊印、網點、墨框" : "明朝體、紙色、朱色點綴"}</small>
                </span>
              </button>
            ))}
          </div>
        </div>
        <div className="lk-item">
          <div className="h">
            <b>送出鍵</b>
            <small>聊天、派工、追問的輸入框</small>
          </div>
          {touch ? (
            <p>觸控裝置上沒有 Ctrl：一律 Enter 換行、按鈕送出。</p>
          ) : (
            <>
              <div className="sseg" role="group" aria-label="送出鍵">
                <button
                  type="button"
                  aria-pressed={sendKey === "enter"}
                  onClick={() => {
                    if (sendKey === "enter") return;
                    setSendKey("enter");
                    logLocal(["送出鍵 → Enter"]);
                  }}
                >
                  Enter 送出<small>Shift+Enter 換行</small>
                </button>
                <button
                  type="button"
                  aria-pressed={sendKey === "ctrl"}
                  onClick={() => {
                    if (sendKey === "ctrl") return;
                    setSendKey("ctrl");
                    logLocal(["送出鍵 → Ctrl+Enter"]);
                  }}
                >
                  Ctrl+Enter 送出<small>Enter 換行</small>
                </button>
              </div>
              <p>觸控裝置上沒有 Ctrl：一律 Enter 換行、按鈕送出。</p>
            </>
          )}
        </div>
      </div>
      <div className="local-note">
        <span className="srcst def">只在這裡</span>
        <span>桌面視窗與瀏覽器分頁各記一份（兩邊的儲存空間是分開的）。換一台電腦或清掉瀏覽器資料就回到預設：D3、Ctrl+Enter。</span>
      </div>
    </section>
  );
}

/* ================= 05 服務資訊 ================= */
function SvcSection() {
  const status = useBoard((s) => s.status);
  const tools = useSettings((s) => s.tools);
  const toolsBusy = useSettings((s) => s.toolsBusy);
  const toolsError = useSettings((s) => s.toolsError);
  const svc = status?.service;
  const copy = async (t: string, what: string) => {
    const ok = await copyText(t);
    logLocal([ok ? `已複製${what}的路徑` : `複製不了${what}的路徑（瀏覽器不允許），請手動選取`]);
  };
  const miss = tools?.summary.missing ?? 0;
  return (
    <section className="sx" id="s-svc">
      <div className="sx-h">
        <span className="no">05</span>
        <b>服務資訊</b>
        <span className="la">Service</span>
        <span className="aside">以顯示為主</span>
      </div>
      <div className="svc">
        <dl className="svc-dl">
          <dt>版本</dt>
          <dd>
            <span>
              <span className="code">v{svc?.version ?? status?.version ?? "—"}</span> · 服務 <span className="code">{status ? `${status.host}:${status.port}` : "—"}</span> · pid <span className="n">{status?.pid ?? "—"}</span>
            </span>
          </dd>
          {status?.desktop_update ? <UpdateRow u={status.desktop_update} /> : null}
          <AutostartRow />
          <dt>資料</dt>
          <dd>
            <span className="code">{svc?.data_home ?? status?.data_home ?? "—"}</span>
            <span className="x">
              資料庫、快取、log、<span className="code">settings.json</span> 都在這裡。
            </span>
            {svc?.data_home ? (
              <span className="acts">
                <button type="button" className="btn soft" onClick={() => void copy(svc.data_home, "資料夾")}>
                  複製路徑
                </button>
              </span>
            ) : null}
          </dd>
          <dt>.env</dt>
          <dd>
            {svc?.env_file ? (
              <>
                <span className="code">{svc.env_file}</span>
                <span className="x">有找到。設定頁的值會蓋過它，但不會改它。</span>
              </>
            ) : (
              <>
                <span className="x">沒有找到 .env 檔（不需要也可以：key 存在設定頁就好）。</span>
                {svc?.env_file_suggested ? (
                  <span className="x">
                    要用的話放在 <span className="code">{svc.env_file_suggested}</span>
                  </span>
                ) : null}
              </>
            )}
          </dd>
          <dt>作品資料夾</dt>
          <dd>
            <span className="code">{svc?.storage ?? status?.storage ?? "—"}</span>
            <span className="x">生圖、語音、音樂、轉錄的檔案都存在這裡。</span>
            {svc?.storage ? (
              <span className="acts">
                <button type="button" className="btn soft" onClick={() => void copy(svc.storage, "作品資料夾")}>
                  複製路徑
                </button>
              </span>
            ) : null}
            <span className="x">瀏覽器分頁打不開本機資料夾：複製路徑後貼到檔案總管的網址列。</span>
            <details>
              <summary>要換位置？</summary>
              <ol>
                <li>停掉服務（系統匣 › 結束，或 omni stop）。</li>
                <li>把舊資料夾整個搬過去。</li>
                <li>
                  在 <span className="code">.env</span> 寫 <span className="code">{svc?.storage_env ?? "STORAGE__BASE_PATH"}=新位置</span>。
                </li>
                <li>重新開啟。作品牆照舊找得到舊作品。</li>
              </ol>
            </details>
          </dd>
        </dl>
        <div>
          <div className="sx-lead" style={{ marginTop: 10 }}>
            <b>這台電腦上的外部工具</b>：少了不會壞，只是某些功能用不了。{miss ? (
              <>
                目前少 <b>{miss}</b> 個。
              </>
            ) : null}
          </div>
          {tools ? <ToolsList data={tools} /> : <p className="x-dim">{toolsError ? `偵測不了：${toolsError}` : "偵測中…"}</p>}
          <div className="tools-foot">
            <span>上次偵測 {tools ? hhmm(tools.checked_at) : "—"}</span>
            <span className="sp" />
            <button type="button" className="btn soft" onClick={() => void loadTools(true)} disabled={toolsBusy}>
              {toolsBusy ? "偵測中…" : "重新偵測"}
            </button>
          </div>
          <ClaudeConnect />
        </div>
      </div>
    </section>
  );
}

/** 桌面版的新版檢查（殼每天最多查一次公開 repo 的最新 Release；這裡只顯示結果）。
    沒有桌面版時整列不出現（呼叫端判斷）；有新版才給下載頁連結，下載後執行安裝包覆蓋即可 */
function UpdateRow({ u }: { u: DesktopUpdate }) {
  const checked = u.checked_at_ms ? hhmm(u.checked_at_ms / 1000) : null;
  let body: ReactNode;
  if (!u.enabled) {
    body = <span className="x">桌面版的新版檢查關著（殼的設定檔 update_check）。</span>;
  } else if (u.newer && u.url) {
    body = (
      <>
        <span>
          有新版 <span className="code">v{u.latest}</span>（現在 <span className="code">v{u.current}</span>）
        </span>
        <span className="acts">
          <a className="btn solid" href={u.url} target="_blank" rel="noreferrer">
            開啟下載頁
          </a>
        </span>
        <span className="x">下載新的安裝包、直接執行就好：它會先請服務收尾再換檔，設定與作品都不動。</span>
      </>
    );
  } else if (u.latest) {
    body = (
      <>
        <span>已是最新</span>
        <span className="x">
          公開的最新版 <span className="code">v{u.latest}</span>
          {checked ? ` · 上次檢查 ${checked}` : ""}
          {u.error ? " · 最近一次沒查成，之後會再試" : ""}
        </span>
      </>
    );
  } else {
    body = <span className="x">{u.error ? "還沒查成（連不上或被限流），之後會再試。" : "還沒查過：桌面版啟動一分鐘後才查，每天最多一次。"}</span>;
  }
  return (
    <>
      <dt>新版</dt>
      <dd>{body}</dd>
    </>
  );
}

/** 桌面版的「開機時啟動」：跟系統匣選單的勾是同一個 Windows 開機項目，兩邊改哪邊都算。
    不是桌面版起的服務（從 repo 或 zip 跑）整列不出現；點了就存，結果記進變更紀錄 */
function AutostartRow() {
  const a = useSettings((s) => s.autostart);
  const busy = useSettings((s) => s.autostartBusy);
  if (!a?.available) return null;
  return (
    <>
      <dt>開機時啟動</dt>
      <dd>
        <span className="acts">
          <span className={`sw${a.enabled ? "" : " is-off"}`} role="group" aria-label="開機時啟動">
            <button type="button" aria-pressed={a.enabled} disabled={busy} onClick={() => (!a.enabled || a.other) && void setAutostart(true)}>
              開
            </button>
            <button type="button" aria-pressed={!a.enabled} disabled={busy} onClick={() => a.enabled && void setAutostart(false)}>
              關
            </button>
          </span>
        </span>
        <span className="x">登入 Windows 時，桌面版在背景起來（只有系統匣與服務，不開視窗）。跟系統匣選單的「開機時啟動」是同一個開關。</span>
        {a.enabled && a.other ? (
          <span className="x">
            現在的開機項目指向另一份 OmniAPI：<span className="code">{a.other}</span>。再按一次「開」就改成這一份。
          </span>
        ) : null}
        {a.legacy ? <span className="x">提醒：{a.legacy}</span> : null}
      </dd>
    </>
  );
}

/** 「把 Claude Code 接上」：顯示現在的狀態；按了才寫；409 講清楚為什麼沒辦法自動接 */
function ClaudeConnect() {
  const c = useSettings((s) => s.claude);
  const busy = useSettings((s) => s.claudeBusy);
  const err = useSettings((s) => s.claudeError);
  if (!c) return null;
  const btn = (txt: string) => (
    <button type="button" className="btn solid" onClick={() => void connectClaude()} disabled={busy}>
      {busy ? "寫入中…" : txt}
    </button>
  );
  let body: ReactNode;
  if (c.state === "connected")
    body = (
      <>
        <span className="st ok">已接上</span>
        <p>
          Claude Code 的 MCP 設定（<span className="code">{c.name}</span>）指向這個服務 <span className="code">{c.expected.url}</span>。在 Claude Code 裡就能叫這裡的生圖、轉錄、聊天與派工。
        </p>
      </>
    );
  else if (c.state === "missing")
    body = (
      <>
        <span className="st none">還沒接上</span>
        <p>
          按下去會在 <span className="code">{c.path}</span> 加一項 <span className="code">{c.name}</span>（指向 <span className="code">{c.expected.url}</span>），檔案的其他內容不動。Claude Code 下次啟動時生效。
        </p>
        <div className="acts">{btn("把 Claude Code 接上")}</div>
      </>
    );
  else if (c.state === "other")
    body = (
      <>
        <span className="st off">指向別處</span>
        <p>
          <span className="code">{c.name}</span> 現在指向 <span className="code">{c.entry?.url ?? c.entry?.command ?? c.entry?.type ?? "?"}</span>，不是這個服務。改成指向 <span className="code">{c.expected.url}</span>，原本的那一項會先備份。
        </p>
        <div className="acts">{btn("改成指向這個服務")}</div>
      </>
    );
  else
    body = (
      <>
        <span className="st off">{c.state === "no_config" ? "沒辦法自動接" : "設定檔讀不懂"}</span>
        <p>{claudeWhy(c.state, c.path)}</p>
      </>
    );
  return (
    <div className="cmc" id="claude-mcp">
      <h5>
        把 Claude Code 接上<small>MCP</small>
      </h5>
      {body}
      {err ? (
        <div className="warn" role="alert">
          <b>沒有接上</b>：{claudeWhy(err.reason, c.path)}
        </div>
      ) : null}
    </div>
  );
}
function claudeWhy(reason: string, path: string): ReactNode {
  if (reason === "no_config")
    return (
      <>
        找不到 Claude Code 的設定檔 <span className="code">{path}</span>：Claude Code 好像還沒在這台電腦跑過。先開一次 Claude Code 再回來按；為了不弄壞東西，這裡不會自己建這個檔。
      </>
    );
  if (reason === "unreadable")
    return (
      <>
        <span className="code">{path}</span> 不是讀得懂的設定檔（可能被手動改壞了）。為了不弄壞它，這裡不會自動改；修好之後再按一次。
      </>
    );
  if (reason === "write_failed") return <>寫不進去（檔案被別的程式鎖住或沒有權限）。關掉 Claude Code 再試一次。</>;
  return <>服務回了錯誤（{reason}）。</>;
}
