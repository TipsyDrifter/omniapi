"""Replay harness (development only): re-emits a stored run's events as a live run.

No model is called and nothing is billed. It exists so the dispatch → live
ticket → cancel → resume path of the GUI can be exercised end to end without
spending the owner's money. Enabled only when ``OMNIAPI_DEV=1``; point the
daemon at a scratch ``OMNIAPI_HOME`` so replays do not pollute real history.

Usage: ``{"harness": "replay", "model": "replay:<source run id>", "prompt": "..."}``.
With ``model`` = ``replay`` (no id) the most recent finished run is replayed.
A resumed replay plays the same source again and appends one line quoting the
follow-up prompt, which is enough to see the thread grow in the GUI.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, AsyncIterator, Optional

from .base import HarnessAdapter
from .events import RunEvent, RunSpec

# seconds between events; OMNIAPI_REPLAY_DELAY overrides (tests use 0)
_DEFAULT_DELAY = 0.35
_SKIP = ("status",)


def replay_enabled() -> bool:
    return os.environ.get("OMNIAPI_DEV") == "1"


class ReplayHarness(HarnessAdapter):
    name = "replay"
    supports_resume = True

    def __init__(self, settings: Any, store: Any = None):
        super().__init__(settings)
        self._store = store

    def _get_store(self) -> Any:
        if self._store is not None:
            return self._store
        from ..runtime import runtime

        if runtime.context is None:
            raise RuntimeError("replay harness needs the daemon runtime (store)")
        return runtime.context.store

    async def _source(self, spec: RunSpec) -> Optional[dict[str, Any]]:
        store = self._get_store()
        model = spec.resolved_model or spec.model or ""
        src_id = model.split(":", 1)[1] if ":" in model else ""
        if src_id:
            return await store.run(src_id)
        for row in await store.runs(limit=50):
            if row.get("state") == "done" and row.get("harness") != "replay":
                return row
        return None

    async def start(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        delay = float(os.environ.get("OMNIAPI_REPLAY_DELAY", _DEFAULT_DELAY))
        src = await self._source(spec)
        if not src:
            yield RunEvent("error", {"message": "replay: source run not found"})
            return
        events = await self._get_store().events(src["id"], limit=2000)
        session_id = spec.resume_session_id or str(uuid.uuid4())
        yield RunEvent("session_start", {"session_id": session_id, "model": f"replay:{src['id']}", "tools": [], "mcp_servers": [], "replay_of": src["id"]})
        turns = 0
        last_text = ""
        for e in events:
            if self._cancelled:
                yield RunEvent("error", {"message": "cancelled"})
                return
            t = e.get("type")
            if t in _SKIP or t in ("session_start", "result"):
                continue
            payload = dict(e.get("payload") or {})
            if t == "tool_call":
                turns += 1
            if t == "text":
                last_text = str(payload.get("text") or "")
            if delay:
                await asyncio.sleep(delay)
            yield RunEvent(str(t), payload)
        if spec.resume_session_id:
            last_text = f"（重播・續接）收到追問：{spec.prompt}"
            yield RunEvent("text", {"text": last_text})
        yield RunEvent(
            "result",
            {
                "text": last_text or f"replay of {src['id']} finished",
                "usage": {},
                "cost_usd": 0.0,
                "num_turns": turns,
                "session_id": session_id,
                "is_error": False,
                "replay_of": src["id"],
            },
        )
