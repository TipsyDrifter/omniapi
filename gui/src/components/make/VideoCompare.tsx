/* 比較全部影片模型（v1.4）：壓在頂欄下的一層（同燈箱的做法）。寬版是表；手機改一列兩行的清單。
   點一列＝選這個模型並回到表單；Esc 關。價格與能力全部取自名單（/api/generate/options 的 video_params、pricing.skus）。 */
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import type { GenModel, VideoKindExtras } from "@/api/types";
import { useLockScroll } from "@/lib/rwd";
import { Caps } from "./VideoCaps";
import { durTxt, modelRank, perSecondSpan, perTxt, resTxt, sortRes, tokenPriced, tokenUnit, videoPrice, vparams, vstate, vUsd } from "./video";

type Sort = "cheap" | "long" | "name";
type Only = "ff" | "lf" | "audio" | "long" | "hd";
const ONLY: [Only, string, (m: GenModel) => boolean][] = [
  ["ff", "要首幀", (m) => vparams(m).frames.includes("first_frame")],
  ["lf", "要尾幀", (m) => vparams(m).frames.includes("last_frame")],
  ["audio", "要聲音", (m) => vparams(m).audio === true],
  ["long", "最長 ≥ 15 秒", (m) => Math.max(0, ...vparams(m).durations) >= 15],
  ["hd", "有 1080p", (m) => vparams(m).resolutions.some((r) => /1080p|[1-9](\.\d)?k/i.test(r))],
];

/** 一支 5 秒（或模型最短的長度）在最低解析度的價 */
function fiveOf(m: GenModel): { usd: number; note: string } | null {
  const vp = vparams(m);
  const secs = vp.durations.includes(5) ? 5 : vp.durations.find((d) => d > 5) ?? vp.durations[vp.durations.length - 1] ?? null;
  // 最低的、算得出價的那一級（Gemini Omni 只有 720p 算得出）
  const at = (r: string | null) => videoPrice(m.pricing?.skus, { seconds: secs, resolution: r, audio: vp.audio === true && !vp.audio_fixed ? false : vp.audio, firstFrame: false, frames: 0 });
  const tiers = sortRes(vp.resolutions);
  const res = tiers.find((r) => at(r).kind !== "unknown") ?? tiers[0] ?? null;
  const p = at(res);
  if (p.kind === "unknown") return null;
  return { usd: p.kind === "exact" ? p.usd : p.low, note: `${res ?? ""}${secs !== 5 && secs ? `・${secs} 秒` : ""}${p.kind === "range" ? " 起" : ""}` };
}
const audioTxt = (m: GenModel) => {
  const a = vparams(m).audio;
  if (a === true && vparams(m).audio_fixed) return "有（一定有）";
  if (a === true) {
    const on = videoPrice(m.pricing?.skus, { seconds: 1, audio: true }), off = videoPrice(m.pricing?.skus, { seconds: 1, audio: false });
    return on.kind !== "unknown" && off.kind !== "unknown" && off.low < on.low ? "有（關掉較便宜）" : "有";
  }
  return a === false ? "無" : "名單沒標";
};
const goneTxt = (m: GenModel) => (vstate(m) === "retired" ? " · 原廠已關閉" : vstate(m) === "sunset" ? ` · ${m.shutdown ?? ""} 下架` : vstate(m) === "off" ? " · 這一版還沒接上" : "");

export function VideoCompare({ models, sel, unlisted, onPick, onClose }: { models: GenModel[]; sel: string | null; unlisted: VideoKindExtras["unlisted"]; onPick: (id: string) => void; onClose: () => void }) {
  const [only, setOnly] = useState<Only[]>([]);
  const [sort, setSort] = useState<Sort>("cheap");
  useLockScroll(true);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const rows = useMemo(() => {
    const list = models.filter((m) => only.every((o) => ONLY.find((x) => x[0] === o)![2](m)));
    const dead = (m: GenModel) => (vstate(m) === "retired" || vstate(m) === "off" ? 1 : 0);
    if (sort === "cheap") return list.sort((a, b) => modelRank(a) - modelRank(b));
    if (sort === "long") return list.sort((a, b) => dead(a) - dead(b) || Math.max(0, ...vparams(b).durations) - Math.max(0, ...vparams(a).durations));
    return list.sort((a, b) => dead(a) - dead(b) || (a.name ?? a.id).localeCompare(b.name ?? b.id));
  }, [models, only, sort]);
  const pick = (m: GenModel) => {
    if (vstate(m) === "retired" || vstate(m) === "off") return;
    onPick(m.id);
    onClose();
  };
  const perCell = (m: GenModel) => {
    const s = perSecondSpan(m);
    if (!s) {
      const t = tokenUnit(m);
      return tokenPriced(m) ? (
        <>
          <span className="code">{t != null ? `$${t.toFixed(7).replace(/0+$/, "")}` : "依用量"}</span>
          <small>每 token</small>
        </>
      ) : (
        <span className="dp-note">名單沒寫</span>
      );
    }
    return (
      <>
        <span className="big">{perTxt(s.low)}</span>
        <small>{s.high !== s.low ? `到 ${perTxt(s.high)}` : sortRes(vparams(m).resolutions)[0] ?? ""}</small>
      </>
    );
  };
  const five = (m: GenModel) => {
    const f = fiveOf(m);
    return f ? (
      <>
        <span className="big">{vUsd(f.usd)}</span>
        <small>{f.note}</small>
      </>
    ) : (
      <span className="dp-note">{tokenPriced(m) ? "依用量" : "—"}</span>
    );
  };
  return createPortal(
    <div className="vd-cmp" role="dialog" aria-modal="true" aria-label="比較影片模型">
      <div className="vd-cmp-bar">
        <button type="button" className="wk-back" onClick={onClose}>
          ← 回表單
        </button>
        <h3>比較影片模型</h3>
        <span className="sub">
          {models.length} 個 · 價格與能力取自 OpenRouter 名單
        </span>
        <div className="wk-nav2">
          <span className="wk-esc x-touch">Esc 關閉 · 點一列＝選這個模型</span>
        </div>
      </div>
      <div className="vd-cmp-tool">
        <div className="wk-grp" role="group" aria-label="只看">
          <span className="k">只看</span>
          {ONLY.map(([k, zh]) => (
            <button key={k} type="button" className="wk-chip" aria-pressed={only.includes(k)} onClick={() => setOnly((o) => (o.includes(k) ? o.filter((x) => x !== k) : [...o, k]))}>
              {zh}
            </button>
          ))}
        </div>
        <div className="wk-grp" role="group" aria-label="排序">
          <span className="k">排序</span>
          {(
            [
              ["cheap", "每秒最便宜"],
              ["long", "最長秒數"],
              ["name", "名稱"],
            ] as [Sort, string][]
          ).map(([k, zh]) => (
            <button key={k} type="button" className="wk-chip" aria-pressed={sort === k} onClick={() => setSort(k)}>
              {zh}
            </button>
          ))}
        </div>
      </div>
      <div className="vd-cmp-body">
        {!rows.length ? <div className="dp-empty">沒有符合的模型，拿掉一個「只看」試試。</div> : null}
        <table className="vd-tbl">
          <thead>
            <tr>
              <th>模型</th>
              <th className="r" aria-sort={sort === "cheap" ? "ascending" : undefined}>
                每秒<small>最低解析度起</small>
              </th>
              <th className="r">
                一支 5 秒約<small>最低解析度</small>
              </th>
              <th className="r" aria-sort={sort === "long" ? "descending" : undefined}>
                最長
              </th>
              <th>解析度</th>
              <th>首 尾 聲</th>
              <th className="x-m">聲音</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => {
              const st = vstate(m);
              const dead = st === "retired" || st === "off";
              const vp = vparams(m);
              return (
                <tr key={m.id} className={`${m.id === sel ? "on" : ""}${dead ? " gone" : ""}`} onClick={() => pick(m)} aria-disabled={dead || undefined} data-model={m.id}>
                  <td>
                    <span className="code">{m.id}</span>
                    <small>
                      {m.name ?? ""}
                      {goneTxt(m)}
                    </small>
                  </td>
                  <td className="r">{perCell(m)}</td>
                  <td className="r">{five(m)}</td>
                  <td className="r n">
                    {vp.durations.length ? `${Math.max(...vp.durations)} 秒` : "—"}
                    <small>{durTxt(vp)}</small>
                  </td>
                  <td>{sortRes(vp.resolutions).join("／") || "—"}</td>
                  <td>
                    <Caps m={m} />
                  </td>
                  <td className="x-m">{audioTxt(m)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="vd-cmp-cards">
          {rows.map((m) => {
            const st = vstate(m);
            const dead = st === "retired" || st === "off";
            const s = perSecondSpan(m);
            const f = fiveOf(m);
            const vp = vparams(m);
            return (
              <button key={m.id} type="button" className={`vd-cc${m.id === sel ? " on" : ""}${dead ? " gone" : ""}`} disabled={dead} onClick={() => pick(m)} data-model={m.id}>
                <span>
                  <span className="code">{m.id}</span>
                  <br />
                  <span className="dp-note">
                    {m.name ?? ""}
                    {goneTxt(m)}
                  </span>
                </span>
                <span className="p">
                  {s ? `${perTxt(s.low)}/秒` : tokenPriced(m) ? "依用量" : "—"}
                  <small>{f ? `5 秒約 ${vUsd(f.usd)}` : tokenPriced(m) ? "按 token" : "名單沒寫"}</small>
                </span>
                <span className="vd-caps">
                  <span>{durTxt(vp)}</span>
                  <span>{resTxt(vp)}</span>
                  <Caps m={m} />
                </span>
              </button>
            );
          })}
        </div>
        <div className="vd-cmp-foot">
          按 token 計價的每秒用多少 token 名單沒寫，排在按秒計價的後面；名單還在、但原廠已關閉的排最後、不能選；公告下架的還能用，到期之後送不出。
          {unlisted.length ? (
            <>
              <br />
              不是生影片的 {unlisted.length} 個不列（編輯、放大、數位人，這一版不收）：
              {unlisted.map((u, i) => (
                <span key={u.id}>
                  {i ? "、" : ""}
                  <span className="code">{u.id}</span>
                </span>
              ))}
              。
            </>
          ) : null}
        </div>
      </div>
    </div>,
    document.body,
  );
}
