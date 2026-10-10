import { Fragment, useDeferredValue, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import type { GenModel, ModelEntry, ModelsResponse, OrImageParams, SettingsView, Slot } from "@/api/types";
import { SLOTS } from "@/api/types";
import {
  allModels,
  CAP_ZH,
  CAPS,
  callable,
  capList,
  capsOf,
  daysLeft,
  EXTRA_CAPS,
  hasCap,
  indexById,
  isRetired,
  isSunset,
  MOD,
  MOD_ORDER,
  money,
  orLineLabel,
  orLineMoney,
  orLines,
  PRICE_LABEL,
  priceMain,
  slotOf,
  slotUsable,
  soonSunset,
  SUNSET_DAYS,
  UNIT_NOTE,
  usedBy,
  type Modality,
} from "@/lib/catalog";
import { harnessClass, harnessName } from "@/lib/format";
import { RankBadge, byPopularity } from "@/components/ModelFilterBar";
import { useTier } from "@/lib/rwd";
import { loadModels, useSettings } from "@/store/settings";
import { loadOptions, useMake } from "@/store/make";
import { audioPricedApart, durTxt, orderRatios, perSecondSpan, perTxt, sortRes, videoPrice, vparams, vUsd, type VideoPrice } from "@/components/make";

/* 模型頁 /models（1.3-M4，版面稿 models-*）：查表用，不是用來挑模型的。三欄：篩選｜名單｜詳情。
   資料：/api/models?include_retired=true（定價、狀態、下架日、能力——收 PDF、收音訊是 API 算出的布林）＋/api/settings（誰用到）。
   快下架的放最上面，先講「你的設定用到哪個」；已下架的收在名單最底下可展開；OpenRouter 現查的照 API 回來的顯示。 */

type ModF = Modality | "all";
interface Filt {
  mod: ModF;
  q: string;
  provs: Slot[];
  status: "live" | "sunset";
  caps: string[];
  usable: boolean;
  sort: "popular" | "provider" | "price" | "sunset" | "name";
}
const F0: Filt = { mod: "text", q: "", provs: [], status: "live", caps: [], usable: false, sort: "popular" };
const MAX_ROWS = 200;
/** 影片的能力字（audio 在文字模型是「收音訊」，影片是「有聲音」） */
const VIDEO_CAP_ZH: Record<string, string> = { first_frame: "收首幀", last_frame: "收尾幀", audio: "有聲音" };

function Wg({ m, cls = "sm" }: { m: Modality; cls?: string }) {
  return (
    <i className={`mk-wg ${MOD[m].k} ${cls}`} title={MOD[m].zh}>
      {MOD[m].g}
    </i>
  );
}

function SunStamp({ m }: { m: ModelEntry }) {
  if (isRetired(m))
    return (
      <span className="sun gone">
        已下架{m.shutdown ? <small>{m.shutdown}</small> : null}
      </span>
    );
  if (!isSunset(m)) {
    if (m.implemented === false) return <span className="tg def">這一版還沒接上</span>;
    return m.status === "discovered" ? <span className="tg def">未整理</span> : null;
  }
  if (!m.shutdown)
    return (
      <span className="sun nodate">
        快下架<small>未給日期</small>
      </span>
    );
  const d = daysLeft(m.shutdown);
  return (
    <span className={`sun${d <= SUNSET_DAYS ? " soon" : ""}`}>
      {d >= 0 ? `剩 ${d} 天` : "已過下架日"}
      <small>{m.shutdown} 下架</small>
    </span>
  );
}

function UsedTags({ settings, id, full }: { settings: SettingsView | null; id: string; full?: boolean }) {
  return (
    <>
      {usedBy(settings, id).map((u, i) => (
        <span key={i} className={`tg ${u.kind}`}>
          {full && u.via ? `${u.label}（經 ${u.via}）` : u.label}
        </span>
      ))}
    </>
  );
}

export default function ModelsPage() {
  const data = useSettings((s) => s.models);
  const err = useSettings((s) => s.modelsError);
  const settings = useSettings((s) => s.settings);
  const tier = useTier();
  const wide = tier === "xl" || tier === "l";
  const [f, setF] = useState<Filt>(F0);
  const [sel, setSel] = useState<string | null>(null);
  const [fopen, setFopen] = useState(false);
  const [goneOpen, setGoneOpen] = useState(false);
  const dq = useDeferredValue(f.q.trim().toLowerCase());
  useEffect(() => {
    void loadModels(true);
  }, []);
  const all = useMemo(() => allModels(data), [data]);
  const idx = useMemo(() => indexById(all), [all]);
  const provOrder = useMemo(() => Object.keys(data?.providers ?? {}), [data]);
  const label = (prov: string) => data?.providers?.[prov]?.label ?? prov;
  const uOf = (m: ModelEntry) => slotUsable(settings, slotOf(m.provider, data));
  const live = useMemo(() => all.filter((m) => !isRetired(m)), [all]);
  const upd = (p: Partial<Filt>) => setF((x) => ({ ...x, ...p }));

  const list = useMemo(() => {
    let l = all.filter((m) => f.mod === "all" || m.modality === f.mod);
    l = f.status === "sunset" ? l.filter(isSunset) : l.filter((m) => !isRetired(m));
    if (f.provs.length) l = l.filter((m) => f.provs.includes(slotOf(m.provider, data) as Slot));
    if (f.caps.length) l = l.filter((m) => f.caps.every((c) => hasCap(m, c)));
    if (f.usable) l = l.filter((m) => callable(uOf(m)));
    if (dq) l = l.filter((m) => `${m.id} ${m.name ?? ""} ${label(m.provider)} ${m.vendor_label ?? ""}`.toLowerCase().includes(dq));
    const pr = (m: ModelEntry) => provOrder.indexOf(m.provider);
    const st = (m: ModelEntry) => (m.status === "current" ? 0 : m.status === "deprecated" ? 1 : 2);
    if (f.sort === "price") l = [...l].sort((a, b) => (priceMain(a)?.key ?? 1e9) - (priceMain(b)?.key ?? 1e9));
    else if (f.sort === "sunset") l = [...l].sort((a, b) => (a.shutdown || "9999").localeCompare(b.shutdown || "9999") || st(a) - st(b));
    else if (f.sort === "name") l = [...l].sort((a, b) => a.id.localeCompare(b.id));
    // 熱門：Artificial Analysis 各榜的名次（沒上榜的照供應商、狀態排在後面）
    else if (f.sort === "popular") l = [...l].sort((a, b) => byPopularity(a, b) || pr(a) - pr(b) || st(a) - st(b));
    else l = [...l].sort((a, b) => pr(a) - pr(b) || st(a) - st(b));
    return l;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, f, dq, settings, data]);

  // 寬版：沒選的時候詳情欄放名單第一個；中寬與手機：點了才在那一列底下展開
  // （稿：文字一進來先看 cheap 現在指到的那個——最常被問「現在用的是哪個、多少錢」）
  const cheapId = settings?.tiers.cheap?.model;
  const selected = sel ? idx.get(sel) ?? null : wide ? list.find((m) => m.id === cheapId) ?? list[0] ?? null : null;

  if (!data)
    return (
      <div className="smp wrap">
        <ModelsHead data={null} />
        <p className="x-dim">{err ? `讀不到模型清單：${err}` : "讀取模型清單…"}</p>
      </div>
    );

  const soon = live.filter(soonSunset).sort((a, b) => a.shutdown!.localeCompare(b.shutdown!));
  const mine = all.filter((m) => (isSunset(m) || isRetired(m)) && usedBy(settings, m.id).length);
  const nearest = soon.length ? daysLeft(soon[0].shutdown!) : null;
  const cnt = (mod: ModF) => (mod === "all" ? live : live.filter((m) => m.modality === mod)).length;
  const onlySunset = () => {
    setF((x) => ({ ...x, status: "sunset", mod: "all", sort: "sunset" }));
    setSel(null);
  };
  const activeCount = f.provs.length + f.caps.length + (f.usable ? 1 : 0) + (f.status !== "live" ? 1 : 0);
  const pick = (id: string) => {
    const m = idx.get(id);
    if (m && f.mod !== "all" && m.modality !== f.mod) upd({ mod: m.modality as Modality });
    if (m && isRetired(m)) setGoneOpen(true);
    setSel((cur) => (cur === id && !wide ? null : id));
  };

  const filterGroups = (
    <FilterGroups f={f} upd={upd} all={all} data={data} settings={settings} />
  );

  return (
    <div className="smp wrap">
      <ModelsHead data={data} />
      {soon.length || mine.length ? (
        <div className="mp-alert" role="status">
          <span className="big">
            {nearest ?? "—"}
            <small>天</small>
          </span>
          <div className="body">
            <b className="t">快下架</b>
            {mine.length ? (
              <div className="mine">
                <b>你的設定用到 {mine.length} 個：</b>
                {mine.map((m, i) => (
                  <span key={m.id}>
                    {i ? "；" : null}
                    <UsedTags settings={settings} id={m.id} /> <span className="code">{m.id}</span>
                    {m.shutdown ? `（${m.shutdown}）` : ""}
                    {m.replacement ? (
                      <>
                        {" → "}
                        <span className="code">{m.replacement}</span>
                      </>
                    ) : null}
                  </span>
                ))}
              </div>
            ) : (
              <div>你的設定沒有用到快下架的模型。</div>
            )}
            <div className="x-dim">
              目錄裡共 <b>{soon.length}</b> 個在 {SUNSET_DAYS} 天內下架
              {soon.length ? (
                <>
                  ：
                  {soon.slice(0, 4).map((m, i) => (
                    <Fragment key={m.id}>
                      {i ? "、" : null}
                      <span className="code">{m.id}</span>
                    </Fragment>
                  ))}
                  {soon.length > 4 ? " 等" : ""}
                </>
              ) : null}
              。
            </div>
          </div>
          <div className="acts">
            {mine.length ? (
              <Link className="btn solid" to="/settings?sec=s-defs">
                去設定換掉
              </Link>
            ) : null}
            <button type="button" className="btn" onClick={onlySunset}>
              只看快下架
            </button>
          </div>
        </div>
      ) : null}
      <div className="mp-mods" role="group" aria-label="模態">
        <button type="button" className="all" aria-pressed={f.mod === "all"} onClick={() => upd({ mod: "all", caps: [] })}>
          <b>全部</b>
          <span className="n">{cnt("all")}</span>
        </button>
        {MOD_ORDER.map((k) => (
          <button key={k} type="button" aria-pressed={f.mod === k} onClick={() => upd({ mod: k, caps: [] })}>
            <Wg m={k} cls="" />
            <b>{MOD[k].zh}</b>
            <span className="n">{cnt(k)}</span>
          </button>
        ))}
      </div>
      <div className="mp-grid">
        <aside className="mf-rail" aria-label="篩選">
          {filterGroups}
        </aside>
        <div className="mp-list">
          <div className="ml-bar">
            <label className="q">
              <span>搜</span>
              <input type="search" placeholder="id、名稱、供應商" value={f.q} onChange={(e) => upd({ q: e.target.value })} aria-label="搜尋模型" />
            </label>
            <select aria-label="排序" value={f.sort} onChange={(e) => upd({ sort: e.target.value as Filt["sort"] })}>
              <option value="popular">熱門（AA 名次）</option>
              <option value="provider">依供應商</option>
              <option value="price">價格 低→高</option>
              <option value="sunset">下架日 近→遠</option>
              <option value="name">依 id</option>
            </select>
            <button type="button" className="btn fbtn" aria-expanded={fopen} onClick={() => setFopen((v) => !v)}>
              篩選{activeCount ? ` · ${activeCount}` : ""} ▾
            </button>
            <span className="cnt">
              <span className="n">{list.length}</span> 個
            </span>
          </div>
          {fopen && !wide ? <div className="mf-inline">{filterGroups}</div> : null}
          <ModelList
            f={f}
            upd={upd}
            setF={setF}
            list={list}
            all={all}
            data={data}
            settings={settings}
            selected={selected}
            wide={wide}
            pick={pick}
            goneOpen={goneOpen}
            setGoneOpen={setGoneOpen}
          />
        </div>
        <div className="mp-detail">{wide && selected ? <Detail m={selected} data={data} settings={settings} pick={pick} inline={false} /> : null}</div>
      </div>
    </div>
  );
}

function ModelsHead({ data }: { data: ModelsResponse | null }) {
  return (
    <div className="sechead sm-head">
      <h2 className="ovp" data-t="Models">
        Models
      </h2>
      <span className="zh">模型</span>
      <span className="sub">查價格、狀態、能力。要用哪個，在派工頁、生成頁挑</span>
      {data ? (
        <span className="aside x-s">
          目錄 <span className="code">{data.updated ?? "—"}</span> · 整理過的 <span className="n">{allModels(data).filter((m) => m.status !== "discovered").length}</span> 個 · 另外現查
        </span>
      ) : null}
    </div>
  );
}

function FilterGroups({ f, upd, all, data, settings }: { f: Filt; upd: (p: Partial<Filt>) => void; all: ModelEntry[]; data: ModelsResponse; settings: SettingsView | null }) {
  const base = all.filter((m) => f.mod === "all" || m.modality === f.mod);
  const capDefs = f.mod === "all" ? [] : [...CAPS[f.mod].map(([k, g, zh]) => [k, zh, g] as [string, string, string]), ...(EXTRA_CAPS[f.mod] ?? []).map(([k, zh]) => [k, zh, ""] as [string, string, string])];
  const toggle = <T,>(arr: T[], v: T) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  const slots = SLOTS.filter((s) => base.some((m) => slotOf(m.provider, data) === s) || (s === "openrouter" && (f.mod === "all" || f.mod === "text")));
  return (
    <>
      <div className="mf-g">
        <h5>
          狀態<small>STATUS</small>
        </h5>
        <button type="button" className="ck2" aria-pressed={f.status === "live"} onClick={() => upd({ status: "live", sort: f.sort === "sunset" ? "popular" : f.sort })}>
          <span>現行＋快下架</span>
          <span className="n">{base.filter((m) => !isRetired(m)).length}</span>
        </button>
        <button type="button" className="ck2" aria-pressed={f.status === "sunset"} onClick={() => upd({ status: "sunset", sort: "sunset" })}>
          <span>只看快下架</span>
          <span className="n">{base.filter(isSunset).length}</span>
          <span className="sub">有下架日的照日期排</span>
        </button>
        <div className="mf-note" style={{ marginTop: 6 }}>
          已下架的 <span className="n">{base.filter(isRetired).length}</span> 個收在清單最下面。
        </div>
      </div>
      <div className="mf-g">
        <h5>
          供應商<small>PROVIDER</small>
        </h5>
        {slots.map((s) => {
          const u = slotUsable(settings, s);
          const n = base.filter((m) => slotOf(m.provider, data) === s && !isRetired(m)).length;
          const L = settings?.providers[s]?.label ?? s;
          return (
            <button key={s} type="button" className="ck2" aria-pressed={f.provs.includes(s)} onClick={() => upd({ provs: toggle(f.provs, s) })}>
              <span>{L}</span>
              <span className="n">{s === "openrouter" && !n ? "現查" : n}</span>
              {!callable(u) ? (
                <span className="sub">
                  {u === "off" ? "停用中" : u === "none" ? "沒有 key" : "key 不通"} ·{" "}
                  <Link to={`/settings?p=${s}`} onClick={(e) => e.stopPropagation()}>
                    去設定
                  </Link>
                </span>
              ) : null}
            </button>
          );
        })}
        <button type="button" className="ck2" aria-pressed={f.usable} onClick={() => upd({ usable: !f.usable })} style={{ marginTop: 6 }}>
          <span>
            <b>只看現在能用的</b>
          </span>
          <span className="n" />
        </button>
      </div>
      {capDefs.length ? (
        <div className="mf-g">
          <h5>
            能力<small>CAN</small>
          </h5>
          {capDefs.map(([k, zh, g]) => (
            <button key={k} type="button" className="ck2" aria-pressed={f.caps.includes(k)} onClick={() => upd({ caps: toggle(f.caps, k) })}>
              <span>
                {g ? (
                  <span className="capg" style={{ marginRight: 6 }}>
                    {g}
                  </span>
                ) : null}
                {zh}
              </span>
              <span className="n">{base.filter((m) => !isRetired(m) && hasCap(m, k)).length}</span>
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

function ModelList(props: {
  f: Filt;
  upd: (p: Partial<Filt>) => void;
  setF: (f: Filt) => void;
  list: ModelEntry[];
  all: ModelEntry[];
  data: ModelsResponse;
  settings: SettingsView | null;
  selected: ModelEntry | null;
  wide: boolean;
  pick: (id: string) => void;
  goneOpen: boolean;
  setGoneOpen: (v: boolean) => void;
}) {
  const { f, upd, setF, list, all, data, settings, selected, wide, pick, goneOpen, setGoneOpen } = props;
  const label = (prov: string) => data.providers?.[prov]?.label ?? prov;
  const caps = f.mod === "all" ? [] : CAPS[f.mod];
  const chips: [string, () => void][] = [];
  if (f.status === "sunset") chips.push(["只看快下架", () => upd({ status: "live", sort: f.sort === "sunset" ? "popular" : f.sort })]);
  f.provs.forEach((s) => chips.push([settings?.providers[s]?.label ?? s, () => upd({ provs: f.provs.filter((x) => x !== s) })]));
  f.caps.forEach((c) => chips.push([(f.mod === "video" ? VIDEO_CAP_ZH[c] : undefined) ?? CAP_ZH[c] ?? c, () => upd({ caps: f.caps.filter((x) => x !== c) })]));
  if (f.usable) chips.push(["只看能用的", () => upd({ usable: false })]);
  if (f.q) chips.push([`搜「${f.q}」`, () => upd({ q: "" })]);

  const row = (m: ModelEntry) => {
    const u = slotUsable(settings, slotOf(m.provider, data));
    const pr = priceMain(m);
    const on = selected?.id === m.id;
    const cls = ["mr", !callable(u) ? "is-blocked" : "", isSunset(m) ? "is-sunset" : "", isRetired(m) ? "is-gone" : ""].filter(Boolean).join(" ");
    const cs = capsOf(m.modality);
    return (
      <Fragment key={`${m.provider}/${m.id}`}>
        <button type="button" className={cls} aria-current={on} onClick={() => pick(m.id)} data-model={m.id}>
          <span className="mr-id">
            <span className="mf-id">
              <span className="code">{m.id}</span>
              <RankBadge m={m} />
            </span>
            <small>
              {m.vendor_label ? `${m.vendor_label} · ` : ""}
              {m.name ?? ""}
              {f.mod === "all" ? ` · ${MOD[m.modality as Modality]?.zh ?? m.modality}` : ""}
              {f.sort !== "provider" ? ` · ${label(m.provider)}` : ""}
            </small>
          </span>
          <span className="mr-tags">
            <UsedTags settings={settings} id={m.id} />
          </span>
          <span className="mr-caps">
            {cs.map(([k, g, zh], i) => {
              // 影片的聲音：名單沒標（null）畫虛線「?」，不當成「沒有」
              const unk = m.modality === "video" && (m.capabilities ?? {})[k] === null;
              return (
                <span key={k} className={`capg${unk ? " unk" : hasCap(m, k) ? "" : " off"}${i === 2 ? " x-l" : ""}`} title={`${zh}${unk ? "：名單沒標" : hasCap(m, k) ? "" : "：沒有"}`}>
                  {unk ? "?" : g}
                </span>
              );
            })}
          </span>
          <span className={`mr-price${pr ? "" : " none"}`}>
            {pr ? (
              <>
                {pr.main}
                <small>{pr.sub}</small>
              </>
            ) : (
              "未定價"
            )}
          </span>
          <span className="mr-st">
            <SunStamp m={m} />
          </span>
        </button>
        {on && !wide ? (
          <div className="md-inline">
            <Detail m={m} data={data} settings={settings} pick={pick} inline />
          </div>
        ) : null}
      </Fragment>
    );
  };

  let body: ReactNode;
  const rows = list.slice(0, MAX_ROWS);
  if (!list.length) body = <div className="ml-empty">沒有符合的模型。{chips.length ? "試著拿掉一個篩選。" : ""}</div>;
  else if (f.sort === "provider") {
    const groups: [string, ModelEntry[]][] = [];
    for (const m of rows) {
      let g = groups.find((x) => x[0] === m.provider);
      if (!g) groups.push((g = [m.provider, []]));
      g[1].push(m);
    }
    body = groups.map(([p, ms]) => (
      <Fragment key={p}>
        <GroupHead prov={p} n={list.filter((m) => m.provider === p).length} data={data} settings={settings} />
        {ms.map(row)}
      </Fragment>
    ));
  } else body = rows.map(row);

  const gone = f.status === "live" ? all.filter((m) => isRetired(m) && (f.mod === "all" || m.modality === f.mod) && (!f.provs.length || f.provs.includes(slotOf(m.provider, data) as Slot))) : [];
  const orSlot = settings?.providers.openrouter;
  const orVideo = f.mod === "video";
  const orImage = f.mod === "image";
  const orModels = all.filter((m) => m.provider === "openrouter" && !isRetired(m) && (!orImage || m.modality === "image")).length;
  const showOr = (f.mod === "all" || f.mod === "text" || orImage) && f.status === "live" && (!f.provs.length || f.provs.includes("openrouter"));
  return (
    <>
      {chips.length ? (
        <div className="ml-chips">
          {chips.map(([t, off], i) => (
            <button key={i} type="button" className="fc" onClick={off}>
              {t}
              <i aria-label="拿掉">×</i>
            </button>
          ))}
          <button type="button" className="lnk" onClick={() => setF({ ...F0, mod: f.mod })}>
            全部清掉
          </button>
        </div>
      ) : null}
      <div className="ml-legend">
        <span>模型</span>
        <span>設定用到</span>
        <span>{caps.length ? caps.map((c) => c[1]).join(" ") : "能力"}</span>
        <span className="r">價格 USD</span>
        <span className="r">狀態</span>
      </div>
      <div className="ml-unit">
        價格 USD · {f.mod === "text" ? "每 1M token，輸入／輸出" : "單位各模型不同，見詳情"}
        {caps.length ? ` · ${caps.map((c) => `${c[1]}＝${c[2]}`).join("、")}` : ""}
      </div>
      {body}
      {list.length > MAX_ROWS ? (
        <div className="ml-empty">
          還有 <span className="n">{list.length - MAX_ROWS}</span> 個沒列出，打字或選一家縮小範圍
        </div>
      ) : null}
      {gone.length ? (
        <details className="ml-retired" open={goneOpen} onToggle={(e) => setGoneOpen((e.target as HTMLDetailsElement).open)}>
          <summary>
            <b>已下架 {gone.length} 個</b>
            <span>呼叫會失敗。留著是為了查得到舊紀錄用的是什麼，以及該換成哪個。</span>
          </summary>
          {goneOpen ? gone.map(row) : null}
        </details>
      ) : null}
      {orVideo ? <VideoLive orKey={!!orSlot?.key.set} n={all.filter((m) => m.modality === "video" && m.provider === "openrouter" && !isRetired(m)).length} /> : null}
      {showOr && orSlot ? (
        <div className="ml-live">
          <b>OpenRouter</b>
          {!orSlot.key.set ? (
            <>
              <span className="st none">沒有 key</span>
              <span>
                {orImage
                  ? "FLUX、Seedream、Grok Imagine、Qwen、Recraft 這些圖片模型經 OpenRouter 用，貼了 key 之後現查（名單與定價每天更新）。"
                  : "OpenRouter 的模型不在目錄裡，是貼了 key 之後現查的（通常幾百個，價格照 OpenRouter 回的）。現查到的會排在名單裡，標「未整理」。"}
              </span>
              <Link className="lnk" to="/settings?p=openrouter">
                去設定貼 key →
              </Link>
            </>
          ) : orModels ? (
            orImage ? (
              <span>
                現查到 <span className="n">{orModels}</span> 個圖片模型，排在上面 OpenRouter 那一段（名單與定價每天更新）。已經直連的 OpenAI、Google 不重複列出 OpenRouter 那一份。
              </span>
            ) : (
              <span>
                現查到 <span className="n">{orModels}</span> 個，排在上面 OpenRouter 那一段，標「未整理」。
              </span>
            )
          ) : (
            <>
              <span className="st unk">還沒查到</span>
              <span>有 key 了，但名單還沒回來（背景還在查、離線，或 key 不通）。</span>
              <Link className="lnk" to="/settings?p=openrouter">
                去設定看看 →
              </Link>
            </>
          )}
        </div>
      ) : null}
    </>
  );
}

/** 影片一類的底下：名單從哪來、不是生影片的那幾個這一版不列（名字取自生成頁的 options） */
function VideoLive({ orKey, n }: { orKey: boolean; n: number }) {
  const unlisted = useMake((s) => s.options?.kinds.video?.unlisted ?? null);
  const sandbox = useMake((s) => s.options?.sandbox ?? false);
  useEffect(() => {
    void loadOptions();
  }, []);
  return (
    <div className="ml-live">
      <b>影片</b>
      {!orKey && !sandbox ? (
        <>
          <span className="st none">沒有 key</span>
          <span>影片這一版都經 OpenRouter 生：貼了 OpenRouter 的 key 之後現查名單與每秒價格（每天更新）。</span>
          <Link className="lnk" to="/settings?p=openrouter">
            去設定貼 key →
          </Link>
        </>
      ) : (
        <span>
          {sandbox ? "離線沙盒的示範名單" : "OpenRouter 名單現查"}：{n} 個生影片的模型。原廠已關閉或公告下架的標在右邊；名單還在的照列，好查得到。
          {unlisted?.length ? (
            <>
              {` 不是生影片的 ${unlisted.length} 個（`}
              {unlisted.map((u, i) => (
                <Fragment key={u.id}>
                  {i ? "、" : ""}
                  <span className="code">{u.id}</span>
                </Fragment>
              ))}
              ）是影片編輯、放大、數位人，這一版不列。
            </>
          ) : null}
        </span>
      )}
    </div>
  );
}

function GroupHead({ prov, n, data, settings }: { prov: string; n: number; data: ModelsResponse; settings: SettingsView | null }) {
  const slot = slotOf(prov, data);
  const u = slotUsable(settings, slot);
  const p = data.providers?.[prov];
  const h = p?.harness ? String(p.harness) : null;
  let st: ReactNode;
  if (u === "ok" || u === "untested") st = <span className="st ok">能用</span>;
  else if (u === "off")
    st = (
      <>
        <span className="st off">停用中</span>
        <Link className="lnk" to={`/settings?p=${slot}`}>
          去設定開回來 →
        </Link>
      </>
    );
  else if (u === "none")
    st = (
      <>
        <span className="st none">沒有 key，這些現在用不了</span>
        <Link className="lnk" to={`/settings?p=${slot}`}>
          去設定貼 key →
        </Link>
      </>
    );
  else
    st = (
      <>
        <span className="st error">key 不通</span>
        <Link className="lnk" to={`/settings?p=${slot}`}>
          去設定 →
        </Link>
      </>
    );
  return (
    <div className={`mg${callable(u) ? "" : " is-blocked"}`} data-group={prov}>
      <b>{p?.label ?? prov}</b>
      <span className="n">{n} 個</span>
      {h ? (
        <span className={`hm ${harnessClass(h)}`} style={{ fontSize: "var(--fs-m11)" }}>
          派工走 {harnessName(h)}
        </span>
      ) : null}
      <span className="gst">{st}</span>
    </div>
  );
}

function Detail({ m, data, settings, pick, inline }: { m: ModelEntry; data: ModelsResponse; settings: SettingsView | null; pick: (id: string) => void; inline: boolean }) {
  const slot = slotOf(m.provider, data);
  const u = slotUsable(settings, slot);
  const prov = data.providers?.[m.provider];
  const h = m.modality === "text" && prov?.harness ? String(prov.harness) : null;
  const used = usedBy(settings, m.id);
  const p = (m.pricing ?? {}) as Record<string, unknown>;
  const unit = typeof p.unit === "string" ? UNIT_NOTE[p.unit] ?? p.unit : p.input != null ? "每 1M token" : "";
  const rows = Object.entries(p).filter(([, v]) => typeof v === "number") as [string, number][];
  const lc = p.long_context as { input?: number; output?: number } | undefined;
  const peak = p.peak as { multiplier?: number; utc_hours?: [number, number][] } | undefined;
  const pnote = [typeof p.note === "string" ? p.note : "", peak?.utc_hours ? `尖峰：UTC ${peak.utc_hours.map(([a, b]) => `${a}–${b} 點`).join("、")}` : ""].filter(Boolean).join("；");
  const caps = capList(m);
  const L = slot ? settings?.providers[slot]?.label ?? slot : m.provider;
  const [copied, setCopied] = useState(false);
  return (
    <div className="md-pane" aria-label="模型詳情">
      <div className="hd">
        <div className="row">
          <span className="k10">
            {m.vendor_label ? `經 ${prov?.label ?? m.provider} · 原廠 ${m.vendor_label}` : prov?.label ?? m.provider} · {MOD[m.modality as Modality]?.zh ?? m.modality}
          </span>
          {h ? (
            <span className={`hm ${harnessClass(h)}`} style={{ fontSize: "var(--fs-m11)" }}>
              {harnessName(h)}
            </span>
          ) : null}
          {inline ? (
            <button type="button" className="btn soft md-close" onClick={() => pick(m.id)}>
              收起
            </button>
          ) : null}
        </div>
        <h3>{m.id}</h3>
        <span className="nm">{m.name ?? ""}</span>
        {m.aliases?.length ? (
          <span className="x-dim" style={{ fontSize: "var(--fs-m11)" }}>
            別名{" "}
            {m.aliases.map((a, i) => (
              <Fragment key={a}>
                {i ? "、" : null}
                <span className="code">{a}</span>
              </Fragment>
            ))}
          </span>
        ) : null}
      </div>
      {isSunset(m) || isRetired(m) ? (
        <div className="md-sun">
          <div>
            <b className="t">{isRetired(m) ? `${m.shutdown ? m.shutdown + " " : ""}已下架` : m.shutdown ? `${m.shutdown} 下架 · ${daysLeft(m.shutdown) >= 0 ? `剩 ${daysLeft(m.shutdown)} 天` : "已過下架日"}` : "官方已公告淘汰，未給日期"}</b>
            {isRetired(m) ? "呼叫會失敗。留在名單裡，是為了查得到舊紀錄用的是什麼。" : "到期之後呼叫會失敗。"}
            {m.replacement ? (
              <>
                {" "}
                官方建議改用 <span className="code">{m.replacement}</span>。
              </>
            ) : null}
            {used.length ? (
              <div style={{ marginTop: 4 }}>
                <b>你的設定用到它：</b>
                <UsedTags settings={settings} id={m.id} full />
              </div>
            ) : null}
            <div className="acts">
              {m.replacement && data && allModels(data).some((x) => x.id === m.replacement) ? (
                <button type="button" className="btn" onClick={() => pick(m.replacement!)}>
                  看 {m.replacement}
                </button>
              ) : null}
              {used.length ? (
                <Link className="btn solid" to="/settings?sec=s-defs">
                  去設定換掉
                </Link>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
      <div className="body">
        {!callable(u) && slot ? (
          <div className="sec">
            <div className="blocked">
              <b>{u === "off" ? `${L} 停用中` : u === "none" ? `${L} 沒有 key` : `${L} 的 key 不通`}</b>
              <br />
              這個模型現在叫不動。
              <Link className="lnk" to={`/settings?p=${slot}`}>
                {u === "off" ? "去設定開回來" : "去設定貼 key"} →
              </Link>
            </div>
          </div>
        ) : null}
        <div className="sec">
          <span className="k10">價格 · USD {unit}</span>
          {p.unit === "openrouter" ? (
            <OrPricing p={p} />
          ) : p.unit === "openrouter_video" ? (
            <OrVideoPricing m={m} />
          ) : rows.length ? (
            <>
              <table className="ptab">
                <tbody>
                  {rows.map(([k, v]) => (
                    <tr key={k}>
                      <td>{PRICE_LABEL[k] ?? k}</td>
                      <td className="v">{money(v)}</td>
                    </tr>
                  ))}
                  {lc?.input != null && lc.output != null ? (
                    <tr>
                      <td>長上下文 入／出</td>
                      <td className="v">
                        {money(lc.input)} / {money(lc.output)}
                      </td>
                    </tr>
                  ) : null}
                  {peak?.multiplier ? (
                    <tr>
                      <td>尖峰時段</td>
                      <td className="v">×{peak.multiplier}</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
              {pnote ? (
                <div className="x-dim" style={{ fontSize: "var(--fs-m11)", marginTop: 4 }}>
                  {pnote}
                </div>
              ) : null}
            </>
          ) : (
            <span className="nil">目錄沒有定價</span>
          )}
        </div>
        {m.image_params ? <OrParams ip={m.image_params} /> : null}
        {m.modality === "video" && m.video_params ? <OrVideoParams m={m} /> : null}
        <div className="sec">
          <span className="k10">能力</span>
          <div className="capl">
            {caps.length ? caps.map((c) => <span key={c}>{(m.modality === "video" ? VIDEO_CAP_ZH[c] : undefined) ?? CAP_ZH[c] ?? c}</span>) : <span className="x-dim" style={{ border: 0, padding: 0 }}>目錄沒記</span>}
          </div>
          {typeof m.context === "number" ? (
            <div className="x-dim" style={{ fontSize: "var(--fs-m11)", marginTop: 6 }}>
              上下文 <span className="n">{m.context.toLocaleString()}</span> token
              {typeof m.max_output === "number" ? (
                <>
                  {" "}
                  · 最多輸出 <span className="n">{(m.max_output as number).toLocaleString()}</span>
                </>
              ) : null}
            </div>
          ) : null}
        </div>
        <div className="sec">
          <span className="k10">用在哪</span>
          <div className="used">{used.length ? <UsedTags settings={settings} id={m.id} full /> : <span className="x-dim">設定裡沒有用到它</span>}</div>
        </div>
        <div className="sec">
          <span className="k10">動作</span>
          <div className="acts">
            <button
              type="button"
              className="btn"
              onClick={() => {
                void navigator.clipboard?.writeText(m.id).then(
                  () => setCopied(true),
                  () => setCopied(false),
                );
              }}
            >
              {copied ? "已複製" : "複製 id"}
            </button>
            <Link className="btn soft" to={m.modality === "text" ? "/settings?sec=s-tiers" : "/settings?sec=s-defs"}>
              設成等級或預設 →
            </Link>
          </div>
          {m.note ? (
            <div className="x-dim" style={{ fontSize: "var(--fs-m11)", marginTop: 6 }}>
              目錄附註：{m.note}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/** OpenRouter 圖片模型的定價：名單上的每一條照原樣列（輸出依解析度／品質分級、參考圖另計） */
function OrPricing({ p }: { p: Record<string, unknown> }) {
  const lines = orLines(p);
  if (!lines.length) return <span className="nil">OpenRouter 沒有公布這個模型的定價；生了之後帳上記實際費用</span>;
  return (
    <>
      <table className="ptab">
        <tbody>
          {lines.map((l, i) => (
            <tr key={i}>
              <td>{orLineLabel(l)}</td>
              <td className="v">{orLineMoney(l)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="x-dim" style={{ fontSize: "var(--fs-m11)", marginTop: 4 }}>
        {p.stale ? "這次沒查到，沿用上次查到的定價。" : ""}帳上記的是 OpenRouter 回報的實際費用。
      </div>
    </>
  );
}

/** OpenRouter 影片模型的定價：每種解析度的每秒價，與一支 5 秒、最長一支約多少（名單算的，實際以帳單為準） */
function OrVideoPricing({ m }: { m: ModelEntry }) {
  const gm = m as unknown as GenModel;
  const vp = vparams(gm);
  const res = sortRes(vp.resolutions);
  const longest = vp.durations.length ? Math.max(...vp.durations) : null;
  const five = vp.durations.includes(5) ? 5 : vp.durations.find((d) => d > 5) ?? longest;
  const audio = vp.audio === true ? true : vp.audio;
  const at = (r: string | null, s: number | null, a: boolean | null = audio) => videoPrice(gm.pricing?.skus, { seconds: s, resolution: r, audio: a, firstFrame: false, frames: 0 });
  const span = perSecondSpan(gm);
  if (!span) {
    const t = Object.entries((gm.pricing?.skus ?? {}) as Record<string, unknown>).find(([k]) => k.startsWith("video_tokens"));
    return t ? (
      <span className="nil">
        按 token 計價（每 token <span className="code">${Number(t[1]).toFixed(7).replace(/0+$/, "")}</span>）：每秒用多少 token 名單上沒寫，送出前算不出；做過幾支之後，生成頁會拿過去的實際花費當參考。
      </span>
    ) : (
      <span className="nil">OpenRouter 沒有公布這個模型的定價；生了之後帳上記實際費用</span>
    );
  }
  const cell = (p: VideoPrice) => (p.kind === "exact" ? vUsd(p.usd) : p.kind === "range" ? `${vUsd(p.low)}–${vUsd(p.high)}` : "—");
  const rows = (res.length ? res : [null]).map((r) => ({ r, per: at(r, 1), five: at(r, five), long: at(r, longest) }));
  const apart = audioPricedApart(gm);
  return (
    <>
      <table className="vd-ptab">
        <thead>
          <tr>
            <th>解析度</th>
            <th className="r">每秒</th>
            <th className="r">{five ? `${five} 秒` : "一支"}</th>
            <th className="r">{longest ? `最長 ${longest} 秒` : "最長"}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x.r ?? "-"}>
              <td>{x.r ?? "照模型預設"}</td>
              <td className="r">{x.per.kind === "exact" ? perTxt(x.per.usd) : cell(x.per)}</td>
              <td className="r">{cell(x.five)}</td>
              <td className="r">{cell(x.long)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="x-dim" style={{ fontSize: "var(--fs-m11)", marginTop: 6 }}>
        {apart ? `上表是有聲音的價；關掉聲音較便宜（每秒 ${perTxt(span.low)} 起）。` : vp.audio === true ? "有沒有聲音同價。" : ""}
        {vp.frames.length ? "給首幀（圖生影片）時有的模型另有價，生成頁的預估會算進去。" : ""}
        {gm.pricing?.stale ? "這次沒查到，沿用上次查到的定價。" : ""}
        預估照名單；帳上記 OpenRouter 回報的實際費用（實測過比名單低的情形）。
      </div>
    </>
  );
}

/** OpenRouter 影片模型收什麼（名單上寫的） */
function OrVideoParams({ m }: { m: ModelEntry }) {
  const vp = vparams(m as unknown as GenModel);
  const frames = vp.frames.includes("first_frame") ? (vp.frames.includes("last_frame") ? "首幀、尾幀都收" : "只收首幀") : vp.frames.includes("last_frame") ? "只收尾幀" : "不收（只做文生影片）";
  const rows: [string, string][] = [
    ["秒數", vp.durations.length ? durTxt(vp) : "名單沒寫"],
    ["解析度", vp.resolutions.length ? sortRes(vp.resolutions).join("、") : "名單沒寫"],
    ["比例", vp.aspect_ratios.length ? orderRatios(vp.aspect_ratios).join("、") : "名單沒寫"],
    ["首尾幀", frames],
    ["聲音", vp.audio === true ? (vp.audio_fixed ? "有（一定有，不能關）" : "有（可以關掉）") : vp.audio === false ? "沒有聲音" : "名單沒標：做出來才知道"],
  ];
  const direct = m.provider !== "openrouter";
  return (
    <div className="sec">
      <span className="k10">支援範圍 · {direct ? "照原廠文件" : "照 OpenRouter 名單"}</span>
      <table className="ptab">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td style={{ whiteSpace: "nowrap" }}>{k}</td>
              <td className="v">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** OpenRouter 圖片模型收的參數（名單上寫的） */
function OrParams({ ip }: { ip: OrImageParams }) {
  const refs = ip.max_references === 0 ? "不收（只能生成）" : ip.min_references > 0 ? `${ip.min_references}–${ip.max_references} 張（一定要給）` : `最多 ${ip.max_references} 張`;
  const rows: [string, string][] = [
    ["解析度", ip.resolutions.length ? ip.resolutions.join("、") : "不能選"],
    ["比例", ip.aspect_ratios.length ? ip.aspect_ratios.filter((r) => r !== "auto").join("、") : "不能選"],
    ...(ip.qualities.length ? ([["品質", ip.qualities.join("、")]] as [string, string][]) : []),
    ["參考圖", refs],
    ["一次張數", `最多 ${ip.max_n} 張`],
  ];
  return (
    <div className="sec">
      <span className="k10">參數 · 照 OpenRouter 名單</span>
      <table className="ptab">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td style={{ whiteSpace: "nowrap" }}>{k}</td>
              <td className="v">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
