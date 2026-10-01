import { useDeferredValue, useMemo, useState } from "react";
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

  const pool = useMemo(() => (longtail ? list : list.filter((m) => m.status === "current")), [list, longtail]);
  const hits = useMemo(
    () => (dq ? pool.filter((m) => m.id.toLowerCase().includes(dq) || (m.name ?? "").toLowerCase().includes(dq) || m.provider.toLowerCase().includes(dq)) : pool),
    [pool, dq],
  );
  const rows = hits.slice(0, MAX_ROWS);
  const tiers = data?.tiers ?? {};

  const aside = replay ? "重播不呼叫模型、不計費" : data ? `現行 ${list.filter((m) => m.status === "current").length} · 全部 ${list.length}` : null;

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
            含長尾
          </Check>
          <span className="dp-count">
            <span className="n">{hits.length}</span> 筆{hits.length > MAX_ROWS ? <>，列前 <span className="n">{MAX_ROWS}</span></> : null}
          </span>
        </div>

        <div className={`dp-mlist${harnessMarks ? "" : " nohm"}`} role="listbox" aria-label="模型清單">
          {!data && !error ? <div className="dp-empty">讀取模型清單…</div> : null}
          {data && rows.length === 0 ? <div className="dp-empty">沒有符合的模型{longtail ? "" : "；勾「含長尾」會多列 OpenRouter 即時抓到的"}</div> : null}
          {rows.map((m) => {
            const on = !replay && sel.kind === "id" && sel.id === m.id;
            const p = fmtPricing(m.pricing);
            return (
              <button key={m.id} type="button" role="option" aria-selected={on} className="dp-mrow" onClick={() => onSel({ kind: "id", id: m.id })} title={m.name ?? m.id}>
                {harnessMarks ? <span className={`hm ${harnessClass(m.harness ? String(m.harness) : null)}`} title={m.harness ? harnessName(String(m.harness)) : "沒有 harness"} /> : null}
                <span className="code">{m.id}</span>
                <span className="dp-prov">{m.provider}</span>
                {m.online === false ? <span className="dp-tag">離線</span> : null}
                <span className={p ? "dp-price n" : "dp-price none"}>{p ?? "未定價"}</span>
              </button>
            );
          })}
          {hits.length > MAX_ROWS ? <div className="dp-empty">還有 <span className="n">{hits.length - MAX_ROWS}</span> 筆沒列出，打字縮小範圍</div> : null}
        </div>
        <div className="dp-note">價格是每百萬 token 的 USD（輸入 / 輸出）。</div>
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
