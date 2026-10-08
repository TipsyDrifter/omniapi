"""Index generated files as artifacts (the 作品庫 behind the works wall).

The generation tools already write their output under the storage base
(``images/``, ``audio/``, ``music/``); what was missing is one place that
knows every file exists. Two paths feed the index:

* ``index_result`` — called when a generation tool finishes (inline from the
  call recorder, or late from the JobManager when the call had already
  returned a ticket). It reads the tool's result dict, so every door that
  runs a tool through the recorder (MCP today, the GUI next) is covered.
* ``backfill`` — scans the storage base for files generated before the index
  existed. Idempotent: ``artifacts.file_path`` is unique.

Transcripts were never written to disk before; ``index_result`` now saves
the full text under ``transcripts/<date>/`` (the calls ledger only keeps a
1,500-character summary).
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import modalities as M
from ..utils.path_utils import find_existing_image_path

logger = logging.getLogger(__name__)

#: tool -> the kind of work it produces (from omniapi_mcp.modalities)
KIND_BY_TOOL = M.KIND_BY_TOOL

MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac",
    ".aac": "audio/aac", ".m4a": "audio/mp4", ".pcm": "application/octet-stream", ".mp4": "video/mp4",
    ".txt": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8", ".vtt": "text/vtt; charset=utf-8",
    ".json": "application/json",
}
IMAGE_EXTS = M.IMAGE_EXTS
# Audio is not decided by the extension alone: an .mp4 is a music video when
# a music tool wrote it (M.is_audio_file), and could be a video otherwise.

_BLOB_ARGS = {"image_data", "mask_data", "audio_data", "additional_images"}
_TRANSCRIPT_EXT = {"srt": ".srt", "vtt": ".vtt", "json": ".json", "verbose_json": ".json", "diarized_json": ".json"}


def mime_for(path: str | Path) -> str:
    return MIME.get(Path(path).suffix.lower(), "application/octet-stream")


@dataclass
class CallInfo:
    """What the recorder knows about the tool call that produced a result."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None
    source: str = "mcp"
    links: dict[str, Any] = field(default_factory=dict)  # e.g. the chat that proposed it (1.2-M4)


#: Set by the call recorder around each tool handler. A job that outlives the
#: call (ticket) still sees it: the background task copies the context.
current_call: contextvars.ContextVar[Optional[CallInfo]] = contextvars.ContextVar("omniapi_current_call", default=None)


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    """An artifact row as the REST API and the bus hand it out."""
    out = dict(row)
    out["hidden"] = bool(out.get("hidden"))
    out["exists"] = Path(out["file_path"]).is_file()
    out["file_url"] = f"/api/artifacts/{out['id']}/file"
    media = M.MEDIA_OF.get(out.get("kind") or "")
    out["thumb_url"] = f"/api/artifacts/{out['id']}/thumb" if media == "image" else None
    if media == "video":
        # a poster exists when ffmpeg took one at collection; else the page shows the video's first frame
        meta = out.get("meta") if isinstance(out.get("meta"), dict) else {}
        out["poster"] = bool(meta.get("poster"))
        out["thumb_url"] = f"/api/artifacts/{out['id']}/thumb" if out["poster"] else None
        out["has_audio"] = meta.get("has_audio")
    return out


# --------------------------------------------------------------------------- file facts


def _image_size(path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def _wav_seconds(path: Path) -> Optional[float]:
    if path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate() or 1), 2)
    except Exception:
        return None


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- indexing a tool result


def _cost(result: dict[str, Any]) -> Optional[float]:
    from ..recorder import extract_call_meta

    return _num(extract_call_meta(result).get("cost_usd"))


def _track_extra(track: dict[str, Any], n: int, count: int) -> dict[str, Any]:
    """Per-song extras for one of several songs from the same job: its own id,
    title and length, and where it sits in the batch."""
    out: dict[str, Any] = {"track": n, "track_count": count}
    for k in ("audio_id", "title", "duration"):
        if track.get(k) is not None:
            out[k] = track[k]
    return out


def _files_in(result: dict[str, Any], base: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every output file a tool result points at, with per-file extras."""
    found: list[tuple[Path, dict[str, Any]]] = []
    ids: list[str] = []
    if isinstance(result.get("image_id"), str):
        ids.append(result["image_id"])
    for item in result.get("images") or []:
        if isinstance(item, dict) and isinstance(item.get("image_id"), str) and item["image_id"] not in ids:
            ids.append(item["image_id"])
    for image_id in ids:
        p = find_existing_image_path(base, image_id)
        if p:
            found.append((p, {"image_id": image_id}))
    tracks = [t for t in result.get("tracks") or [] if isinstance(t, dict)] if isinstance(result.get("tracks"), list) else []
    songs = [t for t in tracks if isinstance(t.get("audio_path"), str)]
    if isinstance(result.get("audio_path"), str):
        first = songs[0] if songs and songs[0]["audio_path"] == result["audio_path"] else None
        found.append((Path(result["audio_path"]), _track_extra(first, 1, len(songs)) if first and len(songs) > 1 else {}))
    # the other songs of a job that returned several (Suno: two), one work each
    for n, t in enumerate(songs, start=1):
        if t["audio_path"] != result.get("audio_path"):
            found.append((Path(t["audio_path"]), _track_extra(t, n, len(songs))))
    for name, p in (result.get("stems") or {}).items() if isinstance(result.get("stems"), dict) else []:
        if isinstance(p, str):
            found.append((Path(p), {"stem": name}))
    if isinstance(result.get("lyrics_path"), str):
        found.append((Path(result["lyrics_path"]), {"lyrics": True}))
    return [(p, x) for p, x in found if p.is_file()]


async def _save_transcript(base: Path, args: dict[str, Any], text: str) -> Path:
    import aiofiles

    now = datetime.now(timezone.utc)
    out_dir = base / "transcripts" / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _TRANSCRIPT_EXT.get(str(args.get("response_format") or "text"), ".txt")
    path = (out_dir / f"transcript_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}").resolve()
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(text)
    return path


async def index_result(ctx: Any, call: Optional[CallInfo], result: Any) -> list[dict[str, Any]]:
    """Index the files a finished generation produced. Never raises: the
    index is bookkeeping and must not turn a successful generation into an
    error."""
    try:
        return await _index_result(ctx, call, result)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("artifact indexing failed for %s: %s", getattr(call, "tool", "?"), e)
        return []


async def _index_result(ctx: Any, call: Optional[CallInfo], result: Any) -> list[dict[str, Any]]:
    if call is None or ctx is None or not isinstance(result, dict):
        return []
    kind = KIND_BY_TOOL.get(call.tool)
    store = getattr(ctx, "store", None)
    if kind is None or store is None:
        return []
    if result.get("error") or (result.get("status") == "running" and result.get("task_id")):
        return []  # failed, or a ticket: the JobManager calls back when the job lands

    base = Path(ctx.settings.storage.base_path)
    args = call.args or {}
    md = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    prompt = args.get("prompt") or args.get("text") or md.get("prompt")
    params = {k: v for k, v in args.items() if k not in _BLOB_ARGS and k not in ("prompt", "text") and v is not None}
    text = result.get("text") if isinstance(result.get("text"), str) else None

    files = _files_in(result, base)
    if kind == "transcript" and text and not files:
        files = [(await _save_transcript(base, args, text), {})]
    if not files:
        return []

    # the work this one was made from: the image an edit started with, the audio a transcript was taken from
    parent_id = None
    source_arg = {"edit_image": "image_path", "transcribe_audio": "audio_path"}.get(call.tool)
    if source_arg and isinstance(args.get(source_arg), str):
        parent = await store.artifact_by_path(str(Path(args[source_arg]).resolve()))
        parent_id = parent["id"] if parent else None

    cost = _cost(result)
    share = round(cost / len(files), 6) if cost is not None else None
    # the last file takes the rounding remainder, so the shares add up to the call's cost exactly
    last_share = round(cost - share * (len(files) - 1), 6) if cost is not None and share is not None else None
    meta_common = {k: result[k] for k in ("task_id", "audio_id", "operation", "voice_id", "output_format", "language") if result.get(k) is not None}

    rows = []
    batch_first: Optional[str] = None  # the first song's work: the others of the same job hang under it
    for i, (path, extra) in enumerate(files):
        path = path.resolve()
        extra = dict(extra)
        own_title = extra.pop("title", None)
        own_duration = extra.pop("duration", None)
        row_parent = parent_id
        if extra.get("track", 1) > 1 and row_parent is None:
            row_parent = batch_first
        row_kind = "lyrics" if extra.get("lyrics") else kind
        width, height = _image_size(path) if path.suffix.lower() in IMAGE_EXTS else (None, None)
        row = await store.add_artifact(
            kind=row_kind,
            tool=call.tool,
            model=result.get("model") or md.get("model") or args.get("model"),
            provider=result.get("provider") or md.get("provider"),
            title=own_title or result.get("title") or args.get("title"),
            prompt=prompt,
            params=params,
            file_path=str(path),
            mime=mime_for(path),
            bytes=path.stat().st_size,
            width=width,
            height=height,
            duration_s=_num(own_duration) or _num(result.get("duration")) or _wav_seconds(path),
            text=text if row_kind in ("transcript", "lyrics") else None,
            cost_usd=last_share if i == len(files) - 1 else share,
            source=call.source,
            call_id=call.call_id,
            parent_id=row_parent,
            meta={**meta_common, **extra, **(call.links or {})},
        )
        if row is None:
            continue
        if extra.get("track") == 1:
            batch_first = row["id"]
        rows.append(row)
        bus = getattr(ctx, "bus", None)
        if bus is not None:
            slim = {k: v for k, v in public_row(row).items() if k not in ("text", "params", "meta")}
            await bus.publish({"type": "artifact.created", "artifact": slim})
    return rows


# --------------------------------------------------------------------------- backfill


def _name_ts(name: str, *, utc: bool) -> Optional[float]:
    """The ``YYYYMMDDHHMMSS`` stamp the tools put in file names (``img_`` in
    local time, ``speech_`` / ``music_`` in UTC)."""
    for part in Path(name).stem.split("_"):
        if len(part) == 14 and part.isdigit():
            try:
                dt = datetime.strptime(part, "%Y%m%d%H%M%S")
                return (dt.replace(tzinfo=timezone.utc) if utc else dt).timestamp()
            except ValueError:
                return None
    return None


def _scan(base: Path) -> list[dict[str, Any]]:
    """Blocking directory walk; run it in a thread."""
    found: list[dict[str, Any]] = []
    images = base / M.BY_KIND["image"].folder
    if images.is_dir():
        for p in sorted(images.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            side: dict[str, Any] = {}
            sidecar = p.with_suffix(".json")
            if sidecar.is_file():
                try:
                    side = json.loads(sidecar.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    side = {}
            cost_info = side.get("cost_info") if isinstance(side.get("cost_info"), dict) else {}
            params = side.get("parameters") if isinstance(side.get("parameters"), dict) else None
            width, height = _image_size(p)
            found.append(dict(
                kind="image",
                tool="edit_image" if side.get("operation") == "edit" or "has_mask" in side else "generate_image",
                model=side.get("model") or (params or {}).get("model"),
                provider=side.get("provider"),
                prompt=side.get("prompt"),
                params=params,
                file_path=str(p.resolve()),
                created_at=_name_ts(p.name, utc=False) or p.stat().st_mtime,
                width=width,
                height=height,
                cost_usd=_num(cost_info.get("estimated_cost_usd")),
                meta={"image_id": p.stem, "task_id": side.get("task_id")} if side else {"image_id": p.stem},
            ))
    # videos/ (1.4-M3): the file says its length, size and sound; the rest was in the job
    for folder in (m.folder for m in M.GENERATION if m.media == "video"):
        root = base / folder
        if not root.is_dir():
            continue
        from ..video.media import read_mp4

        for p in sorted(root.rglob("*")):
            if not p.is_file() or not M.is_video_file(p, folder=folder):
                continue
            facts = read_mp4(p) or {}
            found.append(dict(
                kind="video",
                tool="generate_video",
                file_path=str(p.resolve()),
                created_at=_name_ts(p.name, utc=True) or p.stat().st_mtime,
                width=facts.get("width"),
                height=facts.get("height"),
                duration_s=facts.get("duration_s"),
                meta={"has_audio": facts.get("has_audio"), "fps": facts.get("fps"), "backfilled": True},
            ))
    # every other kind's folder (audio/ speech, music/ music, transcripts/ transcripts)
    for folder, default_kind in ((m.folder, m.kind) for m in M.GENERATION if m.media not in ("image", "video")):
        root = base / folder
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            ext = p.suffix.lower()
            if not p.is_file():
                continue
            kind, text = default_kind, None
            if ext in (".txt", ".srt", ".vtt", ".json") and folder != "audio":
                kind = "lyrics" if folder == "music" else "transcript"
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    text = None
            elif not M.is_audio_file(p, folder=folder):  # an .mp4 counts only in an audio tool's folder
                continue
            meta = {"stem": p.stem.split("_")[1]} if p.name.startswith("stem_") else None
            found.append(dict(
                kind=kind,
                file_path=str(p.resolve()),
                created_at=_name_ts(p.name, utc=True) or p.stat().st_mtime,
                duration_s=_wav_seconds(p),
                text=text,
                meta=meta,
            ))
    return found


async def backfill(store: Any, base: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Index files generated before the artifacts table existed.

    Images come with a sidecar ``.json`` (prompt, model, cost); speech and
    music were saved without one, so those rows only know the file."""
    import asyncio

    base = Path(base)
    t0 = time.perf_counter()
    found = await asyncio.to_thread(_scan, base)
    added: dict[str, int] = {}
    known = 0
    for item in found:
        path = Path(item["file_path"])
        if dry_run:
            exists = await store.artifact_by_path(item["file_path"])
            row = None if exists else item
        else:
            row = await store.add_artifact(
                source="backfill", mime=mime_for(path), bytes=path.stat().st_size, **item
            )
        if row is None:
            known += 1
        else:
            added[item["kind"]] = added.get(item["kind"], 0) + 1
    return {
        "base": str(base),
        "scanned": len(found),
        "added": added,
        "added_total": sum(added.values()),
        "already_indexed": known,
        "dry_run": dry_run,
        "seconds": round(time.perf_counter() - t0, 2),
    }
