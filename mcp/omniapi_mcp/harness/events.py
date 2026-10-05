"""Unified run model shared by every harness adapter.

``RunSpec`` is what a caller asks for; ``RunEvent`` is the one event shape the
board understands. Adapters translate their native streams (Agent SDK
messages, Codex JSONL, Gemini stream-json) into these eight event types:

    session_start  {session_id, tools?, mcp_servers?, model?}
    text           {text}                     assistant prose
    thinking       {text}                     chain-of-thought when exposed
    tool_call      {id, name, input}          Bash / Read / Edit / mcp__x / …
    tool_result    {id, name?, output, is_error}
    status         {message}                  retries, rate limits, progress
    result         {text, usage, cost_usd, num_turns, session_id, is_error}
    error          {message}
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

EVENT_TYPES = ("session_start", "text", "thinking", "tool_call", "tool_result", "status", "result", "error")
TERMINAL_STATES = ("done", "error", "cancelled", "dead")


def new_run_id() -> str:
    """dsk-style id: YYYYMMDDHHMMSS-xyz (sorts by time, easy to read aloud)."""
    return time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:3]


@dataclass
class RunSpec:
    prompt: str
    model: Optional[str] = None          # tier alias or model id (resolved by the registry); None = the dispatch default (settings, "cheap")
    harness: Optional[str] = None        # claude | codex | gemini | None = route by model
    cwd: Optional[str] = None
    title: Optional[str] = None
    yolo: bool = False                   # bypass permissions / sandbox
    search: bool = True                  # attach search MCP servers (claude harness)
    max_turns: Optional[int] = None
    resume_session_id: Optional[str] = None   # harness-side session to continue
    resume_run_id: Optional[str] = None       # our run id this continues
    dispatcher: Optional[str] = None
    system_append: Optional[str] = None
    auth: Optional[str] = None           # Claude models: None/"subscription" (default) | "api"
    extra: dict[str, Any] = field(default_factory=dict)

    # filled by the registry
    resolved_model: Optional[str] = None
    provider: Optional[str] = None
    endpoint: Optional[str] = None       # claude harness: anthropic | deepseek | openrouter

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ts": self.ts, "payload": self.payload}


def short_tool_summary(name: str, inp: Any) -> str:
    """One-line human summary of a tool call (board / CLI log)."""
    i = inp if isinstance(inp, dict) else {}
    pick = i.get("command") or i.get("file_path") or i.get("query") or i.get("pattern") or i.get("url") or i.get("path") or ""
    short = name.replace("mcp__", "").replace("__", ":") if name.startswith("mcp__") else name
    pick = " ".join(str(pick).split())[:70]
    return f"{short} → {pick}" if pick else short
