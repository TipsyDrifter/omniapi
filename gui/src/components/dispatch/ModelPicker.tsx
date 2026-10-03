import { Fragment, useDeferredValue, useMemo, useState } from "react";
import type { ModelEntry, ModelsResponse } from "@/api/types";
import { harnessClass, harnessName } from "@/lib/format";
import { Check, Field } from "./Field";
import { TIERS, fmtPricing, type ModelSel } from "./draft";

const MAX_ROWS = 200;

/* 模型（M5-b）：三顆等級鈕＋可搜尋清單；replay 時整塊停用 */
export function ModelPicker(props: {
  data: ModelsResponse | null;
  list: ModelEntry[];
  idx: Map<string, ModelEntry>;
  error: string | null;
  sel: ModelSel;
  onSel: (s: ModelSel) => void;
  longtail: boolean;
  onLongtail: (v: boolean) => void;
  /** 重播：不呼叫模型 */
  replay: boolean;
  /** 選到的實際 id 與目錄那筆 */
  resolved: { id: string | null; entry: ModelEntry | null };
  /** 標出每個模型預設走的 harness（油墨方塊）；聊天情境不受 harness 限制，傳 false 改標 provider。預設 true */
  harnessMarks?: boolean;
}) {
  const { data, list, idx, error, sel, onSel, longtail, onLongtail, replay, resolved, harnessMarks = true } = props;
  const [q, setQ] = useState("");
  const dq = useDeferredValue(q.trim().toLowerCase());

  // 預設＝整理過的（現行＋已公告關閉日的舊版）；即時抓到還沒整理的與 OpenRouter 要勾「顯示全部」
  const curated = useMemo(() => list.filter((m) => m.status !== "discovered"), [list]);
  const pool = longtail ? list : curated;
  const hits = useMemo(
    () => (dq ? pool.filter((m) => m.id.toLowerCase().includes(dq) || (m.name ?? "").toLowerCase().includes(dq) || m.provider.toLowerCase().includes(dq)) : pool),
    [pool, dq],
  );
  // 廠商分類：頁籤的數字跟著搜尋與「顯示全部」走；選了某一家就只列那一家，選「全部」時清單按廠商分段
  const [vendor, setVendor] = useState<string | null>(null);
  const vendors = useMemo(() => {
    const order = Object.keys(data?.providers ?? {});
    const rank = (p: string) => (order.indexOf(p) < 0 ? order.length : order.indexOf(p));
    const n = new Map<string, number>();
    for (const m of hits) n.set(m.provider, (n.get(m.provider) ?? 0) + 1);
    return [...n.entries()].map(([id, count]) => ({ id, count, label: data?.providers?.[id]?.label ?? id })).sort((a, b) => rank(a.id) - rank(b.id) || a.id.localeCompare(b.id));
  }, [hits, data]);
  // 選著的那一家不在目前的範圍裡（例如關掉「顯示全部」後的 OpenRouter）＝當成「全部」
  const activeVendor = vendor && vendors.some((v) => v.id === vendor) ? vendor : null;
  const shown = useMemo(() => {
    if (activeVendor) return hits.filter((m) => m.provider === activeVendor);
    const rank = new Map(vendors.map((v, i) => [v.id, i]));
    return [...hits].sort((a, b) => (rank.get(a.provider) ?? 0) - (rank.get(b.provider) ?? 0));
  }, [hits, activeVendor, vendors]);
  const rows = shown.slice(0, MAX_ROWS);
  const vendorLabel = (id: string) => vendors.find((v) => v.id === id)?.label ?? id;
  // 預設清單沒有、但「顯示全部」裡找得到的筆數——搜尋落空時告訴使用者東西在哪
  const hidden = useMemo(
    () =>
      longtail || !dq
        ? 0
        : list.filter((m) => m.status === "discovered" && (m.id.toLowerCase().includes(dq) || (m.name ?? "").toLowerCase().includes(dq) || m.provider.toLowerCase().includes(dq))).length,
    [list, longtail, dq],
  );
  const tiers = data?.tiers ?? {};

  const aside = replay ? "重播不呼叫模型、不計費" : data ? `已整理 ${curated.length} · 全部 ${list.length}` : null;

  return (
    <Field lbl="Model" zh="模型" aside={aside} className={replay ? "dp-off" : undefined}>
      {error ? (
        <div className="warn">
          <b>讀不到模型清單</b>：{error}。仍可送出等級別名。
        </div>
      ) : null}
      {replay ? (
        <div className="warn">
          選了「重播（開發用）」：<b>不呼叫模型、不計費</b>，這一區的選擇不會送出。
        </div>
      ) : null}
      <fieldset className="dp-fs" disabled={replay}>
        <div className="dp-tiers" role="group" aria-label="等級">
          {TIERS.map((t) => {
            const id = tiers[t] ?? null;
            const e = id ? idx.get(id) ?? null : null;
            return (
              <button key={t} type="button" aria-pressed={!replay && sel.kind === "tier" && sel.tier === t} onClick={() => onSel({ kind: "tier", tier: t })}>
                <span className="lbl">{t}</span>
                <span className="code">{id ?? (data ? "未設定" : "…")}</span>
                {!harnessMarks ? (
                  e ? <span className="dp-prov">{e.provider}</span> : null
                ) : e?.harness ? (
                  <span className={`hm ${harnessClass(String(e.harness))}`}>{harnessName(String(e.harness))}</span>
                ) : null}
              </button>
            );
          })}
        </div>

        <div className="dp-mfilter">
          <input type="search" className="dp-in" placeholder="搜尋 id／名稱／provider" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜尋模型" />
          <Check checked={longtail} onChange={onLongtail}>
            顯示全部
          </Check>
          <span className="dp-count">
            <span className="n">{shown.length}</span> 筆{shown.length > MAX_ROWS ? <>，列前 <span className="n">{MAX_ROWS}</span></> : null}
          </span>
        </div>

        {vendors.length > 1 ? (
          <div className="dp-vendors" role="group" aria-label="廠商">
            <button type="button" aria-pressed={activeVendor === null} onClick={() => setVendor(null)}>
              全部 <span className="n">{hits.length}</span>
            </button>
            {vendors.map((v) => (
              <button key={v.id} type="button" aria-pressed={activeVendor === v.id} onClick={() => setVendor(activeVendor === v.id ? null : v.id)}>
                {v.label} <span className="n">{v.count}</span>
              </button>
            ))}
          </div>
        ) : null}

        <div className={`dp-mlist${harnessMarks ? "" : " nohm"}`} role="listbox" aria-label="模型清單">
          {!data && !error ? <div className="dp-empty">讀取模型清單…</div> : null}
          {data && rows.length === 0 ? <div className="dp-empty">沒有符合的模型{longtail ? "" : hidden ? `；勾「顯示全部」還有 ${hidden} 筆（剛上線還沒整理的、OpenRouter）` : "；勾「顯示全部」會加上剛上線還沒整理的模型與 OpenRouter"}</div> : null}
          {rows.map((m, i) => {
            const on = !replay && sel.kind === "id" && sel.id === m.id;
            const p = fmtPricing(m.pricing);
            // 「全部」時每換一家插一條段落標題
            const head = !activeVendor && vendors.length > 1 && (i === 0 || rows[i - 1].provider !== m.provider);
            const row = (
              <button key={`${m.provider}/${m.id}`} type="button" role="option" aria-selected={on} className="dp-mrow" onClick={() => onSel({ kind: "id", id: m.id })} title={m.name ?? m.id}>
                {harnessMarks ? <span className={`hm ${harnessClass(m.harness ? String(m.harness) : null)}`} title={m.harness ? harnessName(String(m.harness)) : "沒有 harness"} /> : null}
                <span className="code">{m.id}</span>
                <span className="dp-prov">{m.provider}</span>
                {m.online === false ? <span className="dp-tag">離線</span> : null}
                {m.status === "deprecated" ? <span className="dp-tag" title={m.replacement ? `官方建議改用 ${m.replacement}` : undefined}>{m.shutdown ? `${m.shutdown} 關閉` : "即將關閉"}</span> : m.status === "discovered" && m.provider !== "openrouter" ? <span className="dp-tag">未整理</span> : null}
                <span className={p ? "dp-price n" : "dp-price none"}>{p ?? "未定價"}</span>
              </button>
            );
            return head ? (
              <Fragment key={`h/${m.provider}`}>
                <div className="dp-mgroup" role="presentation">
                  <span className="zh">{vendorLabel(m.provider)}</span>
                  <span className="n">{vendors.find((v) => v.id === m.provider)?.count ?? ""}</span>
                </div>
                {row}
              </Fragment>
            ) : (
              row
            );
          })}
          {shown.length > MAX_ROWS ? <div className="dp-empty">還有 <span className="n">{shown.length - MAX_ROWS}</span> 筆沒列出，打字或選一家廠商縮小範圍</div> : null}
        </div>
        <div className="dp-note">價格是每百萬 token 的 USD（輸入 / 輸出）。預設列出整理過的模型（官方已公告關閉日的會標日期）；「顯示全部」再加上剛上線還沒整理的，以及 OpenRouter 的所有模型。</div>
      </fieldset>

      {!replay ? (
        <div className="dp-chosen">
          <span className="lbl">送出</span>
          <span className="code">{sel.kind === "tier" ? sel.tier : sel.id}</span>
          {sel.kind === "tier" && resolved.id ? (
            <>
              <span className="k">→</span>
              <span className="code">{resolved.id}</span>
            </>
          ) : null}
          {resolved.entry ? <span className="k">{resolved.entry.provider}</span> : data && sel.kind === "id" ? <span className="k">目錄裡沒有這筆，送出時由後端判斷</span> : null}
        </div>
      ) : null}
    </Field>
  );
}
