import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import type { HarnessesResponse, ModelsResponse } from "@/api/types";
import { SendKeyHint } from "@/components/SendKeyMenu";
import { harnessClass, harnessName } from "@/lib/format";
import { enterSends, isSendKey, sendKeyName, useSendKey } from "@/lib/sendKey";
import { createChat } from "@/store/chat";
import {
  CwdPicker,
  Field,
  HarnessPicker,
  ModelPicker,
  Permissions,
  Toolbox,
  applyQuery,
  buildSpec,
  claudeEndpoint,
  flattenModels,
  indexModels,
  loadDraft,
  providerOf,
  resolveModel,
  routeHarness,
  saveDraft,
  type Draft,
} from "@/components/dispatch";

const errMsg = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

/* 新對話 `/new`（M5-a）：prompt 為主角，左欄 prompt／標題／工作目錄／送出，右欄模型／harness／權限。
   工具箱關＝聊天（M6-f）：左欄 prompt／標題，右欄模型（不受 harness 限制）＋system prompt；送出＝建立對話並跳到 /chat/:id */
export default function NewRunPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  // 草稿＋URL 預填（預填蓋過草稿；套用後把 query 拿掉，免得重新整理又蓋一次）
  const [d, setD] = useState<Draft>(() => applyQuery(loadDraft(), params));
  const set = (patch: Partial<Draft>) => setD((o) => ({ ...o, ...patch }));

  useEffect(() => {
    if (params.has("cwd") || params.has("model") || params.has("prompt")) {
      const rest = new URLSearchParams(params);
      ["cwd", "model", "prompt"].forEach((k) => rest.delete(k));
      setParams(rest, { replace: true });
    }
    // 只在進頁時跑一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => saveDraft(d), [d]);

  const [models, setModels] = useState<ModelsResponse | null>(null);
  const [modelsErr, setModelsErr] = useState<string | null>(null);
  const [hs, setHs] = useState<HarnessesResponse | null>(null);
  const [hsErr, setHsErr] = useState<string | null>(null);
  useEffect(() => {
    api.models().then(setModels).catch((e) => setModelsErr(errMsg(e)));
    api.harnesses().then(setHs).catch((e) => setHsErr(errMsg(e)));
  }, []);

  // 草稿留著「重播」但這台 daemon 沒有重播（正式 daemon）→ 退回跟著模型
  useEffect(() => {
    if (hs && !hs.replay && d.harness === "replay") set({ harness: "auto" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hs, d.harness]);

  const list = useMemo(() => flattenModels(models), [models]);
  const idx = useMemo(() => indexModels(list), [list]);
  const resolved = resolveModel(d.model, models?.tiers ?? {}, idx);
  const replay = d.harness === "replay";
  const autoHarness = routeHarness(resolved.id, resolved.entry);
  const effHarness = replay ? "replay" : d.harness === "auto" ? autoHarness : d.harness;
  const provider = replay ? null : providerOf(resolved.id, resolved.entry);

  // claude harness 需要的端點沒設定 → 送出前先講
  const endpoint = effHarness === "claude" ? claudeEndpoint(provider, d.auth) : null;
  const endpointOff = endpoint && hs?.claude?.endpoints && hs.claude.endpoints[endpoint] === false ? endpoint : null;

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [sendKey] = useSendKey();
  const errRef = useRef<HTMLDivElement>(null);

  const empty = !d.prompt.trim();
  const canSend = !empty && !busy;

  async function submit() {
    if (!canSend) return;
    setBusy(true);
    setErr(null);
    if (!d.toolbox) {
      // 聊天：建立對話＋送第一則；回覆的逐字內容到對話頁看
      try {
        const title = d.title.trim();
        const system = d.system.trim();
        const id = await createChat({ model: resolved.send, system: system || undefined, title: title || undefined, message: d.prompt });
        // 模型與 system prompt 留著當下次預設
        saveDraft({ ...d, prompt: "", title: "" });
        navigate("/chat/" + encodeURIComponent(id));
      } catch (e) {
        setErr(errMsg(e));
        setBusy(false);
        requestAnimationFrame(() => errRef.current?.scrollIntoView({ block: "nearest" }));
      }
      return;
    }
    try {
      const r = await api.startRun(buildSpec(d, { harness: effHarness, provider }));
      // 成功：清掉 prompt／標題；模型、目錄、權限留著當下次預設（yolo 與 API 計費不留，M5-e：放寬權限、會花錢的不能變預設）
      saveDraft({ ...d, prompt: "", title: "", yolo: false, auth: "subscription" });
      navigate("/runs/" + encodeURIComponent(r.run_id));
    } catch (e) {
      setErr(errMsg(e));
      setBusy(false);
      requestAnimationFrame(() => errRef.current?.scrollIntoView({ block: "nearest" }));
    }
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit();
  };
  const onKey = (e: KeyboardEvent) => {
    // 送出鍵照設定；Enter 模式下單按 Enter 只在 prompt 欄送出（標題、system prompt 等欄位照舊）
    if (isSendKey(e, { plain: enterSends(e.target) })) {
      e.preventDefault();
      void submit();
    }
  };

  const chars = [...d.prompt].length;

  return (
    <section className="wrap dp">
      <div className="sechead">
        <h2>
          <span className="ovp" data-t="New">
            New
          </span>
        </h2>
        <span className="zh">{d.toolbox ? "派工" : "聊天"}</span>
        <span className="dp-sub">{d.toolbox ? "送出後直接看它跑" : "送出後跳到聊天頁"}</span>
        <Toolbox on={d.toolbox} onChange={(v) => set({ toolbox: v })} />
      </div>

      {err ? (
        <div className="warn dp-err" ref={errRef} role="alert">
          <b>{d.toolbox ? "派工失敗" : "開聊失敗"}</b>：{err}
        </div>
      ) : null}

      <form className="dp-grid" onSubmit={onSubmit} onKeyDown={onKey} aria-busy={busy}>
        <div className="dp-main">
          <Field lbl="Prompt" zh={d.toolbox ? "要交代的事" : "第一則訊息"} htmlFor="dp-prompt" aside={<><span className="n">{chars}</span> 字 · <SendKeyHint /></>}>
            <textarea
              id="dp-prompt"
              data-enter-sends=""
              className="dp-prompt"
              value={d.prompt}
              onChange={(e) => set({ prompt: e.target.value })}
              placeholder={d.toolbox ? "寫下要 agent 做的事：目標、範圍、完成的樣子。" : "想問模型什麼？"}
              autoFocus
              spellCheck={false}
            />
          </Field>

          <Field lbl="Title" zh="標題" htmlFor="dp-title" aside="選填">
            <input id="dp-title" className="dp-in dp-title" value={d.title} onChange={(e) => set({ title: e.target.value })} placeholder="留空＝取 prompt 前 40 字" />
          </Field>

          {d.toolbox ? <CwdPicker value={d.cwd} onChange={(v) => set({ cwd: v })} /> : null}

          <div className="dp-submit">
            <div className="dp-recap">
              {!d.toolbox ? (
                <>
                  <span>聊天</span>
                  <span className="code">{resolved.send}</span>
                  {d.model.kind === "tier" && resolved.id ? <span className="code">→ {resolved.id}</span> : null}
                  {d.system.trim() ? <b>有 system prompt</b> : null}
                </>
              ) : replay ? (
                <>
                  <span className="hm h-unknown">重播</span>
                  <span className="code">{d.replayId.trim() ? `replay:${d.replayId.trim()}` : "replay"}</span>
                  <span>不呼叫模型、不計費</span>
                </>
              ) : (
                <>
                  <span className={`hm ${harnessClass(effHarness)}`}>{effHarness ? harnessName(effHarness) : "harness 待定"}</span>
                  <span className="code">{resolved.send}</span>
                  {d.model.kind === "tier" && resolved.id ? <span className="code">→ {resolved.id}</span> : null}
                  {d.yolo ? <b>yolo</b> : null}
                  {provider === "anthropic" && d.auth === "api" ? <b>API 計費</b> : null}
                </>
              )}
              {d.toolbox && endpointOff ? (
                <div className="warn">
                  Claude Code 要走 <span className="code">{endpointOff}</span> 端點，但它<b>沒設定 key</b>，送出會失敗。
                </div>
              ) : null}
            </div>
            <button type="submit" className="stamp-btn dp-go" disabled={!canSend} title={empty ? "先寫 prompt" : sendKeyName(sendKey)}>
              {busy ? (d.toolbox ? "派工中…" : "開聊中…") : (
                <>
                  <b>→</b>
                  {d.toolbox ? "派工" : "開聊"}
                </>
              )}
            </button>
          </div>
          {empty ? <div className="dp-note dp-hint">prompt 空白不能送出。</div> : null}
        </div>

        <aside className="dp-side">
          <ModelPicker
            data={models}
            list={list}
            idx={idx}
            error={modelsErr}
            sel={d.model}
            onSel={(m) => set({ model: m })}
            longtail={d.longtail}
            onLongtail={(v) => set({ longtail: v })}
            replay={d.toolbox && replay}
            resolved={resolved}
            harnessMarks={d.toolbox}
          />
          {d.toolbox ? (
            <>
              <HarnessPicker hs={hs} error={hsErr} sel={d.harness} onSel={(h) => set({ harness: h })} auto={autoHarness} replayId={d.replayId} onReplayId={(v) => set({ replayId: v })} />
              <Permissions d={d} set={set} harness={effHarness} provider={provider} replay={replay} />
            </>
          ) : (
            <Field lbl="System prompt" zh="系統提示詞" htmlFor="dp-system" aside="選填 · 之後可在聊天頁改">
              <textarea
                id="dp-system"
                className="dp-prompt dp-system"
                value={d.system}
                onChange={(e) => set({ system: e.target.value })}
                placeholder="給模型的角色與規則，例如：你是嚴格的程式碼審查者，用繁體中文回答。"
                spellCheck={false}
              />
              <p className="dp-chatnote">聊天不派 agent、不碰檔案，不需要工作目錄與權限；任何文字模型都能聊，不受 harness 限制。</p>
            </Field>
          )}
        </aside>
      </form>
    </section>
  );
}
