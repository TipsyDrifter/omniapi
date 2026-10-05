/* 圖片表單：generate_image；有來源圖＝edit_image（送出鈕變「改圖」） */
import { useState } from "react";
import { api } from "@/api/client";
import { Field } from "@/components/dispatch";
import { setDraft, useMake } from "@/store/make";
import { GOOGLE_RATIOS, GOOGLE_SIZES, buildImage, effModel, imageCaps, type ImageDraft } from "./draft";
import { ImagePicker, Knob, ModelList, Seg, SubmitBar, artName, sendKeys, useFileDrop, useUploader, type FormProps } from "./bits";
import { SendKeyHint } from "@/components/SendKeyMenu";

const MAX_PROMPT = 4000;

/** 尺寸鈕前面畫一個比例框 */
function Ar({ w, h }: { w: number; h: number }) {
  const k = 14 / Math.max(w, h);
  return <span className="mk-ar" style={{ width: Math.round(w * k), height: Math.round(h * k) }} />;
}
const sizeLabel = (s: string) => {
  const m = s.match(/^(\d+)x(\d+)$/);
  return m ? (
    <>
      <Ar w={+m[1]} h={+m[2]} />
      {s.replace("x", "×")}
    </>
  ) : (
    s
  );
};
const ratioLabel = (r: string) => {
  const [w, h] = r.split(":").map(Number);
  return (
    <>
      <Ar w={w} h={h} />
      {r}
    </>
  );
};
const BG: Record<string, string> = { auto: "auto", transparent: "透明", opaque: "不透明" };

export function ImageForm({ opts, optsError, busy, error, onSend }: FormProps) {
  const d = useMake((s) => s.drafts.image);
  const set = (p: Partial<ImageDraft>) => setDraft("image", p);
  const models = opts?.kinds.image.models ?? [];
  const m = effModel(opts, "image", d.model);
  const caps = imageCaps(opts, m);
  const built = buildImage(d, m, caps);
  const google = caps.provider === "google";
  const edit = !!d.source;
  const [pick, setPick] = useState(false);
  const up = useUploader();
  const drop = useFileDrop(async (f) => {
    const u = await up.run(f);
    if (!u) return;
    if (u.kind !== "image") {
      up.setErr("這是音檔，不是圖。");
      return;
    }
    set({ source: { ref: { upload_id: u.id }, name: u.filename, url: api.uploadFileUrl(u.id), from: "upload", bytes: u.bytes, mime: u.mime } });
  });
  const send = () => built.req && !busy && onSend(built.req);
  const chars = [...d.prompt].length;
  const perImage = m?.pricing?.unit === "per_image" ? m.pricing : null;

  return (
    <>
      <div className="mk-main" onKeyDown={sendKeys(send)}>
        <Field
          lbl="Prompt"
          zh="提示詞"
          htmlFor="mk-prompt"
          aside={
            <>
              <span className="n">{chars}</span> / <span className="n">{MAX_PROMPT}</span> 字<SendKeyHint sep />
            </>
          }
        >
          <textarea
            id="mk-prompt"
            data-enter-sends=""
            className="dp-prompt mk-prompt"
            value={d.prompt}
            maxLength={MAX_PROMPT}
            onChange={(e) => set({ prompt: e.target.value })}
            placeholder={edit ? "要怎麼改：保留什麼、換掉什麼、改成什麼樣子。" : "想要什麼畫面：主體、構圖、光線、風格。"}
            spellCheck={false}
            autoFocus
          />
        </Field>

        <Field lbl="Source" zh="來源圖" aside="選填 · 有圖＝改圖，留空＝生圖">
          <div className="mk-src">
            <div className={`mk-slot${d.source ? " has" : ""}${drop.over ? " over" : ""}`} {...drop.handlers} onClick={drop.open} role="button" tabIndex={0} aria-label="上傳來源圖">
              {d.source ? <img src={d.source.url} alt="" /> : up.busy ? "上傳中…" : (
                <>
                  <span className="x-touch">
                    拖一張圖進來
                    <br />
                    或點這裡上傳
                  </span>
                  <span className="only-touch">點這裡選一張圖</span>
                </>
              )}
            </div>
            {drop.input("image/png,image/jpeg,image/webp,image/gif")}
            <div className="mk-src-r">
              {d.source ? (
                <div>
                  <span className="code">{d.source.name ?? ("artifact_id" in d.source.ref ? d.source.ref.artifact_id : "")}</span>
                  <div className="dp-note">{d.source.from === "upload" ? "從電腦上傳的" : "從作品挑的"} · 送出會用<b>改圖</b></div>
                </div>
              ) : (
                <p className="dp-note mk-flush">
                  有來源圖＝<b>改圖</b>；沒有＝<b>生圖</b>。可以上傳（png、jpg、webp，50 MB 內），也可以直接從作品挑。
                </p>
              )}
              <div className="mk-row">
                <button type="button" className="mk-mini" onClick={drop.open} disabled={up.busy}>
                  上傳…
                </button>
                <button type="button" className="mk-mini" aria-pressed={pick} onClick={() => setPick(!pick)}>
                  {pick ? "收起作品" : "從作品挑"}
                </button>
                {d.source ? (
                  <button type="button" className="mk-mini" onClick={() => set({ source: null })}>
                    拿掉
                  </button>
                ) : null}
              </div>
              {up.err ? <div className="warn">{up.err}</div> : null}
            </div>
          </div>
          {pick ? (
            <ImagePicker
              selected={d.source && "artifact_id" in d.source.ref ? d.source.ref.artifact_id ?? null : null}
              onPick={(a) => {
                set({ source: { ref: { artifact_id: a.id }, name: artName(a), url: a.thumb_url ? `${a.thumb_url}?w=480` : a.file_url, from: "image", bytes: a.bytes, mime: a.mime } });
                setPick(false);
              }}
            />
          ) : null}
        </Field>

        <SubmitBar
          kind="image"
          built={built}
          busy={busy}
          error={error}
          onSend={send}
          recap={
            <>
              <span className="code">{m?.id ?? "—"}</span>
              {google ? (edit ? <span>跟著來源圖</span> : <span>{d.aspect_ratio} · {d.image_size}</span>) : caps.provider === "openai" ? <span>{d.size.replace("x", "×")} · {d.quality}</span> : null}
              {!edit && caps.provider === "openai" ? <span>{Math.min(d.n, caps.max_images)} 張</span> : null}
              {edit ? (
                <span>
                  來源 <span className="code">{d.source?.name ?? "—"}</span>
                </span>
              ) : null}
            </>
          }
        />
      </div>

      <aside className="mk-side" onKeyDown={sendKeys(send)}>
        <ModelList models={models} sel={m?.id ?? null} onSel={(id) => set({ model: id })} loading={!opts} error={optsError} />
        <Field lbl="Output" zh="輸出" aside={google ? "Google 系列：比例＋解析度" : caps.provider === "openai" ? "OpenAI 系列" : m?.provider ?? ""}>
          {google ? (
            edit ? (
              <p className="dp-note mk-flush">Google 系列改圖只送提示詞與模型；比例與解析度跟著來源圖。</p>
            ) : (
              <div>
                <Knob zh="比例" en="RATIO">
                  <Seg label="比例" opts={GOOGLE_RATIOS.map((r) => ({ v: r, label: ratioLabel(r) }))} value={d.aspect_ratio} onChange={(v) => set({ aspect_ratio: v })} />
                </Knob>
                <Knob zh="解析度" en="RES">
                  <Seg
                    label="解析度"
                    opts={GOOGLE_SIZES.map((r) => ({
                      v: r,
                      label: (
                        <>
                          {r}
                          {perImage && typeof perImage[r] === "number" ? <span className="code">${+(perImage[r] as number).toFixed(4)}</span> : null}
                        </>
                      ),
                    }))}
                    value={d.image_size}
                    onChange={(v) => set({ image_size: v })}
                  />
                </Knob>
                <Knob zh="張數" en="N">
                  <span className="dp-note">Google 系列一次一張。</span>
                </Knob>
              </div>
            )
          ) : caps.provider === "openai" ? (
            <div>
              <Knob zh="尺寸" en="SIZE">
                <Seg label="尺寸" opts={caps.sizes.map((s) => ({ v: s, label: sizeLabel(s) }))} value={d.size} onChange={(v) => set({ size: v })} />
              </Knob>
              <Knob zh="品質" en="QUALITY">
                <Seg label="品質" opts={caps.qualities.map((s) => ({ v: s, label: s }))} value={d.quality} onChange={(v) => set({ quality: v })} />
              </Knob>
              {caps.supports_background ? (
                <Knob zh="背景" en="BG">
                  <Seg label="背景" opts={["auto", "transparent", "opaque"].map((s) => ({ v: s, label: BG[s] }))} value={d.background} onChange={(v) => set({ background: v })} />
                </Knob>
              ) : null}
              <Knob zh="格式" en="FORMAT">
                <Seg label="格式" opts={caps.formats.map((s) => ({ v: s, label: s }))} value={d.output_format} onChange={(v) => set({ output_format: v })} />
              </Knob>
              <Knob zh="張數" en="N">
                {edit ? (
                  <span className="dp-note">改圖一次一張。</span>
                ) : (
                  <Seg label="張數" opts={Array.from({ length: caps.max_images }, (_, i) => ({ v: i + 1, label: String(i + 1) }))} value={Math.min(d.n, caps.max_images)} onChange={(v) => set({ n: v })} />
                )}
              </Knob>
            </div>
          ) : (
            <p className="dp-note mk-flush">這個模型只送提示詞與模型。</p>
          )}
        </Field>
      </aside>
    </>
  );
}
