"""Video jobs (1.4-M3): send, wait (across restarts), collect, bill.

Why this is not the generation tools' usual "run the handler and wait": a
video takes minutes and is paid for the moment the vendor takes it. So the
vendor's job id is written to the ``generations`` row **as soon as the
vendor answers** (before any waiting), and the waiting is a separate watcher
that any process holding the database can (re)start:

* ``start()``      check the request against the model's listing, estimate,
                   create the row, send — store the remote id — start watching
* watcher         asks the vendor (10 s, 15 s, 20 s, then every 30 s as the
                   docs suggest), records what it said and when; on
                   ``completed`` downloads the file into ``videos/<date>/``,
                   reads its real length / size / sound, makes a poster when
                   ffmpeg is there, indexes the work, bills the real cost
* restart         the service starts: every video still ``running`` /
                   ``detached`` with a remote id gets a watcher again (a
                   lease on the row keeps two processes from collecting it
                   twice; a sweeper picks up jobs whose holder died)
* waited too long past ``video.max_wait_minutes`` (default 20) the row
                   becomes ``gave_up``: the remote id stays, and
                   ``recheck()`` ("ask again") asks once more — nothing is
                   sent again, nothing is billed again
* stop waiting   ``stop_waiting()``: the vendor has no cancel, so this only
                   ends *our* waiting. With ``video.keep_collecting`` (default
                   on) the row becomes ``detached`` and the watcher goes on —
                   the video, when done, still lands in the works (marked
                   late) with its real cost; off, the row is ``abandoned``
                   and the ledger says "cost unknown (sent, not collected)"

Statuses a video row goes through::

    running ─┬─> done                       (collected; ``late`` if after "stop waiting")
             ├─> error                      (refused by the vendor, failed there, gone there)
             ├─> gave_up ──recheck──> running
             └─> detached ─> done | error | gave_up      (keep_collecting on)
                 abandoned ──recheck──> running          (keep_collecting off)
    interrupted / lost                       (the service died before the vendor's id came back)

Events: ``generation.started``, ``generation.updated`` (each answer from
the vendor, a resume, stop waiting, recheck), ``generation.finished``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .. import modalities as M
from ..bus import EventBus
from ..capabilities.video import VideoProvider, VideoProviderError, VideoRequest
from ..store.db import Store

logger = logging.getLogger(__name__)

TOOL = "generate_video"
#: this process, as a lease holder
OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
#: seconds between the vendor answers: denser at first, then the docs' 30 s
POLL_SCHEDULE = (10.0, 15.0, 20.0, 30.0)
#: how long a held lease outlives the next poll (a holder that died frees it after this)
LEASE_SLACK = 90.0
SWEEP_EVERY = 60.0
#: downloads tried this many times (transient failures) before the row goes ``gave_up`` / ``download``
DOWNLOAD_TRIES = 3
_TITLE_CHARS = 60
PROMPT_MAX = 4000
#: what a request may set (the MCP tool's own arguments; frames go separately)
PARAMS = ("prompt", "model", "duration", "resolution", "aspect_ratio", "generate_audio", "seed")
WAITING = ("running", "detached")
#: rows "ask again" can act on
RECHECKABLE = ("gave_up", "abandoned")

NOT_COLLECTED_NOTE = "cost unknown: the video was sent but not collected"
#: a video whose vendor answers only with the finished file (Gemini Omni), lost when the service stopped meanwhile
WAITED_LOST = ("the service stopped while the provider was making this video. It was sent and is probably made and billed, "
               "but the provider hands the video back only to the request that was waiting for it, so it cannot be collected. "
               "Making it again costs again.")


class VideoError(ValueError):
    """A request the caller can fix, or an action that does not apply. ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 400, **detail: Any):
        super().__init__(message)
        self.status = status
        self.detail = detail


def _dev_poll() -> Optional[float]:
    """``OMNIAPI_VIDEO_POLL`` (seconds) in a development daemon: a fixed, short
    interval for trying the waiting states without waiting minutes."""
    from ..devmode import dev_enabled

    if not dev_enabled():
        return None
    try:
        v = float(os.environ.get("OMNIAPI_VIDEO_POLL", "") or 0)
    except ValueError:
        return None
    return v if v > 0 else None


def poll_interval(polls: int) -> float:
    dev = _dev_poll()
    if dev is not None:
        return dev
    return POLL_SCHEDULE[min(polls, len(POLL_SCHEDULE) - 1)]


def _pid_alive(pid: int) -> bool:
    """Is process ``pid`` running? (Windows: OpenProcess + GetExitCodeProcess —
    no subprocess, and never ``os.kill``, which would end the process there.)"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists (another user's); else it is gone
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell: assume alive (the lease then runs out on its own)
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def holder_dead(owner: Optional[str]) -> bool:
    """A lease holder ("<pid>-<tag>") whose process is gone: its lease does not
    count (a killed service must not keep its videos waiting for the lease to run out)."""
    if not owner or owner == OWNER:
        return False
    try:
        pid = int(str(owner).split("-", 1)[0])
    except ValueError:
        return False
    return not _pid_alive(pid)


def _first_line(text: Any) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()[:_TITLE_CHARS]
    return ""


def video_view(row: dict[str, Any], settings: Any = None, *, now: Optional[float] = None) -> dict[str, Any]:
    """The waiting card's facts for a video row (``generation.video``)."""
    now = now or time.time()
    remote = row.get("remote") if isinstance(row.get("remote"), dict) else {}
    max_wait = _max_wait(settings)
    wait_from = remote.get("wait_from") or row.get("submitted_at")
    status = row.get("status")
    polls = int(remote.get("polls") or 0)
    waiting = status in WAITING
    end = row.get("finished_at") if not waiting else None
    view = {
        "provider": row.get("provider"),
        "remote_id": row.get("remote_id"),
        "submitted_at": row.get("submitted_at"),
        "remote_status": row.get("remote_status"),
        "polled_at": row.get("polled_at"),
        "polls": polls,
        "waited_s": round((end or now) - row["submitted_at"], 1) if row.get("submitted_at") else None,
        "wait_from": wait_from,
        "max_wait_s": max_wait,
        "deadline": (wait_from + max_wait) if (waiting and wait_from) else None,
        "next_poll_at": (row["polled_at"] or wait_from or now) + poll_interval(polls) if (waiting and (row.get("polled_at") or wait_from)) else None,
        "detached_at": remote.get("detached_at"),
        "late": bool(remote.get("late")),
        "keep_collecting": remote.get("keep_collecting", True),
        "resumed": remote.get("resumed") or [],
        "charged": _charged(row),
        "request": remote.get("request") or {},
        "warnings": remote.get("warnings") or [],
        "last_error": remote.get("last_error"),
        "can_stop_waiting": status == "running",
        "can_recheck": bool(row.get("remote_id")) and status in RECHECKABLE,
    }
    return view


def _charged(row: dict[str, Any]) -> str:
    """Could this job have cost money? ``yes`` (the vendor said what), ``no``
    (refused before anything ran), ``likely`` (the vendor took it: billed
    whether or not anyone waits), ``unknown``."""
    remote = row.get("remote") if isinstance(row.get("remote"), dict) else {}
    if remote.get("charged"):
        return remote["charged"]
    if row.get("status") == "done":
        return "yes" if row.get("cost_usd") is not None else "likely"
    if row.get("remote_id"):
        return "likely"
    return "unknown"


def _max_wait(settings: Any) -> float:
    video = getattr(settings, "video", None)
    minutes = getattr(video, "max_wait_minutes", 20.0) if video is not None else 20.0
    return float(minutes) * 60.0


def _keep_collecting(settings: Any) -> bool:
    video = getattr(settings, "video", None)
    return bool(getattr(video, "keep_collecting", True)) if video is not None else True


def mcp_limit(settings: Any) -> Optional[float]:
    """The per-video limit for MCP / CLI calls, or ``None`` (no limit)."""
    video = getattr(settings, "video", None)
    if video is None:
        return 1.0
    if getattr(video, "mcp_unlimited", False):
        return None
    return float(getattr(video, "mcp_max_usd", 1.0))


class VideoJobs:
    def __init__(self, store: Store, bus: EventBus, settings: Callable[[], Any]) -> None:
        self.store = store
        self.bus = bus
        self._settings = settings
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._providers: dict[tuple[str, ...], VideoProvider] = {}
        #: videos being sent to a vendor that answers with the finished video (the task holds the request)
        self._sending: dict[str, asyncio.Task] = {}
        self._sweeper: Optional[asyncio.Task] = None
        self.closing = False

    @property
    def settings(self) -> Any:
        return self._settings()

    # ------------------------------------------------------------ providers
    def _sandbox(self) -> bool:
        from ..devmode import dev_enabled, offline

        return offline() and dev_enabled()

    def provider(self, name: str) -> VideoProvider:
        """The provider called ``name`` with the key in effect now."""
        from ..devmode import offline, offline_message

        if name == "sandbox":
            if not self._sandbox():
                raise VideoError("this video was made in the offline sandbox; a real daemon cannot ask about it", 409)
            key = ("sandbox",)
            if key not in self._providers:
                from .sandbox import SandboxVideoProvider

                self._providers[key] = SandboxVideoProvider()
            return self._providers[key]
        if offline():
            raise VideoError(offline_message("generating a video"), 409)
        if name == "google":
            return self._google()
        if name != "openrouter":
            raise VideoError(f"no video provider called '{name}' in this version", 409)
        cfg = getattr(getattr(self.settings, "providers", None), "openrouter", None)
        api_key = getattr(cfg, "api_key", "") if cfg is not None else ""
        if not (cfg is not None and getattr(cfg, "enabled", False) and api_key):
            raise VideoError("video models go through OpenRouter: set its key (PROVIDERS__OPENROUTER__API_KEY or the settings page)", 409)
        from ..catalog import health

        base = getattr(cfg, "base_url", None) or "https://openrouter.ai/api/v1"
        key = ("openrouter", base, health.fingerprint(api_key))
        if key not in self._providers:
            from ..providers.openrouter_videos import OpenRouterVideoProvider

            for old in [k for k in self._providers if k[0] == "openrouter"]:
                stale = self._providers.pop(old)  # the key changed: the old client goes once nothing uses it
                asyncio.ensure_future(stale.close())
            self._providers[key] = OpenRouterVideoProvider(api_key, base)
        return self._providers[key]

    def _google(self) -> VideoProvider:
        """Gemini Omni with the Google key (the settings' ``gemini`` slot, 1.4-M5)."""
        cfg = getattr(getattr(self.settings, "providers", None), "gemini", None)
        api_key = getattr(cfg, "api_key", "") if cfg is not None else ""
        if not (cfg is not None and getattr(cfg, "enabled", False) and api_key):
            raise VideoError("Gemini Omni is called with the Google key: set it (PROVIDERS__GEMINI__API_KEY or the settings "
                             "page); its project needs a paid Gemini API tier", 409)
        from ..catalog import health
        from ..providers.gemini_videos import GeminiVideoProvider, api_base

        base = api_base(getattr(cfg, "base_url", None))
        key = ("google", base, health.fingerprint(api_key))
        if key not in self._providers:
            for old in [k for k in self._providers if k[0] == "google"]:
                stale = self._providers.pop(old)  # the key changed: the old client goes once nothing uses it
                asyncio.ensure_future(stale.close())
            self._providers[key] = GeminiVideoProvider(api_key, base)
        provider = self._providers[key]
        # a waiting request may hold as long as the owner waits for any video ("longest wait")
        setattr(provider, "_sync_timeout", max(_max_wait(self.settings), 60.0))
        return provider

    def route_ready(self, route: Optional[str]) -> bool:
        """Can a video of this route be sent now (the sandbox's stand-in answers
        for every route; otherwise the route's key must be set and switched on)?"""
        if route is None:
            return False
        if self._sandbox():
            return True
        slot = {"openrouter": "openrouter", "google": "gemini"}.get(route)
        cfg = getattr(getattr(self.settings, "providers", None), slot, None) if slot else None
        return bool(cfg is not None and getattr(cfg, "enabled", False) and getattr(cfg, "api_key", ""))

    # ------------------------------------------------------------ the request
    def _entry(self, model: str):
        from ..catalog import catalog

        return catalog.openrouter_video(model) or catalog.get(model, modality="video")

    def default_model(self) -> Optional[str]:
        """The configured default video model when it can be called, else the
        first current one that can (OpenRouter's roster, or Gemini Omni direct
        once the Google key is set)."""
        from ..catalog import catalog
        from ..config.user_settings import default_model

        usable = [e for e in catalog.models(modality="video") if e.video_route and e.implemented and e.online is not False
                  and self.route_ready(e.video_route)]
        wanted = default_model(self.settings, "video")
        if wanted and any(e.id == wanted for e in usable):
            return wanted
        current = [e.id for e in usable if e.status == "current"]
        return (current or [e.id for e in usable] or [None])[0]

    def check(self, args: dict[str, Any], *, first: bool, last: bool) -> tuple[Any, VideoRequest, list[str]]:
        """``(catalog entry, request without frames, warnings)``, or
        :class:`VideoError` naming what the model does take."""
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise VideoError("a video needs a prompt")
        if len(prompt) > PROMPT_MAX:
            raise VideoError(f"the prompt is longer than {PROMPT_MAX} characters")
        model = str(args.get("model") or "").strip() or self.default_model()
        if not model:
            raise VideoError("no video model can be called: video goes through OpenRouter, or Gemini Omni with the Google "
                             "key (set one of them)", 409)
        from ..catalog import catalog

        model = catalog.resolve(model) or model
        entry = self._entry(model)
        if entry is None:
            raise VideoError(f"'{model}' is not a video model this version knows: not on OpenRouter's video roster, and not "
                             "Gemini Omni (list the models again: list_available_models with modality='video' and refresh=true)", 404)
        if not entry.video_route:
            raise VideoError(f"{entry.id} cannot be called in this version: {entry.note or 'not connected yet'}", 409)
        if entry.unlisted:
            raise VideoError(f"{entry.id} is not a text- or image-to-video model: {entry.note}")
        if entry.status == "retired":
            raise VideoError(f"{entry.id} is closed by its maker{f' since {entry.shutdown}' if entry.shutdown else ''}: "
                             f"{entry.note or 'it cannot be used'}", 410)
        if not entry.implemented:
            raise VideoError(f"{entry.id} cannot be used in this version: {entry.note or 'not supported'}", 409)
        if entry.online is False:
            raise VideoError(f"{entry.id} is no longer on OpenRouter's video roster", 410)
        if not self.route_ready(entry.video_route):
            self.provider(entry.video_route or "openrouter")  # raises the message that names the missing key
        from ..catalog.openrouter_videos import check_request, default_duration

        vp = entry.video_params or {}
        duration = args.get("duration")
        if duration is not None:
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                raise VideoError("duration is a whole number of seconds") from None
        else:
            duration = default_duration(vp)
        audio = args.get("generate_audio")
        if audio is not None and not isinstance(audio, bool):
            raise VideoError("generate_audio is true or false")
        problem = check_request(vp, duration=duration, resolution=args.get("resolution") or None,
                                aspect_ratio=args.get("aspect_ratio") or None, audio=audio, first_frame=first, last_frame=last)
        if problem:
            raise VideoError(problem)
        warnings: list[str] = []
        seed = args.get("seed")
        if seed is not None and vp.get("seed") is not True:
            warnings.append("the listing does not say this model takes a seed: it was not sent")
            seed = None
        if entry.status == "deprecated":
            warnings.append(f"{entry.id} is announced for shutdown{f' on {entry.shutdown}' if entry.shutdown else ''}"
                            f"{f'; its replacement is {entry.replacement}' if entry.replacement else ''}")
        resolution = args.get("resolution") or None
        if resolution and vp.get("resolutions"):  # the listing's own spelling ("4K", not "4k")
            resolution = next((r for r in vp["resolutions"] if r.lower() == str(resolution).lower()), resolution)
        req = VideoRequest(model=entry.id, prompt=prompt, duration=duration, resolution=resolution,
                           aspect_ratio=args.get("aspect_ratio") or None, generate_audio=audio,
                           seed=int(seed) if seed is not None else None)
        return entry, req, warnings

    async def estimate(self, req: VideoRequest, *, first: bool, frames: int) -> dict[str, Any]:
        """What this request will roughly cost (``generate.estimate``: the
        listed price; past videos for a per-token model; the sandbox shows the
        listed price as ``listed`` next to its own zero)."""
        from ..generate.estimate import estimate as estimate_cost

        params = {k: v for k, v in (("model", req.model), ("duration", req.duration), ("resolution", req.resolution),
                                    ("generate_audio", req.generate_audio)) if v is not None}
        return await estimate_cost(self.store, kind="video", tool=TOOL, params=params, refs=frames, first_frame=first)

    async def _frame(self, value: Any, which: str) -> tuple[str, dict[str, Any]]:
        """A frame given as a work id, a local image path or a data URL ->
        (data URL within the size limits, how the job names its source)."""
        from ..chat.images import ImageUnreadable, prepare_image

        v = str(value or "").strip()
        if v.startswith("data:image/"):
            return v, {"inline": True}
        path: Optional[Path] = None
        ref: dict[str, Any] = {}
        if re.fullmatch(r"[0-9a-f]{16}", v):
            row = await self.store.artifact(v)
            if row is not None:
                if M.MEDIA_OF.get(row["kind"]) != "image":
                    raise VideoError(f"{which}: work {v} is a {row['kind']}, not an image")
                path, ref = Path(row["file_path"]), {"artifact_id": v}
        if path is None:
            path = Path(v).expanduser()
            if not path.is_file():
                raise VideoError(f"{which}: no image work has the id '{v}' and no such file exists")
            if path.suffix.lower() not in M.IMAGE_EXTS:
                raise VideoError(f"{which}: {path.name} is not an image (png, jpg, webp, gif)")
            known = await self.store.artifact_by_path(str(path.resolve()))
            ref = {"artifact_id": known["id"]} if known and M.MEDIA_OF.get(known["kind"]) == "image" else {"file": path.name}
        if not path.is_file():
            raise VideoError(f"{which}: the image is no longer on disk", 410)
        try:
            prepared = await asyncio.to_thread(prepare_image, path)
        except (ImageUnreadable, OSError) as e:
            raise VideoError(f"{which}: the image cannot be read or made small enough to send ({e})") from e
        return prepared.data_url, ref

    # ------------------------------------------------------------ start
    async def start(self, args: dict[str, Any], *, source: str = "gui", links: Optional[dict[str, Any]] = None,
                    sources: Optional[dict[str, Any]] = None, call_id: Optional[str] = None) -> dict[str, Any]:
        """Check, estimate, create the row, send, store the remote id, watch.

        ``args``: the request (``PARAMS``) plus ``first_frame`` / ``last_frame``
        (a work id, a local path or a data URL). ``sources``: how the page named
        the frames (``{"frames": {"first": {artifact_id|upload_id}, …}}``).
        ``call_id``: the ledger row of the MCP call this runs in (else one is
        made here). Returns the public row: ``running``, or ``error`` when the
        vendor refused the request (its own words in ``error``)."""
        first_v, last_v = args.get("first_frame"), args.get("last_frame")
        entry, req, warnings = self.check(args, first=bool(first_v), last=bool(last_v))
        provider = self.provider("sandbox" if self._sandbox() else (entry.video_route or "openrouter"))
        frames_ref: dict[str, Any] = {}
        for which, value in (("first", first_v), ("last", last_v)):
            if value:
                url, ref = await self._frame(value, f"{which}_frame")
                setattr(req, f"{which}_frame", url)
                frames_ref[which] = ((sources or {}).get("frames") or {}).get(which) or ref
        estimate = await self.estimate(req, first=bool(first_v), frames=len(frames_ref))
        own_call = call_id is None
        if own_call:
            call_id = await self.store.call_started(TOOL, {**req.summary(), "prompt": req.prompt[:200]}, source=source,
                                                    conversation_id=(links or {}).get("conversation_id"))
        gid = uuid.uuid4().hex[:16]
        public_params = {k: v for k, v in (("prompt", req.prompt), ("model", req.model), ("duration", req.duration),
                                           ("resolution", req.resolution), ("aspect_ratio", req.aspect_ratio),
                                           ("generate_audio", req.generate_audio), ("seed", req.seed)) if v is not None}
        await self.store.create_generation(gid, kind="video", tool=TOOL, model=req.model, title=_first_line(req.prompt) or None,
                                           params=public_params, sources={"frames": frames_ref} if frames_ref else {},
                                           estimate=estimate, source=source, meta=links)
        remote: dict[str, Any] = {"request": req.summary(), "polls": 0, "warnings": warnings, "own_call": own_call,
                                  "owner": OWNER, "keep_collecting": _keep_collecting(self.settings)}
        await self.store.update_generation(gid, provider=provider.name, call_id=call_id, remote=remote)
        await self._publish("generation.started", gid)
        if getattr(provider, "waits_in_submit", False):
            # Gemini Omni (the default, measured 2026-10-06): the vendor's answer *is* the video, after
            # minutes. The caller is not kept waiting: the sending runs in a task of this process, which
            # holds the row's lease meanwhile (so no sweeper takes it for "died before its id came back").
            sent_at = time.time()
            remote["sending_since"] = sent_at
            remote["waits_in_submit"] = True
            await self.store.update_generation(gid, submitted_at=sent_at, remote=remote)
            await self.store.claim_generation(gid, OWNER, sent_at + _max_wait(self.settings) + LEASE_SLACK)
            task = asyncio.create_task(self._send_quietly(gid, provider, req, remote, call_id, own_call, sent_at),
                                       name=f"video-send-{gid}")
            self._sending[gid] = task
            task.add_done_callback(lambda t, gid=gid: self._sending.pop(gid, None) if self._sending.get(gid) is t else None)
            return await self.public(gid)
        return await self._send(gid, provider, req, remote, call_id, own_call, None)

    async def _send_quietly(self, gid: str, provider: VideoProvider, req: VideoRequest, remote: dict[str, Any],
                            call_id: Any, own_call: bool, sent_at: float) -> None:
        try:
            await self._send(gid, provider, req, remote, call_id, own_call, sent_at)
        except asyncio.CancelledError:
            raise  # the service is stopping: the row stays without an id, the next start marks it lost
        except Exception as e:  # noqa: BLE001 — already recorded on the row by _send
            logger.warning("video %s: sending failed: %s", gid, e)

    async def _send(self, gid: str, provider: VideoProvider, req: VideoRequest, remote: dict[str, Any], call_id: Any,
                    own_call: bool, sent_at: Optional[float]) -> dict[str, Any]:
        """Hand the request to the vendor, store its id, start watching."""
        try:
            submitted = await provider.submit(req)
        except VideoProviderError as e:
            remote["charged"] = e.charged
            await self.store.update_generation(gid, status="error", error=str(e.said)[:2000], error_kind=e.error_kind or "other",
                                               finished_at=time.time(), remote=remote)
            if own_call:
                await self.store.call_finished(call_id, status="error", duration_ms=0, model=req.model, provider=provider.name,
                                               error=str(e.said)[:1000])
            logger.warning("video %s refused by %s: %s", gid, provider.name, str(e.said)[:300])
            await self._publish("generation.finished", gid)
            return await self.public(gid)
        except Exception as e:  # noqa: BLE001 — unexpected: recorded, never left 'running'
            remote["charged"] = "unknown"
            await self.store.update_generation(gid, status="error", error=f"{type(e).__name__}: {e}"[:2000], error_kind="other",
                                               finished_at=time.time(), remote=remote)
            if own_call:
                await self.store.call_finished(call_id, status="error", duration_ms=0, model=req.model, provider=provider.name,
                                               error=str(e)[:1000])
            await self._publish("generation.finished", gid)
            raise
        now = time.time()
        remote["wait_from"] = now
        remote.pop("sending_since", None)
        # the one write this design rests on: the remote id, before any waiting
        await self.store.update_generation(gid, remote_id=submitted.remote_id, submitted_at=sent_at or now,
                                           remote_status=submitted.status, remote=remote, status="running")
        if own_call:
            await self.store.call_finished(call_id, status="ticket", duration_ms=0, model=req.model, provider=provider.name,
                                           result={"generation_id": gid, "remote_id": submitted.remote_id})
        logger.info("video %s sent to %s as %s (%s)", gid, provider.name, submitted.remote_id, req.model)
        await self._publish("generation.updated", gid)
        if submitted.status == "completed":
            # the answer was the finished video (a vendor that holds the request): collect it now
            row, remote = await self._remote(gid)
            await self._ask(gid, provider, row, remote)
        self._spawn(gid)
        return await self.public(gid)

    # ------------------------------------------------------------ rows and events
    async def public(self, gid: str) -> dict[str, Any]:
        from ..generate.manager import public_generation

        row = await self.store.generation(gid)
        if row is None:
            raise VideoError("generation not found", 404)
        return await public_generation(self.store, row, self.settings)

    async def _publish(self, kind: str, gid: str, *, ephemeral: bool = False) -> None:
        try:
            await self.bus.publish({"type": kind, "generation": await self.public(gid)}, ephemeral=ephemeral)
        except Exception as e:  # pragma: no cover - the store is closing
            logger.debug("could not announce %s for %s: %s", kind, gid, e)
        if kind == "generation.finished" or kind == "generation.updated":
            ev = self._events.get(gid)
            if ev is not None:
                ev.set()

    async def _remote(self, gid: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = await self.store.generation(gid)
        if row is None:
            raise VideoError("generation not found", 404)
        return row, dict(row.get("remote") or {})

    # ------------------------------------------------------------ watching
    def _spawn(self, gid: str) -> None:
        if self.closing:
            return
        task = self._tasks.get(gid)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._watch(gid), name=f"video-{gid}")
        self._tasks[gid] = task
        task.add_done_callback(lambda t, gid=gid: self._tasks.pop(gid, None) if self._tasks.get(gid) is t else None)

    async def resume(self) -> list[str]:
        """Watch every video the vendor has and nobody here watches yet (startup,
        then the sweeper). Videos whose sending died before an id came back are lost."""
        now = time.time()
        keep = set(self._sending)
        for row in await self.store.unsent_videos(now - 120):
            holder = row.get("lease_owner")
            if holder and holder != OWNER and (row.get("lease_until") or 0) > now and not holder_dead(holder):
                keep.add(row["id"])  # another live process is still sending it
        lost = await self.store.lose_unsent_videos(now - 120, keep=keep)
        for gid in lost:
            row = await self.store.generation(gid)
            remote = dict((row or {}).get("remote") or {})
            if remote.get("waits_in_submit"):
                remote["charged"] = "likely"
                await self.store.update_generation(gid, remote=remote, error=WAITED_LOST)
            await self._publish("generation.finished", gid)
        started = []
        for row in await self.store.video_jobs_open():
            gid = row["id"]
            task = self._tasks.get(gid)
            if task is not None and not task.done():
                continue
            holder = row.get("lease_owner")
            if holder not in (None, OWNER) and (row.get("lease_until") or 0) > time.time():
                if not holder_dead(holder):
                    continue  # another live process is watching it
                await self.store.release_generation(gid, holder)  # its process died: the lease is void
            self._spawn(gid)
            started.append(gid)
        return started

    def start_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep(), name="video-sweeper")

    async def _sweep(self) -> None:
        while not self.closing:
            await asyncio.sleep(min(SWEEP_EVERY, max(1.0, (_dev_poll() or SWEEP_EVERY) * 4)))
            try:
                await self.resume()
            except Exception as e:  # pragma: no cover - the store is closing
                logger.debug("video sweep failed: %s", e)

    async def _watch(self, gid: str) -> None:
        settings = self.settings
        first_wait = poll_interval(0)
        if not await self.store.claim_generation(gid, OWNER, time.time() + first_wait + LEASE_SLACK):
            row = await self.store.generation(gid)
            holder = (row or {}).get("lease_owner")
            if not holder_dead(holder):
                return
            await self.store.release_generation(gid, holder)
            if not await self.store.claim_generation(gid, OWNER, time.time() + first_wait + LEASE_SLACK):
                return
        try:
            row, remote = await self._remote(gid)
            if remote.get("owner") != OWNER:
                # a job this process did not send: the service restarted (or another one died) while it was being made
                now = time.time()
                remote.setdefault("resumed", []).append({"at": now, "after_s": round(now - (row.get("submitted_at") or now), 1)})
                remote["owner"] = OWNER
                await self.store.update_generation(gid, remote=remote)
                logger.info("video %s (%s) picked up again after a restart", gid, row.get("remote_id"))
                await self._publish("generation.updated", gid)
            try:
                provider = self.provider(row.get("provider") or "openrouter")
            except VideoError as e:
                logger.warning("video %s cannot be watched here: %s", gid, e)
                return
            while not self.closing:
                row, remote = await self._remote(gid)
                status = row["status"]
                if status not in WAITING or not row.get("remote_id"):
                    return
                if status == "detached" and not remote.get("keep_collecting", True):
                    return
                settings = self.settings
                wait_from = remote.get("wait_from") or row.get("submitted_at") or time.time()
                deadline = wait_from + _max_wait(settings)
                polls = int(remote.get("polls") or 0)
                last = row.get("polled_at") or wait_from
                now = time.time()
                if now >= deadline:
                    await self._gave_up(gid, remote, "waited too long")
                    return
                pause = max(0.0, min(last + poll_interval(polls) - now, deadline - now))
                await self.store.claim_generation(gid, OWNER, now + pause + LEASE_SLACK)
                if pause > 0:
                    await asyncio.sleep(pause)
                row, remote = await self._remote(gid)
                if row["status"] not in WAITING:
                    return
                await self._ask(gid, provider, row, remote)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a watcher never takes the service down; the sweeper retries
            logger.exception("video %s: watcher failed: %s", gid, e)
        finally:
            try:
                await self.store.release_generation(gid, OWNER)
            except Exception:  # pragma: no cover - the store is closing
                pass

    async def _ask(self, gid: str, provider: VideoProvider, row: dict[str, Any], remote: dict[str, Any]) -> str:
        """Ask the vendor once and act on the answer. Returns the remote status
        (``"error"`` when asking failed)."""
        remote["polls"] = int(remote.get("polls") or 0) + 1
        try:
            state = await provider.status(row["remote_id"])
        except VideoProviderError as e:
            remote["last_error"] = str(e.said or e)[:500]
            if not e.transient and e.status not in (None, 404) and e.error_kind != "lost":
                # the vendor refuses to answer (a 4xx that says the same next time: the key, a bad request).
                # Asking on would only hide it: stop now, keep the id — "ask again" works once it is fixed
                remote["charged"] = "likely"
                await self.store.update_generation(
                    gid, status="gave_up", error_kind=e.error_kind or "other", finished_at=time.time(), polled_at=time.time(),
                    remote=remote,
                    error=f"the provider refused to say how the video is doing ({e.said}). The job was sent and is probably "
                          "billed; it is still known by its id — ask again once this is fixed (nothing is sent or billed again).")
                logger.warning("video %s: stopped asking, the provider refused: %s", gid, str(e.said)[:300])
                await self._publish("generation.finished", gid)
                return "error"
            if not e.transient:
                # the vendor no longer knows the job (expired, or never kept)
                remote["charged"] = "unknown"
                await self.store.update_generation(gid, status="error", error_kind="lost", finished_at=time.time(), polled_at=time.time(),
                                                   error=f"the provider no longer knows this video job ({e.said}); whether it was "
                                                         "billed is not reported", remote=remote)
                await self._settle_failed(row, remote, "the provider no longer knows the job")
                await self._publish("generation.finished", gid)
                return "error"
            await self.store.update_generation(gid, polled_at=time.time(), remote=remote)
            await self._publish("generation.updated", gid, ephemeral=True)
            return "error"
        remote.pop("last_error", None)
        if state.usage:
            remote["usage"] = state.usage
        changed = state.status != row.get("remote_status")
        await self.store.update_generation(gid, remote_status=state.status, polled_at=time.time(), remote=remote)
        if state.status == "completed":
            await self._collect(gid, provider, state)
        elif state.status in ("failed", "cancelled", "expired"):
            words = {"failed": "the provider could not make the video", "cancelled": "the provider cancelled the job",
                     "expired": "the job expired at the provider before it was collected"}[state.status]
            remote["charged"] = "unknown"
            message = f"{words}: {state.error}" if state.error else words
            from ..generate.errors import classify

            kind = classify(state.error or "") if state.error else "other"
            if state.status == "expired":
                kind = "timeout"
            await self.store.update_generation(gid, status="error", error=message[:2000], error_kind=kind,
                                               finished_at=time.time(), remote=remote)
            await self._settle_failed(row, remote, message)
            await self._publish("generation.finished", gid)
        else:
            await self._publish("generation.updated", gid, ephemeral=not changed)
        return state.status

    async def _gave_up(self, gid: str, remote: dict[str, Any], why: str) -> None:
        minutes = _max_wait(self.settings) / 60
        await self.store.update_generation(
            gid, status="gave_up", error_kind="gave_up", finished_at=time.time(), remote=remote,
            error=f"{why}: no video after {minutes:g} minutes. The job is still known by its id; ask again later "
                  "(nothing is sent or billed again).")
        logger.info("video %s: %s", gid, why)
        await self._publish("generation.finished", gid)

    async def _settle_failed(self, row: dict[str, Any], remote: dict[str, Any], message: str) -> None:
        call_id = row.get("call_id")
        if not call_id:
            return
        try:
            call = await self.store.call(call_id)
            if call and call.get("status") in ("ticket", "started") and (remote.get("own_call") or call.get("status") == "ticket"):
                await self.store.call_finished(call_id, status="error", duration_ms=call.get("duration_ms") or 0,
                                               model=row.get("model"), provider=row.get("provider"), error=message[:1000])
        except Exception as e:  # pragma: no cover - bookkeeping
            logger.debug("ledger update for %s failed: %s", row.get("id"), e)

    # ------------------------------------------------------------ collecting
    def _dest(self, row: dict[str, Any]) -> Path:
        """Where the file goes: ``videos/<date>/video_<stamp>_<id>.mp4``, named
        from the job (not the clock) so a collection that is repeated after a
        restart lands on the same file and the same work."""
        base = Path(self.settings.storage.base_path)
        sent = datetime.fromtimestamp(row.get("submitted_at") or row["created_at"], tz=timezone.utc)
        folder = base / M.BY_KIND["video"].folder / sent.strftime("%Y-%m-%d")
        return (folder / f"video_{sent.strftime('%Y%m%d%H%M%S')}_{row['id'][:8]}.mp4").resolve()

    async def _collect(self, gid: str, provider: VideoProvider, state: Any) -> None:
        from ..artifacts import mime_for, public_row
        from ..catalog.paths import data_home
        from .media import extract_poster, probe

        row, remote = await self._remote(gid)
        dest = self._dest(row)
        delay = 2.0
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            try:
                if not (dest.is_file() and dest.stat().st_size > 0):
                    await provider.download(row["remote_id"], dest)
                break
            except VideoProviderError as e:
                remote["last_error"] = str(e.said or e)[:500]
                if attempt == DOWNLOAD_TRIES or not e.transient:
                    await self.store.update_generation(
                        gid, status="gave_up", error_kind="download", finished_at=time.time(), remote=remote,
                        error=f"the video is done at the provider but could not be downloaded ({e.said}); ask again later "
                              "(nothing is sent or billed again)")
                    await self._publish("generation.finished", gid)
                    return
                await asyncio.sleep(delay if not _dev_poll() else 0.2)
                delay *= 2.5
        facts = await asyncio.to_thread(probe, dest)
        existing = await self.store.artifact_by_path(str(dest))
        if existing is not None:
            aid = existing["id"]
        else:
            aid = uuid.uuid4().hex[:16]
            poster = data_home() / "cache" / "posters" / f"{aid}.jpg"
            has_poster = await asyncio.to_thread(extract_poster, dest, poster)
            frames = (row.get("sources") or {}).get("frames") or {}
            parent = (frames.get("first") or {}).get("artifact_id") or (frames.get("last") or {}).get("artifact_id")
            cost = self._cost(provider, state, row)
            params = dict(row.get("params") or {})
            prompt = params.pop("prompt", None)
            est = row.get("estimate") or {}
            meta = {
                "generation_id": gid, "remote_id": row.get("remote_id"), "has_audio": facts.get("has_audio"),
                "fps": facts.get("fps"), "video_codec": facts.get("video_codec"), "audio_codec": facts.get("audio_codec"),
                "facts_from": facts.get("source"), "poster": has_poster, "frames": frames or None,
                "requested": {k: params.get(k) for k in ("duration", "resolution", "aspect_ratio", "generate_audio") if params.get(k) is not None},
                "estimate": {k: est.get(k) for k in ("basis", "usd", "low", "high") if est.get(k) is not None} or None,
                "waited_s": round(time.time() - (row.get("submitted_at") or time.time()), 1),
                "late": bool(remote.get("detached_at")) or None, "resumed": bool(remote.get("resumed")) or None,
                **(row.get("meta") or {}),
            }
            created = await self.store.add_artifact(
                artifact_id=aid, kind="video", tool=TOOL, model=row.get("model"), provider=row.get("provider"),
                title=row.get("title"), prompt=prompt, params=params, file_path=str(dest), mime=mime_for(dest),
                bytes=dest.stat().st_size, width=facts.get("width"), height=facts.get("height"),
                duration_s=facts.get("duration_s"), cost_usd=cost, source=row.get("source"), call_id=row.get("call_id"),
                parent_id=parent, meta={k: v for k, v in meta.items() if v is not None})
            if created is None:  # indexed meanwhile (another process): use that row
                existing = await self.store.artifact_by_path(str(dest))
                aid = existing["id"] if existing else aid
            else:
                slim = {k: v for k, v in public_row(created).items() if k not in ("text", "params", "meta")}
                await self.bus.publish({"type": "artifact.created", "artifact": slim})
        remote.pop("last_error", None)
        remote["late"] = bool(remote.get("detached_at"))
        cost = self._cost(provider, state, row)
        remote["charged"] = "yes" if state.cost_usd is not None else "likely"
        if cost is not None and state.cost_usd is None:
            remote["cost_from"] = "estimate"  # the vendor reported no usage: the estimate is billed (charged: likely)
        await self.store.update_generation(gid, status="done", finished_at=time.time(), cost_usd=cost, artifact_ids=[aid],
                                           remote_status="completed", remote=remote, error=None, error_kind=None)
        if row.get("call_id"):
            await self.store.settle_ticket_call(row["call_id"], model=row.get("model"), provider=row.get("provider"), cost_usd=cost)
        logger.info("video %s collected (%s, $%s)", gid, dest.name, cost)
        await self._publish("generation.finished", gid)

    @staticmethod
    def _cost(provider: VideoProvider, state: Any, row: dict[str, Any]) -> Optional[float]:
        """What to bill: the vendor's own figure; else, for a provider that bills
        by usage it did not report (``estimate_when_unreported``), the estimate's
        single amount; else nothing (the ledger says "cost unknown")."""
        if state.cost_usd is not None:
            return state.cost_usd
        if not getattr(provider, "estimate_when_unreported", False):
            return None
        usd = (row.get("estimate") or {}).get("usd")
        return float(usd) if isinstance(usd, (int, float)) and not isinstance(usd, bool) else None

    # ------------------------------------------------------------ the owner's two actions
    async def _video_row(self, gid: str) -> dict[str, Any]:
        row = await self.store.generation(gid)
        if row is None:
            raise VideoError("generation not found", 404)
        if row.get("kind") != "video":
            raise VideoError("only a video job can do this", 400)
        return row

    async def stop_waiting(self, gid: str) -> dict[str, Any]:
        """"Stop waiting": the vendor cannot cancel, so only our waiting ends.
        Collecting goes on in the background (``detached``) unless the setting
        ``video.keep_collecting`` is off (``abandoned``: not asked again, the
        ledger says the cost is unknown)."""
        row = await self._video_row(gid)
        if row["status"] != "running":
            return await self.public(gid)
        remote = dict(row.get("remote") or {})
        remote["detached_at"] = time.time()
        keep = _keep_collecting(self.settings)
        remote["keep_collecting"] = keep
        if not row.get("remote_id"):
            # still being sent: nothing to collect later; the sending finishes on its own
            remote["keep_collecting"] = True
            await self.store.update_generation(gid, remote=remote)
            return await self.public(gid)
        if keep:
            await self.store.update_generation(gid, status="detached", remote=remote)
            await self._publish("generation.updated", gid)
            return await self.public(gid)
        remote["charged"] = "likely"
        await self.store.update_generation(
            gid, status="abandoned", error_kind=None, finished_at=time.time(), remote=remote,
            error="stopped waiting: the provider has no cancel, so the video is probably still made and billed; "
                  "it was not collected (ask again to collect it after all)")
        task = self._tasks.get(gid)
        if task is not None and not task.done():
            task.cancel()
        if row.get("call_id"):
            await self.store.call_unsettled(row["call_id"], NOT_COLLECTED_NOTE)
        await self._publish("generation.finished", gid)
        return await self.public(gid)

    async def recheck(self, gid: str) -> dict[str, Any]:
        """"Ask again" for a job we stopped waiting on (``gave_up`` /
        ``abandoned``): one question to the vendor now — collected if it is
        done, failed if it failed, otherwise waited on again for another
        ``max_wait_minutes``. Nothing is sent or billed again."""
        row = await self._video_row(gid)
        if not row.get("remote_id") or row["status"] not in RECHECKABLE:
            raise VideoError(f"only a video we stopped waiting on can be asked about again (this one is {row['status']})", 409)
        provider = self.provider(row.get("provider") or "openrouter")
        if not await self.store.claim_generation(gid, OWNER, time.time() + LEASE_SLACK):
            raise VideoError("another OmniAPI process is asking about this video right now", 409)
        try:
            remote = dict(row.get("remote") or {})
            remote["rechecked"] = [*(remote.get("rechecked") or []), time.time()]
            remote["wait_from"] = time.time()
            remote["keep_collecting"] = True
            if row["status"] == "abandoned":
                remote.pop("detached_at", None)
            await self.store.update_generation(gid, status="running", error=None, error_kind=None, finished_at=None, remote=remote)
            row, remote = await self._remote(gid)
            status = await self._ask(gid, provider, row, remote)
            if status in ("pending", "in_progress", "queued", "processing", "running", "error"):
                row = await self.store.generation(gid)
                if row and row["status"] == "running":
                    await self._publish("generation.updated", gid)
        finally:
            await self.store.release_generation(gid, OWNER)
        row = await self.store.generation(gid)
        if row and row["status"] in WAITING:
            self._spawn(gid)
        return await self.public(gid)

    # ------------------------------------------------------------ for MCP
    async def wait(self, gid: str, timeout: Optional[float]) -> dict[str, Any]:
        """The row once it is no longer ``running`` (or after ``timeout`` s)."""
        t_end = None if timeout is None else time.monotonic() + timeout
        ev = self._events.setdefault(gid, asyncio.Event())
        try:
            while True:
                row = await self.store.generation(gid)
                if row is None or row["status"] != "running":
                    return row or {}
                left = None if t_end is None else t_end - time.monotonic()
                if left is not None and left <= 0:
                    return row
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(1.0, left) if left is not None else 1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._events.pop(gid, None)

    async def result(self, gid: str) -> dict[str, Any]:
        """What MCP hands back for a video job (inline or through a ticket)."""
        row = await self.store.generation(gid)
        if row is None or row.get("kind") != "video":
            return {"status": "not_found", "task_id": f"video_{gid}", "message": "Unknown video job."}
        view = video_view(row, self.settings)
        base = {"generation_id": gid, "task_id": f"video_{gid}", "model": row.get("model"), "provider": row.get("provider"),
                "estimate": {k: (row.get("estimate") or {}).get(k) for k in ("basis", "usd", "low", "high", "listed") if (row.get("estimate") or {}).get(k) is not None}}
        if view["warnings"]:
            base["warnings"] = view["warnings"]
        status = row["status"]
        if status == "done":
            works = await self.store.artifacts_by_ids(row.get("artifact_ids") or [])
            w = works[0] if works else {}
            meta = w.get("meta") or {}
            path = w.get("file_path")
            return {**base, "status": "completed", "artifact_id": w.get("id"), "file_path": path,
                    "video_url": Path(path).resolve().as_uri() if path else None,
                    "file_url": f"/api/artifacts/{w['id']}/file" if w else None,
                    "duration_s": w.get("duration_s"), "width": w.get("width"), "height": w.get("height"),
                    "has_audio": meta.get("has_audio"), "fps": meta.get("fps"), "cost_usd": row.get("cost_usd"),
                    "waited_s": view["waited_s"], **({"late": True} if view["late"] else {}),
                    **({"resumed_after_restart": True} if view["resumed"] else {})}
        if status in WAITING:
            remote = row.get("remote") if isinstance(row.get("remote"), dict) else {}
            keep = ("the job is kept across service restarts." if row.get("remote_id") or not remote.get("waits_in_submit") else
                    "this provider hands the video back only when it is finished: if the service stops before then, the video "
                    "is lost (it was sent and is probably billed).")
            return {**base, "status": "running", "operation": TOOL, "remote_status": row.get("remote_status"),
                    "waited_s": view["waited_s"],
                    "message": (f"The video is still being made ({row.get('remote_status') or 'pending'}, {view['waited_s'] or 0:.0f} s so far). "
                                f"Call get_job_result(task_id='video_{gid}') again in a minute or two; {keep}")}
        message = row.get("error") or status
        return {**base, "status": {"gave_up": "waited_too_long", "abandoned": "not_collected"}.get(status, "failed"),
                "error": message, "error_kind": row.get("error_kind"), "charged": view["charged"],
                **({"hint": "ask again from the generate page or POST /api/generations/{id}/recheck — nothing is sent or billed again"}
                   if view["can_recheck"] else {})}

    # ------------------------------------------------------------ shutdown
    async def close(self) -> None:
        """Stop watching. Rows stay as they are (``running`` / ``detached``):
        the next start picks them up again — that is the point."""
        self.closing = True
        tasks = [t for t in self._tasks.values() if not t.done()] + [t for t in self._sending.values() if not t.done()]
        if self._sweeper is not None:
            tasks.append(self._sweeper)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._sending.clear()
        for p in self._providers.values():
            try:
                await p.close()
            except Exception:  # pragma: no cover
                pass
        self._providers.clear()
