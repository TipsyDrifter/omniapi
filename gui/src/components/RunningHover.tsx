/* 頂欄「生成」的進行中章：有影片在等時，滑過出一張小卡，列出哪幾件在做、各等了多久（第 7 題，VIDEO_UI.hoverCard）。
   只在滑得過去的裝置出（觸控照全站的替代：點「生成」到出件口看等待票；章本身的 aria-label 也寫了件數）。
   卡掛在 body 上（不被頂欄裁掉），位置照章算；滑到卡上不會收起。 */
import { useEffect, useRef, useState, type FocusEvent, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import type { Generation } from "@/api/types";
import { useTouch } from "@/lib/rwd";
import { isRunning, useMake } from "@/store/make";
import { Glyph, VIDEO_UI, mmss, useNow } from "@/components/make";

const sel = (s: { order: string[]; gens: Record<string, Generation> }) => s.order.filter((id) => isRunning(s.gens[id]));

/** 給 NavLink 的滑過處理＋卡本身 */
export function useRunningHover() {
  const touch = useTouch();
  const ids = useMake(sel);
  const gens = useMake((s) => ids.map((id) => s.gens[id]));
  const anyVideo = gens.some((g) => g?.kind === "video");
  const on = VIDEO_UI.hoverCard && !touch && anyVideo;
  const [open, setOpen] = useState<DOMRect | null>(null);
  const t = useRef<number | null>(null);
  const cancel = () => {
    if (t.current) window.clearTimeout(t.current);
    t.current = null;
  };
  const close = () => {
    cancel();
    t.current = window.setTimeout(() => setOpen(null), 220);
  };
  useEffect(() => () => cancel(), []);
  useEffect(() => {
    if (!on) setOpen(null);
  }, [on]);
  const handlers = on
    ? {
        onMouseEnter: (e: MouseEvent<HTMLElement>) => {
          cancel();
          setOpen(e.currentTarget.getBoundingClientRect());
        },
        onMouseLeave: close,
        onFocus: (e: FocusEvent<HTMLElement>) => setOpen(e.currentTarget.getBoundingClientRect()),
        onBlur: close,
      }
    : {};
  const card = on && open ? <HoverCard gens={gens.filter(Boolean)} at={open} onEnter={cancel} onLeave={close} onPick={() => setOpen(null)} /> : null;
  return { handlers, card };
}

function HoverCard({ gens, at, onEnter, onLeave, onPick }: { gens: Generation[]; at: DOMRect; onEnter: () => void; onLeave: () => void; onPick: () => void }) {
  const now = useNow(true);
  const left = Math.max(16, Math.min(at.left - 40, window.innerWidth - 336));
  return createPortal(
    <div className="vd-hover" role="tooltip" style={{ position: "fixed", top: at.bottom + 8, left }} onMouseEnter={onEnter} onMouseLeave={onLeave}>
      <h5>生成中 · {gens.length} 件</h5>
      {gens.map((g) => {
        const from = g.video?.submitted_at ?? g.created_at;
        const p = g.params ?? {};
        const spec = g.kind === "video" && typeof p.duration === "number" ? ` · ${p.duration} 秒` : "";
        return (
          <Link key={g.id} className="it" to={`/make/${g.kind}`} onClick={onPick}>
            <Glyph kind={g.kind} size="sm" />
            <span>
              <span className="code">{g.model ?? (typeof p.model === "string" ? p.model : "預設模型")}</span>
              {spec}
            </span>
            <span className="n">{mmss(now - from)}</span>
          </Link>
        );
      })}
      <p>點一件回生成頁看等待票。</p>
    </div>,
    document.body,
  );
}
