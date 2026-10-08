/* 影片播放器（v1.4）：原生 <video>＋自己畫的控制列（同音檔播放器的語彙：圓鈕＋時間尺）。
   small＝出件口的卡上（播放、時間尺、聲音、全螢幕）；big＝燈箱的舞台（多目前時間、拖鈕、音量條）。
   全站同一時間只播一段（claimAudio）；音量與靜音在這次開頁之內沿用上一支的設定；
   每一支停在哪記在記憶體裡（換一件再回來從那裡接著播，不自動播）。 */
import { useEffect, useImperativeHandle, useRef, useState, type CSSProperties, type PointerEvent, type Ref } from "react";
import { claimAudio } from "./bits";
import { msShort } from "./draft";

export interface PlayerHandle {
  toggle(): void;
  /** 往前／往後跳幾秒 */
  skip(delta: number): void;
  pause(): void;
  playing(): boolean;
  time(): number;
}

/** 這次開頁之內：音量、靜音（沿用上一支）、每一支停在哪 */
const pref = { volume: 0.8, muted: false };
const stoppedAt = new Map<string, number>();
/** 最近一次「播到一半被換走」的那一支（燈箱換一件時說一句） */
let lastStop: { key: string; t: number } | null = null;
export function takeLastStop(): { key: string; t: number } | null {
  const s = lastStop;
  lastStop = null;
  return s;
}

export function VideoPlayer({
  src,
  poster,
  hasAudio,
  big,
  width,
  height,
  durationHint,
  posKey,
  handleRef,
  label,
}: {
  src: string;
  poster?: string | null;
  /** false＝沒有聲音（不給聲音鈕，給虛線章「無聲」）；null＝不知道（照有聲音處理） */
  hasAudio: boolean | null | undefined;
  big?: boolean;
  width?: number | null;
  height?: number | null;
  durationHint?: number | null;
  /** 記住停在哪的鍵（作品 id） */
  posKey?: string;
  handleRef?: Ref<PlayerHandle>;
  label?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const ref = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [t, setT] = useState(0);
  const [dur, setDur] = useState<number | null>(durationHint ?? null);
  const [muted, setMuted] = useState(pref.muted);
  const [vol, setVol] = useState(pref.volume);
  const [broken, setBroken] = useState(false);
  const drag = useRef<"time" | "vol" | null>(null);
  /** 播放中與否：從 play／pause 事件記（元素被拿出頁面時瀏覽器會先自己暫停，卸載那一刻看 paused 已經不準） */
  const playingRef = useRef(false);

  const toggle = () => {
    const v = ref.current;
    if (!v) return;
    if (v.paused) {
      claimAudio(v);
      void v.play().catch(() => setPlaying(false));
    } else v.pause();
  };
  const skip = (d: number) => {
    const v = ref.current;
    if (!v || !Number.isFinite(v.duration)) return;
    v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + d));
    setT(v.currentTime);
  };
  useImperativeHandle(handleRef, () => ({
    toggle,
    skip,
    pause: () => ref.current?.pause(),
    playing: () => !!ref.current && !ref.current.paused,
    time: () => ref.current?.currentTime ?? 0,
  }));

  // 換走（燈箱換一件、離開頁面）：停下來，記住停在哪
  useEffect(() => {
    const v = ref.current;
    return () => {
      if (!v) return;
      const was = playingRef.current;
      v.pause();
      if (posKey && v.currentTime > 0.05) {
        stoppedAt.set(posKey, v.currentTime);
        if (was) lastStop = { key: posKey, t: v.currentTime };
      }
    };
  }, [posKey, src]);

  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    v.volume = vol;
    v.muted = muted;
  }, [vol, muted]);

  const at = (e: PointerEvent<HTMLElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  };
  const seekTo = (e: PointerEvent<HTMLElement>) => {
    const v = ref.current;
    const d = v && Number.isFinite(v.duration) ? v.duration : dur;
    if (!v || !d) return;
    v.currentTime = at(e) * d;
    setT(v.currentTime);
  };
  const setVolAt = (e: PointerEvent<HTMLElement>) => {
    const x = at(e);
    pref.volume = x;
    setVol(x);
    if (x > 0 && muted) {
      pref.muted = false;
      setMuted(false);
    }
  };
  const fs = () => {
    const el = box.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    else void el.requestFullscreen?.().catch(() => {});
  };
  const sound = hasAudio !== false;
  const pct = dur ? Math.min(100, (t / dur) * 100) : 0;
  const tk = dur ? `${(100 / Math.max(1, Math.round(dur))).toFixed(3)}%` : "20%";
  const ar = width && height ? `${width} / ${height}` : "16 / 9";
  const arn = width && height ? width / height : 16 / 9;

  const video = (
    <div className="vd-vid" style={{ "--ar": ar } as CSSProperties}>
      <video
        ref={ref}
        src={src}
        poster={poster ?? undefined}
        preload="metadata"
        playsInline
        aria-label={label}
        onClick={toggle}
        onLoadedMetadata={(e) => {
          const v = e.currentTarget;
          if (Number.isFinite(v.duration)) setDur(v.duration);
          const back = posKey ? stoppedAt.get(posKey) : undefined;
          if (back && Number.isFinite(v.duration) && back < v.duration - 0.2) {
            v.currentTime = back;
            setT(back);
          }
          v.volume = pref.volume;
          v.muted = pref.muted;
        }}
        onTimeUpdate={(e) => !drag.current && setT(e.currentTarget.currentTime)}
        onPlay={() => {
          playingRef.current = true;
          setPlaying(true);
        }}
        onPause={() => {
          playingRef.current = false;
          setPlaying(false);
        }}
        onEnded={() => {
          playingRef.current = false;
          setPlaying(false);
        }}
        onError={() => setBroken(true)}
      />
      {broken ? <span className="vd-broken">這支影片在這裡播不出來；可以下載後用別的播放器開。</span> : null}
    </div>
  );
  const play = <button type="button" className={`mk-play${playing ? " on" : ""}`} aria-label={playing ? "暫停" : "播放"} onClick={toggle} />;
  const ruler = (
    <div
      className="mk-ruler"
      style={{ "--tk": tk } as CSSProperties}
      role="slider"
      tabIndex={-1}
      aria-label="時間軸"
      aria-valuemin={0}
      aria-valuemax={dur ? Math.round(dur) : 0}
      aria-valuenow={Math.round(t)}
      aria-valuetext={`${msShort(t)} / ${msShort(dur)}`}
      onPointerDown={(e) => {
        drag.current = "time";
        e.currentTarget.setPointerCapture(e.pointerId);
        seekTo(e);
      }}
      onPointerMove={(e) => drag.current === "time" && seekTo(e)}
      onPointerUp={() => (drag.current = null)}
      onPointerCancel={() => (drag.current = null)}
    >
      {big ? null : (
        <div className="ends">
          <span>{msShort(t)}</span>
          <span>{msShort(dur)}</span>
        </div>
      )}
      <div className="ticks" />
      <div className="fill" style={{ width: `${pct}%` }} />
      {big ? <span className="vd-knob" style={{ left: `${pct}%` }} /> : null}
    </div>
  );
  const snd = sound ? (
    <button
      type="button"
      className="vd-snd"
      aria-pressed={muted}
      aria-label={muted ? "取消靜音" : "靜音"}
      title={muted ? "取消靜音" : "靜音"}
      onClick={() => {
        pref.muted = !muted;
        setMuted(!muted);
      }}
    >
      聲
    </button>
  ) : (
    <span className="vd-nosnd" title="這支影片沒有聲音">
      無聲
    </span>
  );
  const full = <button type="button" className="vd-fs" aria-label="全螢幕" title="全螢幕" onClick={fs} />;

  if (!big)
    return (
      <div className="vd-out" ref={box}>
        {video}
        <div className="vd-ctl">
          {play}
          {ruler}
          {snd}
          {full}
        </div>
      </div>
    );
  return (
    <div className="vd-stage" ref={box} style={{ "--arn": arn } as CSSProperties}>
      {video}
      <div className="vd-bigctl">
        {play}
        <span className="vd-t cur">
          <b>{msShort(t)}</b>
        </span>
        {ruler}
        <span className="vd-t end">{msShort(dur)}</span>
        {sound ? (
          <div className="vd-vol">
            {snd}
            <span
              className="bar"
              role="slider"
              aria-label="音量"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round((muted ? 0 : vol) * 100)}
              onPointerDown={(e) => {
                drag.current = "vol";
                e.currentTarget.setPointerCapture(e.pointerId);
                setVolAt(e);
              }}
              onPointerMove={(e) => drag.current === "vol" && setVolAt(e)}
              onPointerUp={() => (drag.current = null)}
            >
              <i style={{ width: `${(muted ? 0 : vol) * 100}%` }} />
            </span>
          </div>
        ) : (
          snd
        )}
        {full}
      </div>
    </div>
  );
}
