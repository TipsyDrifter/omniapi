/* 語音表單：generate_speech。聲音依所選模型的 provider 列，可搜尋、可試聽；做好在出件口當場可播 */
import { useDeferredValue, useState } from "react";
import type { Voice } from "@/api/types";
import { Field } from "@/components/dispatch";
import { setDraft, useMake } from "@/store/make";
import { buildSpeech, effModel, speechKnobs, type SpeechDraft } from "./draft";
import { Knob, ModelList, SubmitBar, sendKeys, type FormProps } from "./bits";
import { SendKeyHint } from "@/components/SendKeyMenu";

let previewing: HTMLAudioElement | null = null;

export function SpeechForm({ opts, optsError, busy, error, onSend }: FormProps) {
  const d = useMake((s) => s.drafts.speech);
  const set = (p: Partial<SpeechDraft>) => setDraft("speech", p);
  const models = opts?.kinds.speech.models ?? [];
  const m = effModel(opts, "speech", d.model);
  const provider = m?.provider ?? "";
  const vs = opts?.kinds.speech.voices?.[provider] ?? null;
  const usable = (v: Voice) => !v.only || !m || v.only.includes(m.id);
  const want = d.voice[provider];
  const chosen = vs?.voices.find((v) => v.id === want && usable(v)) ?? vs?.voices.find((v) => v.id === vs.default) ?? null;
  const voiceId = chosen?.id ?? vs?.default ?? null;
  const built = buildSpeech(d, m, voiceId);
  const k = speechKnobs(m);
  const send = () => built.req && !busy && onSend(built.req);

  const [q, setQ] = useState("");
  const dq = useDeferredValue(q.trim().toLowerCase());
  const list = (vs?.voices ?? []).filter((v) => !dq || v.name.toLowerCase().includes(dq) || v.id.toLowerCase().includes(dq) || (v.note ?? "").toLowerCase().includes(dq));
  const [playing, setPlaying] = useState<string | null>(null);
  const preview = (v: Voice) => {
    if (!v.preview_url) return;
    previewing?.pause();
    if (playing === v.id) {
      setPlaying(null);
      return;
    }
    const a = new Audio(v.preview_url);
    previewing = a;
    a.onended = () => setPlaying(null);
    setPlaying(v.id);
    void a.play().catch(() => setPlaying(null));
  };

  return (
    <>
      <div className="mk-main" onKeyDown={sendKeys(send)}>
        <Field
          lbl="Text"
          zh="要念的文字"
          htmlFor="mk-text"
          aside={
            <>
              <span className="n">{[...d.text].length}</span> 字<SendKeyHint sep />
            </>
          }
        >
          <textarea
            id="mk-text"
            data-enter-sends=""
            className="dp-prompt mk-prompt tall"
            value={d.text}
            onChange={(e) => set({ text: e.target.value })}
            placeholder="貼上要念的文字。標點會影響停頓；一次念完，不用分段送。"
            spellCheck={false}
            autoFocus
          />
        </Field>
        <SubmitBar
          kind="speech"
          built={built}
          busy={busy}
          error={error}
          onSend={send}
          recap={
            <>
              <span className="code">{m?.id ?? "—"}</span>
              {chosen ? (
                <span>
                  聲音 <b className="mk-plain">{chosen.name}</b>
                </span>
              ) : null}
            </>
          }
        />
      </div>

      <aside className="mk-side" onKeyDown={sendKeys(send)}>
        <ModelList models={models} sel={m?.id ?? null} onSel={(id) => set({ model: id })} loading={!opts} error={optsError} />
        <Field lbl="Voice" zh="聲音" aside={vs ? `${provider} · ${vs.voices.length} 個${vs.live ? " · 現查" : ""}` : null}>
          {vs?.error ? (
            <div className="warn">
              <b>聲音清單拿不到</b>：仍可用預設聲音送出。<span className="code">{vs.error}</span>
            </div>
          ) : null}
          {(vs?.voices.length ?? 0) > 8 ? (
            <input type="search" className="dp-in mk-vq" placeholder="搜尋聲音名稱、id、說明" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜尋聲音" />
          ) : null}
          <div className="mk-mlist mk-vlist" role="listbox" aria-label="聲音">
            {!vs ? <div className="dp-empty">{opts ? "這個 provider 沒有聲音清單" : "讀取聲音…"}</div> : null}
            {vs && !list.length ? <div className="dp-empty">沒有符合的聲音</div> : null}
            {list.map((v) => {
              const ok = usable(v);
              return (
                <div key={v.id} className="mk-vrow" role="option" aria-selected={v.id === voiceId} aria-disabled={!ok}>
                  <button type="button" className="mk-vpick" disabled={!ok} onClick={() => set({ voice: { ...d.voice, [provider]: v.id } })}>
                    <span className="mk-vname">{v.name}</span>
                    {v.id === vs?.default ? <span className="dp-tag">預設</span> : null}
                    {v.name !== v.id ? <span className="code mk-vid">{v.id}</span> : null}
                    {v.note ? <span className="mk-prov">{v.note}</span> : null}
                    {!ok ? (
                      <span className="mk-why">
                        只有 <span className="code">{v.only!.join("、")}</span> 能用
                      </span>
                    ) : null}
                  </button>
                  {v.preview_url ? (
                    <button type="button" className="mk-mini mk-vplay" aria-pressed={playing === v.id} onClick={() => preview(v)}>
                      {playing === v.id ? "停" : "試聽"}
                    </button>
                  ) : null}
                </div>
              );
            })}
          </div>
        </Field>
        {k.instructions || k.speed || k.language ? (
          <Field lbl="Options" zh="選項" aside="只列這個模型吃的">
            <div>
              {k.instructions ? (
                <Knob zh="語氣" en="TONE">
                  <input className="dp-in" value={d.instructions} onChange={(e) => set({ instructions: e.target.value })} placeholder="選填：例如「溫和、放慢、句尾帶笑」" />
                </Knob>
              ) : null}
              {k.speed ? (
                <Knob zh="語速" en="SPEED">
                  <input className="dp-in code mk-narrow" type="number" min={0.25} max={4} step={0.05} value={d.speed} onChange={(e) => set({ speed: e.target.value })} placeholder="1.0" />
                  <span className="dp-note"> 0.25～4，留空＝1.0</span>
                </Knob>
              ) : null}
              {k.language ? (
                <Knob zh="語言" en="LANG">
                  <input className="dp-in code mk-narrow" value={d.language_code} onChange={(e) => set({ language_code: e.target.value })} placeholder="zh" />
                  <span className="dp-note"> ISO 代碼（zh、en、ja）；留空＝自動</span>
                </Knob>
              ) : null}
            </div>
          </Field>
        ) : null}
      </aside>
    </>
  );
}
