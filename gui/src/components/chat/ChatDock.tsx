import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ModelPicker } from "@/components/dispatch";
import ChatComposer from "./ChatComposer";
import { resolveChatModel, toSel, useModels } from "./useModels";

/* 貼底區（M6-f）：錯誤 → 一行細列（左「下一則用：模型」、右「System prompt 有／無」）→ 輸入列。
   細列的兩個展開區預設收合，展開時往上長（蓋在訊息區上），不擠壓輸入列。 */

export interface ChatDockProps {
  /** 下一則會用的模型（等級別名或 id） */
  model: string;
  onModel: (m: string) => void;
  /** 目前的 system prompt（空字串＝無） */
  system: string;
  /** 儲存 system prompt；丟錯就把原因顯示在編輯區 */
  onSystem: (v: string) => Promise<void> | void;
  /** save＝存到對話（「儲存」）；draft＝還沒建立對話，只是先填（「套用」） */
  systemMode: "save" | "draft";
  live: boolean;
  onSend: (text: string) => Promise<boolean>;
  onStop?: () => Promise<void>;
  /** 顯示在細列上方的錯誤（送出失敗、上一則回覆失敗…） */
  notices?: ReactNode[];
  blocked?: string | null;
  autoFocus?: boolean;
}

type Pop = null | "model" | "system";

// 「含長尾」的勾選：整個分頁記住就好
let longtailMemo = false;

export default function ChatDock(props: ChatDockProps) {
  const { model, onModel, system, onSystem, systemMode, live, onSend, onStop, notices = [], blocked, autoFocus } = props;
  const [pop, setPop] = useState<Pop>(null);
  const [longtail, setLongtail] = useState(longtailMemo);
  const m = useModels();
  const r = resolveChatModel(model, m);
  const boxRef = useRef<HTMLDivElement>(null);

  // 點到細列與展開區以外就收起來
  useEffect(() => {
    if (!pop) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setPop(null);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [pop]);

  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && pop) {
      e.stopPropagation();
      setPop(null);
    }
  };

  const shown = notices.filter(Boolean);
  const toggle = (p: Exclude<Pop, null>) => setPop((o) => (o === p ? null : p));
  return (
    <div className="ch-dock">
      {shown.length ? <div className="ch-notices">{shown}</div> : null}
      <div className="cs-wrap" ref={boxRef} onKeyDown={onKey}>
        {pop === "model" ? (
          <div className="cs-pop cs-pop-model" role="dialog" aria-label="換模型">
            <ModelPicker
              data={m.data}
              list={m.list}
              idx={m.idx}
              error={m.error}
              sel={toSel(model)}
              onSel={(s) => {
                onModel(s.kind === "tier" ? s.tier : s.id);
                setPop(null);
              }}
              longtail={longtail}
              onLongtail={(v) => {
                longtailMemo = v;
                setLongtail(v);
              }}
              replay={false}
              resolved={{ id: r.id, entry: r.entry }}
              harnessMarks={false}
            />
            <p className="cs-note">換了只影響下一則以後，不用先存；每則回覆都會標明是哪個模型答的。聊天可用所有文字模型，不受 harness 限制。</p>
          </div>
        ) : null}
        {pop === "system" ? <SystemEditor value={system} mode={systemMode} onSave={onSystem} onClose={() => setPop(null)} /> : null}
        <div className="cs-strip">
          <button type="button" className="cs-btn" aria-expanded={pop === "model"} onClick={() => toggle("model")} title="換模型">
            <span className="cs-k">下一則用</span>
            <span className="code">{model}</span>
            {r.id && r.id !== model ? <span className="code">→ {r.id}</span> : null}
            <span className="cs-car" aria-hidden="true">
              {pop === "model" ? "▾" : "▴"}
            </span>
          </button>
          <button type="button" className="cs-btn cs-sys" aria-expanded={pop === "system"} onClick={() => toggle("system")} title="編輯 system prompt">
            <span className="cs-k">System prompt</span>
            <span className={`cs-has${system.trim() ? " on" : ""}`}>{system.trim() ? "有" : "無"}</span>
            <span className="cs-car" aria-hidden="true">
              {pop === "system" ? "▾" : "▴"}
            </span>
          </button>
        </div>
      </div>
      <ChatComposer live={live} onSend={onSend} onStop={onStop} blocked={blocked} autoFocus={autoFocus} placeholder={`對 ${model} 說……（Ctrl+Enter 送出）`} />
    </div>
  );
}

function SystemEditor({ value, mode, onSave, onClose }: { value: string; mode: "save" | "draft"; onSave: (v: string) => Promise<void> | void; onClose: () => void }) {
  const [v, setV] = useState(value);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const dirty = v.trim() !== value.trim();
  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await onSave(v.trim());
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };
  return (
    <div className="cs-pop cs-pop-sys" role="dialog" aria-label="System prompt">
      <div className="dp-fh">
        <label className="lbl" htmlFor="cs-system">
          System prompt
        </label>
        <span className="zh">系統提示詞</span>
        <span className="aside">{mode === "draft" ? "送出第一則時一起帶上" : "存了之後，下一則開始生效"}</span>
      </div>
      <textarea
        id="cs-system"
        className="cs-sysin"
        value={v}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return;
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            void save();
          }
        }}
        placeholder="給模型的角色與規則；留空＝不用 system prompt。"
        rows={5}
        autoFocus
        spellCheck={false}
      />
      {err ? (
        <div className="warn">
          <b>儲存失敗</b>：{err}
        </div>
      ) : null}
      <div className="cs-acts">
        <span className="cs-hint">Ctrl+Enter 儲存 · Esc 關閉</span>
        <button type="button" className="cs-act" onClick={onClose}>
          取消
        </button>
        <button type="button" className="cs-act main" onClick={() => void save()} disabled={!dirty || busy}>
          {busy ? "儲存中…" : mode === "draft" ? "套用" : "儲存"}
        </button>
      </div>
    </div>
  );
}
