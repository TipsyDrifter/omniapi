import { Check, Field } from "./Field";
import type { Draft } from "./draft";

/* 權限（M5-e）：自動接受改檔是預設；yolo 要主動勾；搜尋只給 Claude Code；計費只給 Anthropic 模型 */
export function Permissions(props: {
  d: Pick<Draft, "yolo" | "search" | "maxTurns" | "auth">;
  set: (patch: Partial<Draft>) => void;
  /** 實際會走的 harness */
  harness: string | null;
  provider: string | null;
  replay: boolean;
}) {
  const { d, set, harness, provider, replay } = props;
  const isClaude = !replay && harness === "claude";
  const isAnthropic = !replay && provider === "anthropic";
  return (
    <Field lbl="Permissions" zh="權限" aside="headless 不能中途問人">
      <div className="dp-prow">
        <span className="dp-tag">預設</span>
        <div>
          <b className="dp-pk">自動接受改檔</b>
          <div className="dp-note">agent 可以直接改檔、跑 Bash、上網查資料，不會停下來問。</div>
        </div>
      </div>

      <div className="dp-prow">
        <Check checked={d.yolo} onChange={(v) => set({ yolo: v })}>
          <b className="dp-pk">yolo</b>
        </Check>
        <div>
          <div className="dp-note">略過所有權限確認。不勾＝維持上面的預設。</div>
          {d.yolo ? (
            <div className="warn">
              <b>yolo＝略過所有權限確認</b>，agent 可以執行任何指令。
            </div>
          ) : null}
        </div>
      </div>

      {isClaude ? (
        <div className="dp-prow">
          <Check checked={d.search} onChange={(v) => set({ search: v })}>
            <b className="dp-pk">搜尋工具</b>
          </Check>
          <div className="dp-note">掛上 OmniAPI 的搜尋 MCP（DuckDuckGo＋Tavily）。只有 Claude Code 會用到。</div>
        </div>
      ) : null}

      <div className="dp-prow">
        <label className="dp-pk" htmlFor="dp-turns">
          回合上限
        </label>
        <div className="dp-turns">
          <input id="dp-turns" className="dp-in code" type="number" min={1} step={1} inputMode="numeric" placeholder="不限" value={d.maxTurns} onChange={(e) => set({ maxTurns: e.target.value })} />
          <span className="dp-note">選填；到了就停。</span>
        </div>
      </div>

      {isAnthropic ? (
        <div className="dp-prow">
          <span className="dp-pk">計費</span>
          <div>
            <div className="sim" role="group" aria-label="計費">
              <button type="button" aria-pressed={d.auth === "subscription"} onClick={() => set({ auth: "subscription" })}>
                訂閱
              </button>
              <button type="button" aria-pressed={d.auth === "api"} onClick={() => set({ auth: "api" })}>
                API 計費
              </button>
            </div>
            {d.auth === "api" ? (
              <div className="warn">
                <b>會從 Anthropic API 餘額扣款</b>，按 token 計費。
              </div>
            ) : (
              <div className="dp-note">用主人的 Claude 訂閱額度，不另外扣款。</div>
            )}
          </div>
        </div>
      ) : null}
    </Field>
  );
}
