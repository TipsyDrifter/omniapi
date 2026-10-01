import type { HarnessesResponse } from "@/api/types";
import { harnessClass, harnessName } from "@/lib/format";
import { Field } from "./Field";
import { harnessBlock, type HarnessSel } from "./draft";

const FIXED = ["claude", "codex", "gemini"] as const;

/* Harness（M5-c）：預設跟著模型；可覆寫；不能用的反灰寫原因；測試 daemon 多一個重播 */
export function HarnessPicker(props: {
  hs: HarnessesResponse | null;
  error: string | null;
  sel: HarnessSel;
  onSel: (h: HarnessSel) => void;
  /** 模型預設會走的 harness */
  auto: string | null;
  replayId: string;
  onReplayId: (v: string) => void;
}) {
  const { hs, error, sel, onSel, auto, replayId, onReplayId } = props;
  const autoBlock = harnessBlock(auto, hs);
  return (
    <Field lbl="Harness" zh="執行器" aside="預設原廠優先">
      {error ? (
        <div className="warn">
          <b>讀不到 harness 狀態</b>：{error}。仍可送出，能不能跑由後端判斷。
        </div>
      ) : null}
      <div className="dp-hopts" role="radiogroup" aria-label="harness">
        <button type="button" role="radio" aria-checked={sel === "auto"} onClick={() => onSel("auto")}>
          <span className={`hm ${harnessClass(auto)}`} />
          <span className="nm">
            跟著模型
            <span className="to">
              → {auto ? harnessName(auto) : "送出時由後端判斷"}
            </span>
          </span>
          <span className="why">{autoBlock ?? "預設"}</span>
        </button>
        {FIXED.map((h) => {
          const block = harnessBlock(h, hs);
          return (
            <button key={h} type="button" role="radio" aria-checked={sel === h} disabled={!!block} onClick={() => onSel(h)}>
              <span className={`hm ${harnessClass(h)}`} />
              <span className="nm">{harnessName(h)}</span>
              <span className="why">{block ?? (h === auto ? "＝模型預設" : "覆寫")}</span>
            </button>
          );
        })}
        {hs?.replay ? (
          <button type="button" role="radio" aria-checked={sel === "replay"} disabled={hs.replay.available === false} onClick={() => onSel("replay")}>
            <span className="hm h-unknown" />
            <span className="nm">重播（開發用）</span>
            <span className="why">只有測試 daemon 有</span>
          </button>
        ) : null}
      </div>
      {sel === "auto" && autoBlock ? (
        <div className="warn">
          這個模型預設走 <b>{harnessName(auto)}</b>，但它目前{autoBlock}，送出會失敗。換模型或覆寫 harness。
        </div>
      ) : null}
      {sel !== "auto" && sel !== "replay" && auto && sel !== auto ? (
        <div className="warn">
          覆寫成 <b>{harnessName(sel)}</b>（模型預設是 {harnessName(auto)}）。非原廠組合，例如 Claude Code 跑 OpenAI 模型要經 OpenRouter。
        </div>
      ) : null}
      {sel === "replay" ? (
        <div className="dp-replay">
          <label className="lbl" htmlFor="dp-replay-id">
            Source run
          </label>
          <input id="dp-replay-id" className="dp-in code" placeholder="留空＝重播最近一筆完成的 run" value={replayId} onChange={(e) => onReplayId(e.target.value)} spellCheck={false} />
          <div className="dp-note">
            送出 <span className="code">{replayId.trim() ? `replay:${replayId.trim()}` : "replay"}</span>：把舊 run 的事件重吐一次，不呼叫模型、不計費。
          </div>
        </div>
      ) : null}
    </Field>
  );
}
