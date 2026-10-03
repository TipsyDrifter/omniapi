"""ChatManager — multi-turn chat conversations, streamed over the event bus.

One manager per daemon; the GUI, the MCP ``chat`` tool and ``omni chat`` all
go through it and share the same ``conversations`` / ``messages`` tables.

A turn is asynchronous: ``send()`` stores the user message, starts a task
and returns; the task streams the reply, publishing

    chat.started   {conversation_id, turn_id, action, parent_id, model, resolved_model, user_message, vision, images_skipped}
    chat.delta     {conversation_id, turn_id, kind: text|reasoning, delta}   (ephemeral)
    chat.finished  {conversation_id, turn_id, state: done|cancelled|error, message, error}
    chat.updated   {conversation_id, conversation}                           (title / model / system / archive / branch)
    chat.deleted   {conversation_id}
    chat.proposal  {conversation_id, message_id, tool_call_id, action: created|updated, proposal}

and stores the assistant message when it ends. Each reply records the model
that produced it, so switching models mid-conversation is just sending the
next message with another ``model``. Costs go to the tool-call ledger
(``calls`` table, tool ``chat``), one row per reply.

Branches (決策記錄 1.2-M1-b): every message points at the one it follows
(``parent_id``); the conversation remembers the tip of the branch it is on
(``meta.leaf_id``). Regenerating a reply and editing a question both add a
sibling under the same parent, then a new reply streams from there. The
history sent to the model, and the messages the GUI shows, are the path from
the root to the current leaf. Only one reply streams per conversation at a
time (決策記錄 1.2-M1-c); send, regenerate, edit and switching versions all
answer 409 while one does.

Images (1.2-M2): a reply gets the branch's images only when the routed
model's catalog ``vision`` flag says it takes them (決策記錄 1.2-M2-a). Files
are read and sized right before the request (``chat/images.py``); a missing
file or one over the request's image budget is left out with a line of text
in its place. A text-only model gets the text only and the turn still runs:
``chat.started`` / the turn carry ``vision`` and ``images_skipped``, and the
stored reply's ``meta`` records ``vision, images_sent, images_skipped,
images_missing`` whenever the branch had images.

Tools (1.2-M4): a GUI turn on a model whose catalog ``tools`` flag is set is
offered the registered chat tools (``chat/tools.py``; today only
``propose_generation``). A reply's tool calls are stored with it
(``tool_calls``) and each gets a record in ``meta.tools[tool_call_id]`` —
its state lives on the message, so a restart loses nothing. Once a record
leaves ``pending`` it is answered by a ``tool`` message that follows the
reply on the same branch (several calls: a chain), rewritten in place as the
state moves on; the history sent to a model therefore always has a result
for every call. Accepting a proposal starts a generation job (決策記錄
1.2-M4-b) and the record follows it to done / failed / cancelled; nothing
asks the model to answer again (決策記錄 1.2-M4-c). A pending proposal is
declined (``auto``) when the owner sends, regenerates or edits instead; a
declined one may still be reopened and accepted (D43).
``tool`` messages are not shown as messages and not counted: the API hangs
the records on their reply as ``proposals``.

Files (1.2-M5, D41): a message may carry any file (``kind: file``) or audio
(``kind: audio``) besides images. Right before the request each one is
extracted (``chat/files.py``) and ``chat/delivery.decide`` picks how it goes
— as it is (``native``), its text (``text``), its opening (``excerpt``) or
nothing but a plain "cannot read it" (``unreadable``); the reply's meta
records it per file (``files`` + ``files_native / _text / _excerpt /
_unreadable / _missing``). When the model can call tools and the branch has
files, the turn offers ``list_files`` / ``read_file`` / ``search_file`` and
runs a loop inside the turn: the model calls them, the results go back, the
model is called again — up to ``MAX_TOOL_ROUNDS`` rounds and
``MAX_TURN_READ_CHARS`` read. Every round's text streams into the one reply
(rounds joined by a blank line), every round's usage and cost add up on its
one ledger row, and ``meta.reads`` lists what was read; ``chat.tool`` tells
the page "reading …" as it happens.

    chat.tool  {conversation_id, turn_id, round, tool_call_id, tool, state: running|done, read}

Audio the routed model cannot hear (1.2-M5, the owner's call): a GUI turn
whose question carries such audio, not transcribed yet, does not call the
model at first. The reply is stored at once, empty, with one transcription
question per file (``meta.gate``, shown as ``proposals`` with ``kind:
transcript``) and ``meta.state: awaiting``; the turn and ``chat.finished``
say ``awaiting``. Accepting runs the transcription job; when every question
is answered (done or declined) the reply streams into that same message. The
owner moving on (send / regenerate / edit) settles the questions as
declined and the waiting reply as ``skipped``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..bus import EventBus
from ..capabilities.echo import echo_tools, echo_vision
from ..catalog import catalog
from ..store.db import Store
from . import files as F
from .delivery import (
    NATIVE_AUDIO_FORMATS,
    NATIVE_MAX_FILE_B64,
    BranchFile,
    Budget,
    Delivery,
    FileSet,
    NativeSupport,
    decide,
    describe_delivery,
    native_support,
    summarize,
)
from .images import (
    MAX_REQUEST_IMAGE_B64_BYTES,
    ImageUnreadable,
    PreparedImage,
    prepare_image,
)
from .tools import (
    ACCEPTABLE_STATES,
    LIMIT_NOTE,
    MAX_TOOL_ROUNDS,
    OPEN_STATES,
    ReadBudget,
    ToolArgsError,
    ToolEnv,
    ToolRegistry,
    TranscriptionGate,
    default_registry,
    parse_arguments,
)

logger = logging.getLogger(__name__)

_TAIPEI = timezone(timedelta(hours=8))
#: request params a chat message may carry through to the provider
ALLOWED_PARAMS = ("temperature", "reasoning_effort", "max_completion_tokens")
_TITLE_CHARS = 40
#: attachments (images, files, audio together) one message may carry
MAX_ATTACHMENTS = 10
#: what a work of each kind is when attached (1.2-M5)
_WORK_ATTACH_KIND = {"image": "image", "speech": "audio", "music": "audio", "transcript": "file", "lyrics": "file"}
#: what the model reads for a proposal made in a round that also read files (決策記錄 1.2-M5-g)
DEFERRED_NOTE = "（提議已記下：這次回覆結束後主人會看到提議卡，結果之後的工具訊息會告訴你；不要重複提議。）"


def att_kind(a: dict[str, Any]) -> str:
    """An attachment's kind; attachments stored before 1.2-M5 are images."""
    return a.get("kind") or "image"


class _Totals:
    """One turn's model calls added up (決策記錄 1.2-M5-f: every round is one
    more call and one more cost, all on the one reply and its one ledger row)."""

    def __init__(self) -> None:
        self.rounds = 0  # model calls that finished
        self.tool_rounds = 0  # rounds whose automatic tools ran
        self.texts: list[str] = []
        self.usage: Optional[dict[str, Any]] = None
        self.cost: Optional[float] = None
        self.last: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []  # calls kept for the reply (proposals)
        self.replay: Optional[dict[str, Any]] = None
        self.ignored: list[dict[str, Any]] = []

    def add(self, done: Optional[dict[str, Any]]) -> None:
        if done is None:
            return
        self.rounds += 1
        self.last = done
        if done.get("text"):
            self.texts.append(done["text"])
        u = done.get("usage")
        if isinstance(u, dict):
            acc = dict(self.usage or {})
            for k, v in u.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    acc[k] = (acc.get(k) or 0) + v
                elif k not in acc:
                    acc[k] = v
            self.usage = acc
        if done.get("cost_usd") is not None:
            self.cost = (self.cost or 0.0) + float(done["cost_usd"])

    def done(self, live: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not self.rounds:
            return None
        text = "\n\n".join(t for t in self.texts if t.strip()) if self.rounds > 1 else (self.last.get("text") or "")
        return {"text": text or "".join(live["text"]), "reasoning": self.last.get("reasoning") if self.rounds == 1 else None,
                "model": self.last.get("model"), "provider": self.last.get("provider"), "finish_reason": self.last.get("finish_reason"),
                "usage": self.usage, "cost_usd": self.cost, "tool_calls": self.calls or None, "replay": self.replay,
                "ignored": self.ignored, "model_calls": self.rounds}

    def report(self, live: dict[str, Any]) -> dict[str, Any]:
        """``meta.tool_rounds / model_calls / reads / read_chars`` when files were read."""
        reads = list(live.get("reads") or [])
        if not self.tool_rounds and not reads:
            return {}
        return {"tool_rounds": self.tool_rounds, "model_calls": self.rounds, "reads": reads,
                "read_chars": sum(int(r.get("chars") or 0) for r in reads)}


def _reading_label(tool: str, args: dict[str, Any], files: Any) -> str:
    """What a file tool call is about to read, before it runs (「正在讀 …」)."""
    if tool == "list_files":
        return "檔案清單"
    if tool == "search_file":
        return f"搜尋「{str(args.get('query') or '')[:40]}」"
    for key, unit in (("pages", "頁"), ("lines", "行")):
        if args.get(key) not in (None, ""):
            return f"第 {args[key]} {unit}"
    if args.get("sheet") not in (None, ""):
        return f"工作表「{args['sheet']}」" + (f"第 {args['rows']} 列" if args.get("rows") else "")
    if args.get("start") not in (None, "") or args.get("length") not in (None, ""):
        try:
            return f"從第 {int(args.get('start') or 0) + 1:,} 字起"
        except (TypeError, ValueError):
            return "一段文字"
    return "開頭"


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


# ---------------------------------------------------------------- provider messages
def accepts_images(provider: Any, model: str) -> bool:
    """Does this reply get the images of the conversation? The routed model's
    catalog ``vision`` flag decides (決策記錄 1.2-M2-a); the echo models carry
    their own. No vendor documents what it does with an image sent to a
    text-only model, so this gate is the only safeguard — not an API error."""
    key = getattr(provider, "PROVIDER_KEY", None)
    if key == "echo":
        return echo_vision(model)
    return bool(key) and catalog.vision(model, key)


def accepts_tools(provider: Any, model: str) -> bool:
    """Is this reply offered tools? The routed model's catalog ``tools`` flag
    (1.2-M4); the echo models carry their own; unknown models get none."""
    key = getattr(provider, "PROVIDER_KEY", None)
    if key == "echo":
        return echo_tools(model)
    return bool(key) and catalog.tools(model, key)


#: what the model reads for a call whose result never got written
NO_RESULT = "（這個工具呼叫沒有結果）"


def attachment_key(m: dict[str, Any], i: int) -> str:
    """One attachment of one message (the same upload may be in two messages)."""
    return f"{m.get('id')}:{i}"


#: why an attachment of a vision reply was not sent → the line the model reads instead
_LEFT_OUT = {
    "missing": "[{n} 張附圖的檔案已不在，沒有送出]",
    "unreadable": "[{n} 張附圖讀不出來，沒有送出]",
    "too_large": "[{n} 張較早的附圖超過單次可送的大小，沒有送出]",
}


def _flat_calls(calls: list[dict[str, Any]], results: dict[str, str]) -> str:
    """Tool calls and their results as plain lines, for a turn that is not
    offered tools (a vendor may refuse tool blocks in a request without
    tool definitions, and a model without tools cannot read them anyway)."""
    lines = []
    for c in calls:
        fn = c.get("function") or {}
        lines.append(f"[呼叫了工具 {fn.get('name')}：{fn.get('arguments') or '{}'} → 結果：{results.get(c.get('id') or '', NO_RESULT)}]")
    return "\n".join(lines)


def to_provider_message(m: dict[str, Any], *, images: bool,
                        prepared: Optional[dict[str, Any]] = None, tools: bool = True,
                        results: Optional[dict[str, str]] = None) -> Optional[dict[str, Any]]:
    """The one place a stored message becomes a message for the provider
    (``None``: leave it out). Text stays a plain string. A user message with
    attachments becomes OpenAI-style parts when the reply can take images —
    each image a base64 ``image_url`` data URL from ``prepared`` (attachment
    key → ``PreparedImage``, or a reason it was left out: missing /
    unreadable / too_large, which becomes a line of text so the model knows).
    Otherwise only its text goes (an image-only message says it had images,
    so the turn is not lost). Anthropic's image block is made from this in
    its own message conversion.

    Tool calls (1.2-M4) stay OpenAI-shaped — an assistant message with
    ``tool_calls`` (content ``None`` when it said nothing) and a ``tool``
    message with ``tool_call_id`` per result; each provider converts them
    (Anthropic: ``tool_use`` / ``tool_result`` blocks). Replay data a vendor
    gave with the calls (``meta.replay``, e.g. Anthropic's signed thinking)
    rides along under private keys that only that vendor's converter reads.
    When the turn is not offered tools (``tools=False``) calls and results
    (``results``: tool call id → text) are folded into the reply's text."""
    role = m.get("role")
    if role == "tool":
        if not tools:
            return None  # folded into its reply
        return {"role": "tool", "tool_call_id": (m.get("meta") or {}).get("tool_call_id") or "",
                "content": _text_of(m.get("content")) or NO_RESULT}
    if role not in ("user", "assistant"):
        return None
    body = _text_of(m.get("content"))
    calls = m.get("tool_calls") if role == "assistant" and isinstance(m.get("tool_calls"), list) else None
    if calls:
        if not tools:
            flat = _flat_calls(calls, results or {})
            return {"role": "assistant", "content": f"{body}\n\n{flat}" if body.strip() else flat}
        out: dict[str, Any] = {"role": "assistant", "content": body if body.strip() else None, "tool_calls": calls}
        replay = (m.get("meta") or {}).get("replay")
        if isinstance(replay, dict):
            out.update({k: v for k, v in replay.items() if isinstance(k, str) and k.startswith("_")})
        return out
    atts = (m.get("attachments") or []) if role == "user" else []
    if not atts:
        if not body.strip():
            return None  # a reply that failed before its first word (or one still waiting for the owner)
        return {"role": role, "content": body}
    prepared = prepared or {}
    n_images = sum(1 for a in atts if att_kind(a) == "image")
    parts: list[dict[str, Any]] = [{"type": "text", "text": body}] if body.strip() else []
    left_out: dict[str, int] = {}
    file_parts: list[dict[str, Any]] = []
    for i, a in enumerate(atts):
        got = prepared.get(attachment_key(m, i))
        if att_kind(a) != "image":  # 1.2-M5: a file / audio goes as its delivery decided
            if isinstance(got, Delivery):
                file_parts += got.parts
            else:
                file_parts.append({"type": "text", "text": "[附件：這個檔案這次沒有準備好，沒有內容可以給你]"})
            continue
        if not images:
            continue
        got = got if got is not None else "missing"
        if isinstance(got, PreparedImage):
            parts.append(got.part())
        else:
            left_out[got] = left_out.get(got, 0) + 1
    if images:
        parts += [{"type": "text", "text": _LEFT_OUT.get(why, _LEFT_OUT["missing"]).format(n=n)} for why, n in left_out.items()]
    elif n_images and not body.strip():
        parts.append({"type": "text", "text": f"[{n_images} 張圖片]"})
    parts += file_parts
    if all(p.get("type") == "text" for p in parts) and not (images and n_images):
        # plain text after all: one string (every vendor takes it; some text-only ones only that)
        return {"role": "user", "content": "\n\n".join(p["text"] for p in parts)}
    return {"role": "user", "content": parts}


_KIND_WORD = {"image": "圖片", "speech": "語音", "transcript": "逐字稿"}
_STATE_WORD = {"pending": "還沒決定", "generating": "生成中", "done": "做好了", "declined": "不用了", "failed": "失敗", "cancelled": "已中止"}


def _export_proposal(p: dict[str, Any]) -> list[str]:
    """A proposal and how it ended, as quoted lines of the exported chat."""
    if p.get("kind") == "transcript":
        head = f"> 回覆前先問：要不要把錄音 {(p.get('file') or {}).get('name') or ''} 轉成文字（{p.get('model') or '預設模型'}）"
    else:
        head = f"> 提議生成{_KIND_WORD.get(p.get('kind') or '', '作品')}（{p.get('model') or '預設模型'}）：{p.get('prompt') or ''}"
    state = p.get("state") or ""
    result = f"> 結果：{_STATE_WORD.get(state, state)}"
    if state == "declined" and p.get("auto"):
        result += "（沒有回應就繼續對話）"
    if p.get("artifact"):
        result += f"，作品 `{p['artifact'].get('id')}`（{p['artifact'].get('name')}）"
    if state == "failed" and p.get("error"):
        result += f"：{p.get('error_kind') or ''} {str(p['error'])[:200]}"
    return [head, ">", result, ""]


# ---------------------------------------------------------------- branches
def branch_path(rows: list[dict[str, Any]], leaf_id: Optional[str]) -> list[dict[str, Any]]:
    """Root → leaf, following ``parent_id`` back from the leaf."""
    by_id = {r["id"]: r for r in rows}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur = by_id.get(leaf_id) if leaf_id else None
    while cur is not None and cur["id"] not in seen:
        seen.add(cur["id"])
        out.append(cur)
        cur = by_id.get(cur.get("parent_id")) if cur.get("parent_id") else None
    out.reverse()
    return out


def _children(rows: list[dict[str, Any]]) -> dict[Optional[str], list[str]]:
    """parent id (``None`` = the opening messages) → child ids in ``seq`` order."""
    kids: dict[Optional[str], list[str]] = {}
    for r in sorted(rows, key=lambda r: r["seq"]):
        kids.setdefault(r.get("parent_id"), []).append(r["id"])
    return kids


def newest_leaf(rows: list[dict[str, Any]], mid: str) -> str:
    """The newest message in ``mid``'s subtree. A child is always written
    after its parent, so the newest one has no children: it is a leaf."""
    kids = _children(rows)
    seq = {r["id"]: r["seq"] for r in rows}
    best, stack = mid, [mid]
    while stack:
        cur = stack.pop()
        if seq[cur] > seq[best]:
            best = cur
        stack.extend(kids.get(cur, []))
    return best


class ChatManager:
    def __init__(self, store: Store, bus: EventBus, text_tool: Any):
        self.store = store
        self.bus = bus
        self.text = text_tool
        self._tasks: dict[str, asyncio.Task] = {}
        self._live: dict[str, dict[str, Any]] = {}
        #: conversations whose turn is being set up (claimed before the first await, so two requests cannot both pass)
        self._claims: set[str] = set()
        self._deleting: set[str] = set()
        #: the tools a chat model may call (1.2-M4) and what they may look at
        self.tools: ToolRegistry = default_registry()
        self.tool_env = ToolEnv()
        self.generations: Any = None  # GenerationManager, attached by the runtime
        #: one writer at a time for a conversation's tool records and result messages
        self._tool_locks: dict[str, asyncio.Lock] = {}

    async def attach(self, generations: Any, ctx: Any = None) -> None:
        """Hook up the generation jobs (accepting a proposal starts one) and the
        daemon context (the tool description's model menu), then settle what a
        previous process left generating."""
        self.generations = generations
        self.tool_env = ToolEnv(ctx=ctx)
        await self.reconcile()

    def _tool_lock(self, cid: str) -> asyncio.Lock:
        lock = self._tool_locks.get(cid)
        if lock is None:
            lock = self._tool_locks[cid] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------ queries
    async def _conv(self, cid: str) -> dict[str, Any]:
        conv = await self.store.conversation(cid)
        if not conv or conv.get("kind") != "chat":
            raise ChatError(f"conversation '{cid}' not found", 404)
        return conv

    async def _open_conv(self, cid: str) -> dict[str, Any]:
        conv = await self._conv(cid)
        if conv.get("status") == "archived":
            raise ChatError("這段聊天已封存，先取消封存才能繼續", 409)
        return conv

    def busy(self, cid: str) -> bool:
        return cid in self._tasks or cid in self._claims

    def _claim(self, cid: str) -> None:
        """Take the conversation's one reply slot (決策記錄 1.2-M1-c). No await
        between the check and the claim: a second request sees it at once."""
        if cid in self._deleting:
            raise ChatError("這段聊天正在刪除", 409)
        if self.busy(cid):
            raise ChatError("上一則還在回覆中，結束或取消後才能送下一則", 409)
        self._claims.add(cid)

    def _live_view(self, cid: str, kids: Optional[dict[Optional[str], list[str]]] = None,
                   roles: Optional[dict[str, str]] = None) -> Optional[dict[str, Any]]:
        """The reply being written. ``versions`` counts it among the replies
        already under its question (``ids`` ends with ``live-<turn_id>``), so a
        page reloaded mid-regenerate still shows ‹ n/n ›."""
        lv = self._live.get(cid)
        if not lv:
            return None
        siblings = [k for k in (kids or {}).get(lv["parent_id"], []) if (roles or {}).get(k) == "assistant"]
        live_id = f"live-{lv['turn_id']}"
        if lv.get("fill_id") in siblings:  # a reply that waited for the owner streams into its own message
            ids = [live_id if k == lv["fill_id"] else k for k in siblings]
        else:
            ids = siblings + [live_id]
        return {"turn_id": lv["turn_id"], "model": lv["model"], "resolved_model": lv["resolved_model"], "started_at": lv["started_at"],
                "parent_id": lv["parent_id"], "action": lv["action"], "text": "".join(lv["text"]), "reasoning": "".join(lv["reasoning"]),
                "versions": {"count": len(ids), "index": ids.index(live_id) + 1, "ids": ids},
                "fill_id": lv.get("fill_id"), "reads": list(lv.get("reads") or []), "reading": lv.get("reading")}

    @staticmethod
    def _totals(messages: list[dict[str, Any]]) -> dict[str, Any]:
        replies = [m for m in messages if m.get("role") == "assistant"]
        return {
            # yours and the replies; a tool call's result message is not a message to the owner
            "n_messages": sum(1 for m in messages if m.get("role") in ("user", "assistant")),
            "cost_usd": sum(m["cost_usd"] for m in replies if m.get("cost_usd") is not None),
            # priced / unpriced: a $0.0000 total means "free" only when some reply was actually priced
            "priced": sum(1 for m in replies if m.get("cost_usd") is not None),
            "unpriced": sum(1 for m in replies if m.get("cost_usd") is None and _text_of(m.get("content"))),
        }

    async def _attachment_views(self, atts: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
        """Stored attachments are ids only; the API adds what a page needs to
        show them (a name and urls — never a path)."""
        if not atts:
            return None
        out = []
        for a in atts:
            v = dict(a)
            kind = att_kind(a)
            v["kind"] = kind
            if a.get("upload_id"):
                row = await self.store.upload(a["upload_id"])
                meta = (row or {}).get("meta") or {}
                v.update(name=(row or {}).get("filename"), file_url=f"/api/uploads/{a['upload_id']}/file", thumb_url=None,
                         exists=bool(row) and Path(row["file_path"]).is_file())
                if kind != "image":  # 1.2-M5: what the file card shows
                    v.update(mime=(row or {}).get("mime"), bytes=(row or {}).get("bytes"), info=meta.get("info"),
                             download_url=f"/api/uploads/{a['upload_id']}/file?download=true")
                    if kind == "audio":
                        v["transcript"] = await self._transcript_view(meta.get("transcript_id"))
            else:
                aid = a.get("artifact_id") or ""
                row = await self.store.artifact(aid)
                v.update(name=(row or {}).get("title") or (Path(row["file_path"]).name if row else None),
                         file_url=f"/api/artifacts/{aid}/file", thumb_url=f"/api/artifacts/{aid}/thumb" if kind == "image" else None,
                         exists=bool(row) and Path(row["file_path"]).is_file())
                if kind != "image" and row:
                    v.update(mime=row.get("mime"), bytes=row.get("bytes"), download_url=f"/api/artifacts/{aid}/file?download=true")
                    if kind == "audio":
                        v["info"] = {"type": "audio", "type_name": F.TYPE_NAMES["audio"], "duration_s": row.get("duration_s")}
                        kids = [c for c in await self.store.artifact_children(aid) if c.get("kind") == "transcript"]
                        v["transcript"] = await self._transcript_view(kids[-1]["id"] if kids else None)
                    else:
                        v["info"] = F.public_info(F.from_text(row.get("kind") or "text", row.get("text")))
            out.append(v)
        return out

    async def _transcript_view(self, artifact_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not artifact_id:
            return None
        row = await self.store.artifact(artifact_id)
        if not row:
            return None
        return {"artifact_id": artifact_id, "chars": len(row.get("text") or ""), "file_url": f"/api/artifacts/{artifact_id}/file"}

    async def _view(self, m: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not m:
            return m
        m = dict(m)
        m["attachments"] = await self._attachment_views(m.get("attachments"))
        if m.get("role") == "assistant":
            meta = dict(m.get("meta") or {})
            meta.pop("replay", None)  # opaque vendor data (signed thinking): not for the page
            m["meta"] = meta
            calls = {c.get("id"): c for c in m.get("tool_calls") or [] if isinstance(c, dict)}
            m["proposals"] = [self._record_view(tcid, rec, calls.get(tcid)) for tcid, rec in self._records(m) if self._view_key(rec) == "proposals"]
        return m

    def _view_key(self, rec: dict[str, Any]) -> Optional[str]:
        tool = self.tools.get(rec.get("tool"))
        return tool.view_key if tool else None

    @staticmethod
    def _records(m: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """(tool call id, record) of a reply, in the order the calls were made;
        then the transcription questions asked before it (1.2-M5, ``meta.gate``)."""
        meta = m.get("meta") or {}
        records = meta.get("tools") or {}
        out = [(c["id"], records[c["id"]]) for c in m.get("tool_calls") or []
               if isinstance(c, dict) and isinstance(records.get(c.get("id")), dict)]
        out += [(k, r) for k, r in (meta.get("gate") or {}).items() if isinstance(r, dict)]
        return out

    @staticmethod
    def _proposed(rec: dict[str, Any], call: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """What the model proposed (``{prompt, model?, voice?}``). Records
        written before it was kept are read back from the call's arguments;
        a pending one has not been overwritten yet."""
        if isinstance(rec.get("proposed"), dict):
            return dict(rec["proposed"])
        if rec.get("gate"):
            return None
        if call is not None:
            try:
                args = parse_arguments((call.get("function") or {}).get("arguments"))
            except ToolArgsError:
                args = {}
            out = {k: str(args[k]).strip() for k in ("prompt", "model", "voice") if isinstance(args.get(k), str) and args[k].strip()}
            if out.get("prompt"):
                return out
        if rec.get("state") == "pending":
            return {k: rec[k] for k in ("prompt", "model", "voice") if rec.get(k)}
        return None

    @classmethod
    def _record_view(cls, tool_call_id: str, rec: dict[str, Any], call: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """A proposal as the API shows it: ``{id, kind, prompt, model, voice?,
        note?, state, generation_id?, artifact?, error?, error_kind?, auto?,
        proposed?}`` — ``prompt`` / ``model`` are what was last used,
        ``proposed: {prompt, model?, voice?}`` what the model proposed; a
        transcription question (1.2-M5) adds ``gate: true``, ``file: {id,
        name, kind, bytes, duration_s}`` and ``duration_s``."""
        v: dict[str, Any] = {"id": tool_call_id, "kind": rec.get("kind"), "prompt": rec.get("prompt"), "model": rec.get("model"),
                             "state": rec.get("state")}
        for k in ("voice", "note", "generation_id", "error", "error_kind", "auto", "attempts", "gate", "file", "duration_s"):
            if rec.get(k) not in (None, ""):
                v[k] = rec[k]
        proposed = cls._proposed(rec, call)
        if proposed:
            v["proposed"] = proposed
        art = rec.get("artifact")
        if isinstance(art, dict) and art.get("id"):
            v["artifact"] = {"id": art["id"], "kind": art.get("kind"), "name": art.get("name"),
                             "file_url": f"/api/artifacts/{art['id']}/file",
                             "thumb_url": f"/api/artifacts/{art['id']}/thumb" if art.get("kind") == "image" else None}
            if art.get("chars") is not None:
                v["artifact"]["chars"] = art["chars"]
        return v

    async def _current_path(self, cid: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
        """(all messages, the current branch root → leaf, the leaf id)."""
        rows = await self.store.messages(cid, limit=None)
        leaf = await self.store.leaf_id(cid)
        return rows, branch_path(rows, leaf), leaf

    async def get(self, cid: str) -> dict[str, Any]:
        """The conversation with the messages of its current branch. Each
        message says which version it is among its siblings
        (``versions: {count, index (1-based), ids}``) for a ‹ 2/3 › switch.
        Totals count every branch: what was spent stays spent."""
        conv = await self._conv(cid)
        rows, path, leaf = await self._current_path(cid)
        kids = _children(rows)
        roles = {r["id"]: r.get("role") for r in rows}
        messages = []
        for m in path:
            if m.get("role") == "tool":
                continue  # a tool call's result: shown through its reply's proposals
            ids = [k for k in kids.get(m.get("parent_id"), [m["id"]]) if roles.get(k) != "tool"]
            m = await self._view(m)
            m["versions"] = {"count": len(ids), "index": ids.index(m["id"]) + 1, "ids": ids}
            messages.append(m)
        conv["messages"] = messages
        conv["leaf_id"] = leaf
        conv["live"] = self._live_view(cid, kids, roles)
        conv.update(self._totals(rows))
        conv["generation_cost_usd"] = (await self.store.chat_generation_costs([cid])).get(cid)
        return conv

    async def list(self, *, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        """The chats with their totals; ``generation_cost_usd`` is what the
        generations started from each cost (``None``: none reported a cost)."""
        rows = await self.store.chats(limit=limit, include_archived=include_archived)
        gen = await self.store.chat_generation_costs([r["id"] for r in rows])
        for r in rows:
            r["live"] = r["id"] in self._tasks
            r["generation_cost_usd"] = gen.get(r["id"])
        return rows

    # ------------------------------------------------------------ create / update / delete
    async def create(self, *, model: str | None = None, system: str | None = None, title: str | None = None, source: str = "gui") -> dict[str, Any]:
        model = (model or "cheap").strip()
        try:
            self.text.route(model)  # fail now, not on the first message
        except RuntimeError as e:
            raise ChatError(str(e)) from e
        conv = await self.store.create_conversation(kind="chat", title=(title or "").strip() or None, model=model, system_prompt=(system or "").strip() or None, meta={"source": source})
        conv.update({"messages": [], "leaf_id": None, "live": None, "n_messages": 0, "cost_usd": 0, "priced": 0, "unpriced": 0,
                     "generation_cost_usd": None})
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
            if archived and self.busy(cid):
                raise ChatError("這段聊天還在回覆中，先取消或等它結束再封存", 409)
            fields["status"] = "archived" if archived else "open"
        if fields:
            await self.store.update_conversation(cid, **fields)
        conv = await self.store.conversation(cid) or {}
        await self.bus.publish({"type": "chat.updated", "conversation_id": cid, "conversation": conv})
        return conv

    async def delete(self, cid: str) -> dict[str, Any]:
        """Delete a chat for real (D37): its messages and the conversation.
        A reply still streaming is cancelled first. The ledger rows of its
        replies, the works made in it and the files uploaded to it all stay
        (決策記錄 1.2-M1-d). Agent runs are not chats and are refused (404)."""
        await self._conv(cid)
        self._deleting.add(cid)
        cancelled = False
        try:
            # Wait until nothing can still write to it: a turn being set up gets
            # its task first and is then cancelled like any other; a cancelled
            # turn is done once its partial reply is stored (the claim is
            # released last), so that reply is deleted too instead of landing
            # in a conversation that no longer exists.
            deadline = time.monotonic() + 10
            while self.busy(cid):
                if time.monotonic() > deadline:
                    raise ChatError("the reply in this conversation could not be stopped, try again", 409)
                task = self._tasks.get(cid)
                if task is not None and not cancelled:
                    task.cancel()
                    cancelled = True
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    continue
                await asyncio.sleep(0.01)
            # a proposal being written to waits for that write; a generation it
            # started keeps going (its work still reaches the wall, 1.2-M4)
            async with self._tool_lock(cid):
                counts = await self.store.delete_conversation(cid)
        finally:
            self._deleting.discard(cid)
            self._tool_locks.pop(cid, None)
        await self.bus.publish({"type": "chat.deleted", "conversation_id": cid})
        return {"conversation_id": cid, "deleted": True, "messages": counts["messages"], "cancelled": cancelled}

    # ------------------------------------------------------------ turns
    def _route(self, model: Optional[str], conv: dict[str, Any]) -> tuple[str, str, Any]:
        requested = (model or conv.get("model") or "cheap").strip()
        try:
            resolved, provider = self.text.route(requested)
        except RuntimeError as e:
            raise ChatError(str(e)) from e
        return requested, resolved, provider

    @staticmethod
    def _clean_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
        return {k: v for k, v in (params or {}).items() if k in ALLOWED_PARAMS and v is not None}

    async def _clean_attachments(self, raw: Any) -> list[dict[str, Any]]:
        """``[{upload_id} | {artifact_id}]`` with an optional ``kind`` (image |
        audio | file; when given it must match) → what is stored: ids and the
        kind only, each checked to exist (決策記錄 1.2-M1-a). An upload's kind
        is the one it was stored with; a work's follows its kind (image →
        image, speech / music → audio, lyrics / transcript → file). Anything
        else — a path, a url — is refused."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ChatError("attachments must be a list of {upload_id} or {artifact_id}")
        if len(raw) > MAX_ATTACHMENTS:
            raise ChatError(f"a message carries at most {MAX_ATTACHMENTS} attachments")
        out: list[dict[str, Any]] = []
        for ref in raw:
            if not isinstance(ref, dict) or set(ref) - {"kind", "upload_id", "artifact_id"}:
                raise ChatError("an attachment is {upload_id} or {artifact_id}")
            uid, aid = ref.get("upload_id"), ref.get("artifact_id")
            if bool(uid) == bool(aid):
                raise ChatError("an attachment is {upload_id} or {artifact_id}")
            if ref.get("kind") not in (None, "image", "audio", "file"):
                raise ChatError("an attachment's kind is image, audio or file")
            row = await (self.store.upload(str(uid)) if uid else self.store.artifact(str(aid)))
            if not row:
                raise ChatError(f"attachment {'upload' if uid else 'work'} '{uid or aid}' not found", 404)
            kind = row.get("kind") if uid else _WORK_ATTACH_KIND.get(row.get("kind") or "")
            if kind not in ("image", "audio", "file"):
                raise ChatError(f"'{uid or aid}' ({row.get('kind')}) cannot be attached")
            if ref.get("kind") not in (None, kind):
                raise ChatError(f"'{uid or aid}' is {kind}, not {ref.get('kind')}")
            out.append({"kind": kind, "upload_id": str(uid)} if uid else {"kind": kind, "artifact_id": str(aid)})
        return out

    async def _message_in(self, cid: str, mid: str) -> dict[str, Any]:
        m = await self.store.message(mid)
        if not m or m.get("conversation_id") != cid:
            raise ChatError(f"message '{mid}' not found in this conversation", 404)
        return m

    async def send(self, cid: str, text: str, *, model: str | None = None, params: dict[str, Any] | None = None,
                   source: str = "gui", attachments: Any = None) -> dict[str, Any]:
        """A new message at the tip of the current branch, then its reply."""
        conv = await self._open_conv(cid)
        text = (text or "").strip()
        self._claim(cid)
        try:
            atts = await self._clean_attachments(attachments)
            if not text and not atts:
                raise ChatError("message is empty")
            requested, resolved, provider = self._route(model, conv)
            await self._settle_open(cid)  # a proposal left waiting counts as declined (決策記錄 1.2-M4-c)
            user_msg = await self.store.add_message(cid, role="user", content=text, attachments=atts or None)
            return await self._launch(cid, conv, user_msg, requested, resolved, provider, self._clean_params(params), source, action="send")
        except BaseException:
            self._claims.discard(cid)
            raise

    async def regenerate(self, cid: str, message_id: str, *, model: str | None = None, params: dict[str, Any] | None = None,
                         source: str = "gui") -> dict[str, Any]:
        """Answer the same question again: a new reply next to ``message_id``
        (an assistant message) under the same question. Given a user message,
        it answers that one again. ``model`` may differ from the first reply's."""
        conv = await self._open_conv(cid)
        self._claim(cid)
        try:
            m = await self._message_in(cid, message_id)
            if m["role"] == "user":
                question = m
            elif m["role"] == "assistant":
                question = await self.store.message(m["parent_id"]) if m.get("parent_id") else None
                if not question or question.get("role") != "user":
                    raise ChatError("this reply does not follow a message of yours, there is nothing to answer again")
            else:
                raise ChatError("only a reply (or one of your messages) can be answered again")
            requested, resolved, provider = self._route(model, conv)
            await self._settle_open(cid)
            return await self._launch(cid, conv, question, requested, resolved, provider, self._clean_params(params), source,
                                      action="regenerate", extra={"regenerate_of": m["id"] if m["role"] == "assistant" else None})
        except BaseException:
            self._claims.discard(cid)
            raise

    async def edit(self, cid: str, message_id: str, text: str, *, attachments: Any = None, model: str | None = None,
                   params: dict[str, Any] | None = None, source: str = "gui") -> dict[str, Any]:
        """Send an edited version of one of your messages: a new message next to
        it (same parent), then its reply. The old branch stays. Without
        ``attachments`` the original's images go along; ``[]`` drops them."""
        conv = await self._open_conv(cid)
        text = (text or "").strip()
        self._claim(cid)
        try:
            m = await self._message_in(cid, message_id)
            if m["role"] != "user":
                raise ChatError("only your own messages can be edited")
            atts = list(m.get("attachments") or []) if attachments is None else await self._clean_attachments(attachments)
            if not text and not atts:
                raise ChatError("message is empty")
            requested, resolved, provider = self._route(model, conv)
            await self._settle_open(cid)
            user_msg = await self.store.add_message(cid, role="user", content=text, attachments=atts or None, parent_id=m.get("parent_id"))
            return await self._launch(cid, conv, user_msg, requested, resolved, provider, self._clean_params(params), source,
                                      action="edit", extra={"edit_of": m["id"]})
        except BaseException:
            self._claims.discard(cid)
            raise

    async def switch(self, cid: str, message_id: str) -> dict[str, Any]:
        """Show another version: the current leaf becomes the newest message
        in ``message_id``'s subtree. Returns the conversation as ``get()``."""
        await self._open_conv(cid)
        self._claim(cid)
        try:
            await self._message_in(cid, message_id)
            rows = await self.store.messages(cid, limit=None)
            await self.store.set_leaf(cid, newest_leaf(rows, message_id))
        finally:
            self._claims.discard(cid)
        conv = await self.get(cid)
        await self.bus.publish({"type": "chat.updated", "conversation_id": cid,
                                "conversation": {k: v for k, v in conv.items() if k not in ("messages", "live")}})
        return conv

    async def _launch(self, cid: str, conv: dict[str, Any], question: dict[str, Any], requested: str, resolved: str, provider: Any,
                      params: dict[str, Any], source: str, *, action: str, extra: dict[str, Any] | None = None,
                      fill: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Start the reply to ``question`` (already stored, holding the claim).
        ``fill`` is a reply that waited for the owner (1.2-M5): the new reply
        streams into it instead of becoming a new message. A GUI turn whose
        question carries audio the model cannot hear, not transcribed yet,
        stops before the model and asks the owner first (see ``_ask_first``)."""
        fields: dict[str, Any] = {"model": requested, "status": "running"}
        if not conv.get("title"):
            atts = question.get("attachments") or []
            label = "（圖片）" if atts and all(att_kind(a) == "image" for a in atts) else "（檔案）" if atts else ""
            first = " ".join(_text_of(question.get("content")).split()) or label
            if first:
                fields["title"] = first[:_TITLE_CHARS]
        view = await self._view(question)
        files = await self._branch_files(cid, question["id"])
        support = native_support(provider, resolved)
        if fill is None and source == "gui":
            unheard = [f for f in files if f.message_id == question["id"] and self._unheard(f, support)]
            if unheard and isinstance(self.tools.get(TranscriptionGate.name), TranscriptionGate):
                return await self._ask_first(cid, conv, question, view, unheard, requested, resolved, params, source, action, extra, fields)
        await self.store.update_conversation(cid, **fields)
        await self.store.set_leaf(cid, question["id"])  # while the new reply streams, the branch ends at the question

        turn_id = ((fill or {}).get("meta") or {}).get("turn_id") or uuid.uuid4().hex[:12]
        self._live[cid] = {"turn_id": turn_id, "model": requested, "resolved_model": resolved, "started_at": time.time(),
                           "parent_id": question["id"], "action": action, "text": [], "reasoning": [],
                           "fill_id": (fill or {}).get("id"), "reads": [], "reading": None}
        # a model that does not take images still answers; the images of the
        # branch are left out and the turn says how many (the GUI greys the
        # attach button out first — this is the backstop, not a 400)
        vision = accepts_images(provider, resolved)
        images = {"vision": vision, "images_skipped": 0 if vision else await self._images_on_branch(cid, question["id"])}
        specs = await self._tool_specs(provider, resolved, source, files)
        if fill is not None:
            extra = {**(extra or {}), "fill_id": fill["id"]}
        await self.bus.publish({"type": "chat.started", "conversation_id": cid, "turn_id": turn_id, "action": action, "parent_id": question["id"],
                                "model": requested, "resolved_model": resolved, "user_message": view,
                                "title": fields.get("title") or conv.get("title"), "tools": bool(specs), **images, **(extra or {})})
        self._tasks[cid] = asyncio.create_task(
            self._drive(cid, turn_id, requested, resolved, params, source, question["id"], vision, specs,
                        files=files, support=support, fill=fill), name=f"chat-{cid}")
        return {"conversation_id": cid, "turn_id": turn_id, "state": "streaming", "action": action, "parent_id": question["id"],
                "model": requested, "resolved_model": resolved, "user_message": view, "tools": bool(specs), **images, **(extra or {})}

    @staticmethod
    def _unheard(f: BranchFile, support: NativeSupport) -> bool:
        """An audio file this model cannot take as it is and nobody has transcribed."""
        if f.kind != "audio" or f.transcript_text is not None or f.missing:
            return False
        if not support.audio:
            return True
        fmt = F.audio_format(f.path) if f.path is not None else None
        return fmt not in NATIVE_AUDIO_FORMATS or ((f.bytes or 0) + 2) // 3 * 4 > NATIVE_MAX_FILE_B64

    async def _ask_first(self, cid: str, conv: dict[str, Any], question: dict[str, Any], view: Any, unheard: list[BranchFile],
                         requested: str, resolved: str, params: dict[str, Any], source: str, action: str,
                         extra: Optional[dict[str, Any]], fields: dict[str, Any]) -> dict[str, Any]:
        """Store the reply-to-be, empty, with one transcription question per
        audio file (決策記錄 1.2-M5-a). Nothing streams: the conversation is not
        busy while the owner decides; the questions and the waiting reply live
        on the message, so a restart loses nothing."""
        gate = self.tools.get(TranscriptionGate.name)
        turn_id = uuid.uuid4().hex[:12]
        env = ToolEnv(ctx=self.tool_env.ctx, source=source)
        records: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for f in unheard:
            if f.id in seen:
                continue
            seen.add(f.id)
            records[f"tr_{uuid.uuid4().hex[:12]}"] = await gate.record(f, env)
        meta = {"state": "awaiting", "turn_id": turn_id, "requested_model": requested, "resolved_model": resolved, "params": params,
                "source": source, "action": action, "gate": records, **({"extra": extra} if extra else {})}
        fields = {**fields, "status": "open"}
        await self.store.update_conversation(cid, **fields)
        reply = await self.store.add_message(cid, role="assistant", content="", model=resolved, meta=meta, parent_id=question["id"])
        self._claims.discard(cid)
        rview = await self._view(reply)
        await self.bus.publish({"type": "chat.started", "conversation_id": cid, "turn_id": turn_id, "action": action, "parent_id": question["id"],
                                "model": requested, "resolved_model": resolved, "user_message": view, "awaiting": True,
                                "title": fields.get("title") or conv.get("title"), "tools": False, **(extra or {})})
        await self.bus.publish({"type": "chat.finished", "conversation_id": cid, "turn_id": turn_id, "state": "awaiting", "message": rview, "error": None})
        for p in rview.get("proposals") or []:
            await self.bus.publish({"type": "chat.proposal", "conversation_id": cid, "message_id": reply["id"], "tool_call_id": p["id"],
                                    "action": "created", "proposal": p})
        return {"conversation_id": cid, "turn_id": turn_id, "state": "awaiting", "action": action, "parent_id": question["id"],
                "model": requested, "resolved_model": resolved, "user_message": view, "tools": False, "message": rview, **(extra or {})}

    async def _tool_specs(self, provider: Any, resolved: str, source: str, files: Optional[FileSet] = None) -> list[dict[str, Any]]:
        """The tools this turn offers, on a model that can call tools: the
        proposal tool on a GUI turn only (it needs someone to press its
        button; the MCP tool and the CLI have none), the file tools whenever
        the branch has files — each tool decides from the turn's ``ToolEnv``."""
        if not accepts_tools(provider, resolved):
            return []
        env = ToolEnv(ctx=self.tool_env.ctx, source=source, files=files if files else None)
        return await self.tools.specs(env)

    async def _images_on_branch(self, cid: str, leaf: str) -> int:
        rows = await self.store.messages(cid, limit=None)
        return sum(sum(1 for a in m.get("attachments") or [] if att_kind(a) == "image")
                   for m in branch_path(rows, leaf) if m.get("role") == "user")

    async def _branch_files(self, cid: str, leaf: str) -> FileSet:
        """The non-image attachments on the branch ending at ``leaf``, resolved
        to files (or a work's text) but not extracted yet."""
        rows = await self.store.messages(cid, limit=None)
        items: list[BranchFile] = []
        for m in branch_path(rows, leaf):
            if m.get("role") != "user":
                continue
            for i, a in enumerate(m.get("attachments") or []):
                kind = att_kind(a)
                if kind == "image":
                    continue
                items.append(await self._branch_file(m, i, a, kind))
        return FileSet(items)

    async def _branch_file(self, m: dict[str, Any], i: int, a: dict[str, Any], kind: str) -> BranchFile:
        key = attachment_key(m, i)
        if a.get("upload_id"):
            uid = a["upload_id"]
            row = await self.store.upload(uid) or {}
            meta = row.get("meta") or {}
            bf = BranchFile(key=key, id=uid, ref={"upload_id": uid}, kind=kind, name=row.get("filename") or uid, message_id=m["id"],
                            path=Path(row["file_path"]) if row.get("file_path") else None, mime=row.get("mime"), bytes=row.get("bytes"),
                            duration_s=(meta.get("info") or {}).get("duration_s"))
            if kind == "audio" and meta.get("transcript_id"):
                t = await self.store.artifact(meta["transcript_id"])
                if t is not None:
                    bf.transcript_id, bf.transcript_text = t["id"], t.get("text") or ""
            return bf
        aid = a.get("artifact_id") or ""
        row = await self.store.artifact(aid) or {}
        name = row.get("title") or (Path(row["file_path"]).name if row.get("file_path") else aid)
        bf = BranchFile(key=key, id=aid, ref={"artifact_id": aid}, kind=kind, name=name, message_id=m["id"],
                        path=Path(row["file_path"]) if row.get("file_path") else None, mime=row.get("mime"), bytes=row.get("bytes"),
                        duration_s=row.get("duration_s"), work_kind=row.get("kind"))
        if kind == "file" and row:
            bf.text_work = row.get("text")  # None: no stored body, the file itself is extracted
        if kind == "audio" and row:
            kids = [c for c in await self.store.artifact_children(aid) if c.get("kind") == "transcript"]
            if kids:
                bf.transcript_id, bf.transcript_text = kids[-1]["id"], kids[-1].get("text") or ""
        return bf

    async def wait(self, started: dict[str, Any]) -> dict[str, Any]:
        """Wait for the reply a turn started (MCP tool / CLI / ``?wait=true``)."""
        cid = started["conversation_id"]
        task = self._tasks.get(cid)
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
        rows = await self.store.messages(cid, limit=None)
        reply = next((m for m in reversed(rows) if m["role"] == "assistant" and (m.get("meta") or {}).get("turn_id") == started["turn_id"]), None)
        reply = await self._view(reply)
        meta = (reply or {}).get("meta") or {}
        out = {**started, "state": meta.get("state", "error"), "message": reply}
        if "images_skipped" in meta:  # the final count (adds images left out for the size budget)
            out["images_skipped"] = meta["images_skipped"]
        return out

    async def send_and_wait(self, cid: str, text: str, **kw: Any) -> dict[str, Any]:
        """Send and wait for the reply (MCP tool / CLI without streaming)."""
        return await self.wait(await self.send(cid, text, **kw))

    async def _attachment_file(self, att: dict[str, Any]) -> Optional[Path]:
        row = await (self.store.upload(att["upload_id"]) if att.get("upload_id") else self.store.artifact(att.get("artifact_id") or ""))
        path = Path(row["file_path"]) if row and row.get("file_path") else None
        return path if path is not None and path.is_file() else None

    async def _prepare_images(self, path: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, int]]:
        """Every image on the branch → its payload or why it is left out.
        Newest first, so when the request's image budget runs out it is the
        older images that stay behind (``MAX_REQUEST_IMAGE_B64_BYTES``)."""
        prepared: dict[str, Any] = {}
        counts = {"images_sent": 0, "images_skipped": 0, "images_missing": 0}
        budget = MAX_REQUEST_IMAGE_B64_BYTES
        for m in reversed(path):
            if m.get("role") != "user":
                continue
            atts = m.get("attachments") or []
            for i in reversed(range(len(atts))):
                if att_kind(atts[i]) != "image":
                    continue
                key = attachment_key(m, i)
                file = await self._attachment_file(atts[i])
                if file is None:
                    prepared[key] = "missing"
                    counts["images_missing"] += 1
                    continue
                try:
                    img = await asyncio.to_thread(prepare_image, file)
                except (OSError, ImageUnreadable) as e:
                    logger.warning("chat attachment %s could not be read: %s", atts[i], e)
                    prepared[key] = "unreadable"
                    counts["images_missing"] += 1
                    continue
                if img.size > budget:
                    prepared[key] = "too_large"
                    counts["images_skipped"] += 1
                    continue
                budget -= img.size
                prepared[key] = img
                counts["images_sent"] += 1
        return prepared, counts

    async def _prepare_files(self, files: FileSet, support: NativeSupport, prepared: dict[str, Any], *, file_tools: bool) -> dict[str, Any]:
        """Every file on the branch → its ``Delivery`` (into ``prepared``),
        newest first so the older files are the ones that drop to excerpts;
        returns the reply's file report (``delivery.summarize``). The native
        budget is what the request's images left (決策記錄 1.2-M5-e)."""
        used = sum(v.size for v in prepared.values() if isinstance(v, PreparedImage))
        budget = Budget(native_b64=max(0, support.budget - used))
        for f in files:
            await f.extract()
        for f in reversed(files.items):
            d = decide(f, support, budget, tools=file_tools)
            prepared[f.key] = d
            f.sent = d.meta
        report = summarize([prepared[f.key].meta for f in files.items])
        report["files_tools"] = file_tools
        return report

    async def _history(self, cid: str, *, leaf: Optional[str] = None, images: bool = False, tools: bool = False,
                       files: Optional[FileSet] = None, support: Optional[NativeSupport] = None,
                       file_tools: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """(system prompt + the branch from the root to ``leaf`` (default: the
        current leaf), what happened to the branch's images and files). The
        image report is empty when the branch has none; otherwise ``{vision,
        images_sent, images_skipped, images_missing}`` — skipped counts images
        a text-only model was not sent and images left out for the size
        budget. The file report (1.2-M5) is there when the branch has files:
        ``files`` (one entry per file, how it went) and the counts.

        Every tool call gets a result right after its reply: the stored
        ``tool`` messages, and for a call that has none (it cannot happen
        through the API — pending calls are settled before a new turn — but
        an old or broken record must not make the request invalid) a stand-in.
        ``tools=False`` (the turn offers none) folds calls and results into
        the reply's text instead."""
        conv = await self.store.conversation(cid) or {}
        out: list[dict[str, Any]] = []
        if conv.get("system_prompt"):
            out.append({"role": "system", "content": conv["system_prompt"]})
        rows = await self.store.messages(cid, limit=None)
        path = branch_path(rows, leaf or await self.store.leaf_id(cid))
        total = sum(1 for m in path if m.get("role") == "user" for a in m.get("attachments") or [] if att_kind(a) == "image")
        report: dict[str, Any] = {}
        prepared: dict[str, Any] = {}
        if total:
            if images:
                prepared, counts = await self._prepare_images(path)
            else:
                counts = {"images_sent": 0, "images_skipped": total, "images_missing": 0}
            report = {"vision": images, **counts}
        if files is None:
            files = await self._branch_files(cid, path[-1]["id"]) if path else FileSet()
        if files:
            report.update(await self._prepare_files(files, support or NativeSupport(), prepared, file_tools=file_tools))
        results = {(m.get("meta") or {}).get("tool_call_id"): _text_of(m.get("content")) for m in path if m.get("role") == "tool"}
        owed: list[str] = []  # calls of the last reply still without a result

        def settle_owed() -> None:
            out.extend({"role": "tool", "tool_call_id": tcid, "content": NO_RESULT} for tcid in owed)
            owed.clear()

        for m in path:
            if m.get("role") == "tool":
                tcid = (m.get("meta") or {}).get("tool_call_id")
                if not tools or tcid not in owed:
                    continue  # folded into its reply, or a result whose call is not in the history
                owed.remove(tcid)
            elif tools:
                settle_owed()
            pm = to_provider_message(m, images=images, prepared=prepared, tools=tools, results=results)
            if pm is None:
                continue
            out.append(pm)
            if tools and pm.get("tool_calls"):
                owed = [c.get("id") for c in pm["tool_calls"]]
        if tools:
            settle_owed()
        return out, report

    async def _drive(self, cid: str, turn_id: str, requested: str, resolved: str, params: dict[str, Any], source: str,
                     parent_id: str, images: bool, specs: Optional[list[dict[str, Any]]] = None, *,
                     files: Optional[FileSet] = None, support: Optional[NativeSupport] = None,
                     fill: Optional[dict[str, Any]] = None) -> None:
        """Stream the reply. With the file tools offered this is a loop
        (決策記錄 1.2-M5-f): a round whose calls include automatic tools has
        them run and the model called again with the results; the rounds'
        text streams as one reply, their usage and cost add up."""
        live = self._live[cid]
        t0 = time.perf_counter()
        call_id: str | None = None
        state, error = "error", None
        report: dict[str, Any] = {}
        total = _Totals()
        if specs:
            params = {**params, "tools": specs}
        auto = {s["function"]["name"] for s in specs or [] if getattr(self.tools.get((s.get("function") or {}).get("name")), "auto", False)}
        try:
            history, report = await self._history(cid, leaf=parent_id, images=images, tools=bool(specs), files=files, support=support,
                                                  file_tools=bool(auto))
            try:
                call_id = await self.store.call_started("chat", {"model": requested, "messages": len(history),
                                                                 "chars": sum(len(_text_of(m.get("content"))) for m in history)},
                                                        source=source, conversation_id=cid)
                await self.bus.publish({"type": "call.started", "call_id": call_id, "tool": "chat", "args": {"model": requested, "conversation_id": cid}})
            except Exception as e:  # bookkeeping never breaks a chat
                logger.debug("chat call_started failed: %s", e)
            convo = list(history)
            budget = ReadBudget()
            final = False
            while True:
                round_text: list[str] = []
                done = None
                async for piece in self.text.stream(convo, model=requested, **params):
                    kind = piece.get("type")
                    if kind == "done":
                        done = piece
                        continue
                    delta = piece.get("delta") or ""
                    if not delta or kind not in ("text", "reasoning"):
                        continue
                    if kind == "text" and not round_text and total.rounds and "".join(live["text"]).strip():
                        live["text"].append("\n\n")  # the rounds of one reply read as one text
                        await self.bus.publish({"type": "chat.delta", "conversation_id": cid, "turn_id": turn_id, "kind": "text", "delta": "\n\n"},
                                               ephemeral=True)
                    if kind == "text":
                        round_text.append(delta)
                    live[kind].append(delta)
                    await self.bus.publish({"type": "chat.delta", "conversation_id": cid, "turn_id": turn_id, "kind": kind, "delta": delta}, ephemeral=True)
                total.add(done)
                calls = [c for c in (done or {}).get("tool_calls") or [] if isinstance(c, dict)]
                reads = [c for c in calls if (c.get("function") or {}).get("name") in auto]
                others = [c for c in calls if c not in reads]
                total.calls += others
                if others and isinstance((done or {}).get("replay"), dict) and total.replay is None:
                    total.replay = done["replay"]
                if not reads:
                    break
                if final:  # past the limit: these reads are not run, the reply ends here
                    total.ignored += [{"name": (c.get("function") or {}).get("name"), "reason": "over the read limit of this turn"} for c in reads]
                    break
                total.tool_rounds += 1
                for c in calls:
                    c["id"] = str(c.get("id") or "") or f"call_{uuid.uuid4().hex[:16]}"
                msg: dict[str, Any] = {"role": "assistant", "content": "".join(round_text) or None, "tool_calls": calls}
                if isinstance((done or {}).get("replay"), dict):
                    msg.update({k: v for k, v in done["replay"].items() if isinstance(k, str) and k.startswith("_")})
                convo.append(msg)
                for c in calls:
                    text = await self._run_auto(cid, turn_id, total.tool_rounds, c, files or FileSet(), budget, live) if c in reads else DEFERRED_NOTE
                    convo.append({"role": "tool", "tool_call_id": c["id"], "content": text})
                if total.tool_rounds >= MAX_TOOL_ROUNDS or budget.spent:
                    final = True
                    convo[-1]["content"] += "\n" + LIMIT_NOTE
            state = "done"
        except asyncio.CancelledError:
            state, error = "cancelled", None
        except Exception as e:
            logger.warning("chat %s turn failed: %s", cid, e)
            state, error = "error", str(e)[:1000]
        finally:
            if total.rounds or live.get("reads"):
                report.update(total.report(live))
            await asyncio.shield(self._finish(cid, turn_id, requested, resolved, state, error, total.done(live), live, call_id,
                                              int((time.perf_counter() - t0) * 1000), parent_id, report, fill=fill))

    async def _run_auto(self, cid: str, turn_id: str, rnd: int, call: dict[str, Any], files: FileSet, budget: ReadBudget,
                        live: dict[str, Any]) -> str:
        """Run one automatic tool call; what the model reads back. ``chat.tool``
        says it is running and how it went (the page's "正在讀 …")."""
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        tool = self.tools.get(name)
        try:
            args = parse_arguments(fn.get("arguments"))
        except ToolArgsError as e:
            args, bad = {}, str(e)
        else:
            bad = None
        running = {"tool": name, "file_id": None, "name": None, "what": _reading_label(name, args, files)}
        try:
            f = files.find(args.get("file")) if args.get("file") else None
            if f is not None:
                running.update(file_id=f.id, name=f.name)
        except KeyError:
            pass
        live["reading"] = running
        await self.bus.publish({"type": "chat.tool", "conversation_id": cid, "turn_id": turn_id, "round": rnd, "tool_call_id": call["id"],
                                "tool": name, "state": "running", "read": running})
        if bad is not None or tool is None:
            text, read = (f"參數不對：{bad}" if bad else f"沒有這個工具：{name}"), {**running, "chars": 0, "error": True}
        else:
            run = await tool.run(args, files, budget)
            text, read = run.text, {**running, **(run.read or {})}
        read["round"] = rnd
        live["reading"] = None
        live.setdefault("reads", []).append(read)
        await self.bus.publish({"type": "chat.tool", "conversation_id": cid, "turn_id": turn_id, "round": rnd, "tool_call_id": call["id"],
                                "tool": name, "state": "done", "read": read})
        return text

    async def _finish(self, cid: str, turn_id: str, requested: str, resolved: str, state: str, error: str | None,
                      done: dict[str, Any] | None, live: dict[str, Any], call_id: str | None, duration_ms: int, parent_id: str,
                      images: dict[str, Any] | None = None, *, fill: Optional[dict[str, Any]] = None) -> None:
        text = (done or {}).get("text") or "".join(live["text"])
        reasoning = (done or {}).get("reasoning") or "".join(live["reasoning"]) or None
        model = (done or {}).get("model") or resolved
        usage = (done or {}).get("usage")
        cost = (done or {}).get("cost_usd")
        provider = (done or {}).get("provider")
        meta = {"state": state, "finish_reason": (done or {}).get("finish_reason"), "provider": provider, "requested_model": requested,
                "duration_ms": duration_ms, "turn_id": turn_id}
        if images:  # the branch had images / files: what this reply was actually sent (and read)
            meta.update(images)
        if error:
            meta["error"] = error
        # tool calls count only from a reply that finished: a cut-off one may hold half a call
        kept, records, ignored = self._admit((done or {}).get("tool_calls") if state == "done" else None)
        if records:
            meta["tools"] = records
            if isinstance((done or {}).get("replay"), dict):
                meta["replay"] = done["replay"]
        ignored = list((done or {}).get("ignored") or []) + ignored
        if ignored:
            meta["tool_calls_ignored"] = ignored
        message = None
        try:
            if fill is not None:  # the reply that waited for the owner: its questions stay on it
                cur = await self.store.message(fill["id"]) or fill
                old = cur.get("meta") or {}
                meta = {**meta, "gate": old.get("gate") or {}, "asked_first": True}
                await self.store.update_message(fill["id"], content=text, meta=meta, model=model, usage=usage, cost_usd=cost,
                                                reasoning=reasoning, tool_calls=kept or None)
                await self.store.set_leaf(cid, fill["id"])
                message = await self.store.message(fill["id"])
            else:
                message = await self.store.add_message(cid, role="assistant", content=text, model=model, usage=usage, cost_usd=cost,
                                                       reasoning=reasoning, tool_calls=kept or None, meta=meta, parent_id=parent_id)
            await self.store.update_conversation(cid, status="open")
            if call_id:
                await self.store.call_finished(call_id, status="ok" if state == "done" else "error", duration_ms=duration_ms, model=model, provider=provider,
                                               cost_usd=cost, usage=usage, result={"chars": len(text), "state": state,
                                                                                   "model_calls": (done or {}).get("model_calls") or 1},
                                               error=error or (None if state == "done" else state))
        except Exception as e:
            logger.error("chat %s could not store the reply: %s", cid, e)
            error = error or f"reply could not be stored: {e}"
            state = "error"
        finally:
            self._tasks.pop(cid, None)
            self._live.pop(cid, None)
            self._claims.discard(cid)
        view = await self._view(message) if message else None
        await self.bus.publish({"type": "chat.finished", "conversation_id": cid, "turn_id": turn_id, "state": state, "message": view, "error": error})
        for p in (view or {}).get("proposals") or []:
            if p.get("gate"):
                continue  # announced when it was asked
            await self.bus.publish({"type": "chat.proposal", "conversation_id": cid, "message_id": view["id"], "tool_call_id": p["id"],
                                    "action": "created", "proposal": p})
        if call_id:
            await self.bus.publish({"type": "call.finished", "call_id": call_id, "tool": "chat", "status": "ok" if state == "done" else "error",
                                    "duration_ms": duration_ms, "model": model, "cost_usd": cost})

    # ------------------------------------------------------------ tool calls (1.2-M4)
    def _admit(self, calls: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """A finished reply's tool calls → (calls kept, their records by id,
        calls ignored with why). Unknown tools, unusable arguments and calls
        over a tool's ``max_per_reply`` are ignored — dropped from the stored
        calls so no call is ever left without a result."""
        kept: list[dict[str, Any]] = []
        records: dict[str, dict[str, Any]] = {}
        ignored: list[dict[str, Any]] = []
        per_tool: dict[str, int] = {}
        for call in calls or []:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = fn.get("name")
            tool = self.tools.get(name)
            if tool is None or tool.auto or isinstance(tool, TranscriptionGate):
                # automatic tools run inside the turn (only when offered); the transcription question is not the model's
                ignored.append({"name": name, "reason": "unknown tool" if tool is None else "not offered this turn"})
                continue
            try:
                record = tool.admit(parse_arguments(fn.get("arguments")))
            except ToolArgsError as e:
                ignored.append({"name": name, "reason": str(e)[:200]})
                continue
            if per_tool.get(name, 0) >= tool.max_per_reply:
                ignored.append({"name": name, "reason": f"over {tool.max_per_reply} per reply"})
                continue
            tcid = str(call.get("id") or "") or f"call_{uuid.uuid4().hex[:16]}"
            while tcid in records:
                tcid = f"{tcid}_{uuid.uuid4().hex[:4]}"
            per_tool[name] = per_tool.get(name, 0) + 1
            kept.append({**call, "id": tcid, "type": "function", "function": {"name": name, "arguments": fn.get("arguments") or "{}"}})
            records[tcid] = {**record, "tool": name}
        return kept, records, ignored

    async def _find_call(self, cid: str, tool_call_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """(the reply holding the call, its record) — newest first should an id repeat."""
        rows = await self.store.messages(cid, limit=None)
        for m in reversed(rows):
            if m.get("role") != "assistant":
                continue
            meta = m.get("meta") or {}
            rec = (meta.get("tools") or {}).get(tool_call_id) or (meta.get("gate") or {}).get(tool_call_id)
            if isinstance(rec, dict):
                return m, rec
        raise ChatError(f"proposal '{tool_call_id}' not found in this conversation", 404)

    async def _append_result(self, cid: str, reply: dict[str, Any], tool_call_id: str, text: str, name: str) -> str:
        """Write the ``tool`` message answering one call: after the reply's
        earlier results, so the results of one reply form a chain on its
        branch. The conversation stays on the branch it is showing — it only
        moves onto the new message when it was showing the chain's end."""
        rows = await self.store.messages(cid, limit=None)
        kids = _children(rows)
        by_id = {r["id"]: r for r in rows}
        cur = reply["id"]
        while True:
            nxt = [k for k in kids.get(cur, []) if by_id[k].get("role") == "tool" and (by_id[k].get("meta") or {}).get("for_message") == reply["id"]]
            if not nxt:
                break
            cur = nxt[-1]
        leaf = await self.store.leaf_id(cid)
        msg = await self.store.add_message(cid, role="tool", content=text, meta={"tool_call_id": tool_call_id, "for_message": reply["id"], "tool": name},
                                           parent_id=cur)
        if leaf != cur:
            await self.store.set_leaf(cid, leaf)
        return msg["id"]

    async def _put_record(self, cid: str, reply: dict[str, Any], tool_call_id: str, rec: dict[str, Any], *, action: str = "updated") -> dict[str, Any]:
        """Store a call's new record on its reply and bring its result message
        up to date (written the first time it leaves an open state; never for
        a transcription question, which has no call in the history). Holds no
        lock itself: callers hold the conversation's tool lock."""
        tool = self.tools.get(rec.get("tool"))
        if rec.get("state") not in OPEN_STATES and tool is not None and tool.writes_result:
            text = tool.result_text(rec)
            if rec.get("result_message_id") and await self.store.message(rec["result_message_id"]):
                await self.store.update_message(rec["result_message_id"], content=text)
            else:
                rec["result_message_id"] = await self._append_result(cid, reply, tool_call_id, text, tool.name)
        fresh = await self.store.message(reply["id"])  # the reply may have been filled in since it was read
        meta = dict((fresh or reply).get("meta") or {})
        bucket = "gate" if rec.get("gate") else "tools"
        meta[bucket] = {**(meta.get(bucket) or {}), tool_call_id: rec}
        await self.store.update_message(reply["id"], meta=meta)
        reply["meta"] = meta
        call = next((c for c in (fresh or reply).get("tool_calls") or [] if isinstance(c, dict) and c.get("id") == tool_call_id), None)
        view = self._record_view(tool_call_id, rec, call)
        if self._view_key(rec) == "proposals":
            await self.bus.publish({"type": "chat.proposal", "conversation_id": cid, "message_id": reply["id"], "tool_call_id": tool_call_id,
                                    "action": action, "proposal": view})
        return view

    async def _settle_open(self, cid: str) -> int:
        """The owner moved on (send / regenerate / edit): every call on the
        current branch still waiting for them is settled by its tool
        (``propose_generation``: declined, ``auto``), so the next request has a
        result for every call. A generating one is not waiting: it keeps going.
        A reply still waiting on its transcription questions is ``skipped``
        (決策記錄 1.2-M5-b): it will not run, even when a transcription it
        started finishes later (the transcript still becomes the file's text)."""
        n = 0
        async with self._tool_lock(cid):
            _, path, _ = await self._current_path(cid)
            for m in path:
                if m.get("role") != "assistant":
                    continue
                if (m.get("meta") or {}).get("state") == "awaiting":
                    meta = {**m["meta"], "state": "skipped"}
                    await self.store.update_message(m["id"], meta=meta)
                    m["meta"] = meta
                for tcid, rec in self._records(m):
                    tool = self.tools.get(rec.get("tool"))
                    if rec.get("state") in OPEN_STATES and tool is not None:
                        await self._put_record(cid, m, tcid, {**rec, **tool.auto_settle(rec)})
                        n += 1
        return n

    def _proposal_tool(self, rec: dict[str, Any]) -> Any:
        tool = self.tools.get(rec.get("tool"))
        if tool is None or not tool.confirmable:
            raise ChatError("this tool call is not a proposal", 400)
        return tool

    async def accept(self, cid: str, tool_call_id: str, *, prompt: Any = None, model: Any = None, params: Any = None) -> dict[str, Any]:
        """The owner pressed 生成 (or 轉錄後回覆): start the generation job
        (決策記錄 1.2-M4-b) with their edits (``prompt``, ``model``, extra
        generation ``params``). A failed, cancelled or declined one (by the
        owner or ``auto``: the owner reopened it, D43) may be accepted again,
        its result message rewritten in place; a generating or done one
        answers 409. Returns the proposal."""
        from ..generate import GenerationError

        await self._open_conv(cid)
        if cid in self._deleting:
            raise ChatError("這段聊天正在刪除", 409)
        if self.generations is None:
            raise ChatError("generation is not available", 503)
        if params is not None and not isinstance(params, dict):
            raise ChatError("params must be an object")
        params = dict(params or {})
        model = model if model not in (None, "") else params.pop("model", None)
        params.pop("prompt", None), params.pop("text", None)
        async with self._tool_lock(cid):
            reply, rec = await self._find_call(cid, tool_call_id)
            tool = self._proposal_tool(rec)
            if rec.get("state") not in ACCEPTABLE_STATES:
                raise ChatError(f"this proposal is already {rec.get('state')}", 409)
            if rec.get("gate") and (reply.get("meta") or {}).get("state") != "awaiting":
                raise ChatError("this reply is no longer waiting for a transcription", 409)
            kind, gparams, sources = await tool.job(rec, self.tool_env, prompt=prompt if isinstance(prompt, str) else None,
                                                    model=str(model) if model else None, params=params)
            links = {"conversation_id": cid, "message_id": reply["id"], "tool_call_id": tool_call_id}
            try:
                gen = await self.generations.start(kind, gparams, sources, source="chat", links=links,
                                                   on_finished=lambda row, closing: self._generation_finished(cid, reply["id"], tool_call_id, row, closing))
            except GenerationError as e:
                raise ChatError(str(e), e.status) from e
            new = {k: v for k, v in rec.items() if k not in ("error", "error_kind", "artifact", "auto")}
            if not rec.get("gate") and "proposed" not in rec and rec.get("state") == "pending":
                new["proposed"] = {k: rec[k] for k in ("prompt", "model", "voice") if rec.get(k)}  # a record from before it was kept
            new.update(state="generating", generation_id=gen["id"], model=gparams.get("model") or rec.get("model"),
                       prompt=gparams.get("prompt") or gparams.get("text") or rec.get("prompt") or "", attempts=int(rec.get("attempts") or 0) + 1)
            return await self._put_record(cid, reply, tool_call_id, new)

    async def decline(self, cid: str, tool_call_id: str) -> dict[str, Any]:
        """The owner pressed 不用了 (or 不轉錄，直接回覆). A pending proposal
        can be declined, and so can one whose job failed or was cancelled
        (409 otherwise). When that answers the last open question of a
        waiting reply, the reply starts."""
        await self._open_conv(cid)
        async with self._tool_lock(cid):
            reply, rec = await self._find_call(cid, tool_call_id)
            self._proposal_tool(rec)
            if rec.get("state") not in ("pending", "failed", "cancelled"):
                raise ChatError(f"this proposal is already {rec.get('state')}", 409)
            if rec.get("gate") and (reply.get("meta") or {}).get("state") != "awaiting":
                raise ChatError("this reply is no longer waiting for a transcription", 409)
            view = await self._put_record(cid, reply, tool_call_id, {**rec, "state": "declined", "auto": False})
        if rec.get("gate"):
            await self._maybe_resume(cid, reply["id"])
        return view

    @staticmethod
    def _ending(gen: dict[str, Any], shutting_down: bool) -> dict[str, Any]:
        """How a generation job ended → the proposal's new fields."""
        status = gen.get("status")
        if status == "done":
            art = (gen.get("artifacts") or [{}])[0]
            name = Path(art["file_path"]).name if art.get("file_path") else art.get("title")
            out = {"state": "done", "model": gen.get("model"), "artifact": {"id": art.get("id"), "kind": art.get("kind"), "name": name}}
            if art.get("kind") in ("transcript", "lyrics"):
                out["artifact"]["chars"] = len(art.get("text") or "")
            return out
        if status == "cancelled" and not shutting_down:
            return {"state": "cancelled"}
        if status in ("cancelled", "interrupted"):
            # the service stopped under it: like any failure it can be tried again
            return {"state": "failed", "error_kind": "interrupted", "error": gen.get("error") or "the service restarted while this was generating"}
        return {"state": "failed", "error_kind": gen.get("error_kind") or "other", "error": gen.get("error") or "the generation failed"}

    async def _generation_finished(self, cid: str, mid: str, tool_call_id: str, gen: dict[str, Any], shutting_down: bool = False) -> None:
        """A proposal's generation job ended. The chat may be gone by now
        (deleted while it ran): the work is on the wall all the same. A
        transcript made for an uploaded audio file becomes that file's text
        (決策記錄 1.2-M5-a; a work's transcript is found as its child)."""
        async with self._tool_lock(cid):
            reply = await self.store.message(mid)
            meta = (reply or {}).get("meta") or {}
            rec = (meta.get("tools") or {}).get(tool_call_id) or (meta.get("gate") or {}).get(tool_call_id)
            if not isinstance(rec, dict) or rec.get("generation_id") != gen.get("id"):
                rec = None  # deleted, or an older attempt
            ending = self._ending(gen, shutting_down)
            if ending["state"] == "done" and gen.get("kind") == "transcript":
                ref = (gen.get("sources") or {}).get("audio") or (rec or {}).get("ref") or {}
                art = ending.get("artifact") or {}
                if ref.get("upload_id") and art.get("id"):
                    await self.store.update_upload_meta(ref["upload_id"], transcript_id=art["id"])
            if rec is None:
                return
            await self._put_record(cid, reply, tool_call_id, {**rec, **ending})
        if rec.get("gate") and ending["state"] == "done" and not shutting_down:
            await self._maybe_resume(cid, mid)

    async def _maybe_resume(self, cid: str, mid: str) -> bool:
        """A waiting reply whose questions are all answered (transcribed or
        declined) now runs: streamed into that same message, answering the
        question it follows, with the model and params the owner sent it with."""
        reply = await self.store.message(mid)
        meta = (reply or {}).get("meta") or {}
        gate = meta.get("gate") or {}
        if not reply or meta.get("state") != "awaiting" or not gate:
            return False
        if any((r or {}).get("state") not in ("done", "declined") for r in gate.values()):
            return False
        if cid in self._deleting or self.busy(cid):
            return False  # something else holds the conversation: that turn moved the owner on
        conv = await self.store.conversation(cid)
        if not conv or conv.get("status") == "archived":
            return False
        question = await self.store.message(reply.get("parent_id") or "")
        if not question:
            return False
        self._claim(cid)
        try:
            requested, resolved, provider = self._route(meta.get("requested_model"), conv)
            return bool(await self._launch(cid, conv, question, requested, resolved, provider, dict(meta.get("params") or {}),
                                           meta.get("source") or "gui", action=meta.get("action") or "send", extra=meta.get("extra"), fill=reply))
        except ChatError as e:  # the model cannot be routed any more: the reply says so instead of waiting forever
            self._claims.discard(cid)
            await self.store.update_message(mid, meta={**meta, "state": "error", "error": str(e)[:1000]})
            await self.bus.publish({"type": "chat.finished", "conversation_id": cid, "turn_id": meta.get("turn_id"), "state": "error",
                                    "message": await self._view(await self.store.message(mid)), "error": str(e)[:1000]})
            return False
        except BaseException:
            self._claims.discard(cid)
            raise

    async def reconcile(self) -> int:
        """At startup: a proposal still ``generating`` lost its job with the
        previous process (v1.1 marks such jobs ``interrupted``). Each takes
        how its job ended — failed (``interrupted``, may be tried again) in
        the usual case. A waiting reply whose questions were all answered but
        whose reply never ran (the process stopped in between) runs now."""
        if self.generations is None:
            return 0
        n = 0
        for reply in await self.store.messages_with_tool_state("generating"):
            for tcid, rec in self._records(reply):
                if rec.get("state") != "generating":
                    continue
                gen = await self.store.generation(rec.get("generation_id") or "")
                if gen is None:
                    gen = {"id": rec.get("generation_id"), "status": "interrupted", "error": "the generation job is gone"}
                elif gen.get("status") == "running":
                    continue  # still running in this process (attach called late): it will report itself
                else:
                    gen = await self.generations.public(gen)
                async with self._tool_lock(reply["conversation_id"]):
                    await self._put_record(reply["conversation_id"], reply, tcid, {**rec, **self._ending(gen, True)})
                n += 1
        if n:
            logger.info("Proposals left generating by the previous process settled: %s", n)
        for reply in await self.store.messages_with_tool_state("awaiting"):
            try:
                await self._maybe_resume(reply["conversation_id"], reply["id"])
            except Exception as e:  # one stuck reply must not stop the daemon from starting
                logger.warning("waiting reply %s could not be resumed: %s", reply["id"], e)
        return n

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
        """(filename, markdown) for the conversation's current branch."""
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
        ]
        if any((m.get("versions") or {}).get("count", 1) > 1 for m in conv["messages"]):
            lines.append(f"- 這段對話有分岔：以下是目前顯示的那一條（{len(conv['messages'])} 則）；訊息數與費用含其他版本")
        lines.append("")
        if conv.get("system_prompt"):
            lines += ["## System prompt", ""] + [f"> {ln}" if ln else ">" for ln in str(conv["system_prompt"]).splitlines()] + [""]
        lines += ["---", ""]
        for m in conv["messages"]:
            body = _text_of(m.get("content"))
            meta = m.get("meta") or {}
            if m["role"] == "user":
                lines += [f"## 你 · {_stamp(m.get('created_at'))}", ""]
                if body:
                    lines += [body, ""]
                atts = m.get("attachments") or []
                name = lambda a: str(a.get("name") or a.get("upload_id") or a.get("artifact_id"))  # noqa: E731
                imgs = [a for a in atts if att_kind(a) == "image"]
                if imgs:
                    lines += [f"> 附圖 {len(imgs)} 張：{'、'.join(name(a) for a in imgs)}", ""]
                others = [a for a in atts if att_kind(a) != "image"]
                if others:
                    lines += [f"> 附件 {len(others)} 個："]
                    for a in others:
                        info = a.get("info") or {}
                        bits = [str(info.get("type_name") or ("音檔" if att_kind(a) == "audio" else "檔案"))]
                        if a.get("bytes"):
                            bits.append(f"{a['bytes']:,} bytes")
                        if info.get("readable") is False and info.get("reason_text"):
                            bits.append(f"讀不了：{info['reason_text']}")
                        lines.append(f"> - {name(a)}（{'，'.join(bits)}）")
                    lines.append("")
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
            elif state == "awaiting":
                head += " · 等你決定要不要轉錄"
            elif state == "skipped":
                head += " · 沒有回覆（你直接送了下一則）"
            lines += [head, ""]
            if meta.get("files"):  # 1.2-M5: how each file reached this model
                lines += ["> 檔案："] + [f"> - {e.get('name')}：{describe_delivery(e)}" for e in meta["files"]] + [""]
            if meta.get("reads"):
                lines += [f"> 讀過（{meta.get('tool_rounds') or 0} 輪工具、共約 {int(meta.get('read_chars') or 0):,} 字）："]
                lines += [f"> - {r.get('name') or '檔案'} {r.get('what') or ''}（約 {int(r.get('chars') or 0):,} 字）" for r in meta["reads"]]
                lines.append("")
            if m.get("reasoning"):
                lines += ["<details><summary>思考</summary>", "", str(m["reasoning"]), "", "</details>", ""]
            if body:
                lines += [body, ""]
            for p in m.get("proposals") or []:
                lines += _export_proposal(p)
            if meta.get("error"):
                lines += [f"> 錯誤：{meta['error']}", ""]
        stamp =datetime.now(_TAIPEI).strftime("%Y%m%d-%H%M")
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in title).strip().replace(" ", "_")[:40] or "chat"
        return f"{safe}-{stamp}.md", "\n".join(lines).rstrip() + "\n"
