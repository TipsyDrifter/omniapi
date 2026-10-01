import { useEffect, useState } from "react";
import { ApiError, api } from "@/api/client";
import type { CwdEntry, FsDirs } from "@/api/types";
import { Field } from "./Field";

const errMsg = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

/* 工作目錄（M5-d）：空＝最近用過；打字＝debounce 200ms 列子資料夾，點一下填入並繼續往下列 */
export function CwdPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [recent, setRecent] = useState<CwdEntry[] | null>(null);
  const [recentErr, setRecentErr] = useState<string | null>(null);
  const [fs, setFs] = useState<FsDirs | null>(null);
  const [fsErr, setFsErr] = useState<string | null>(null);
  const typed = value.trim();

  useEffect(() => {
    api
      .cwds()
      .then(setRecent)
      .catch((e) => setRecentErr(errMsg(e)));
  }, []);

  useEffect(() => {
    if (!typed) {
      setFs(null);
      setFsErr(null);
      return;
    }
    let alive = true;
    const t = setTimeout(() => {
      api
        .fsDirs(typed)
        .then((r) => {
          if (!alive) return;
          setFs(r);
          setFsErr(null);
        })
        .catch((e) => alive && setFsErr(errMsg(e)));
    }, 200);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [typed]);

  // 回應可能落後輸入：只有同一個輸入的結果才拿來標「不存在」
  const missing = !!typed && !!fs && fs.exists === false;

  return (
    <Field lbl="Cwd" zh="工作目錄" htmlFor="dp-cwd" aside="選填 · 只列資料夾名，不讀檔案">
      <div className="dp-cwdrow">
        <input id="dp-cwd" className="dp-in code" value={value} onChange={(e) => onChange(e.target.value)} placeholder="C:\path\to\project" spellCheck={false} autoComplete="off" />
        {missing ? <span className="dp-tag solid">路徑不存在</span> : null}
        {value ? (
          <button type="button" className="dp-mini" onClick={() => onChange("")}>
            清除
          </button>
        ) : null}
      </div>
      {!typed ? <div className="warn">沒指定工作目錄：agent 會在 daemon 的目錄下工作。</div> : null}

      <div className="dp-cands">
        {!typed ? (
          <>
            <div className="dp-ch">
              <span className="lbl">Recent</span>
              <span className="zh">最近用過</span>
            </div>
            {recentErr ? <div className="dp-empty">讀不到：{recentErr}</div> : null}
            {!recent && !recentErr ? <div className="dp-empty">讀取中…</div> : null}
            {recent && recent.length === 0 ? <div className="dp-empty">還沒有派工紀錄</div> : null}
            {recent?.map((c) => (
              <button key={c.cwd} type="button" className="dp-crow" disabled={!c.exists} onClick={() => onChange(c.cwd)} title={c.exists ? "填入" : "這個資料夾已不存在"}>
                <span className="code">{c.cwd}</span>
                {c.exists ? null : <span className="dp-tag">已不存在</span>}
                <span className="dp-uses">
                  <span className="n">{c.n}</span> 次
                </span>
              </button>
            ))}
          </>
        ) : (
          <>
            <div className="dp-ch">
              <span className="lbl">Dirs</span>
              <span className="zh">{fs?.exists === false ? "開頭相符的資料夾" : "子資料夾"}</span>
            </div>
            {fsErr ? <div className="dp-empty">讀不到：{fsErr}</div> : null}
            {fs?.parent ? (
              <button type="button" className="dp-crow up" onClick={() => onChange(fs.parent!)}>
                <span className="code">↑ {fs.parent}</span>
                <span className="dp-uses">上一層</span>
              </button>
            ) : null}
            {fs && fs.dirs.length === 0 ? <div className="dp-empty">{fs.exists ? "底下沒有資料夾" : "找不到相符的資料夾"}</div> : null}
            {fs?.dirs.map((d) => (
              <button key={d} type="button" className="dp-crow" onClick={() => onChange(d)}>
                <span className="code">{d}</span>
              </button>
            ))}
          </>
        )}
      </div>
    </Field>
  );
}
