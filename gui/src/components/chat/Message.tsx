import { memo, useId, useState, type ReactNode } from "react";
import type { ChatAttachment, ChatLive, ChatMessage, ChatVersions } from "@/api/types";
import { dt, dur, usd } from "@/lib/format";
import { ImageViewer, type ViewerImage } from "@/components/works/Lightbox";
import Sheet from "@/components/Sheet";
import CopyButton from "./CopyButton";
import { FileNotes, FileSlip, ReadLine } from "./files";
import Markdown from "./Markdown";
import { Proposals } from "./Proposal";
import Reasoning from "./Reasoning";

/* 一則訊息。使用者＝圖（有的話）＋檔案單（1.2-M5，另起一排）＋楷 400 16/1.6 純文字（不跑 markdown）；
   模型回覆＝meta 行 →（切換版本的說明行）→ 圖片略過的小字 →「檔案」小字（模型怎麼看這些檔）→ 轉錄提問（1.2-M5）
   →「讀」章（模型用工具讀了什麼）→ 思考 → markdown 本文 → 提議卡（1.2-M4）→ 狀態。 */

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

/** 訊息裡的附件：圖照舊一排縮圖（點開看大圖、檔案不在畫佔位）；檔案與音檔另起一排檔案單 */
function Attachments({ atts }: { atts: ChatAttachment[] }) {
  const pics = atts.filter((a) => a.kind !== "file" && a.kind !== "audio");
  const files = atts.filter((a) => a.kind === "file" || a.kind === "audio");
  return (
    <>
      {pics.length ? <Pictures atts={pics} /> : null}
      {files.length ? (
        <div className="cm-files">
          {files.map((a, i) => (
            <FileSlip key={`${a.upload_id ?? a.artifact_id ?? ""}-${i}`} a={a} />
          ))}
        </div>
      ) : null}
    </>
  );
}

function Pictures({ atts }: { atts: ChatAttachment[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const shown = atts.filter((a) => a.exists);
  const images: ViewerImage[] = shown.map((a) => ({
    file_url: a.file_url,
    src: a.thumb_url ? `${a.thumb_url}?w=1600` : a.file_url,
    name: a.name,
    artifact_id: a.artifact_id,
  }));
  return (
    <div className="cm-atts">
      {atts.map((a, i) =>
        a.exists ? (
          <button key={i} type="button" className="cm-att" title={`${a.name ?? ""}\n點開看大圖`} onClick={() => setOpen(shown.indexOf(a))}>
            <img src={a.thumb_url ? `${a.thumb_url}?w=240` : a.file_url} alt={a.name ?? ""} loading="lazy" />
          </button>
        ) : (
          <span key={i} className="cm-att gone" title={a.name ?? undefined}>
            檔案已不在
          </span>
        ),
      )}
      {open != null ? <ImageViewer images={images} start={open} onClose={() => setOpen(null)} /> : null}
    </div>
  );
}

/** 回覆旁的小字：這一則有幾張圖沒送到模型、為什麼 */
function imageNotes(m: { vision?: boolean; images_skipped?: number; images_missing?: number }): string[] {
  const out: string[] = [];
  const skipped = m.images_skipped ?? 0;
  const missing = m.images_missing ?? 0;
  if (skipped > 0) out.push(m.vision === false ? `這個模型不會看圖，略過了 ${skipped} 張` : `圖片合計超過一次能送的大小上限，略過了較舊的 ${skipped} 張`);
  if (missing > 0) out.push(`有 ${missing} 張圖的檔案已不在或讀不出來，沒有送出`);
  return out;
}

function ImageNotes({ notes }: { notes: string[] }) {
  if (!notes.length) return null;
  return (
    <p className="cm-skip">
      <span className="k">圖片</span>
      {notes.join("；")}
    </p>
  );
}

/* ---------------- 1.2-M3：版本切換與訊息上的動作 ----------------
   動作（重新生成、編輯、複製）放資訊列右側；‹ n/m › 放資訊列最前面（使用者在「你」後面、回覆在模型名前面）。
   不能按（回覆進行中、已封存）＝虛線框＋墨 70，用 aria-disabled 保留可聚焦，滑過或聚焦出原因小框。 */

/** 為什麼不能按：回覆進行中／已封存 */
export type MsgOff = "live" | "archived" | null;
const offText = (off: MsgOff | undefined, verb: string): string | null =>
  off === "live" ? `回覆結束後才能${verb}` : off === "archived" ? `這段聊天已封存，不能${verb}` : null;

/** 訊息上的一個動作；off＝不能按的原因 */
function Act({ label, onClick, main, title, off }: { label: string; onClick: () => void; main?: boolean; title?: string; off?: string | null }) {
  const tip = useId();
  if (off)
    return (
      <span className="tipw r">
        <button type="button" className={`cm-act${main ? " main" : ""}`} aria-disabled="true" aria-describedby={tip}>
          {label}
        </button>
        <span className="cx-tip" id={tip} role="tooltip">
          {off}
        </span>
      </span>
    );
  return (
    <button type="button" className={`cm-act${main ? " main" : ""}`} onClick={onClick} title={title}>
      {label}
    </button>
  );
}

/* 1.3-M3 觸控：較早訊息的動作（平常滑過才出現）收成資訊列最右邊一顆常駐的「⋯」，點了從底部升起動作單子
   （D48 第 4 題 A）。最後一則回覆的動作照舊常駐，不另給「⋯」。滑鼠裝置上這顆鈕藏著（rwd.css）。 */
interface MenuAct {
  label: string;
  hint: string;
  off?: string | null;
  run: () => void | Promise<void>;
}
function MsgMenu({ acts, what }: { acts: MenuAct[]; what: string }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  if (!acts.length) return null;
  return (
    <>
      <button type="button" className="kebab cm-kebab" aria-label={`${what}的動作`} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>
        ⋯
      </button>
      <Sheet
        open={open}
        onClose={() => {
          setOpen(false);
          setDone(null);
        }}
        label={`${what}的動作`}
      >
        {acts.map((a) => (
          <button
            key={a.label}
            type="button"
            className={`sheet-it${a.off ? " dis" : ""}`}
            aria-disabled={a.off ? "true" : undefined}
            onClick={async () => {
              if (a.off) return;
              await a.run();
              if (a.label === "複製") {
                setDone("已複製");
                window.setTimeout(() => {
                  setOpen(false);
                  setDone(null);
                }, 700);
              } else setOpen(false);
            }}
          >
            <b>{a.label === "複製" && done ? done : a.label}</b>
            <span className="r">{a.off ?? a.hint}</span>
          </button>
        ))}
      </Sheet>
    </>
  );
}
const copyText = async (text: string) => {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    /* 沒有剪貼簿權限：不擋單子 */
  }
};

/** ‹ n/m ›：同一個 parent 底下的版本（使用者訊息＝改寫留下的、回覆＝重新生成留下的） */
export function VersionSwitch({ v, user, off, onSwitch }: { v?: ChatVersions; user: boolean; off?: string | null; onSwitch?: (to: string, dir: -1 | 1) => void }) {
  const tip = useId();
  if (!v || v.count < 2) return null;
  const i = v.index - 1;
  const what = user ? "改寫" : "重新生成";
  const btn = (dir: -1 | 1, ch: string) => {
    const label = dir < 0 ? "上一版" : "下一版";
    if (off)
      return (
        <button type="button" className="cv-b" aria-disabled="true" aria-label={label} aria-describedby={tip}>
          {ch}
        </button>
      );
    const to = v.ids[i + dir];
    return (
      <button type="button" className="cv-b" aria-label={label} disabled={!to} onClick={() => to && onSwitch?.(to, dir)}>
        {ch}
      </button>
    );
  };
  return (
    <span className="tipw">
      <span className={`cv${off ? " off" : ""}`} role="group" aria-label="版本" title={off ? undefined : `這則有 ${v.count} 個版本（${what}留下的）；切換時，後面的對話跟著換`}>
        {btn(-1, "‹")}
        <span className="cv-n">
          {v.index}/{v.count}
        </span>
        {btn(1, "›")}
      </span>
      {off ? (
        <span className="cx-tip" id={tip} role="tooltip">
          {off}
        </span>
      ) : null}
    </span>
  );
}

export interface UserMessageProps {
  msg: ChatMessage;
  /** 後面還有幾則（說明「後面 N 則留在原本那條」用） */
  after?: number;
  /** 不能編輯／切換的原因 */
  off?: MsgOff;
  onEdit?: (m: ChatMessage) => void;
  onSwitch?: (m: ChatMessage, to: string, dir: -1 | 1) => void;
}

export const UserMessage = memo(function UserMessage({ msg, after = 0, off, onEdit, onSwitch }: UserMessageProps) {
  return (
    <div className={`cm-u${msg.local ? " local" : ""}`} data-msg={msg.id}>
      <div className="cm-meta">
        <span className="k">你</span>
        <VersionSwitch v={msg.versions} user off={msg.local ? "送出後才能切換版本" : offText(off, "切換版本")} onSwitch={(to, dir) => onSwitch?.(msg, to, dir)} />
        <span className="n">{dt(msg.created_at)}</span>
        {msg.local ? <span className="cm-sending">送出中…</span> : null}
        {onEdit && !msg.local ? (
          <span className="cm-acts">
            <Act
              label="編輯"
              onClick={() => onEdit(msg)}
              off={offText(off, "編輯")}
              title={after ? `改這一則再送出：從這裡分出新的一條，後面 ${after} 則留在原本那條` : "改這一則再送出"}
            />
          </span>
        ) : null}
        {onEdit && !msg.local ? (
          <MsgMenu
            what="這則訊息"
            acts={[
              { label: "編輯這則", hint: after ? `從這裡分岔，後面 ${after} 則留在原本那條` : "改這一則再送出", off: offText(off, "編輯"), run: () => onEdit(msg) },
              ...(msg.content ? [{ label: "複製", hint: "複製這則的文字", run: () => copyText(msg.content ?? "") }] : []),
            ]}
          />
        ) : null}
      </div>
      {msg.attachments?.length ? <Attachments atts={msg.attachments} /> : null}
      {msg.content ? <div className="cm-utext">{msg.content}</div> : null}
    </div>
  );
});

export interface AssistantMessageProps {
  msg: ChatMessage;
  /** 現行那一條的最後一則回覆：「重新生成」常駐、墨框實線 */
  last?: boolean;
  after?: number;
  off?: MsgOff;
  /** 細列「下一則用」的模型（重新生成用它，D40） */
  nextModel?: string;
  onRegenerate?: (m: ChatMessage) => void;
  onSwitch?: (m: ChatMessage, to: string, dir: -1 | 1) => void;
  /** 1.2-M4：切換到這一版時的說明行，放在資訊列正下方（回覆底下有提議卡，放最後會被捲出畫面） */
  note?: ReactNode;
}

export const AssistantMessage = memo(function AssistantMessage({ msg, last, after = 0, off, nextModel, onRegenerate, onSwitch, note }: AssistantMessageProps) {
  const meta = msg.meta ?? {};
  const state = meta.state ?? "done";
  const u = msg.usage;
  const cost = usd(msg.cost_usd, 4);
  const notes = imageNotes(meta);
  const made = (msg.proposals ?? []).some((p) => p.state === "done" && !p.gate);
  const kept = made ? "原本這版和它生成的作品都留著，按 ‹ 換回去" : "原本這版留著";
  const regenTitle = last || !after ? `用 ${nextModel ?? "細列選的模型"} 再答一次；${kept}` : `從這裡重新生成：後面 ${after} 則留在原本那條，按 ‹ 換回去就看得到`;
  // 已封存仍可複製；回覆進行中照稿一起停用
  const copyOff = off === "live" ? offText(off, "複製") : null;
  // 1.2-M5：回覆前先問主人（轉錄提問還掛著）／主人沒回答就送了下一則
  if (state === "awaiting" || state === "skipped")
    return (
      <div className={`cm-a asking${last ? " last" : ""}`} data-msg={msg.id}>
        <div className="cm-meta">
          <ModelName requested={meta.requested_model} actual={msg.model} />
          <span className="n">{dt(msg.created_at)}</span>
          <span className={`cm-tag${state === "awaiting" ? " on" : ""}`}>{state === "awaiting" ? "回覆前先問你" : "略過"}</span>
        </div>
        {note}
        <Proposals msg={msg} hasNext={after > 0} archived={off === "archived"} gate />
      </div>
    );
  return (
    <div className={`cm-a${state === "error" ? " err" : ""}${last ? " last" : ""}`} data-msg={msg.id}>
      <div className="cm-meta">
        <VersionSwitch v={msg.versions} user={false} off={offText(off, "切換版本")} onSwitch={(to, dir) => onSwitch?.(msg, to, dir)} />
        <ModelName requested={meta.requested_model} actual={msg.model} />
        <span className="n">{dt(msg.created_at)}</span>
        {meta.duration_ms != null ? (
          <span className="cm-dur">
            <span className="k">耗時</span> <span className="n">{fmtMs(meta.duration_ms)}</span>
          </span>
        ) : null}
        {u && (u.prompt_tokens != null || u.completion_tokens != null) ? (
          <span className="cm-tok" title="輸入 token / 輸出 token">
            <span className="k">token</span> <span className="n">{u.prompt_tokens ?? "—"}</span> / <span className="n">{u.completion_tokens ?? "—"}</span>
          </span>
        ) : null}
        <span>
          <span className="k">費用</span> {cost ? <span className="n">{cost}</span> : <span className="na">未計價</span>}
        </span>
        <span className="cm-acts">
          {onRegenerate ? <Act label="重新生成" main={last} onClick={() => onRegenerate(msg)} title={regenTitle} off={offText(off, "重新生成")} /> : null}
          {msg.content ? copyOff ? <Act label="複製" onClick={() => undefined} off={copyOff} /> : <CopyButton text={msg.content} className="cm-act" /> : null}
        </span>
        {last ? null : (
          <MsgMenu
            what="這則回覆"
            acts={[
              ...(onRegenerate ? [{ label: "重新生成", hint: made ? "原本這版和它的作品留著，用 ‹ › 換回去" : "原本這版留著，用 ‹ › 換回去", off: offText(off, "重新生成"), run: () => onRegenerate(msg) }] : []),
              ...(msg.content ? [{ label: "複製", hint: "複製這則回覆的文字", off: copyOff, run: () => copyText(msg.content ?? "") }] : []),
            ]}
          />
        )}
      </div>
      {note}
      <ImageNotes notes={notes} />
      <FileNotes files={meta.files} question={msg.parent_id} />
      <Proposals msg={msg} hasNext={after > 0} archived={off === "archived"} gate />
      <ReadLine reads={meta.reads} />
      {msg.reasoning ? <Reasoning text={msg.reasoning} /> : null}
      {msg.content ? <Markdown text={msg.content} /> : null}
      <Proposals msg={msg} hasNext={after > 0} archived={off === "archived"} />
      {state === "cancelled" ? <span className="cm-tag">已取消</span> : null}
      {state === "error" ? (
        <div className="warn cm-err" role="alert">
          <span className="errstamp">錯誤</span> {meta.error || "回覆失敗（沒有給原因）"}
        </div>
      ) : null}
    </div>
  );
});

/** 回覆進行中：吃 store 的 live（已經到的字）。重新生成時新的一版先佔好 ‹ n/n ›（虛線，不能按）。
 *  fill＝先問過主人、回覆寫進的那一則（1.2-M5）：它的轉錄卡（逐字稿）留在上面，回覆接著流出來 */
export function LiveReply({ live, fill }: { live: ChatLive; fill?: ChatMessage }) {
  const reading = !!live.reading;
  return (
    <div className="cm-a live last" aria-busy="true" data-msg={fill ? fill.id : `live-${live.turn_id}`}>
      <div className="cm-meta">
        <VersionSwitch v={live.versions} user={false} off="回覆結束後才能切換版本" />
        <ModelName requested={live.model} actual={live.resolved_model} />
        <span className="n">{dt(live.started_at)}</span>
        <span className="cm-tag on">回覆中…</span>
      </div>
      {/* 送出當下只知道「模型不看圖」略過幾張；超過大小、檔案不在的要等回覆存下才知道 */}
      <ImageNotes notes={imageNotes(live)} />
      {fill ? <Proposals msg={fill} hasNext={false} gate /> : null}
      <ReadLine reads={live.reads} reading={live.reading} />
      {live.reasoning ? <Reasoning text={live.reasoning} /> : null}
      {live.text ? <Markdown text={live.text} /> : live.reasoning || reading ? null : <div className="cm-wait">等待第一個字…</div>}
    </div>
  );
}
