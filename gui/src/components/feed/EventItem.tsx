import { useState, type KeyboardEvent, type ReactNode } from "react";
import type { EventPayload, EventRow, Run } from "@/api/types";
import { dur, glyph, seq3, summarizeInput, toolParts, usd } from "@/lib/format";

/* 活動流的一則事件（THEME.md §5.2）。純 props 驅動；樣式全用 board.css 既有 class。
   result 事件不走 .ev 三欄格線，改渲染成結果卡 .resultcard（左緣對齊內容欄）。 */

export interface EventItemProps {
  event: EventRow;
  /** 該 run 事件列中的順序（1 起） */
  seq: number;
  /** tool_result 對回同 payload.id 的 tool_call 序號；找不到就不顯示 */
  callSeq?: number;
  /** result 卡在事件本身缺欄位時的後備（turns／cost_usd／started_at→ended_at） */
  run?: Run | null;
  /** 篩選沒過：保留掛載（不重播進場動畫），只隱藏 */
  hidden?: boolean;
}

const str = (v: unknown): string => (v == null ? "" : typeof v === "string" ? v : safeJson(v));
function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/** 點擊／Enter／空白鍵切換展開 */
function useToggle(): [boolean, { onClick: () => void; onKeyDown: (e: KeyboardEvent) => void; role: string; tabIndex: number; "aria-expanded": boolean }] {
  const [open, setOpen] = useState(false);
  const toggle = () => setOpen((o) => !o);
  return [
    open,
    {
      onClick: toggle,
      onKeyDown: (e: KeyboardEvent) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggle();
        }
      },
      role: "button",
      tabIndex: 0,
      "aria-expanded": open,
    },
  ];
}

/* 三欄外框：序號｜章｜內容 */
function Row({ type, seq, err, open, hidden, children, bodyProps }: { type: string; seq: number; err?: boolean; open?: boolean; hidden?: boolean; children: ReactNode; bodyProps?: object }) {
  const cls = `ev t-${type}${err ? " err" : ""}${open ? " open" : ""}`;
  return (
    <div className={cls} data-k={type} hidden={hidden}>
      <div className="seq">{seq3(seq)}</div>
      <div className={`gl t-${type}`} aria-hidden="true">
        {glyph(type)}
      </div>
      <div className="body" {...bodyProps}>
        {children}
      </div>
    </div>
  );
}

function SessionStart({ p }: { p: EventPayload }) {
  const mcp = (p.mcp_servers ?? []).join(", ");
  return (
    <>
      <div className="tag">session_start</div>
      <div className="sess">
        model <span className="code">{p.model ?? "—"}</span> · <span className="n">{(p.tools ?? []).length}</span> tools · mcp{" "}
        <span className="code">{mcp || "—"}</span> · session <span className="code">{p.session_id ?? "—"}</span>
      </div>
    </>
  );
}

function ThinkingItem({ e, seq, hidden }: { e: EventRow; seq: number; hidden?: boolean }) {
  const [open, bind] = useToggle();
  return (
    <Row type="thinking" seq={seq} open={open} hidden={hidden} bodyProps={{ ...bind, title: open ? "點一下收合" : "點一下展開" }}>
      <div className="tag">thinking</div>
      <p>{str(e.payload?.text)}</p>
    </Row>
  );
}

function ToolResultItem({ e, seq, callSeq, hidden }: { e: EventRow; seq: number; callSeq?: number; hidden?: boolean }) {
  const p = e.payload ?? {};
  const [open, bind] = useToggle();
  const out = str(p.output);
  const lines = out === "" ? 0 : out.split("\n").length;
  const t = toolParts(p.name);
  const err = !!p.is_error;
  return (
    <Row type="tool_result" seq={seq} err={err} open={open} hidden={hidden} bodyProps={lines ? bind : undefined}>
      <div className="res-h">
        {err && <span className="errstamp">錯誤</span>}
        <span>
          ↳ {callSeq != null && <span className="n">{seq3(callSeq)} </span>}
          <span className="code">{t.n}</span> 回應 · <span className="n">{lines}</span> 行
        </span>
        {lines > 0 && <span className="tog">{open ? "收合 ▴" : "展開 ▾"}</span>}
      </div>
      {lines > 0 && <pre className="out">{out}</pre>}
    </Row>
  );
}

function ResultCard({ e, run, hidden }: { e: EventRow; run?: Run | null; hidden?: boolean }) {
  const p = e.payload ?? {};
  const [open, setOpen] = useState(false);
  const err = !!p.is_error;
  const turns = p.num_turns ?? run?.turns ?? null;
  // cost：事件有帶（含 null）就用事件的；完全沒這欄才退回 run
  const cost = "cost_usd" in p ? (p.cost_usd ?? null) : (run?.cost_usd ?? null);
  const ms = typeof p.duration_ms === "number" ? p.duration_ms : null;
  const secs = ms != null ? ms / 1000 : run?.ended_at != null ? run.ended_at - run.started_at : null;
  const text = str(p.text);
  const costS = usd(cost, 6);
  return (
    <div className={`resultcard${err ? " error" : ""}${open ? " open" : ""}`} data-k="result" hidden={hidden}>
      <h3>結 · {err ? "錯誤" : "完成"}</h3>
      <div className="nums">
        <div>
          <small>回合 TURNS</small>
          {turns ?? <span className="nil">未回報</span>}
        </div>
        <div>
          <small>費用 USD</small>
          {costS ?? <span className="nil">未回報</span>}
        </div>
        <div>
          <small>耗時</small>
          {secs != null ? dur(secs) : <span className="nil">未回報</span>}
        </div>
      </div>
      <div className="note">
        數值取自 result 事件（num_turns／cost_usd／duration_ms）{ms == null && run?.ended_at != null ? "；耗時取自 run 的 started_at → ended_at" : ""}。
      </div>
      {text && (
        <>
          <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "收合" : "展開全文"}
          </button>
          <div className="text">{text}</div>
        </>
      )}
    </div>
  );
}

export default function EventItem({ event: e, seq, callSeq, run, hidden }: EventItemProps) {
  const p = e.payload ?? {};
  switch (e.type) {
    case "session_start":
      return (
        <Row type="session_start" seq={seq} hidden={hidden}>
          <SessionStart p={p} />
        </Row>
      );
    case "text":
      return (
        <Row type="text" seq={seq} hidden={hidden}>
          <div className="tag">agent 說</div>
          <p>{str(p.text)}</p>
        </Row>
      );
    case "thinking":
      return <ThinkingItem e={e} seq={seq} hidden={hidden} />;
    case "tool_call": {
      const t = toolParts(p.name);
      const sum = summarizeInput(p.input);
      return (
        <Row type="tool_call" seq={seq} hidden={hidden}>
          <div className="tool">
            <b>{t.n}</b>
            {t.srv && <span className="srv">mcp · {t.srv}</span>}
          </div>
          {sum && <div className="sum">{sum}</div>}
        </Row>
      );
    }
    case "tool_result":
      return <ToolResultItem e={e} seq={seq} callSeq={callSeq} hidden={hidden} />;
    case "status":
      // 後端 status 多半是 hook_started 之類的雜訊：只印 message，不整包印 data
      return (
        <Row type="status" seq={seq} hidden={hidden}>
          <div className="tag">status</div>
          <p>{p.message ? String(p.message) : "（無訊息）"}</p>
        </Row>
      );
    case "result":
      return <ResultCard e={e} run={run} hidden={hidden} />;
    case "error":
      // 錯誤全文不收合（.open 解除 .out 的兩行限制）
      return (
        <Row type="error" seq={seq} err open hidden={hidden}>
          <div className="tag">error</div>
          <pre className="out">{p.message ? String(p.message) : "（無訊息）"}</pre>
          {p.stderr ? <pre className="out">{String(p.stderr)}</pre> : null}
        </Row>
      );
    default:
      return (
        <Row type={String(e.type)} seq={seq} hidden={hidden}>
          <div className="tag">{String(e.type)}</div>
          <pre className="out">{safeJson(p).slice(0, 300)}</pre>
        </Row>
      );
  }
}
