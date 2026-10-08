/* 影片模型的三格能力章：首、尾、聲（實心＝有；虛線劃掉＝沒有；虛線「?」＝名單沒標） */
import type { GenModel } from "@/api/types";
import { vparams } from "./video";

function Cap({ on, g, t }: { on: boolean | null; g: string; t: string }) {
  return (
    <i className={`vd-cap${on === true ? "" : on === null ? " unk" : " off"}`} title={`${t}${on === true ? "" : on === null ? "：名單沒標" : "：沒有"}`} aria-label={`${t}${on === true ? "：有" : on === null ? "：名單沒標" : "：沒有"}`}>
      {on === null ? "?" : g}
    </i>
  );
}

export function Caps({ m }: { m: GenModel }) {
  const vp = vparams(m);
  return (
    <span className="gl">
      <Cap on={vp.frames.includes("first_frame")} g="首" t="首幀" />
      <Cap on={vp.frames.includes("last_frame")} g="尾" t="尾幀" />
      <Cap on={vp.audio} g="聲" t="聲音" />
    </span>
  );
}
