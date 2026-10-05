/* 全站 RWD（1.3-M3，D44／D48）：JS 這邊不寫斷點數字。
   斷點只定義在 styles/rwd.css：每一段在 :root 寫一個 --tier（xl／l／m／s），這裡讀它；
   觸控與否看 (hover: none)，不看寬度（NOTES §4）。 */
import { useEffect, useSyncExternalStore } from "react";

export type Tier = "xl" | "l" | "m" | "s";

function readTier(): Tier {
  if (typeof window === "undefined") return "xl";
  const v = getComputedStyle(document.documentElement).getPropertyValue("--tier").trim();
  return v === "l" || v === "m" || v === "s" ? v : "xl";
}

let tier: Tier = readTier();
const tierListeners = new Set<() => void>();
if (typeof window !== "undefined") {
  window.addEventListener("resize", () => {
    const t = readTier();
    if (t === tier) return;
    tier = t;
    tierListeners.forEach((l) => l());
  });
}

/** 目前在哪一段：xl ≥1440／l／m／s（數值見 rwd.css） */
export function useTier(): Tier {
  return useSyncExternalStore(
    (l) => {
      tierListeners.add(l);
      // 掛上時樣式表可能剛載入：補讀一次
      const t = readTier();
      if (t !== tier) {
        tier = t;
        queueMicrotask(() => tierListeners.forEach((x) => x()));
      }
      return () => tierListeners.delete(l);
    },
    () => tier,
    () => "xl" as Tier,
  );
}

const touchMq = typeof window !== "undefined" ? window.matchMedia("(hover: none)") : null;

/** 觸控裝置（滑不過去）：滑過才出現的東西改常駐或收進「⋯」 */
export function useTouch(): boolean {
  return useSyncExternalStore(
    (l) => {
      touchMq?.addEventListener("change", l);
      return () => touchMq?.removeEventListener("change", l);
    },
    () => !!touchMq?.matches,
    () => false,
  );
}

export const isTouch = (): boolean => !!touchMq?.matches;

/** 觸控上看不到滑過才出的原因小框（.cx-tip）：點一下停用的鈕就在下方展開那句原因，下一個動作就收起（NOTES §4）。
 *  全站掛一次：點到 .tipw／.cx-attwrap 裡的停用鈕 → 那一塊加 .tap；點別處 → 拿掉。 */
export function useTapReasons(): void {
  useEffect(() => {
    let open: HTMLElement | null = null;
    const onClick = (e: MouseEvent) => {
      if (!touchMq?.matches) return;
      const t = e.target instanceof Element ? e.target : null;
      const btn = t?.closest<HTMLElement>('[aria-disabled="true"], .cs-nogen');
      const wrap = btn?.closest<HTMLElement>(".tipw, .cx-attwrap");
      if (open && open !== wrap) open.classList.remove("tap");
      if (wrap && wrap.querySelector(".cx-tip")) {
        wrap.classList.toggle("tap");
        open = wrap.classList.contains("tap") ? wrap : null;
      } else open = null;
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);
}

/** 鎖住頁面捲動（底部單子開著時） */
export function useLockScroll(on: boolean): void {
  useEffect(() => {
    if (!on) return;
    document.documentElement.classList.add("sheet-lock");
    return () => document.documentElement.classList.remove("sheet-lock");
  }, [on]);
}
