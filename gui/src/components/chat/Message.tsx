import { memo } from "react";
import type { ChatLive, ChatMessage } from "@/api/types";
import { dt, dur, usd } from "@/lib/format";
import CopyButton from "./CopyButton";
import Markdown from "./Markdown";
import Reasoning from "./Reasoning";

/* 一則訊息。使用者＝楷 400 16/1.6 純文字（不跑 markdown）；模型回覆＝meta 行 → 思考 → markdown 本文 → 狀態。 */

/** 毫秒 → `850ms`／`2.2秒`／`1分05秒` */
export const fmtMs = (ms: number): string => (ms < 1000 ? `${Math.round(ms)}ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)}秒` : dur(ms / 1000));

/** 模型名：使用者選的（別名）和實際答的不同時兩個都寫 */
function ModelName({ requested, actual }: { requested?: string | null; actual?: string | null }) {
  if (requested && actual && requested !== actual)
    return (
      <span className="code cm-model">
        {requested} → {actual}
      </span>
    );
  return <span className="code cm-model">{actual ?? requested ?? "—"}</span>;
}

export const UserMessage = memo(function UserMessage({ msg }: { msg: ChatMessage }) {
  return (
    <div className="cm-u" data-msg={msg.id}>
      <div className="cm-meta">
        <span className="k">你</span>
        <span className="n">{dt(msg.created_at)}</span>
      </div>
      <div className="cm-utext">{msg.content}</div>
    </div>
  );
});

export const AssistantMessage = memo(function AssistantMessage({ msg }: { msg: ChatMessage }) {
  const meta = msg.meta ?? {};
  const state = meta.state ?? "done";
  const u = msg.usage;
  const cost = usd(msg.cost_usd, 4);
  return (
    <div className={`cm-a${state === "error" ? " err" : ""}`} data-msg={msg.id}>
      <div className="cm-meta">
        <ModelName requested={meta.requested_model} actual={msg.model} />
        <span className="n">{dt(msg.created_at)}</span>
        {meta.duration_ms != null ? (
          <span>
            <span className="k">耗時</span> <span className="n">{fmtMs(meta.duration_ms)}</span>
          </span>
        ) : null}
        {u && (u.prompt_tokens != null || u.completion_tokens != null) ? (
          <span title="輸入 token / 輸出 token">
            <span className="k">token</span> <span className="n">{u.prompt_tokens ?? "—"}</span> / <span className="n">{u.completion_tokens ?? "—"}</span>
          </span>
        ) : null}
        <span>
          <span className="k">費用</span> {cost ? <span className="n">{cost}</span> : <span className="na">未計價</span>}
        </span>
        {msg.content ? <CopyButton text={msg.content} /> : null}
      </div>
      {msg.reasoning ? <Reasoning text={msg.reasoning} /> : null}
      {msg.content ? <Markdown text={msg.content} /> : null}
      {state === "cancelled" ? <span className="cm-tag">已取消</span> : null}
      {state === "error" ? (
        <div className="warn cm-err" role="alert">
          <span className="errstamp">錯誤</span> {meta.error || "回覆失敗（沒有給原因）"}
        </div>
      ) : null}
    </div>
  );
});

/** 回覆進行中：吃 store 的 live（已經到的字） */
export function LiveReply({ live }: { live: ChatLive }) {
  return (
    <div className="cm-a live" aria-busy="true">
      <div className="cm-meta">
        <ModelName requested={live.model} actual={live.resolved_model} />
        <span className="n">{dt(live.started_at)}</span>
        <span className="cm-tag on">回覆中…</span>
      </div>
      {live.reasoning ? <Reasoning text={live.reasoning} /> : null}
      {live.text ? <Markdown text={live.text} /> : live.reasoning ? null : <div className="cm-wait">等待第一個字…</div>}
    </div>
  );
}
