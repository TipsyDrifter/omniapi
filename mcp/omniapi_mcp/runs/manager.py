"""RunManager — starts harness runs, persists their events, tracks state.

A run is asynchronous: ``start()`` returns the run id immediately and a
background task drives the adapter, writing every event to the store and
publishing ``run.event`` on the bus (the board subscribes). Terminal states:
done / error / cancelled / dead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from ..bus import EventBus
from ..harness.events import TERMINAL_STATES, RunEvent, RunSpec, new_run_id, short_tool_summary
from ..harness.registry import HarnessRegistry
from ..store.db import Store

logger = logging.getLogger(__name__)

_EVENT_PAYLOAD_LIMIT = 6000


def _slim(payload: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > _EVENT_PAYLOAD_LIMIT:
            out[k] = v[:_EVENT_PAYLOAD_LIMIT] + f"…[+{len(v) - _EVENT_PAYLOAD_LIMIT} chars]"
        else:
            out[k] = v
    return out


class RunManager:
    def __init__(self, store: Store, bus: EventBus, settings: Any):
        self.store = store
        self.bus = bus
        self.settings = settings
        self.registry = HarnessRegistry(settings, store=store)
        self._tasks: dict[str, asyncio.Task] = {}
        self._adapters: dict[str, Any] = {}

    # ------------------------------------------------------------ start
    async def start(self, spec: RunSpec) -> dict[str, Any]:
        if spec.resume_run_id:
            parent = await self.store.run(spec.resume_run_id)
            if not parent:
                raise ValueError(f"run '{spec.resume_run_id}' not found")
            if not parent.get("session_id"):
                raise ValueError(f"run '{spec.resume_run_id}' has no harness session to resume")
            spec.resume_session_id = parent["session_id"]
            spec.harness = spec.harness or parent["harness"]
            spec.cwd = spec.cwd or parent.get("cwd")
            if spec.model == "cheap" and parent.get("model"):
                spec.model = parent["model"]
            spec.title = spec.title or f"↩ {parent.get('title') or ''}".strip()
        self.registry.resolve(spec)
        adapter = self.registry.adapter(spec.harness or "claude")
        if spec.resume_session_id and not adapter.supports_resume:
            raise ValueError(f"harness '{spec.harness}' cannot resume sessions")
        if spec.cwd and not os.path.isdir(spec.cwd):
            raise ValueError(f"cwd does not exist: {spec.cwd}")

        run_id = new_run_id()
        title = spec.title or " ".join(spec.prompt.split())[:40]
        await self.store.create_run(
            run_id,
            title=title,
            prompt=spec.prompt,
            harness=spec.harness,
            model=spec.resolved_model,
            cwd=spec.cwd,
            state="starting",
            dispatcher=spec.dispatcher,
            meta={
                "requested_model": spec.model,
                "provider": spec.provider,
                "endpoint": spec.endpoint,
                "yolo": spec.yolo,
                "search": spec.search,
                "max_turns": spec.max_turns,
                "resume_run_id": spec.resume_run_id,
                "resume_session_id": spec.resume_session_id,
            },
        )
        self._adapters[run_id] = adapter
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id, spec, adapter), name=f"run-{run_id}")
        await self.bus.publish({"type": "run.started", "run_id": run_id, "title": title, "harness": spec.harness, "model": spec.resolved_model, "cwd": spec.cwd})
        return {
            "run_id": run_id,
            "state": "starting",
            "title": title,
            "harness": spec.harness,
            "model": spec.resolved_model,
            "provider": spec.provider,
            "endpoint": spec.endpoint,
            "cwd": spec.cwd,
        }

    # ------------------------------------------------------------ drive
    async def _drive(self, run_id: str, spec: RunSpec, adapter: Any) -> None:
        turns = 0
        final_state = "error"
        result_text = None
        error_text = None
        cost = None
        session_id = spec.resume_session_id
        try:
            async for ev in adapter.start(spec):
                payload = _slim(ev.payload)
                event_id = await self.store.add_event(run_id, ev.type, payload, ts=ev.ts)
                summary = None
                if ev.type == "session_start":
                    session_id = payload.get("session_id") or session_id
                    await self.store.update_run(run_id, state="running", session_id=session_id, pid=os.getpid())
                elif ev.type == "tool_call":
                    turns += 1
                    summary = short_tool_summary(payload.get("name", "tool"), payload.get("input"))
                    await self.store.update_run(run_id, turns=turns)
                elif ev.type == "text":
                    summary = "💬 " + " ".join(str(payload.get("text", "")).split())[:80]
                elif ev.type == "result":
                    result_text = payload.get("text")
                    cost = payload.get("cost_usd")
                    session_id = payload.get("session_id") or session_id
                    final_state = "error" if payload.get("is_error") else "done"
                    if payload.get("is_error") and not error_text:
                        error_text = payload.get("stderr") or (payload.get("errors") and str(payload["errors"])) or "harness reported an error"
                    usage = payload.get("usage") or {}
                    await self.store.update_run(
                        run_id,
                        context_tokens=usage.get("prompt_tokens"),
                        cost_usd=cost,
                        session_id=session_id,
                        turns=payload.get("num_turns") or turns,
                    )
                elif ev.type == "error":
                    error_text = payload.get("message")
                    if error_text == "cancelled":
                        final_state = "cancelled"
                await self.bus.publish(
                    {"type": "run.event", "run_id": run_id, "event_id": event_id, "event": {"type": ev.type, "ts": ev.ts, "payload": payload}, "summary": summary}
                )
        except asyncio.CancelledError:
            final_state = "cancelled"
            error_text = "cancelled"
        except Exception as e:
            logger.exception("run %s crashed", run_id)
            error_text = f"{type(e).__name__}: {e}"
            final_state = "error"
        finally:
            if final_state == "error" and result_text and not error_text:
                final_state = "done"
            await self.store.update_run(
                run_id,
                state=final_state,
                ended_at=time.time(),
                result=result_text,
                error=error_text,
                session_id=session_id,
            )
            await self.bus.publish({"type": "run.finished", "run_id": run_id, "state": final_state, "cost_usd": cost, "error": error_text})
            self._tasks.pop(run_id, None)
            self._adapters.pop(run_id, None)

    # ------------------------------------------------------------ queries
    async def get(self, run_id: str, *, after_event: int = 0, include_events: bool = True, limit: int = 500) -> Optional[dict[str, Any]]:
        row = await self.store.run(run_id)
        if not row:
            return None
        row["live"] = run_id in self._tasks
        if row["state"] == "running" and not row["live"]:
            row["state"] = "dead"  # daemon restarted while it was running
        if include_events:
            row["events"] = await self.store.events(run_id, after_id=after_event, limit=limit)
        return row

    async def list(self, *, limit: int = 20, state: Optional[str] = None) -> list[dict[str, Any]]:
        rows = await self.store.runs(limit=limit, state=state)
        for r in rows:
            r["live"] = r["id"] in self._tasks
            if r["state"] in ("starting", "running") and not r["live"]:
                r["state"] = "dead"
        return rows

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        row["live"] = row["id"] in self._tasks
        if row["state"] in ("starting", "running") and not row["live"]:
            row["state"] = "dead"
        return row

    def resumable(self, row: dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Can this run be continued (追問)? Returns (ok, reason-if-not)."""
        if row.get("live") or row.get("state") in ("starting", "running"):
            return False, "還在執行中，結束後才能追問"
        if not row.get("session_id"):
            return False, "這筆 run 沒有留下 harness session（多半是啟動前就失敗）"
        try:
            adapter = self.registry.adapter(row.get("harness") or "claude")
        except ValueError:
            return False, f"harness「{row.get('harness')}」不支援續接"
        if not adapter.supports_resume:
            return False, f"harness「{row.get('harness')}」不支援續接"
        return True, None

    async def thread(self, run_id: str, *, max_depth: int = 50) -> Optional[dict[str, Any]]:
        """The resume chain this run belongs to, root first.

        Walk up through ``meta.resume_run_id`` to the root, then down again;
        where a run was resumed more than once the chain follows the branch
        that contains ``run_id`` (and below it, the most recent child).
        """
        row = await self.store.run(run_id)
        if not row:
            return None
        up: list[dict[str, Any]] = [row]
        seen = {row["id"]}
        cur = row
        for _ in range(max_depth):
            parent_id = (cur.get("meta") or {}).get("resume_run_id") if isinstance(cur.get("meta"), dict) else None
            if not parent_id or parent_id in seen:
                break
            parent = await self.store.run(parent_id)
            if not parent:
                break
            up.append(parent)
            seen.add(parent_id)
            cur = parent
        chain = list(reversed(up))
        cur = row
        for _ in range(max_depth):
            kids = [k for k in await self.store.run_children(cur["id"]) if k["id"] not in seen]
            if not kids:
                break
            cur = kids[-1]
            seen.add(cur["id"])
            chain.append(cur)
        chain = [self._decorate(r) for r in chain]
        leaf = chain[-1]
        ok, reason = self.resumable(leaf)
        return {"run_id": run_id, "root_id": chain[0]["id"], "leaf_id": leaf["id"], "runs": chain, "resumable": ok, "reason": reason}

    async def cancel(self, run_id: str) -> dict[str, Any]:
        adapter = self._adapters.get(run_id)
        task = self._tasks.get(run_id)
        if not task:
            row = await self.store.run(run_id)
            return {"run_id": run_id, "state": row["state"] if row else "not_found", "cancelled": False}
        if adapter:
            await adapter.cancel()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return {"run_id": run_id, "state": "cancelled", "cancelled": True}

    async def close(self) -> None:
        for run_id, task in list(self._tasks.items()):
            adapter = self._adapters.get(run_id)
            if adapter:
                await adapter.cancel()
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def live_count(self) -> int:
        return len(self._tasks)
