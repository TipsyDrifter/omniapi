"""Record every MCP tool call: a row in the store and two bus events
(``call.started`` / ``call.finished``). Installed by wrapping ``mcp.tool``
in server.py so the 14 tool handlers stay untouched.

The wrapper preserves the handler's signature (``functools.wraps`` sets
``__wrapped__``, which ``inspect.signature`` follows), so FastMCP still
derives the JSON schema from the original parameters.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CallScope:
    """Set by a caller that runs a tool handler on someone else's behalf (the
    GUI's generation jobs): names the door the call came through, and gets
    told which call row and which works it produced."""

    source: str
    call_id: Optional[str] = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    #: where the request came from beyond the door, e.g. ``{conversation_id,
    #: message_id}`` for a generation a chat proposed (1.2-M4): the ledger row
    #: gets the conversation, the works get all of it in their ``meta``
    links: dict[str, Any] = field(default_factory=dict)


call_scope: contextvars.ContextVar[Optional[CallScope]] = contextvars.ContextVar("omniapi_call_scope", default=None)

_MAX_ARG_CHARS = 2000
_MAX_RESULT_CHARS = 1500
_SECRET_KEYS = ("api_key", "token", "authorization", "password")


def _trim_value(v: Any, limit: int) -> Any:
    """Shrink base64 blobs / long strings so the DB stays small."""
    if isinstance(v, str):
        if len(v) > limit:
            return v[:limit] + f"…[+{len(v) - limit} chars]"
        return v
    if isinstance(v, dict):
        return {k: ("***" if k.lower() in _SECRET_KEYS else _trim_value(x, limit)) for k, x in v.items()}
    if isinstance(v, list):
        return [_trim_value(x, limit) for x in v[:50]]
    return v


def summarize_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        if k in ("image_data", "mask_data", "audio_data") and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
        elif k == "additional_images" and isinstance(v, list):
            out[k] = f"<{len(v)} images>"
        else:
            out[k] = _trim_value(v, _MAX_ARG_CHARS)
    return out


def summarize_result(result: Any) -> Any:
    if isinstance(result, dict):
        slim = {k: v for k, v in result.items() if k not in ("audio_base64", "image_base64", "data")}
        return _trim_value(slim, _MAX_RESULT_CHARS)
    return _trim_value(result, _MAX_RESULT_CHARS)


def extract_call_meta(result: Any) -> dict[str, Any]:
    """Pull model / provider / cost / usage / ticket-ness out of a tool result."""
    meta: dict[str, Any] = {}
    if not isinstance(result, dict):
        return meta
    meta["model"] = result.get("model") or (result.get("metadata") or {}).get("model")
    meta["provider"] = result.get("provider") or (result.get("metadata") or {}).get("provider")
    cost = result.get("cost_usd")
    if cost is None:
        ce = result.get("cost_estimate") or (result.get("metadata") or {}).get("cost_estimate")
        if isinstance(ce, dict):
            cost = ce.get("estimated_cost_usd")
        elif isinstance(ce, (int, float)):
            cost = ce
    meta["cost_usd"] = cost
    meta["usage"] = result.get("usage")
    meta["ticket"] = result.get("status") == "running" and bool(result.get("task_id"))
    meta["error"] = result.get("error") if isinstance(result.get("error"), str) else None
    return meta


def make_recorded(get_context: Callable[[], Any], source: str = "mcp"):
    """Return a decorator that records calls using the runtime context
    returned by ``get_context()`` (must expose ``.store`` and ``.bus``)."""

    def recorded(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        tool_name = fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            from .artifacts import CallInfo, current_call, index_result
            from .artifacts.fakes import FAKEABLE, fake_generate
            from .devmode import PAID_TOOLS, dev_enabled, offline, offline_message

            ctx = None
            try:
                ctx = get_context()
            except Exception:
                ctx = None
            fake = False
            if tool_name in PAID_TOOLS and offline():
                # an offline sandbox never reaches a vendor. A development sandbox answers the
                # generation tools with stand-ins (recorded and indexed like the real thing);
                # everything else is refused, and nothing is recorded because nothing happened
                fake = dev_enabled() and tool_name in FAKEABLE and ctx is not None
                if not fake:
                    return {"error": offline_message(f"the '{tool_name}' tool"), "status": "refused"}
            store = getattr(ctx, "store", None)
            bus = getattr(ctx, "bus", None)
            scope = call_scope.get()
            door = scope.source if scope else source
            t0 = time.perf_counter()
            call_id = None
            arg_summary = summarize_args(kwargs)
            if store is not None:
                try:
                    call_id = await store.call_started(tool_name, arg_summary, source=door,
                                                       conversation_id=(scope.links.get("conversation_id") if scope else None))
                except Exception as e:  # never let bookkeeping break a tool
                    logger.debug("call_started failed: %s", e)
            if scope is not None:
                scope.call_id = call_id
            if bus is not None:
                await bus.publish({"type": "call.started", "call_id": call_id, "tool": tool_name, "args": arg_summary})
            # who is calling: read by the works index, also from a job that outlives this call
            call_info = CallInfo(tool=tool_name, args=dict(kwargs), call_id=call_id, source=door, links=dict(scope.links) if scope else {})
            token = current_call.set(call_info)
            try:
                result = await (fake_generate(tool_name, kwargs, ctx) if fake else fn(*args, **kwargs))
            except (Exception, asyncio.CancelledError) as e:  # a cancelled call still closes its ledger row
                current_call.reset(token)
                dur = int((time.perf_counter() - t0) * 1000)
                if store is not None and call_id:
                    try:
                        await store.call_finished(call_id, status="error", duration_ms=dur, error=f"{type(e).__name__}: {e}"[:1000])
                    except Exception:
                        pass
                if bus is not None:
                    await bus.publish({"type": "call.finished", "call_id": call_id, "tool": tool_name, "status": "error", "duration_ms": dur, "error": str(e)[:300]})
                raise
            current_call.reset(token)
            dur = int((time.perf_counter() - t0) * 1000)
            indexed = await index_result(ctx, call_info, result)  # a ticket indexes nothing here; the job does when it lands
            if scope is not None:
                scope.artifacts.extend(indexed)
            meta = extract_call_meta(result)
            if tool_name == "get_job_result":
                # the cost belongs to the call that started the job (settled on its own row
                # when the job lands); counting it here too would bill the same work twice
                meta["cost_usd"] = None
            status = "ticket" if meta.get("ticket") else ("error" if meta.get("error") else "ok")
            if store is not None and call_id:
                try:
                    await store.call_finished(
                        call_id,
                        status=status,
                        duration_ms=dur,
                        model=meta.get("model"),
                        provider=meta.get("provider"),
                        cost_usd=meta.get("cost_usd"),
                        usage=meta.get("usage"),
                        result=summarize_result(result),
                        error=meta.get("error"),
                    )
                except Exception as e:
                    logger.debug("call_finished failed: %s", e)
            if bus is not None:
                await bus.publish(
                    {
                        "type": "call.finished",
                        "call_id": call_id,
                        "tool": tool_name,
                        "status": status,
                        "duration_ms": dur,
                        "model": meta.get("model"),
                        "provider": meta.get("provider"),
                        "cost_usd": meta.get("cost_usd"),
                    }
                )
            return result

        # FastMCP reads the signature to build the schema; make sure the
        # wrapper reports the original one even if something bypasses __wrapped__.
        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        return wrapper

    return recorded
