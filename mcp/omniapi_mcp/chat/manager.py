"""ChatManager — multi-turn chat conversations, streamed over the event bus.

One manager per daemon; the GUI, the MCP ``chat`` tool and ``omni chat`` all
go through it and share the same ``conversations`` / ``messages`` tables.

A turn is asynchronous: ``send()`` stores the user message, starts a task
and returns; the task streams the reply, publishing

    chat.started   {conversation_id, turn_id, model, resolved_model, user_message}
    chat.delta     {conversation_id, turn_id, kind: text|reasoning, delta}   (ephemeral)
    chat.finished  {conversation_id, turn_id, state: done|cancelled|error, message, error}
    chat.updated   {conversation_id, conversation}                           (title / model / system / archive)

and stores the assistant message when it ends. Each reply records the model
that produced it, so switching models mid-conversation is just sending the
next message with another ``model``. Costs go to the tool-call ledger
(``calls`` table, tool ``chat``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..bus import EventBus
from ..store.db import Store

logger = logging.getLogger(__name__)

_TAIPEI = timezone(timedelta(hours=8))
#: request params a chat message may carry through to the provider
ALLOWED_PARAMS = ("temperature", "reasoning_effort", "max_completion_tokens")
_TITLE_CHARS = 40


class ChatError(ValueError):
    """A request the caller can fix (unknown conversation, busy, bad model)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # OpenAI-style content parts
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def _stamp(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, _TAIPEI).strftime("%m-%d %H:%M")


class ChatManager:
    def __init__(self, store: Store, bus: EventBus, text_tool: Any):
        self.store = store
        self.bus = bus
        self.text = text_tool
        self._tasks: dict[str, asyncio.Task] = {}
        self._live: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ queries
    async def _conv(self, cid: str) -> dict[str, Any]:
        conv = await self.store.conversation(cid)
        if not conv or conv.get("kind") != "chat":
            raise ChatError(f"conversation '{cid}' not found", 404)
        return conv

    def _live_view(self, cid: str) -> Optional[dict[str, Any]]:
        lv = self._live.get(cid)
        if not lv:
            return None
        return {"turn_id": lv["turn_id"], "model": lv["model"], "resolved_model": lv["resolved_model"], "started_at": lv["started_at"],
                "text": "".join(lv["text"]), "reasoning": "".join(lv["reasoning"])}

    @staticmethod
    def _totals(messages: list[dict[str, Any]]) -> dict[str, Any]:
        replies = [m for m in messages if m.get("role") == "assistant"]
        return {
            "n_messages": len(messages),
            "cost_usd": sum(m["cost_usd"] for m in replies if m.get("cost_usd") is not None),
            # priced / unpriced: a $0.0000 total means "free" only when some reply was actually priced
            "priced": sum(1 for m in replies if m.get("cost_usd") is not None),
            "unpriced": sum(1 for m in replies if m.get("cost_usd") is None and _text_of(m.get("content"))),
        }

    async def get(self, cid: str) -> dict[str, Any]:
        conv = await self._conv(cid)
        messages = await self.store.messages(cid)
        conv["messages"] = messages
        conv["live"] = self._live_view(cid)
        conv.update(self._totals(messages))
        return conv

    async def list(self, *, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        rows = await self.store.chats(limit=limit, include_archived=include_archived)
        for r in rows:
            r["live"] = r["id"] in self._tasks
        return rows

    # ------------------------------------------------------------ create / update
    async def create(self, *, model: str | None = None, system: str | None = None, title: str | None = None, source: str = "gui") -> dict[str, Any]:
        model = (model or "cheap").strip()
        try:
            self.text.route(model)  # fail now, not on the first message
        except RuntimeError as e:
            raise ChatError(str(e)) from e
        conv = await self.store.create_conversation(kind="chat", title=(title or "").strip() or None, model=model, system_prompt=(system or "").strip() or None, meta={"source": source})
        conv.update({"messages": [], "live": None, "n_messages": 0, "cost_usd": 0, "priced": 0, "unpriced": 0})
        await self.bus.publish({"type": "chat.updated", "conversation_id": conv["id"], "conversation": {k: v for k, v in conv.items() if k != "messages"}})
        return conv

    async def update(self, cid: str, *, title: Any = None, model: Any = None, system_prompt: Any = None, archived: Any = None) -> dict[str, Any]:
        await self._conv(cid)
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = str(title).strip() or None
        if model is not None:
            try:
                self.text.route(str(model))
            except RuntimeError as e:
                raise ChatError(str(e)) from e
            fields["model"] = str(model).strip()
        if system_prompt is not None:
            fields["system_prompt"] = str(system_prompt).strip() or None
        if archived is not None:
            if archived and cid in self._tasks:
                raise ChatError("這段聊天還在回覆中，先取消或等它結束再封存", 409)
            fields["status"] = "archived" if archived else "open"
        if fields:
            await self.store.update_conversation(cid, **fields)
        conv = await self.store.conversation(cid) or {}
        await self.bus.publish({"type": "chat.updated", "conversation_id": cid, "conversation": conv})
        return conv

    # ------------------------------------------------------------ send
    async def send(self, cid: str, text: str, *, model: str | None = None, params: dict[str, Any] | None = None, source: str = "gui") -> dict[str, Any]:
        conv = await self._conv(cid)
        text = (text or "").strip()
        if not text:
            raise ChatError("message is empty")
        if conv.get("status") == "archived":
            raise ChatError("這段聊天已封存，先取消封存才能繼續", 409)
        if cid in self._tasks:
            raise ChatError("上一則還在回覆中，結束或取消後才能送下一則", 409)
        requested = (model or conv.get("model") or "cheap").strip()
        try:
            resolved, _provider = self.text.route(requested)
        except RuntimeError as e:
            raise ChatError(str(e)) from e
        clean = {k: v for k, v in (params or {}).items() if k in ALLOWED_PARAMS and v is not None}

        user_msg = await self.store.add_message(cid, role="user", content=text)
        fields: dict[str, Any] = {"model": requested, "status": "running"}
        if not conv.get("title"):
            fields["title"] = " ".join(text.split())[:_TITLE_CHARS]
        await self.store.update_conversation(cid, **fields)

        turn_id = uuid.uuid4().hex[:12]
        self._live[cid] = {"turn_id": turn_id, "model": requested, "resolved_model": resolved, "started_at": time.time(), "text": [], "reasoning": []}
        await self.bus.publish({"type": "chat.started", "conversation_id": cid, "turn_id": turn_id, "model": requested, "resolved_model": resolved,
                                "user_message": user_msg, "title": fields.get("title") or conv.get("title")})
        self._tasks[cid] = asyncio.create_task(self._drive(cid, turn_id, requested, resolved, clean, source), name=f"chat-{cid}")
        return {"conversation_id": cid, "turn_id": turn_id, "state": "streaming", "model": requested, "resolved_model": resolved, "user_message": user_msg}

    async def send_and_wait(self, cid: str, text: str, **kw: Any) -> dict[str, Any]:
        """Send and wait for the reply (MCP tool / CLI without streaming)."""
        started = await self.send(cid, text, **kw)
        task = self._tasks.get(cid)
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        messages = await self.store.messages(cid)
        reply = next((m for m in reversed(messages) if m["role"] == "assistant" and m["seq"] > started["user_message"]["seq"]), None)
        return {**started, "state": ((reply or {}).get("meta") or {}).get("state", "error"), "message": reply}

    async def _history(self, cid: str) -> list[dict[str, Any]]:
        conv = await self.store.conversation(cid) or {}
        out: list[dict[str, Any]] = []
        if conv.get("system_prompt"):
            out.append({"role": "system", "content": conv["system_prompt"]})
        for m in await self.store.messages(cid):
            if m["role"] not in ("user", "assistant"):
                continue
            body = _text_of(m.get("content"))
            if not body.strip():
                continue  # a reply that failed before its first word
            out.append({"role": m["role"], "content": body})
        return out

    async def _drive(self, cid: str, turn_id: str, requested: str, resolved: str, params: dict[str, Any], source: str) -> None:
        live = self._live[cid]
        t0 = time.perf_counter()
        call_id: str | None = None
        state, error, done = "error", None, None
        try:
            history = await self._history(cid)
            try:
                call_id = await self.store.call_started("chat", {"model": requested, "messages": len(history), "chars": sum(len(m["content"]) for m in history)}, source=source, conversation_id=cid)
                await self.bus.publish({"type": "call.started", "call_id": call_id, "tool": "chat", "args": {"model": requested, "conversation_id": cid}})
            except Exception as e:  # bookkeeping never breaks a chat
                logger.debug("chat call_started failed: %s", e)
            async for piece in self.text.stream(history, model=requested, **params):
                kind = piece.get("type")
                if kind == "done":
                    done = piece
                    continue
                delta = piece.get("delta") or ""
                if not delta or kind not in ("text", "reasoning"):
                    continue
                live[kind].append(delta)
                await self.bus.publish({"type": "chat.delta", "conversation_id": cid, "turn_id": turn_id, "kind": kind, "delta": delta}, ephemeral=True)
            state = "done"
        except asyncio.CancelledError:
            state, error = "cancelled", None
        except Exception as e:
            logger.warning("chat %s turn failed: %s", cid, e)
            state, error = "error", str(e)[:1000]
        finally:
            await asyncio.shield(self._finish(cid, turn_id, requested, resolved, state, error, done, live, call_id, int((time.perf_counter() - t0) * 1000)))

    async def _finish(self, cid: str, turn_id: str, requested: str, resolved: str, state: str, error: str | None,
                      done: dict[str, Any] | None, live: dict[str, Any], call_id: str | None, duration_ms: int) -> None:
        text = (done or {}).get("text") or "".join(live["text"])
        reasoning = (done or {}).get("reasoning") or "".join(live["reasoning"]) or None
        model = (done or {}).get("model") or resolved
        usage = (done or {}).get("usage")
        cost = (done or {}).get("cost_usd")
        provider = (done or {}).get("provider")
        meta = {"state": state, "finish_reason": (done or {}).get("finish_reason"), "provider": provider, "requested_model": requested,
                "duration_ms": duration_ms, "turn_id": turn_id}
        if error:
            meta["error"] = error
        message = None
        try:
            message = await self.store.add_message(cid, role="assistant", content=text, model=model, usage=usage, cost_usd=cost, reasoning=reasoning, meta=meta)
            await self.store.update_conversation(cid, status="open")
            if call_id:
                await self.store.call_finished(call_id, status="ok" if state == "done" else "error", duration_ms=duration_ms, model=model, provider=provider,
                                               cost_usd=cost, usage=usage, result={"chars": len(text), "state": state}, error=error or (None if state == "done" else state))
        except Exception as e:
            logger.error("chat %s could not store the reply: %s", cid, e)
            error = error or f"reply could not be stored: {e}"
            state = "error"
        finally:
            self._tasks.pop(cid, None)
            self._live.pop(cid, None)
        await self.bus.publish({"type": "chat.finished", "conversation_id": cid, "turn_id": turn_id, "state": state, "message": message, "error": error})
        if call_id:
            await self.bus.publish({"type": "call.finished", "call_id": call_id, "tool": "chat", "status": "ok" if state == "done" else "error",
                                    "duration_ms": duration_ms, "model": model, "cost_usd": cost})

    # ------------------------------------------------------------ cancel / close
    async def cancel(self, cid: str) -> dict[str, Any]:
        await self._conv(cid)
        task = self._tasks.get(cid)
        if not task:
            return {"conversation_id": cid, "cancelled": False}
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return {"conversation_id": cid, "cancelled": True}

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def live_count(self) -> int:
        return len(self._tasks)

    # ------------------------------------------------------------ export
    async def export_markdown(self, cid: str) -> tuple[str, str]:
        """(filename, markdown) for the whole conversation."""
        conv = await self.get(cid)
        title = conv.get("title") or "未命名對話"
        lines = [f"# {title}", ""]
        now = datetime.now(_TAIPEI).strftime("%Y-%m-%d %H:%M")
        cost = conv.get("cost_usd") or 0
        unpriced = conv.get("unpriced") or 0
        lines += [
            f"- 匯出時間：{now}（Asia/Taipei）",
            f"- 對話 id：`{cid}`",
            f"- 訊息數：{conv.get('n_messages', 0)}",
            f"- 費用合計：${cost:.4f}" + (f"（另有 {unpriced} 則未計價）" if unpriced else ""),
            "",
        ]
        if conv.get("system_prompt"):
            lines += ["## System prompt", ""] + [f"> {ln}" if ln else ">" for ln in str(conv["system_prompt"]).splitlines()] + [""]
        lines += ["---", ""]
        for m in conv["messages"]:
            body = _text_of(m.get("content"))
            meta = m.get("meta") or {}
            if m["role"] == "user":
                lines += [f"## 你 · {_stamp(m.get('created_at'))}", "", body, ""]
                continue
            if m["role"] != "assistant":
                continue
            head = f"## {m.get('model') or '模型'} · {_stamp(m.get('created_at'))}"
            if m.get("cost_usd") is not None:
                head += f" · ${m['cost_usd']:.4f}"
            state = meta.get("state")
            if state == "cancelled":
                head += " · 已取消"
            elif state == "error":
                head += " · 錯誤"
            lines += [head, ""]
            if m.get("reasoning"):
                lines += ["<details><summary>思考</summary>", "", str(m["reasoning"]), "", "</details>", ""]
            if body:
                lines += [body, ""]
            if meta.get("error"):
                lines += [f"> 錯誤：{meta['error']}", ""]
        stamp = datetime.now(_TAIPEI).strftime("%Y%m%d-%H%M")
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in title).strip().replace(" ", "_")[:40] or "chat"
        return f"{safe}-{stamp}.md", "\n".join(lines).rstrip() + "\n"
