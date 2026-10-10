/* 「估」：這筆費用是照單價預估記的，不是供應商回報的用量（1.4.1，後端在帶費用的物件上給 cost_estimated）。
   後端這樣記的：語音（各家都不回報用量，照每千／每百萬字元價）、知道長度的轉錄、ElevenLabs 音樂、照預估收的影片與圖。
   沒給這一欄（舊服務）＝當實際用量。合計（費用頁的總額、依模型、依日）含任何一筆預估的也標。
   小方章＋滑過出原因；觸控沒有滑過，所以燈箱那種有空間的地方另外把原因寫成一行（estNote）。 */

/** 有費用的那幾種物件共有的欄位 */
export interface CostFlags {
  cost_estimated?: boolean;
  provider?: string | null;
  model?: string | null;
  tool?: string | null;
}

/** 生成工作沒有 provider 欄：拿做出來的作品的 */
export function genFlags(g: { cost_estimated?: boolean; model?: string | null; tool?: string | null; provider?: unknown; artifacts?: { provider?: string | null }[] }): CostFlags {
  const provider = typeof g.provider === "string" ? g.provider : g.artifacts?.find((a) => a.provider)?.provider ?? null;
  return { cost_estimated: g.cost_estimated, model: g.model, tool: g.tool, provider };
}

/** 原因的一句話：看得出是 ElevenLabs 就點名 */
export function estNote(x: CostFlags): string {
  const hay = `${x.provider ?? ""} ${x.model ?? ""} ${x.tool ?? ""}`.toLowerCase();
  return /eleven/.test(hay) ? "ElevenLabs 不回報用量，這筆照單價預估" : "供應商沒回報實際用量，這筆照單價預估";
}
/** 合計含預估時的一句話 */
export const EST_SUM_NOTE = "含照單價預估的費用（供應商不回報用量的那幾筆）";

/** 金額旁的「估」；cost_estimated 不是 true 就什麼都不畫 */
export function EstMark({ x, sum }: { x: CostFlags; /** 合計：說「含預估」 */ sum?: boolean }) {
  if (x.cost_estimated !== true) return null;
  const note = sum ? EST_SUM_NOTE : estNote(x);
  return (
    <span className="est-mark" role="img" aria-label={`估（${note}）`} title={note}>
      估
    </span>
  );
}
