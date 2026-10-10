import { Fragment, useCallback, useMemo } from "react";
import { Link } from "react-router-dom";
import type { ModelEntry, ModelsResponse, SettingsView } from "@/api/types";
import { callable, isRetired, isSunset, priceMain, slotOf, slotUsable, TIER_NAMES, type Modality } from "@/lib/catalog";
import { harnessClass, harnessName } from "@/lib/format";
import { IMAGE_CAPS, ModelFilterBar, RankBadge, TEXT_CAPS, VendorHead, needsFilterBar, useModelFilter } from "@/components/ModelFilterBar";

/* 等級別名、預設模型那一列的模型顯示，與原地展開的挑選器（篩選列同派工頁的 ModelPicker，價格靠右）。 */

export function ModelLine({ id, entry, data }: { id: string; entry: ModelEntry | null; data: ModelsResponse | null }) {
  if (!entry)
    return (
      <div className="ml">
        <span className="code">{id}</span>
        <span className="prov">{data ? "目錄裡沒有這筆" : "…"}</span>
      </div>
    );
  const pr = priceMain(entry);
  const h = entry.modality === "text" && entry.harness ? String(entry.harness) : null;
  return (
    <div className="ml">
      <span className="code">{entry.id}</span>
      <span className="prov">
        {data?.providers?.[entry.provider]?.label ?? entry.provider}
        {entry.vendor_label ? ` · 原廠 ${entry.vendor_label}` : ""}
      </span>
      {h ? (
        <span className={`hm ${harnessClass(h)}`} style={{ fontSize: "var(--fs-m11)" }}>
          {harnessName(h)}
        </span>
      ) : null}
      {pr ? <span className="p">{pr.main}</span> : null}
    </div>
  );
}

const MAX_ROWS = 200;

type ChooserProps = {
  modality: Modality;
  current: string | null;
  withTiers: boolean;
  data: ModelsResponse | null;
  list: ModelEntry[];
  settings: SettingsView | null;
  onChoose: (v: string) => void;
};

/** 原地展開的挑選器。篩選列同新對話頁、生成頁（components/ModelFilterBar）：預設熱門序（AA #n）、廠商是篩選。
   篩選列出不出照生成頁那一條線（needsFilterBar：超過 12 個或有未整理的）；短的清單（語音、轉錄…）直接全列、沒有key的反灰。
   長的清單「只看可用」一開始就按下（1.3 起這裡的習慣：先只列有 key 的；以前那顆「也列沒 key、停用的」就是把它放開）。 */
export function ModelChooser(props: ChooserProps) {
  const { modality, list } = props;
  const pool = useMemo(() => list.filter((m) => m.modality === modality && !isRetired(m)), [list, modality]);
  const long = needsFilterBar(pool);
  // 清單從空（還在讀）變長時重掛，讓「只看可用」的初始值照長清單的規則
  return <Chooser key={long ? "long" : "short"} {...props} pool={pool} long={long} />;
}

function Chooser({ modality, current, withTiers, data, settings, onChoose, pool, long }: ChooserProps & { pool: ModelEntry[]; long: boolean }) {
  const ok = useCallback((m: ModelEntry) => callable(slotUsable(settings, slotOf(m.provider, data))), [settings, data]);
  const providerOrder = useMemo(() => Object.keys(data?.providers ?? {}), [data]);
  const caps = modality === "text" ? TEXT_CAPS : modality === "image" ? IMAGE_CAPS : undefined;
  const f = useModelFilter(pool, { caps, usable: ok, usableDefault: long, keep: current, providerLabels: data?.providers, providerOrder });
  const shown = f.shown;
  const rows = shown.slice(0, MAX_ROWS);
  const hidden = f.usableOnly ? pool.filter((m) => !ok(m)).length : 0;
  const tierRows = withTiers
    ? TIER_NAMES.map((t) => (
        <button key={t} type="button" className="pk-r" role="option" aria-selected={current === t} onClick={() => onChoose(t)} aria-label={`${t}（等級別名，現在是 ${settings?.tiers[t]?.model ?? "…"}）`}>
          <span>
            <span className="code">
              <b>{t}</b>
            </span>{" "}
            <span className="x-dim" style={{ fontSize: "var(--fs-m11)" }}>
              → {settings?.tiers[t]?.model ?? "…"}
            </span>
          </span>
          <span />
          <span />
        </button>
      ))
    : null;
  return (
    <div className="pk">
      {long ? (
        <ModelFilterBar
          f={f}
          placeholder={modality === "text" ? "搜尋 id／名稱／廠商" : "搜 id、名稱或原廠"}
          count={shown.length > MAX_ROWS ? <>，列前 <span className="n">{MAX_ROWS}</span></> : null}
          autoFocus
        />
      ) : null}
      <div className="pk-list" role="listbox" aria-label="模型">
        {withTiers ? (
          <>
            <div className="pk-g">
              等級別名<small>跟著上面的表走</small>
            </div>
            {tierRows}
            {rows.length && f.sort !== "vendor" ? (
              <div className="pk-g">
                模型<small>{f.sort === "popular" ? "照熱門排" : "照價格排"}</small>
              </div>
            ) : null}
          </>
        ) : null}
        {rows.map((m, i) => {
          const head = f.head(rows, i);
          const u = slotUsable(settings, slotOf(m.provider, data));
          const pr = priceMain(m);
          const tag = isSunset(m) ? (m.shutdown ? `${m.shutdown.slice(5)} 下架` : "快下架") : m.status === "discovered" ? "未整理" : null;
          const why = u === "off" ? "供應商停用中" : u === "none" ? "沒有 key" : u === "bad" ? "key 不通" : null;
          return (
            <Fragment key={`${m.provider}/${m.id}`}>
              {head ? <VendorHead label={head.label} count={head.count} /> : null}
              <button type="button" className={`pk-r${callable(u) ? "" : " dim"}`} role="option" aria-selected={current === m.id} onClick={() => onChoose(m.id)} aria-label={[m.name ?? m.id, tag, why].filter(Boolean).join("，")} title={m.name && m.name !== m.id ? `${m.name}（${f.vendorOf(m).label}）` : f.vendorOf(m).label}>
                <span className="mf-id">
                  <span className="code">{m.id}</span>
                  <RankBadge m={m} />
                </span>
                <span className="pv2">{tag ? <span className={`tg${m.status === "discovered" ? " def" : ""}`}>{tag}</span> : null}</span>
                <span className="p">{pr ? pr.main : "未定價"}</span>
              </button>
            </Fragment>
          );
        })}
        {!rows.length ? <div className="ml-empty">{data ? (f.usableOnly && hidden ? "有 key 的供應商裡沒有符合的模型；放開「只看可用」看全部" : "沒有符合的模型") : "讀取模型清單…"}</div> : null}
        {shown.length > MAX_ROWS ? (
          <div className="ml-empty">
            還有 <span className="n">{shown.length - MAX_ROWS}</span> 個沒列出，打字縮小範圍
          </div>
        ) : null}
      </div>
      <div className="pk-foot">
        <span>
          {hidden ? `另有 ${hidden} 個在沒有 key 或停用的供應商（放開「只看可用」就列出來）。` : null}
          點一下就存。完整名單與價格在{" "}
          <Link className="lnk" to="/models">
            模型頁
          </Link>
          。
        </span>
      </div>
    </div>
  );
}
