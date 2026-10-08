"""What a video file is: length, size, sound — read from the file itself.

The vendor's answer is not trusted for this (measured 2026-10-05: a "480p,
1:1" request came back 544×544). An MP4 / MOV is an ISO base media file: the
``moov`` box holds ``mvhd`` (timescale, duration) and one ``trak`` per
stream, each with ``tkhd`` (width, height), ``mdhd`` (its own clock),
``hdlr`` (``vide`` / ``soun``) and ``stsd`` (the codec). Reading those few
boxes needs no ffmpeg; ``ffprobe`` is only a fallback for a file the reader
cannot make sense of (a WebM, a fragmented MP4).

A cover picture stored as a second video track (an MJPEG / PNG "attached
picture", measured in a Grok Imagine file) is not the video: the reader
skips still-image codecs when it picks the picture size.

The poster (one frame as a JPEG) needs ffmpeg; without it there is none, and
the page shows the video's own first frame instead.
"""

from __future__ import annotations

import json
import logging
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

logger = logging.getLogger(__name__)

#: codecs that store still pictures (a cover track, not the video)
_STILL_CODECS = {b"jpeg", b"mjpa", b"mjpb", b"png ", b"gif ", b"tiff"}
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}
_MAX_BOX_READ = 64 * 1024 * 1024  # a moov larger than this is not read into memory


def _boxes(data: bytes, start: int = 0, end: Optional[int] = None) -> Iterator[tuple[bytes, int, int]]:
    """(type, payload start, payload end) of the boxes in ``data[start:end]``."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _find_moov(f: BinaryIO, file_size: int) -> Optional[bytes]:
    pos = 0
    while pos + 8 <= file_size:
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            return None
        size, kind = struct.unpack(">I4s", head[:8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", head[8:16])[0]
            header = 16
        elif size == 0:
            size = file_size - pos
        if size < header:
            return None
        if kind == b"moov":
            if size > _MAX_BOX_READ:
                return None
            f.seek(pos)
            return f.read(size)
        pos += size
    return None


def _full_box(data: bytes, start: int) -> tuple[int, int]:
    """(version, flags) of a full box whose payload starts at ``start``."""
    v = data[start]
    flags = int.from_bytes(data[start + 1:start + 4], "big")
    return v, flags


def _mvhd(data: bytes, s: int) -> tuple[int, int]:
    v, _ = _full_box(data, s)
    if v == 1:
        timescale, duration = struct.unpack(">IQ", data[s + 20:s + 32])
    else:
        timescale, duration = struct.unpack(">II", data[s + 12:s + 20])
    return timescale, duration


def _tkhd(data: bytes, s: int) -> dict[str, Any]:
    v, flags = _full_box(data, s)
    off = s + 4 + (32 if v == 1 else 20)  # times, track id, reserved, duration
    off += 8 + 2 + 2 + 2 + 2 + 36  # reserved, layer, alternate group, volume, reserved, matrix
    w, h = struct.unpack(">II", data[off:off + 8])
    return {"enabled": bool(flags & 1), "width": w >> 16, "height": h >> 16}


def _track(data: bytes, s: int, e: int) -> dict[str, Any]:
    t: dict[str, Any] = {}
    for kind, ps, pe in _boxes(data, s, e):
        if kind == b"tkhd":
            t.update(_tkhd(data, ps))
        elif kind == b"mdia":
            for k2, s2, e2 in _boxes(data, ps, pe):
                if k2 == b"mdhd":
                    t["timescale"], t["duration"] = _mvhd(data, s2)
                elif k2 == b"hdlr":
                    t["handler"] = data[s2 + 8:s2 + 12]
                elif k2 == b"minf":
                    for k3, s3, e3 in _boxes(data, s2, e2):
                        if k3 != b"stbl":
                            continue
                        for k4, s4, e4 in _boxes(data, s3, e3):
                            if k4 == b"stsd" and e4 - s4 >= 16:
                                t["codec"] = data[s4 + 12:s4 + 16]
                            elif k4 == b"stts" and e4 - s4 >= 8:
                                n = struct.unpack(">I", data[s4 + 4:s4 + 8])[0]
                                total = 0
                                for i in range(min(n, 100_000)):
                                    o = s4 + 8 + i * 8
                                    if o + 8 > e4:
                                        break
                                    total += struct.unpack(">I", data[o:o + 4])[0]
                                t["samples"] = total
    return t


def read_mp4(path: str | Path) -> Optional[dict[str, Any]]:
    """``{duration_s, width, height, has_audio, fps, video_codec, audio_codec}``
    of an MP4 / MOV, or ``None`` when the file is not one this can read."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            moov = _find_moov(f, size)
    except OSError:
        return None
    if not moov:
        return None
    try:
        tracks: list[dict[str, Any]] = []
        timescale = duration = 0
        for kind, s, e in _boxes(moov, 8, len(moov)):
            if kind == b"mvhd":
                timescale, duration = _mvhd(moov, s)
            elif kind == b"trak":
                tracks.append(_track(moov, s, e))
    except (struct.error, IndexError):
        return None
    video = [t for t in tracks if t.get("handler") == b"vide"]
    moving = [t for t in video if t.get("codec") not in _STILL_CODECS] or video
    audio = [t for t in tracks if t.get("handler") == b"soun"]
    main = max(moving, key=lambda t: (t.get("enabled", True), (t.get("width") or 0) * (t.get("height") or 0)), default=None)
    seconds = duration / timescale if timescale else None
    if not seconds and main and main.get("timescale"):
        seconds = main.get("duration", 0) / main["timescale"]
    fps = None
    if main and main.get("samples") and main.get("timescale") and main.get("duration"):
        fps = round(main["samples"] / (main["duration"] / main["timescale"]), 2)
    if main is None and not audio:
        return None
    return {
        "duration_s": round(seconds, 3) if seconds else None,
        "width": (main or {}).get("width") or None,
        "height": (main or {}).get("height") or None,
        "has_audio": bool(audio),
        "fps": fps,
        "video_codec": (main or {}).get("codec", b"").decode("latin-1").strip() or None,
        "audio_codec": audio[0].get("codec", b"").decode("latin-1").strip() or None if audio else None,
    }


# ------------------------------------------------------------------ ffmpeg (optional)
def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return shutil.which("ffprobe")


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    kw: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # no console flash from a background service
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False, **kw)


def _ffprobe(path: Path) -> Optional[dict[str, Any]]:
    exe = ffprobe_path()
    if not exe:
        return None
    try:
        r = _run([exe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate:"
                  "stream_disposition=attached_pic:format=duration", "-of", "json", str(path)], 30)
        j = json.loads(r.stdout or b"{}")
    except Exception as e:  # noqa: BLE001 — informational
        logger.info("ffprobe could not read %s: %s", path.name, e)
        return None
    streams = j.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    v = video[0] if video else {}
    try:
        dur = float((j.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        dur = None
    fps = None
    try:
        a, b = str(v.get("avg_frame_rate") or "0/0").split("/")
        fps = round(int(a) / int(b), 2) if int(b) else None
    except ValueError:
        pass
    if not v and not any(s.get("codec_type") == "audio" for s in streams):
        return None
    return {"duration_s": dur, "width": v.get("width"), "height": v.get("height"),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams), "fps": fps,
            "video_codec": v.get("codec_name"), "audio_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)}


def probe(path: str | Path) -> dict[str, Any]:
    """The file's facts, from the file (``read_mp4``; ffprobe when that cannot
    read it and ffprobe is installed). Missing facts are ``None``; ``source``
    says which reader answered (``mp4`` / ``ffprobe`` / ``none``)."""
    p = Path(path)
    facts = read_mp4(p)
    if facts:
        return {**facts, "source": "mp4"}
    facts = _ffprobe(p)
    if facts:
        return {**facts, "source": "ffprobe"}
    return {"duration_s": None, "width": None, "height": None, "has_audio": None, "fps": None, "source": "none"}


def extract_poster(video: str | Path, out: str | Path, *, at: float = 0.0, max_side: int = 1280) -> bool:
    """One frame of ``video`` as a JPEG at ``out`` (ffmpeg). ``False`` when
    ffmpeg is not installed or fails — there is then no poster, by design."""
    exe = ffmpeg_path()
    if not exe:
        return False
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.jpg")
    scale = f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease"
    args = [exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(video),
            "-map", "0:V:0", "-frames:v", "1", "-vf", scale, "-q:v", "3", str(tmp)]
    try:
        r = _run(args, 60)
    except Exception as e:  # noqa: BLE001 — no poster is a normal outcome
        logger.warning("ffmpeg could not be run for a video poster: %s", e)
        tmp.unlink(missing_ok=True)
        return False
    if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        logger.warning("ffmpeg made no poster for %s (exit %s): %s", Path(video).name, r.returncode,
                       (r.stderr or b"").decode("utf-8", "replace")[:300])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out)
    return True
