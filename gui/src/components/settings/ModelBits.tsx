import { useDeferredValue, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { ModelEntry, ModelsResponse, SettingsView } from "@/api/types";
import { callable, isRetired, isSunset, priceMain, slotOf, slotUsable, TIER_NAMES, type Modality } from "@/lib/catalog";
import { harnessClass, harnessName } from "@/lib/format";

/* 等級別名、預設模型那一列的模型顯示，與原地展開的挑選器（語彙同派工頁的 ModelPicker：搜尋＋依供應商分段＋價格靠右）。 */

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

export function ModelChooser(props: {
  modality: Modality;
  current: string | null;
  withTiers: boolean;
  data: ModelsResponse | null;
  list: ModelEntry[];
  settings: SettingsView | null;
  onChoose: (v: string) => void;
}) {
  const { modality, current, withTiers, data, list, settings, onChoose } = props;
  const [q, setQ] = useState("");
  const [all, setAll] = useState(false);
  const dq = useDeferredValue(q.trim().toLowerCase());
  const pool = useMemo(() => list.filter((m) => m.modality === modality && !isRetired(m)), [list, modality]);
  const ok = (m: ModelEntry) => callable(slotUsable(settings, slotOf(m.provider, data)));
  const shown = useMemo(() => {
    let l = all ? pool : pool.filter(ok);
    if (dq) l = l.filter((m) => `${m.id} ${m.name ?? ""} ${m.vendor_label ?? ""}`.toLowerCase().includes(dq));
    const order = Object.keys(data?.providers ?? {});
    return [...l].sort((a, b) => order.indexOf(a.provider) - order.indexOf(b.provider));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pool, all, dq, settings, data]);
  const hidden = all ? 0 : pool.filter((m) => !ok(m)).length;
  const rows = shown.slice(0, MAX_ROWS);
  const count = new Map<string, number>();
  for (const m of shown) count.set(m.provider, (count.get(m.provider) ?? 0) + 1);
  return (
    <div className="pk">
      <div className="pk-bar">
        <input className="in" type="search" placeholder={modality === "image" ? "搜 id、名稱或原廠" : "搜 id 或名稱"} value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜尋模型" autoFocus />
        <button type="button" className="ck2" style={{ width: "auto", border: 0 }} aria-pressed={all} onClick={() => setAll((v) => !v)}>
          <span>也列沒 key、停用的</span>
        </button>
      </div>
      <div className="pk-list" role="listbox" aria-label="模型">
        {withTiers ? (
          <>
            <div className="pk-g">
              等級別名<small>跟著上面的表走</small>
            </div>
            {TIER_NAMES.map((t) => (
              <button key={t} type="button" className="pk-r" role="option" aria-selected={current === t} onClick={() => onChoose(t)}>
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
            ))}
          </>
        ) : null}
        {rows.map((m, i) => {
          const head = i === 0 || rows[i - 1].provider !== m.provider;
          const u = slotUsable(settings, slotOf(m.provider, data));
          const pr = priceMain(m);
          return (
            <div key={`${m.provider}/${m.id}`} style={{ display: "contents" }}>
              {head ? (
                <div className="pk-g">
                  {data?.providers?.[m.provider]?.label ?? m.provider}
                  <small>
                    {count.get(m.provider)} 個{u === "off" ? " · 停用中" : u === "none" ? " · 沒有 key" : u === "bad" ? " · key 不通" : ""}
                  </small>
                </div>
              ) : null}
              <button type="button" className={`pk-r${callable(u) ? "" : " dim"}`} role="option" aria-selected={current === m.id} onClick={() => onChoose(m.id)}>
                <span className="code">{m.id}</span>
                <span className="pv2">
                  {isSunset(m) ? <span className="tg">{m.shutdown ? `${m.shutdown.slice(5)} 下架` : "快下架"}</span> : m.status === "discovered" ? <span className="tg def">未整理</span> : null}
                </span>
                <span className="p">{pr ? pr.main : "未定價"}</span>
              </button>
            </div>
          );
        })}
        {!rows.length ? <div className="ml-empty">{data ? "沒有符合的模型" : "讀取模型清單…"}</div> : null}
        {shown.length > MAX_ROWS ? (
          <div className="ml-empty">
            還有 <span className="n">{shown.length - MAX_ROWS}</span> 個沒列出，打字縮小範圍
          </div>
        ) : null}
      </div>
      <div className="pk-foot">
        <span>
          {hidden ? (
            <>
              另有 <span className="n">{hidden}</span> 個在沒有 key 或停用的供應商。
            </>
          ) : null}
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
