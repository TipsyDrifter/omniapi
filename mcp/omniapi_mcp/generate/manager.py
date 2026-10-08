"""GenerationManager — the GUI's door to the generation tools.

A generation started from the GUI runs the very same tool handler an MCP
client calls (``generate_image``, ``edit_image``, ``generate_speech``,
``generate_music``, ``transcribe_audio``): same argument validation, same
ledger row, same works index. What this adds is a *job* around it:

* ``start()`` stores a row in ``generations``, starts a task and returns —
  the page can be left and come back to; the job belongs to the daemon.
* the task waits for the real result however long it takes (no 45-second
  ticket: that exists for MCP clients' timeouts) and records how it ended,
  with the reason classified when it failed.
* source files are named by id (a work, or an upload) and resolved here —
  a request never carries a path.

Events on the bus:

    generation.started   {generation}
    generation.updated   {generation}      (video: each answer from the vendor, a resume, stop waiting, ask again)
    generation.finished  {generation}      status: done | error | cancelled (video also: gave_up | abandoned)

A video job is not a tool run: it is handed to ``video.jobs.VideoJobs``
(``self.videos``), which stores the vendor's job id the moment it is sent and
waits on it across restarts. ``cancel`` on a video means "stop waiting".

``generation`` is the public row: the stored job plus ``artifacts`` (the
public rows of the works it produced).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .. import modalities as M
from ..artifacts import public_row
from ..bus import EventBus
from ..core.job_manager import wait_to_finish
from ..recorder import CallScope, call_scope, extract_call_meta
from ..store.db import Store
from .errors import classify
from .estimate import estimate as estimate_cost

logger = logging.getLogger(__name__)

KINDS = M.KINDS

#: the tool arguments a GUI request may set, per tool. Blob and path
#: arguments are absent on purpose: files come in through ``sources``.
#: One entry per tool a generation kind runs (checked below).
ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "generate_image": frozenset({"prompt", "model", "quality", "size", "output_format", "compression", "background", "n",
                                 "image_size", "aspect_ratio", "moderation"}),
    "edit_image": frozenset({"prompt", "model", "quality", "size", "output_format", "compression", "background", "input_fidelity",
                             "image_size", "aspect_ratio"}),
    "generate_speech": frozenset({"text", "voice", "model", "output_format", "instructions", "speed", "language_code", "voice_settings"}),
    "generate_music": frozenset({"prompt", "model", "instrumental", "output_format", "music_length_ms", "style", "title",
                                 "custom_mode", "vocal_gender", "negative_tags"}),
    "transcribe_audio": frozenset({"model", "language", "prompt", "response_format", "temperature"}),
    # 1.4-M3: frames come in through ``sources.frames`` ({first, last}), never as paths
    "generate_video": frozenset({"prompt", "model", "duration", "resolution", "aspect_ratio", "generate_audio", "seed"}),
}
M.require_keys(ALLOWED_PARAMS, (t for m in M.GENERATION for t in (m.tool, m.source_tool) if t), "generate.ALLOWED_PARAMS")

#: which kinds of work (or upload) may feed which source slot
_SOURCE_KINDS = M.SOURCE_SLOTS
MAX_SOURCE_IMAGES = 16  # gpt-image edits take up to 16 input images
_TITLE_CHARS = 60


class GenerationError(ValueError):
    """A request the caller can fix (unknown kind, bad source, bad params)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def tool_for(kind: str, sources: dict[str, Any]) -> str:
    slot = M.BY_KIND[kind].source_slot
    return M.tool_for(kind, bool(slot and sources.get(slot)))


def _told_kind(error: BaseException) -> Optional[str]:
    """The ``error_kind`` a provider put on its exception (``ProviderError``),
    looked for down the cause chain (the tool and the MCP layer each wrap it)."""
    seen = 0
    e: Optional[BaseException] = error
    while e is not None and seen < 8:
        kind = getattr(e, "error_kind", None)
        if isinstance(kind, str) and kind:
            return classify(e)
        e = e.__cause__ or e.__context__
        seen += 1
    return None


async def public_generation(store: Store, row: dict[str, Any], settings: Any = None) -> dict[str, Any]:
    """A stored job as the page sees it: the row plus its works and named
    sources; a video adds ``video`` (the waiting card's facts) and leaves
    out its bookkeeping (lease, raw remote answers)."""
    out = dict(row)
    works = await store.artifacts_by_ids(row.get("artifact_ids") or [])
    out["artifacts"] = [public_row(w) for w in works]
    out["sources"] = await _describe_sources(store, row.get("sources") or {})
    if row.get("kind") == "video":
        from ..video.jobs import video_view

        if settings is None:
            from ..runtime import runtime

            settings = getattr(runtime.context, "settings", None)
        out["video"] = video_view(row, settings)
    for k in ("lease_owner", "lease_until", "remote"):
        out.pop(k, None)
    return out


async def _describe_sources(store: Store, sources: dict[str, Any]) -> dict[str, Any]:
    """Sources as the page shows them: ids plus a name and a URL, no paths."""
    async def one(ref: dict[str, Any]) -> dict[str, Any]:
        if ref.get("artifact_id"):
            work = await store.artifact(ref["artifact_id"])
            name = Path(work["file_path"]).name if work else None
            return {**ref, "name": (work or {}).get("title") or name, "file_url": f"/api/artifacts/{ref['artifact_id']}/file",
                    "thumb_url": f"/api/artifacts/{ref['artifact_id']}/thumb" if work and M.MEDIA_OF.get(work["kind"]) == "image" else None}
        if ref.get("upload_id"):
            up = await store.upload(ref.get("upload_id") or "")
            return {**ref, "name": (up or {}).get("filename"), "file_url": f"/api/uploads/{ref.get('upload_id')}/file", "thumb_url": None}
        # a frame MCP named by a local file (or inline data) that is not a work: its name only
        return {**ref, "name": ref.get("file") or ("inline image" if ref.get("inline") else None), "file_url": None, "thumb_url": None}

    out: dict[str, Any] = {}
    if sources.get("images"):
        out["images"] = [await one(r) for r in sources["images"]]
    if sources.get("audio"):
        out["audio"] = await one(sources["audio"])
    if isinstance(sources.get("frames"), dict):
        out["frames"] = {k: await one(v) for k, v in sources["frames"].items() if isinstance(v, dict)}
    return out


def _first_line(text: Any) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line and not (line.startswith("[") and line.endswith("]")):  # skip lyric section tags
            return line[:_TITLE_CHARS]
    return ""


class GenerationManager:
    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        self._tasks: dict[str, asyncio.Task] = {}
        #: jobs whose task has not taken its first step yet, with their ``on_finished``
        self._unstarted: dict[str, Optional[Callable[[dict[str, Any], bool], Awaitable[None]]]] = {}
        #: endings being recorded for jobs cancelled before they started
        self._settling: dict[str, asyncio.Task] = {}
        #: set by ``close()``: a job cancelled from here on was stopped by the shutdown, not by anyone
        self.closing = False
        #: 1.4-M3: video jobs (``video.jobs.VideoJobs``), set by the runtime
        self.videos: Any = None

    @property
    def live_count(self) -> int:
        return len(self._tasks)

    # ------------------------------------------------------------ rows
    async def public(self, row: dict[str, Any]) -> dict[str, Any]:
        settings = getattr(self.videos, "settings", None) if self.videos is not None else None
        return await public_generation(self.store, row, settings)

    async def _describe_sources(self, sources: dict[str, Any]) -> dict[str, Any]:
        return await _describe_sources(self.store, sources)

    async def get(self, generation_id: str) -> dict[str, Any]:
        row = await self.store.generation(generation_id)
        if not row:
            raise GenerationError("generation not found", 404)
        return await self.public(row)

    async def list(self, *, limit: int = 30, status: Optional[str] = None, kind: Optional[str] = None) -> list[dict[str, Any]]:
        return [await self.public(r) for r in await self.store.generations(limit=limit, status=status, kind=kind)]

    # ------------------------------------------------------------ request -> tool call
    async def _resolve(self, ref: Any, slot: str) -> str:
        """A source reference -> the file's path, checked for kind and presence."""
        if not isinstance(ref, dict) or not (ref.get("artifact_id") or ref.get("upload_id")):
            raise GenerationError("a source is {artifact_id} or {upload_id}")
        work_kinds, upload_kinds = _SOURCE_KINDS[slot]
        if ref.get("artifact_id"):
            row = await self.store.artifact(str(ref["artifact_id"]))
            if not row:
                raise GenerationError("source work not found", 404)
            if row["kind"] not in work_kinds:
                if M.MEDIA_OF.get(row["kind"]) == "video":
                    raise GenerationError(f"a video work cannot be used as a source in this version ({slot} take images"
                                          f"{' or audio' if slot == 'audio' else ''})")
                raise GenerationError(f"a {row['kind']} work cannot be used here")
        else:
            row = await self.store.upload(str(ref["upload_id"]))
            if not row:
                raise GenerationError("source upload not found", 404)
            if row["kind"] not in upload_kinds:
                raise GenerationError(f"an {row['kind']} upload cannot be used here")
        if not Path(row["file_path"]).is_file():
            raise GenerationError("the source file is no longer on disk", 410)
        return row["file_path"]

    async def prepare(self, kind: str, params: Any, sources: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Validate a request and return ``(tool, tool arguments, clean sources)``."""
        if kind not in KINDS:
            raise GenerationError(f"kind must be one of {list(KINDS)}")
        params = dict(params or {}) if isinstance(params, dict) or params is None else None
        sources = dict(sources or {}) if isinstance(sources, dict) or sources is None else None
        if params is None or sources is None:
            raise GenerationError("params and sources must be objects")
        bad = set(sources) - set(M.SOURCE_SLOTS)
        if bad:
            raise GenerationError(f"unknown sources: {sorted(bad)}")
        tool = tool_for(kind, sources)
        bad = set(params) - ALLOWED_PARAMS[tool]
        if bad:
            raise GenerationError(f"'{tool}' does not take: {sorted(bad)}")
        args = {k: v for k, v in params.items() if v is not None and v != ""}
        clean: dict[str, Any] = {}
        mod = M.BY_KIND[kind]
        slot = mod.source_slot
        if slot and (sources.get(slot) or mod.source_required):
            if not sources.get(slot):
                raise GenerationError(f"a {mod.catalog} needs an {slot} source")
            await _SLOT_READERS[slot](self, sources[slot], args, clean)
        elif any(sources.get(s) for s in M.SOURCE_SLOTS):
            raise GenerationError(f"a {kind} generation takes no source file")
        if kind == "video":
            # the listing decides (videos.check): refused here, before a job exists or anything is paid
            if self.videos is None:
                raise GenerationError("video generation is not available", 503)
            from ..video.jobs import VideoError

            try:
                self.videos.check(args, first=bool(args.get("first_frame")), last=bool(args.get("last_frame")))
            except VideoError as e:
                raise GenerationError(str(e), e.status) from None
            return tool, args, clean
        self._validate(tool, args)
        if kind == "image" and args.get("model"):
            # an OpenRouter model's listing says what it takes (reference images, tiers):
            # a request it cannot take is refused here, before a job exists or anything is paid
            from ..catalog import catalog
            from ..catalog.openrouter_images import check_tool_args

            problem = check_tool_args(catalog.openrouter_image(str(args["model"])), tool, args)
            if problem:
                raise GenerationError(problem)
        return tool, args, clean

    async def _read_images(self, refs: Any, args: dict[str, Any], clean: dict[str, Any]) -> None:
        """``images``: up to 16 source images -> ``image_path`` + ``additional_image_paths``."""
        if not isinstance(refs, list) or len(refs) > MAX_SOURCE_IMAGES:
            raise GenerationError(f"images is a list of at most {MAX_SOURCE_IMAGES} sources")
        paths = [await self._resolve(r, "images") for r in refs]
        args["image_path"] = paths[0]
        if paths[1:]:
            args["additional_image_paths"] = paths[1:]
        clean["images"] = [{k: r[k] for k in ("artifact_id", "upload_id") if r.get(k)} for r in refs]

    async def _read_frames(self, refs: Any, args: dict[str, Any], clean: dict[str, Any]) -> None:
        """``frames``: ``{"first": ref, "last": ref}`` (either may be left out) ->
        ``first_frame`` / ``last_frame`` paths."""
        if not isinstance(refs, dict) or not refs or set(refs) - {"first", "last"}:
            raise GenerationError("frames is {first?: source, last?: source}")
        clean["frames"] = {}
        for which in ("first", "last"):
            ref = refs.get(which)
            if ref is None:
                continue
            args[f"{which}_frame"] = await self._resolve(ref, "frames")
            clean["frames"][which] = {k: ref[k] for k in ("artifact_id", "upload_id") if ref.get(k)}

    async def _read_audio(self, ref: Any, args: dict[str, Any], clean: dict[str, Any]) -> None:
        """``audio``: one source audio file -> ``audio_path``."""
        args["audio_path"] = await self._resolve(ref, "audio")
        clean["audio"] = {k: ref[k] for k in ("artifact_id", "upload_id") if ref.get(k)}

    @staticmethod
    def _tool(name: str) -> Any:
        from .. import server  # late: server.py builds the FastMCP instance at import time

        tool = server.mcp._tool_manager.get_tool(name)
        if tool is None:  # pragma: no cover - the five names are registered at import
            raise GenerationError(f"tool '{name}' is not registered", 500)
        return tool

    def _validate(self, tool: str, args: dict[str, Any]) -> None:
        """Check the arguments against the tool's own schema before a job exists."""
        from pydantic import ValidationError

        try:
            self._tool(tool).fn_metadata.arg_model.model_validate(args)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
            raise GenerationError(f"invalid request — {problems}")

    async def estimate(self, kind: str, params: Any, sources: Any = None, *, duration_s: Optional[float] = None) -> dict[str, Any]:
        """The cost estimate for a request that has not been sent. Lenient on
        purpose: a half-filled form still gets an answer."""
        if kind not in KINDS:
            raise GenerationError(f"kind must be one of {list(KINDS)}")
        params = params if isinstance(params, dict) else {}
        sources = sources if isinstance(sources, dict) else {}
        tool = tool_for(kind, sources)
        if kind == "video" and not params.get("model") and self.videos is not None:
            params = {**params, "model": self.videos.default_model()}
        refs = len(sources.get("images") or []) if isinstance(sources.get("images"), list) else 0
        frames = sources.get("frames") if isinstance(sources.get("frames"), dict) else None
        if frames:
            refs = sum(1 for k in ("first", "last") if frames.get(k))
        return await estimate_cost(self.store, kind=kind, tool=tool, params=params, duration_s=duration_s, refs=refs,
                                   first_frame=bool(frames and frames.get("first")))

    # ------------------------------------------------------------ jobs
    async def start(self, kind: str, params: Any = None, sources: Any = None, *,
                    duration_s: Optional[float] = None, source: str = "gui", links: Optional[dict[str, Any]] = None,
                    on_finished: Optional[Callable[[dict[str, Any], bool], Awaitable[None]]] = None) -> dict[str, Any]:
        """Start a job. ``links`` (e.g. the chat and message a proposal came
        from, 決策記錄 1.2-M4-b) are stored on the job (``meta``) and carried
        to its ledger row and works. ``on_finished(public row, shutting_down)``
        is awaited once the ending is recorded — ``shutting_down`` tells a
        cancel by the daemon's own shutdown from one somebody asked for."""
        tool, args, clean_sources = await self.prepare(kind, params, sources)
        if kind == "video":
            from ..video.jobs import VideoError

            try:
                return await self.videos.start(args, source=source, links=links, sources=clean_sources)
            except VideoError as e:
                raise GenerationError(str(e), e.status) from None
        estimate = await estimate_cost(self.store, kind=kind, tool=tool, params=args, duration_s=duration_s,
                                       refs=len(clean_sources.get("images") or []))
        public_params = {k: v for k, v in args.items() if not k.endswith("_path") and not k.endswith("_paths")}
        mod = M.BY_KIND[kind]
        if mod.source_required:  # no prompt of its own (a transcription): named after its source
            src = (await self._describe_sources(clean_sources)).get(mod.source_slot or "") or {}
            src = (src[0] if src else {}) if isinstance(src, list) else src
            title = src.get("name") or mod.source_slot
        else:
            title = args.get("title") or _first_line(args.get("prompt") or args.get("text"))
        gid = uuid.uuid4().hex[:16]
        row = await self.store.create_generation(
            gid, kind=kind, tool=tool, model=args.get("model"), title=title or None,
            params=public_params, sources=clean_sources, estimate=estimate, source=source, meta=links,
        )
        public = await self.public(row)
        await self.bus.publish({"type": "generation.started", "generation": public})
        self._unstarted[gid] = on_finished
        task = asyncio.create_task(self._run(gid, tool, args, source, links or {}, on_finished), name=f"generation-{gid}")
        self._tasks[gid] = task
        task.add_done_callback(lambda t, gid=gid: self._task_done(gid, t))
        return public

    def _task_done(self, gid: str, task: asyncio.Task) -> None:
        self._tasks.pop(gid, None)
        if task.cancelled() and gid in self._unstarted:
            # Cancelled before its first step: the coroutine never ran, so nothing
            # recorded how the job ended and its row would stay 'running' for good.
            on_finished = self._unstarted.pop(gid)
            settle = asyncio.create_task(self._finish(gid, {"status": "cancelled"}, None, [], on_finished),
                                         name=f"generation-{gid}-settle")
            self._settling[gid] = settle
            settle.add_done_callback(lambda _t, gid=gid: self._settling.pop(gid, None))

    async def _run(self, gid: str, tool: str, args: dict[str, Any], source: str, links: Optional[dict[str, Any]] = None,
                   on_finished: Optional[Callable[[dict[str, Any], bool], Awaitable[None]]] = None) -> None:
        self._unstarted.pop(gid, None)  # no await before the try below: from here on the ending is recorded there
        scope = CallScope(source=source, links=dict(links or {}))
        scope_token = call_scope.set(scope)
        wait_token = wait_to_finish.set(True)
        fields: dict[str, Any] = {}
        artifact_ids: list[str] = []
        try:
            result = await self._tool(tool).run(args, convert_result=False)
            meta = extract_call_meta(result)
            error = meta.get("error") or (result.get("message") if isinstance(result, dict) and result.get("status") == "refused" else None)
            artifact_ids = [a["id"] for a in scope.artifacts]
            if error:
                fields = {"status": "error", "error": str(error)[:2000], "error_kind": classify(str(error))}
            elif not artifact_ids:
                fields = {"status": "error", "error": "the tool finished but produced no file", "error_kind": "other"}
            else:
                fields = {"status": "done", "model": meta.get("model"), "cost_usd": meta.get("cost_usd")}
        except asyncio.CancelledError:
            fields = {"status": "cancelled"}  # asked for, so not an error: no kind, no message
        except Exception as e:  # noqa: BLE001 — every failure becomes a row the page can show
            cause = e.__cause__ if getattr(e, "__cause__", None) is not None else e
            message = str(cause) or type(cause).__name__
            told = _told_kind(e)  # a provider that classified its own failure, however deep it was wrapped
            fields = {"status": "error", "error": message[:2000],
                      "error_kind": told or classify(f"{type(cause).__name__}: {message}")}
            logger.warning("generation %s (%s) failed: %s", gid, tool, message[:300])
        finally:
            call_scope.reset(scope_token)
            wait_to_finish.reset(wait_token)
        if fields.get("status") == "done" and fields.get("model") is None:
            fields.pop("model")
        await self._finish(gid, fields, scope.call_id, artifact_ids, on_finished)

    async def _finish(self, gid: str, fields: dict[str, Any], call_id: Optional[str], artifact_ids: list[str],
                      on_finished: Optional[Callable[[dict[str, Any], bool], Awaitable[None]]]) -> None:
        """Record how a job ended, announce it, then tell whoever waited on it."""
        try:
            await self.store.update_generation(gid, finished_at=time.time(), call_id=call_id, artifact_ids=artifact_ids, **fields)
            public = await self.get(gid)
            await self.bus.publish({"type": "generation.finished", "generation": public})
        except Exception as e:  # pragma: no cover - the store is closing
            logger.warning("could not record how generation %s ended: %s", gid, e)
            return
        if on_finished is not None:
            try:
                await on_finished(public, self.closing)
            except Exception as e:  # the job is recorded; whoever waited on it copes on its own
                logger.warning("generation %s: on_finished failed: %s", gid, e)

    async def cancel(self, generation_id: str) -> dict[str, Any]:
        row = await self.store.generation(generation_id)
        if not row:
            raise GenerationError("generation not found", 404)
        if row.get("kind") == "video" and self.videos is not None:
            return await self.videos.stop_waiting(generation_id)  # no vendor can cancel: our waiting stops
        task = self._tasks.get(generation_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        settle = self._settling.get(generation_id)
        if settle is not None:  # it never started: its ending is being recorded for it
            await asyncio.gather(settle, return_exceptions=True)
        return await self.get(generation_id)

    async def close(self) -> None:
        """Daemon shutdown: stop what is running; each records itself as cancelled."""
        self.closing = True
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._settling:
            await asyncio.gather(*list(self._settling.values()), return_exceptions=True)
        self._tasks.clear()


#: source slot -> how its references become tool arguments
_SLOT_READERS = {"images": GenerationManager._read_images, "audio": GenerationManager._read_audio,
                 "frames": GenerationManager._read_frames}
M.require_keys(_SLOT_READERS, M.SOURCE_SLOTS, "generate._SLOT_READERS")
