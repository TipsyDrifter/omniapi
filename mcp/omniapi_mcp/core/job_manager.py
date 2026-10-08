"""In-memory job manager for the call-now / fetch-later pattern.

MCP clients (notably Claude Desktop / Cowork) enforce a hard ~60s tool-call
timeout that server-side settings cannot change. Long generations (Suno music,
high-quality / 4K images) exceed it and get silently dropped. JobManager wraps
any coroutine so the tool call returns within a soft window (default 45s, safely
under 60s):

- If the work finishes inside the window, the real result is returned inline
  (fast path — UX unchanged for quick tools like text / low-quality images).
- Otherwise a ticket (task_id) is returned immediately and the work keeps
  running in the background on the event loop, to be fetched later via
  get_job_result.

The registry is in-memory, which is fine for a long-lived stdio MCP server: the
event loop persists across tool calls, so a background task started during one
call keeps running and its result is available to a later get() call.
"""

import asyncio
import contextvars
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Coroutine

logger = logging.getLogger(__name__)

#: Set by a caller that has no client-side timeout to beat (the GUI's
#: generation jobs): ``run`` then waits for the real result however long it
#: takes instead of handing back a ticket.
wait_to_finish: contextvars.ContextVar[bool] = contextvars.ContextVar("omniapi_wait_to_finish", default=False)


class JobManager:
    """Race a coroutine against a soft timeout; hand back a ticket if it loses."""

    def __init__(self, soft_timeout: float = 45.0, retain_seconds: float = 3600.0):
        self.soft_timeout = soft_timeout
        self.retain_seconds = retain_seconds
        self._jobs: dict[str, dict[str, Any]] = {}
        #: awaited with the result of a job that finished *after* its call had
        #: already returned a ticket (the caller never saw this result, so
        #: whoever needs it — the works index — has to be told here)
        self.on_late_result: Callable[[Any], Awaitable[Any]] | None = None

    def _sweep(self) -> None:
        """Drop finished jobs that were never fetched within retain_seconds."""
        now = time.monotonic()
        stale = [
            tid
            for tid, j in self._jobs.items()
            if j["status"] in ("completed", "failed")
            and now - j["finished_at"] > self.retain_seconds
        ]
        for tid in stale:
            self._jobs.pop(tid, None)

    async def _run_bg(self, task_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Run the wrapped coroutine, recording its result or error."""
        try:
            result = await coro
            job = self._jobs.get(task_id)
            if job is not None:
                job["result"] = result
                job["status"] = "completed"
                job["finished_at"] = time.monotonic()
                if job.get("ticketed") and self.on_late_result is not None:
                    try:
                        await self.on_late_result(result)
                    except Exception as hook_err:  # noqa: BLE001 — bookkeeping never fails a job
                        logger.warning("late-result hook failed for %s: %s", task_id, hook_err)
        except Exception as e:  # noqa: BLE001 — capture to surface on fetch
            job = self._jobs.get(task_id)
            if job is not None:
                job["error"] = e
                job["status"] = "failed"
                job["finished_at"] = time.monotonic()
            logger.error(
                "Background job %s (%s) failed: %s",
                task_id,
                job.get("label") if job else "?",
                e,
            )

    async def run(
        self,
        label: str,
        coro: Coroutine[Any, Any, Any],
        soft_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Return coro's result if it finishes within the soft window, else a ticket."""
        self._sweep()
        timeout: float | None = self.soft_timeout if soft_timeout is None else soft_timeout
        if wait_to_finish.get():
            timeout = None
        task_id = f"job_{uuid.uuid4().hex[:12]}"
        task = asyncio.ensure_future(self._run_bg(task_id, coro))
        self._jobs[task_id] = {
            "status": "running",
            "label": label,
            "task": task,
            "result": None,
            "error": None,
            "finished_at": 0.0,
        }
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            # the caller gave up (a GUI generation was cancelled): the work goes with it
            task.cancel()
            self._jobs.pop(task_id, None)
            raise
        if task in done:
            return self._deliver(task_id)
        self._jobs[task_id]["ticketed"] = True
        return {
            "status": "running",
            "task_id": task_id,
            "operation": label,
            "message": (
                f"'{label}' is still generating (long jobs like Suno music or "
                f"high-quality / 4K images can take 1-3 min; a video several minutes). "
                f"Call get_job_result with task_id='{task_id}' in a moment to fetch it."
            ),
        }

    def _deliver(self, task_id: str) -> dict[str, Any]:
        """Return a finished job's result (or raise its error) and drop it."""
        job = self._jobs.get(task_id)
        if job is None:
            return {
                "status": "not_found",
                "task_id": task_id,
                "message": "Unknown or expired task_id.",
            }
        if job["status"] == "failed":
            err = job["error"]
            self._jobs.pop(task_id, None)
            raise err  # surfaced to the tool handler's except -> MCP error
        if job["status"] == "completed":
            result = job["result"]
            self._jobs.pop(task_id, None)
            return result
        return {
            "status": "running",
            "task_id": task_id,
            "operation": job["label"],
            "message": "Still generating; check again shortly.",
        }

    async def get(self, task_id: str) -> dict[str, Any]:
        """Fetch a job's result by ticket (call-now / fetch-later)."""
        self._sweep()
        return self._deliver(task_id)

    async def close(self) -> None:
        """Cancel any still-running background jobs and wait for them to stop."""
        pending = [
            job["task"]
            for job in self._jobs.values()
            if job.get("task") is not None and not job["task"].done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            # Wait so cancellation actually completes (and any cleanup inside
            # the wrapped coroutines runs) instead of leaving destroyed-pending
            # task warnings behind.
            await asyncio.gather(*pending, return_exceptions=True)
        self._jobs.clear()
