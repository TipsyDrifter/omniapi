/* 音樂表單：generate_music。ElevenLabs（music_v*）＝描述＋長度；Suno（kie）＝照描述或照歌詞（custom_mode） */
import { api } from "@/api/client";
import { Field } from "@/components/dispatch";
import { setDraft, useMake } from "@/store/make";
import { MUSIC_LENGTHS, buildMusic, effModel, musicFamily, msShort, type MusicDraft } from "./draft";
import { Knob, LyricsPicker, ModelList, Seg, SubmitBar, ctrlEnter, errMsg, type FormProps } from "./bits";
import { useState } from "react";

export function MusicForm({ opts, optsError, busy, error, onSend }: FormProps) {
  const d = useMake((s) => s.drafts.music);
  const set = (p: Partial<MusicDraft>) => setDraft("music", p);
  const models = opts?.kinds.music.models ?? [];
  const m = effModel(opts, "music", d.model);
  const fam = musicFamily(m);
  const lyrics = fam === "suno" && d.mode === "lyrics";
  const built = buildMusic(d, m);
  const send = () => built.req && !busy && onSend(built.req);
  const [lyrErr, setLyrErr] = useState<string | null>(null);

  return (
    <>
      <div className="mk-main" onKeyDown={ctrlEnter(send)}>
        {fam === "suno" ? (
          <Field lbl="Mode" zh="寫法" aside="Suno 系列可以照歌詞作曲">
            <Seg
              label="寫法"
              opts={[
                { v: "describe", label: "照描述" },
                { v: "lyrics", label: "照歌詞" },
              ]}
              value={d.mode}
              onChange={(v) => set({ mode: v })}
            />
          </Field>
        ) : null}

        {lyrics ? (
          <>
            <Field
              lbl="Lyrics"
              zh="歌詞"
              htmlFor="mk-lyrics"
              aside={
                <>
                  <span className="n">{d.lyrics ? d.lyrics.split("\n").length : 0}</span> 行 · [Verse]／[Chorus] 標段落
                </>
              }
            >
              <textarea id="mk-lyrics" className="dp-prompt mk-prompt lyr" value={d.lyrics} onChange={(e) => set({ lyrics: e.target.value, lyricsFrom: null })} placeholder="貼上歌詞。" spellCheck={false} autoFocus />
              <LyricsPicker
                selected={d.lyricsFrom}
                onPick={async (a) => {
                  setLyrErr(null);
                  try {
                    const full = await api.artifact(a.id);
                    set({ lyrics: full.text ?? "", lyricsFrom: a.id, title: d.title || full.title || "" });
                  } catch (e) {
                    setLyrErr(errMsg(e));
                  }
                }}
              />
              {lyrErr ? <div className="warn">讀不到這份歌詞：{lyrErr}</div> : null}
            </Field>
            <Field lbl="Style" zh="風格" htmlFor="mk-style" aside="必填">
              <input id="mk-style" className="dp-in" value={d.style} onChange={(e) => set({ style: e.target.value })} placeholder="曲風、樂器、速度、情緒；例：city pop, electric piano, 92 bpm, wistful" />
            </Field>
            <Field lbl="Title" zh="標題" htmlFor="mk-title" aside="必填">
              <input id="mk-title" className="dp-in dp-title" value={d.title} onChange={(e) => set({ title: e.target.value })} placeholder="這首歌叫什麼" />
            </Field>
          </>
        ) : (
          <Field
            lbl="Prompt"
            zh="描述"
            htmlFor="mk-mprompt"
            aside={
              <>
                <span className="n">{[...d.prompt].length}</span> 字 · Ctrl＋Enter 送出
              </>
            }
          >
            <textarea
              id="mk-mprompt"
              className="dp-prompt mk-prompt tall"
              value={d.prompt}
              onChange={(e) => set({ prompt: e.target.value })}
              placeholder="描述這首歌：曲風、情緒、樂器、速度感；有人聲時模型會自己寫詞。"
              spellCheck={false}
              autoFocus
            />
          </Field>
        )}

        <SubmitBar
          kind="music"
          built={built}
          busy={busy}
          error={error}
          onSend={send}
          recap={
            <>
              <span className="code">{m?.id ?? "—"}</span>
              <span>{lyrics ? "照歌詞" : "照描述"}</span>
              {fam === "elevenlabs" ? <span>{msShort(d.length_ms / 1000)}</span> : null}
              <span>{d.instrumental ? "純音樂" : "有人聲"}</span>
            </>
          }
        />
      </div>

      <aside className="mk-side" onKeyDown={ctrlEnter(send)}>
        <ModelList models={models} sel={m?.id ?? null} onSel={(id) => set({ model: id })} loading={!opts} error={optsError} />
        <Field lbl="Shape" zh="長度與人聲" aside={fam === "elevenlabs" ? "按分鐘計價" : fam === "suno" ? "Suno 自己決定長度" : ""}>
          <div>
            {fam === "elevenlabs" ? (
              <Knob zh="長度" en="LENGTH">
                <Seg label="長度" opts={MUSIC_LENGTHS.map(([v, l]) => ({ v, label: l }))} value={d.length_ms} onChange={(v) => set({ length_ms: v })} />
              </Knob>
            ) : null}
            <Knob zh="人聲" en="VOCAL">
              <Seg
                label="人聲"
                opts={[
                  { v: false, label: "有人聲" },
                  { v: true, label: "純音樂" },
                ]}
                value={d.instrumental}
                onChange={(v) => set({ instrumental: v })}
              />
            </Knob>
            {lyrics ? (
              <>
                <Knob zh="唱的人" en="GENDER">
                  <Seg
                    label="唱的人"
                    opts={[
                      { v: "", label: "不指定" },
                      { v: "m", label: "男聲" },
                      { v: "f", label: "女聲" },
                    ]}
                    value={d.vocal_gender}
                    onChange={(v) => set({ vocal_gender: v as MusicDraft["vocal_gender"] })}
                  />
                </Knob>
                <Knob zh="避開" en="NEGATIVE">
                  <input className="dp-in" value={d.negative_tags} onChange={(e) => set({ negative_tags: e.target.value })} placeholder="選填：不要的風格或樂器，例：heavy metal, autotune" />
                </Knob>
              </>
            ) : null}
          </div>
        </Field>
        <p className="dp-note mk-flush">音樂要等 1～3 分鐘。送出後可以離開這頁，回來時出件口還留著它；頂欄「生成」旁會標進行中的件數。</p>
      </aside>
    </>
  );
}
