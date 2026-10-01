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

from .bus import EventBus
from .catalog import catalog
from .chat import ChatManager
from .config.settings import Settings
from .core.job_manager import JobManager
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

    # ------------------------------------------------------------ build
    async def _build(self, settings: Settings, mode: str) -> ServerContext:
        storage_path = Path(settings.storage.base_path)
        for subdir in ["images", "cache", "logs"]:
            (storage_path / subdir).mkdir(parents=True, exist_ok=True)

        storage_manager = ImageStorageManager(settings.storage)
        cache_manager = CacheManager(settings.cache)
        bus = EventBus()
        store = Store()
        await store.open()

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
        )
        text_tool = TextTool(settings=settings)
        ctx = ServerContext(
            settings=settings,
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            image_generation_tool=image_generation_tool,
            image_editing_tool=image_editing_tool,
            transcription_tool=TranscriptionTool(settings=settings),
            text_tool=text_tool,
            chat=ChatManager(store, bus, text_tool),
            speech_tool=SpeechTool(settings=settings),
            music_generation_tool=MusicGenerationTool(settings=settings),
            jobs=JobManager(),
            resource_manager=ImageResourceManager(
                storage_manager=storage_manager, settings=settings.storage
            ),
            store=store,
            bus=bus,
            runs=RunManager(store, bus, settings),
            mode=mode,
        )
        await asyncio.gather(cache_manager.initialize(), storage_manager.initialize())

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
        try:
            # replies in flight store their partial text before the store closes
            if ctx.chat is not None:
                await ctx.chat.close()
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


runtime = Runtime()
