import { Fragment, useCallback, useMemo } from "react";
import type { ModelEntry, ModelsResponse } from "@/api/types";
import { harnessClass, harnessName } from "@/lib/format";
import { ModelFilterBar, RankBadge, TEXT_CAPS, VendorHead, useModelFilter } from "@/components/ModelFilterBar";
import { Field } from "./Field";
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
  // 篩選列（components/ModelFilterBar）：預設熱門序、扁平；廠商是篩選；「顯示全部」＝加上即時抓到還沒整理的與 OpenRouter
  const usable = useCallback((m: ModelEntry) => m.online !== false && (data?.providers?.[m.provider]?.configured ?? true) !== false, [data]);
  const providerOrder = useMemo(() => Object.keys(data?.providers ?? {}), [data]);
  const f = useModelFilter(list, { caps: TEXT_CAPS, usable, all: longtail, onAll: onLongtail, providerLabels: data?.providers, providerOrder });
  const shown = f.shown;
  const rows = shown.slice(0, MAX_ROWS);
  const hidden = f.hiddenByAll;
  const curatedCount = f.curatedCount;
  const tiers = data?.tiers ?? {};

  const aside = replay ? "重播不呼叫模型、不計費" : data ? `已整理 ${curatedCount} · 全部 ${list.length}` : null;

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
          {(data?.tiers ? Object.keys(data.tiers) : TIERS).map((t) => {
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

        <ModelFilterBar
          f={f}
          placeholder="搜尋 id／名稱／廠商"
          allAlways
          allLabel="顯示全部"
          disabled={replay}
          count={shown.length > MAX_ROWS ? <>，列前 <span className="n">{MAX_ROWS}</span></> : null}
        />

        <div className={`dp-mlist${harnessMarks ? "" : " nohm"}`} role="listbox" aria-label="模型清單">
          {!data && !error ? <div className="dp-empty">讀取模型清單…</div> : null}
          {data && rows.length === 0 ? <div className="dp-empty">沒有符合的模型{f.filtered && (f.vendors.length || f.caps.length || f.usableOnly) ? "；試著拿掉一個篩選" : ""}{longtail ? "" : hidden ? `；按「顯示全部」還有 ${hidden} 筆（剛上線還沒整理的、OpenRouter）` : "；按「顯示全部」會加上剛上線還沒整理的模型與 OpenRouter"}</div> : null}
          {rows.map((m, i) => {
            const on = !replay && sel.kind === "id" && sel.id === m.id;
            const p = fmtPricing(m.pricing);
            // 排序切到「廠商」時每換一家插一條段落標題
            const head = f.head(rows, i);
            const row = (
              <button key={`${m.provider}/${m.id}`} type="button" role="option" aria-selected={on} className="dp-mrow" onClick={() => onSel({ kind: "id", id: m.id })} title={m.name ?? m.id}>
                {harnessMarks ? <span className={`hm ${harnessClass(m.harness ? String(m.harness) : null)}`} title={m.harness ? harnessName(String(m.harness)) : "沒有 harness"} /> : null}
                <span className="mf-id">
                  <span className="code">{m.id}</span>
                  <RankBadge m={m} />
                </span>
                <span className="dp-prov">{m.provider}{f.vendorOf(m).id !== m.provider ? ` · 原廠 ${f.vendorOf(m).label}` : ""}</span>
                {m.online === false ? <span className="dp-tag">離線</span> : null}
                {m.status === "deprecated" ? <span className="dp-tag" title={m.replacement ? `官方建議改用 ${m.replacement}` : undefined}>{m.shutdown ? `${m.shutdown} 關閉` : "即將關閉"}</span> : m.status === "discovered" && m.provider !== "openrouter" ? <span className="dp-tag">未整理</span> : null}
                <span className={p ? "dp-price n" : "dp-price none"}>{p ?? "未定價"}</span>
              </button>
            );
            return head ? (
              <Fragment key={`h/${head.id}`}>
                <VendorHead label={head.label} count={head.count} />
                {row}
              </Fragment>
            ) : (
              row
            );
          })}
          {shown.length > MAX_ROWS ? <div className="dp-empty">還有 <span className="n">{shown.length - MAX_ROWS}</span> 筆沒列出，打字或挑廠商縮小範圍</div> : null}
        </div>
        <div className="dp-note">預設照熱門排：Artificial Analysis 智慧指數榜的名次（AA #n），沒上榜的排後面。價格是每百萬 token 的 USD（輸入 / 輸出）。預設列出整理過的模型（官方已公告關閉日的會標日期）；「顯示全部」再加上剛上線還沒整理的，以及 OpenRouter 的所有模型。</div>
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
