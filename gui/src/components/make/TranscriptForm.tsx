/* 轉錄表單：transcribe_audio。音檔必填（上傳或從作品挑語音／音樂）；長度從 <audio> metadata 讀，帶給預估費用 */
import { api } from "@/api/client";
import { Field } from "@/components/dispatch";
import { setDraft, useMake } from "@/store/make";
import { buildTranscript, effModel, fmtBytes, msShort, type TranscriptDraft } from "./draft";
import { AudioPicker, AudioPlayer, Knob, ModelList, Seg, SubmitBar, artName, ctrlEnter, useFileDrop, useUploader, type FormProps } from "./bits";

const LANGS = [
  { v: "auto", label: "自動" },
  { v: "zh", label: "中文" },
  { v: "en", label: "英文" },
  { v: "ja", label: "日文" },
];

export function TranscriptForm({ opts, optsError, busy, error, onSend }: FormProps) {
  const d = useMake((s) => s.drafts.transcript);
  const set = (p: Partial<TranscriptDraft>) => setDraft("transcript", p);
  const models = opts?.kinds.transcript.models ?? [];
  const m = effModel(opts, "transcript", d.model);
  const built = buildTranscript(d, m);
  const send = () => built.req && !busy && onSend(built.req);
  const whisper = m?.id === "whisper-1";
  const up = useUploader();
  const drop = useFileDrop(async (f) => {
    const u = await up.run(f);
    if (!u) return;
    if (u.kind !== "audio") {
      up.setErr("這是圖，不是音檔。");
      return;
    }
    set({ source: { ref: { upload_id: u.id }, name: u.filename, url: api.uploadFileUrl(u.id), from: "upload", bytes: u.bytes, mime: u.mime, duration: null } });
  });
  const src = d.source;
  const pickedId = src && "artifact_id" in src.ref ? src.ref.artifact_id ?? null : null;

  return (
    <>
      <div className="mk-main" onKeyDown={ctrlEnter(send)}>
        <Field lbl="Audio" zh="音檔" aside="必填 · 上傳或從作品挑">
          {drop.input("audio/*,.mp3,.wav,.m4a,.mp4,.ogg,.flac,.webm")}
          {src ? (
            <div className={`mk-drop has${drop.over ? " over" : ""}`} {...drop.handlers}>
              <div className="mk-file">
                <div>
                  <div className="mk-fname">{src.name ?? "音檔"}</div>
                  <div className="mk-fmeta">
                    {src.from === "upload" ? "從電腦上傳" : "從作品挑的"}
                    {src.bytes != null ? (
                      <>
                        {" · "}
                        <span className="n">{fmtBytes(src.bytes)}</span>
                      </>
                    ) : null}
                    {src.mime ? ` · ${src.mime}` : null}
                  </div>
                </div>
                <div className="mk-dur big">{msShort(src.duration)}</div>
              </div>
              <AudioPlayer
                key={src.url}
                src={src.url}
                onDuration={(s) => {
                  if (d.source && d.source.duration !== s) set({ source: { ...d.source, duration: s } });
                }}
              />
              <div className="mk-dropfoot">
                <span>{up.busy ? "上傳中…" : "換一個：再拖一個檔進來就會取代"}</span>
                <button type="button" className="mk-mini" onClick={drop.open}>
                  選檔案…
                </button>
                <button type="button" className="mk-mini" onClick={() => set({ source: null })}>
                  拿掉
                </button>
              </div>
            </div>
          ) : (
            <div className={`mk-drop${drop.over ? " over" : ""}`} {...drop.handlers}>
              <span className="mk-dropbig">{up.busy ? "上傳中…" : "把音檔拖進來"}</span>
              <span className="dp-note">mp3、wav、m4a、ogg、flac、webm；25 MB 內</span>
              <button type="button" className="mk-mini" onClick={drop.open} disabled={up.busy}>
                選檔案…
              </button>
            </div>
          )}
          {up.err ? <div className="warn">{up.err}</div> : null}
        </Field>

        <Field lbl="From works" zh="或從作品挑" aside="語音與音樂">
          <AudioPicker
            selected={pickedId}
            onPick={(a) => set({ source: { ref: { artifact_id: a.id }, name: artName(a), url: a.file_url, from: a.kind, bytes: a.bytes, mime: a.mime, duration: a.duration_s } })}
          />
        </Field>

        <SubmitBar
          kind="transcript"
          built={built}
          busy={busy}
          error={error}
          onSend={send}
          recap={
            <>
              <span className="code">{m?.id ?? "—"}</span>
              {src ? <span className="code">{src.name}</span> : <span>還沒放音檔</span>}
              {src?.duration ? <span className="n">{msShort(src.duration)}</span> : null}
            </>
          }
        />
      </div>

      <aside className="mk-side" onKeyDown={ctrlEnter(send)}>
        <ModelList models={models} sel={m?.id ?? null} onSel={(id) => set({ model: id })} loading={!opts} error={optsError} />
        <Field lbl="Options" zh="選項">
          <div>
            <Knob zh="語言" en="LANG">
              <Seg label="語言" opts={LANGS} value={d.language} onChange={(v) => set({ language: v })} />
            </Knob>
            <Knob zh="提示" en="HINT">
              <input className="dp-in" value={d.prompt} onChange={(e) => set({ prompt: e.target.value })} placeholder="選填：人名、專有名詞，幫模型拼對" />
            </Knob>
            <Knob zh="格式" en="FORMAT">
              <Seg
                label="格式"
                opts={(whisper ? ["text", "srt", "vtt"] : ["text"]).map((v) => ({ v, label: v === "text" ? "純文字" : v }))}
                value={whisper ? d.response_format : "text"}
                onChange={(v) => set({ response_format: v })}
              />
              {!whisper ? <p className="dp-note mk-flush">字幕格式（srt、vtt）只有 <span className="code">whisper-1</span> 給。</p> : null}
            </Knob>
          </div>
        </Field>
      </aside>
    </>
  );
}
