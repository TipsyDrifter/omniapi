"""Process-wide runtime: the one ServerContext every MCP session shares.

Why this exists: with the MCP SDK's streamable-http transport, the low-level
server enters the FastMCP lifespan **once per client session**. Building the
providers, storage, cache and JobManager inside that lifespan therefore
re-creates them for every Claude Code window that connects — and job tickets
handed out in one session would be unknown in another. The daemon (M2)
needs one context for the whole process, so construction moves here and the
lifespan only *borrows* it.

Ownership rules:

* ``Runtime.acquire(settings, owner="daemon")`` builds the context once and
  keeps it until ``shutdown()``; sessions that later ``acquire(owner="session")``
  get the same object and never close it.
* In plain stdio mode there is no daemon; the single session acquires with
  ``owner="session"`` and its ``release()`` closes everything on exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .artifacts import backfill, current_call, index_result
from .bus import EventBus
from .catalog import catalog
from .chat import ChatManager
from .config.settings import Settings
from .core.job_manager import JobManager
from .generate import GenerationManager
from .recorder import extract_call_meta
from .resources.image_resources import ImageResourceManager
from .runs.manager import RunManager
from .storage.manager import ImageStorageManager
from .store.db import Store
from .tools.image_editing import ImageEditingTool
from .tools.image_generation import ImageGenerationTool
from .tools.music_generation import MusicGenerationTool
from .tools.speech import SpeechTool
from .tools.text import TextTool
from .tools.transcription import TranscriptionTool
from .utils.cache import CacheManager

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    """Everything a tool handler needs. One per process."""

    settings: Settings
    storage_manager: ImageStorageManager
    cache_manager: CacheManager
    image_generation_tool: ImageGenerationTool
    image_editing_tool: ImageEditingTool
    transcription_tool: TranscriptionTool
    text_tool: TextTool
    speech_tool: SpeechTool
    music_generation_tool: MusicGenerationTool
    jobs: JobManager
    resource_manager: ImageResourceManager
    store: Store
    bus: EventBus
    runs: RunManager
    chat: Optional[ChatManager] = None  # optional so older test doubles still construct
    generations: Optional[GenerationManager] = None
    #: 1.4-M3: video jobs (sent, waited on across restarts, collected)
    videos: Optional[Any] = None
    started_at: float = field(default_factory=time.time)
    pid: int = field(default_factory=os.getpid)
    mode: str = "stdio"  # "stdio" | "daemon"


class Runtime:
    """Builds and owns the ServerContext singleton."""

    def __init__(self) -> None:
        self.context: Optional[ServerContext] = None
        self._lock = asyncio.Lock()
        self._owner: Optional[str] = None
        self._sessions = 0
        self._tasks: list[asyncio.Task] = []
        #: tool objects replaced by a settings change; work that started on them
        #: finishes on them, so they are closed only at shutdown
        self._retired: list[Any] = []

    # ------------------------------------------------------------ build
    @staticmethod
    def _build_tools(settings: Settings, storage_manager: ImageStorageManager, cache_manager: CacheManager) -> dict[str, Any]:
        """The objects that hold provider clients — everything a key change touches."""
        image_generation_tool = ImageGenerationTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings,
        )
        image_editing_tool = ImageEditingTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings,
            openai_client=image_generation_tool.get_openai_provider(),
            generation_tool=image_generation_tool,
        )
        return {
            "image_generation_tool": image_generation_tool,
            "image_editing_tool": image_editing_tool,
            "transcription_tool": TranscriptionTool(settings=settings),
            "text_tool": TextTool(settings=settings),
            "speech_tool": SpeechTool(settings=settings),
            "music_generation_tool": MusicGenerationTool(settings=settings),
        }

    async def _build(self, settings: Settings, mode: str) -> ServerContext:
        from .catalog import data_home
        from .devmode import offline
        from .layout import apply_storage_default

        moved = apply_storage_default(settings)
        if moved:
            logger.info("Installed layout: works folder defaults to %s", moved)
        catalog.set_tier_overrides(getattr(settings, "tiers", None))
        catalog.set_direct_providers(settings)  # 1.4-M2: which OpenRouter image models are duplicates
        for problem in catalog.problems:  # found at import, before logging was set up: say it again here
            logger.error("catalog.json: %s", problem)
        if offline():
            # a sandbox never writes into the owner's real works folder: its stand-in
            # output goes under the sandbox's own data home (OMNIAPI_HOME)
            sandbox_storage = data_home() / "storage"
            if Path(settings.storage.base_path).resolve() != sandbox_storage.resolve():
                logger.info("Offline sandbox: storage redirected %s -> %s", settings.storage.base_path, sandbox_storage)
                settings.storage.base_path = str(sandbox_storage)
        storage_path = Path(settings.storage.base_path)
        for subdir in ["images", "cache", "logs"]:
            (storage_path / subdir).mkdir(parents=True, exist_ok=True)

        storage_manager = ImageStorageManager(settings.storage)
        cache_manager = CacheManager(settings.cache)
        bus = EventBus()
        store = Store()
        await store.open()

        tools = self._build_tools(settings, storage_manager, cache_manager)
        ctx = ServerContext(
            settings=settings,
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            **tools,
            chat=ChatManager(store, bus, tools["text_tool"]),
            jobs=JobManager(soft_timeout=_dev_soft_timeout()),
            resource_manager=ImageResourceManager(
                storage_manager=storage_manager, settings=settings.storage
            ),
            store=store,
            bus=bus,
            runs=RunManager(store, bus, settings),
            mode=mode,
        )
        await asyncio.gather(cache_manager.initialize(), storage_manager.initialize())

        # Works index (v1.1): a generation that finishes after its call returned a
        # ticket is indexed from here; inline results are indexed by the recorder.
        async def _index_late(result: Any) -> None:
            call = current_call.get()
            await index_result(ctx, call, result)
            if call is not None and call.call_id:
                # the ledger row of the ticketed call gets its real cost now, fetched or not
                meta = extract_call_meta(result)
                if not meta.get("error"):
                    await store.settle_ticket_call(call.call_id, model=meta.get("model"), provider=meta.get("provider"), cost_usd=meta.get("cost_usd"),
                                                cost_estimated=bool(meta.get("cost_estimated")))

        ctx.jobs.on_late_result = _index_late

        # GUI generation jobs (v1.1): whatever was still generating died with the last process —
        # except a video the vendor took (1.4-M3): that one is waited on again, below
        ctx.generations = GenerationManager(store, bus)
        stale = await store.interrupt_running_generations()
        if stale:
            logger.info("Generation jobs left running by the previous process marked interrupted: %s", stale)
        from .video.jobs import VideoJobs

        ctx.videos = VideoJobs(store, bus, lambda: ctx.settings)
        ctx.generations.videos = ctx.videos
        try:
            resumed = await ctx.videos.resume()
            if resumed:
                logger.info("Video jobs picked up again after the restart: %s", resumed)
        except Exception as e:  # pragma: no cover - a video that cannot be resumed now is retried by the sweeper
            logger.warning("Video jobs could not be resumed at startup: %s", e)
        ctx.videos.start_sweeper()
        # chat proposals (1.2-M4) start generation jobs; those left generating settle now
        try:
            await ctx.chat.attach(ctx.generations, ctx)
        except Exception as e:  # pragma: no cover - a chat that cannot propose is still a chat
            logger.warning("Chat could not attach to the generation jobs: %s", e)
        self._tasks.append(asyncio.create_task(self._first_backfill(ctx), name="artifact-backfill"))

        # Model discovery in the background: never block startup on a vendor.
        self._tasks.append(
            asyncio.create_task(self._discover(settings, bus), name="model-discovery")
        )
        if settings.storage.cleanup_enabled:
            self._tasks.append(
                asyncio.create_task(storage_manager.start_cleanup_task(), name="storage-cleanup")
            )
            logger.info("Storage retention sweep enabled (retention_days=%s)", settings.storage.retention_days)
        else:
            logger.info("Storage retention sweep disabled — keeping all files")
        return ctx

    @staticmethod
    async def _first_backfill(ctx: ServerContext) -> None:
        """First start with the works index: pick up what was generated before
        it existed. Read-only on the files; later starts skip it (the CLI and
        ``POST /api/artifacts/backfill`` re-scan on demand)."""
        try:
            if await ctx.store.artifact_counts():
                return
            res = await backfill(ctx.store, ctx.settings.storage.base_path)
            if res["added_total"]:
                logger.info("Works index backfilled: %s (%.1fs)", res["added"], res["seconds"])
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Works backfill failed: %s", e)

    @staticmethod
    async def _discover(settings: Settings, bus: EventBus) -> None:
        try:
            res = await catalog.refresh(settings)
            await bus.publish(
                {
                    "type": "catalog.refreshed",
                    "providers": {k: len(v.models) for k, v in res.items()},
                }
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Model discovery failed: %s", e)

    # ------------------------------------------------------------ settings change (1.3-M2)
    #: what a settings change does not touch: the works folder, cache and
    #: server sections need a restart (they are carried over from the running settings)
    RESTART_ONLY = ("storage", "cache", "server")

    async def apply_settings(self, settings: Settings) -> list[str]:
        """Swap new settings into the running context without a restart.

        The tool objects that hold provider clients are rebuilt from
        ``settings`` and replace the old ones on the shared context — every
        MCP session, the REST handlers, chat and runs read them from there on
        their next call. Work already running keeps the old objects (they are
        closed only at shutdown). Tier overrides are laid over the catalog.
        Returns the catalog providers whose key or switch changed."""
        async with self._lock:
            ctx = self.context
            if ctx is None:
                raise RuntimeError("runtime not ready")
            old = ctx.settings
            for section in self.RESTART_ONLY:
                setattr(settings, section, getattr(old, section))
            changed = _changed_providers(old, settings)
            tools = self._build_tools(settings, ctx.storage_manager, ctx.cache_manager)
            retired = [getattr(ctx, name) for name in tools]
            for name, obj in tools.items():
                setattr(ctx, name, obj)
            ctx.settings = settings
            if ctx.chat is not None:
                ctx.chat.text = tools["text_tool"]
            ctx.runs.settings = settings
            ctx.runs.registry.settings = settings
            catalog.set_tier_overrides(settings.tiers)
            catalog.set_direct_providers(settings)
            self._retired.extend(retired)
            logger.info("Settings applied without restart (providers changed: %s)", ", ".join(changed) or "none")
            return changed

    def refresh_discovery(self, settings: Settings, providers: list[str]) -> None:
        """Re-list the models of providers whose key changed (background; never in an offline sandbox)."""
        from .devmode import offline

        ctx = self.context
        if not providers or offline() or ctx is None:
            return

        async def go() -> None:
            try:
                res = await catalog.refresh(settings, force=True, only=providers)
                await ctx.bus.publish({"type": "catalog.refreshed", "providers": {k: len(v.models) for k, v in res.items()}})
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Model discovery after a settings change failed: %s", e)

        self._tasks.append(asyncio.create_task(go(), name="model-discovery-settings"))

    # ------------------------------------------------------------ lifecycle
    async def acquire(self, settings: Settings, *, owner: str = "session") -> ServerContext:
        """Return the shared context, building it on first use.

        ``owner="daemon"`` marks the process as daemon-owned: later session
        releases will not tear the context down.
        """
        async with self._lock:
            if self.context is None:
                mode = "daemon" if owner == "daemon" else "stdio"
                self.context = await self._build(settings, mode)
                self._owner = owner
                logger.info("Runtime built (owner=%s, pid=%s)", owner, self.context.pid)
            elif owner == "daemon":
                self._owner = "daemon"
            if owner == "session":
                self._sessions += 1
            return self.context

    async def release(self, *, owner: str = "session") -> None:
        """A session finished. Closes everything only when nobody owns it."""
        async with self._lock:
            if owner == "session":
                self._sessions = max(0, self._sessions - 1)
            if self._owner == "daemon" and owner != "daemon":
                return
            if owner == "daemon" or self._sessions == 0:
                await self._shutdown_locked()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        ctx = self.context
        if ctx is None:
            return
        logger.info("Shutting down runtime...")
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        try:
            providers_to_close: dict[int, Any] = {}
            registry = ctx.image_generation_tool.provider_registry
            for provider in registry.get_all_providers():
                providers_to_close.setdefault(id(provider), provider)
            for provider in ctx.image_generation_tool._pending_providers:
                providers_to_close.setdefault(id(provider), provider)
            for attr in ("_provider", "_gemini_provider"):
                p = getattr(ctx.image_editing_tool, attr, None)
                if p is not None:
                    providers_to_close.setdefault(id(p), p)
            for provider in providers_to_close.values():
                if hasattr(provider, "close"):
                    await provider.close()
        except Exception:
            pass
        # tools replaced by settings changes: their work is over by now (or being cancelled)
        for tool in self._retired:
            try:
                if hasattr(tool, "close"):
                    await tool.close()
                elif hasattr(tool, "provider_registry"):
                    seen: dict[int, Any] = {}
                    for provider in [*tool.provider_registry.get_all_providers(), *getattr(tool, "_pending_providers", [])]:
                        seen.setdefault(id(provider), provider)
                    for provider in seen.values():
                        if hasattr(provider, "close"):
                            await provider.close()
            except Exception:
                pass
        self._retired.clear()
        try:
            # replies in flight store their partial text before the store closes
            if ctx.chat is not None:
                await ctx.chat.close()
        except Exception:
            pass
        try:
            # jobs in flight record themselves as cancelled before the store closes
            if ctx.generations is not None:
                await ctx.generations.close()
        except Exception:
            pass
        try:
            # video watchers stop; their rows stay waiting and the next start picks them up
            if ctx.videos is not None:
                await ctx.videos.close()
        except Exception:
            pass
        await asyncio.gather(
            ctx.cache_manager.close(),
            ctx.storage_manager.close(),
            ctx.transcription_tool.close(),
            ctx.text_tool.close(),
            ctx.speech_tool.close(),
            ctx.music_generation_tool.close(),
            ctx.jobs.close(),
            ctx.runs.close(),
            ctx.store.close(),
            return_exceptions=True,
        )
        self.context = None
        self._owner = None
        self._sessions = 0
        logger.info("Runtime shutdown complete")


def _dev_soft_timeout() -> float:
    """How long a tool call waits before it hands back a ticket: 45 s, or
    ``OMNIAPI_JOB_SOFT_TIMEOUT`` in a development daemon (to try tickets —
    e.g. a video's — without waiting 45 s)."""
    from .devmode import dev_enabled

    if dev_enabled():
        try:
            v = float(os.environ.get("OMNIAPI_JOB_SOFT_TIMEOUT", "") or 0)
            if v > 0:
                return v
        except ValueError:
            pass
    return 45.0


def _changed_providers(old: Settings, new: Settings) -> list[str]:
    """Catalog provider names whose key, switch or address differs."""
    from .config.user_settings import PROVIDERS

    out = []
    for slot, cat in PROVIDERS.items():
        a, b = getattr(old.providers, slot, None), getattr(new.providers, slot, None)
        fields = ("api_key", "enabled", "base_url")
        if tuple(getattr(a, f, None) for f in fields) != tuple(getattr(b, f, None) for f in fields):
            out.append(cat)
    return out


runtime = Runtime()
