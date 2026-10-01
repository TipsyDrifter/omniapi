"""In-process event bus: tool calls, run events and catalog refreshes are
published here; the store persists them and the WebSocket endpoint fans them
out to the GUI. Deliberately tiny — asyncio queues, no broker.

Every event is a dict with at least ``type`` (dotted, e.g. ``call.finished``)
and ``ts`` (unix seconds, added here if missing).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self, history: int = 500, queue_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._queue_size = queue_size
        self._seq = 0

    async def publish(self, event: dict[str, Any], *, ephemeral: bool = False) -> dict[str, Any]:
        """Fan an event out to the subscribers.

        ``ephemeral`` events (chat text deltas) are delivered live but not
        kept in the history: there are hundreds per reply and replaying them
        after a reconnect is pointless — the client re-reads the conversation.
        """
        self._seq += 1
        event = {"seq": self._seq, "ts": event.get("ts") or time.time(), **event}
        if not ephemeral:
            self._history.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # slow consumer: drop the oldest so the bus never blocks a tool
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass
        return event

    def recent(self, limit: int = 100, since_seq: int = 0) -> list[dict[str, Any]]:
        items = [e for e in self._history if e["seq"] > since_seq]
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
