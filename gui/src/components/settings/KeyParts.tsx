import { forwardRef, useImperativeHandle, useRef, useState } from "react";
import type { GetKey, TestKeyResult } from "@/api/types";
import { hhmm, reasonText } from "@/lib/catalog";

/* 貼 key 的零件（設定頁那一列原地展開、引導第 2、3 步共用）。
   key 的規則（版面稿 NOTES §4）：
   · 輸入框是 password 型、不受 React 控制（值不寫進 DOM 屬性、不進 state），沒有「顯示 key」的按鈕；
   · 畫面上只回報字數與末四碼，前後的空白自動去掉；
   · 送出之後呼叫端立刻 clear()。 */

export interface KeyMeta {
  len: number;
  last4: string;
}
export interface KeyInputHandle {
  /** 去掉前後空白的值（送出用；不要存起來） */
  value(): string;
  clear(): void;
  focus(): void;
}

export const KeyInput = forwardRef<KeyInputHandle, { label: string; disabled?: boolean; onMeta: (m: KeyMeta | null) => void }>(function KeyInput({ label, disabled, onMeta }, ref) {
  const el = useRef<HTMLInputElement>(null);
  const [pasteHint, setPasteHint] = useState(false);
  const report = () => {
    const raw = el.current?.value ?? "";
    const v = raw.trim();
    if (el.current && v !== raw) el.current.value = v;
    onMeta(v ? { len: v.length, last4: v.slice(-4) } : null);
  };
  useImperativeHandle(ref, () => ({
    value: () => (el.current?.value ?? "").trim(),
    clear: () => {
      if (el.current) el.current.value = "";
      onMeta(null);
    },
    focus: () => el.current?.focus(),
  }));
  const paste = async () => {
    try {
      const t = await navigator.clipboard.readText();
      if (el.current && t) {
        el.current.value = t;
        report();
        return;
      }
    } catch {
      /* 沒有剪貼簿權限：請使用者自己按 Ctrl+V */
    }
    setPasteHint(true);
    el.current?.focus();
  };
  return (
    <>
      <div className="kin-row">
        <input
          ref={el}
          type="password"
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          placeholder={pasteHint ? "按 Ctrl+V（或長按）貼上" : "在這裡貼上 key"}
          aria-label={label}
          disabled={disabled}
          onInput={report}
          data-key-input=""
        />
        <button className="paste" type="button" onClick={() => void paste()} disabled={disabled}>
          貼上
        </button>
      </div>
    </>
  );
});

export function KeyMetaLine({ meta }: { meta: KeyMeta | null }) {
  return (
    <div className="kin-meta" aria-live="polite">
      {meta ? (
        <>
          <span>
            已貼上 <span className="n">{meta.len}</span> 字
          </span>
          <span>
            末四碼 <span className="kmask">{meta.last4}</span>
          </span>
          <span>前後的空白已去掉</span>
        </>
      ) : (
        <span>還沒貼。按右邊「貼上」或 Ctrl+V。</span>
      )}
    </div>
  );
}

/** 去哪裡拿 key：只用 API 帶出的網址（前端不寫死），網址只顯示網域 */
export function GetKeyHint({ gk, compact }: { gk: GetKey | null | undefined; compact?: boolean }) {
  const host = (u: string) => {
    try {
      return new URL(u).host;
    } catch {
      return u;
    }
  };
  if (!gk || (!gk.url && !gk.docs)) return null;
  if (gk.url)
    return (
      <span>
        {compact ? "到 " : "在 "}
        <a className="lnk" href={gk.url} target="_blank" rel="noreferrer">
          {host(gk.url)}
        </a>{" "}
        建一把
      </span>
    );
  return (
    <span>
      官方說明：
      <a className="lnk" href={gk.docs!} target="_blank" rel="noreferrer">
        {host(gk.docs!)}
      </a>
    </span>
  );
}

export type TestState = { st: "untested" } | { st: "testing" } | { st: "done"; res: TestKeyResult };

/** 測試結果框：還沒測（虛線）／測試中（滾筒）／通過（反白章）／不通（斜紋＋原因＋還沒存）／沒辦法測（虛線章） */
export function TestBox({ t, label }: { t: TestState; label: string }) {
  if (t.st === "testing")
    return (
      <div className="tres testing" role="status">
        <div className="th">
          <span className="drum" aria-hidden="true" />
          <b>測試中…</b>
        </div>
        <p>請 {label} 列出模型清單。不叫模型、不花錢，通常幾秒內回來。</p>
      </div>
    );
  if (t.st === "untested")
    return (
      <div className="tres untested">
        <div className="th">
          <span className="verdict">還沒測</span>
        </div>
        <p>貼上之後按「測試」。測試只請 {label} 列模型，不花錢。</p>
      </div>
    );
  const r = t.res;
  const at = hhmm(r.checked_at);
  if (r.ok === true)
    return (
      <div className="tres ok" role="status">
        <div className="th">
          <span className="verdict">通過</span>
          <span className="k10">{at} 檢查</span>
        </div>
        {r.simulated ? (
          <p>
            <b>離線沙盒</b>：沒有真的送給 {label}，只檢查了 key 的格式。
          </p>
        ) : (
          <p>
            {label} 認得這把 key
            {r.models != null ? (
              <>
                ，列到 <span className="n">{r.models}</span> 個模型
              </>
            ) : null}
            {r.credits != null ? (
              <>
                ，帳戶還有 <span className="n">{r.credits}</span> credits
              </>
            ) : null}
            。
          </p>
        )}
        <p>按「存起來」就生效，不用重啟。</p>
      </div>
    );
  if (r.ok === null)
    return (
      <div className="tres null" role="status">
        <div className="th">
          <span className="verdict">沒辦法測</span>
          <span className="k10">{at}</span>
        </div>
        <p>{label} 沒有不花錢的檢查方式，這裡只確認了格式。</p>
        <p>存起來之後，第一次用到它就知道通不通；不通會在那一筆的結果裡寫原因。</p>
      </div>
    );
  return (
    <div className="tres fail" role="status">
      <div className="th">
        <span className="verdict">不通</span>
        <span className="k10">
          {at} 檢查{r.status ? ` · HTTP ${r.status}` : ""}
        </span>
      </div>
      <p>
        <b>{reasonText(r.reason, r.status)}。</b>
        {r.reason === "rejected" ? "常見原因：複製時少了幾個字、貼到別家的 key、key 剛建好還沒生效。" : null}
      </p>
      <p>還沒存。可以改貼一次；確定沒問題（例如現在離線）也可以照樣存。</p>
    </div>
  );
}
