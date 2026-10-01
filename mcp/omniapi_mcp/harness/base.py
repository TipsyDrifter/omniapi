"""Harness adapter interface + shared helpers (search MCP config, env cleanup)."""

from __future__ import annotations

import logging
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..catalog.paths import data_home
from .events import RunEvent, RunSpec

logger = logging.getLogger(__name__)

# Variables a parent Claude Code session leaks into children; a harness run
# must not look like a nested session (lesson from dsk).
_PARENT_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)


def clean_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    for k in _PARENT_SESSION_VARS:
        env.pop(k, None)
    return env


def harness_home(name: str, sub: str | None = None) -> Path:
    p = data_home() / "harness" / name
    if sub:
        p = p / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def tavily_key(settings: Any) -> Optional[str]:
    key = getattr(getattr(settings, "search", None), "tavily_api_key", None) or os.environ.get("TAVILY_API_KEY")
    if key:
        return key
    f = Path.home() / ".tavily" / "api-key.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip() or None
    return None


def search_mcp_servers(settings: Any) -> tuple[dict[str, Any], list[str], str]:
    """(mcp_servers config, allowed tool prefixes, guidance sentence)."""
    servers: dict[str, Any] = {
        # Windows: stdio MCP via npx must go through cmd /c
        "ddg": {"command": "cmd", "args": ["/c", "npx", "-y", "@ericthered926/duckduckgo-mcp-server"]}
        if os.name == "nt"
        else {"command": "npx", "args": ["-y", "@ericthered926/duckduckgo-mcp-server"]},
    }
    allowed = ["mcp__ddg"]
    key = tavily_key(settings)
    if key:
        servers["tavily"] = (
            {"command": "cmd", "args": ["/c", "npx", "-y", "tavily-mcp"], "env": {"TAVILY_API_KEY": key}}
            if os.name == "nt"
            else {"command": "npx", "args": ["-y", "tavily-mcp"], "env": {"TAVILY_API_KEY": key}}
        )
        allowed.append("mcp__tavily")
        guide = (
            "搜尋規則：優先用 tavily 系工具（回傳已抽取的正文，品質最好）；tavily 報錯、被限流或額度用盡時，"
            "改用 duckduckgo 工具；已知確切網址要抓全文就直接 WebFetch。"
        )
    else:
        guide = "搜尋規則：用 duckduckgo 工具；被限流時改用 WebFetch 直抓已知的官方頁面。"
    return servers, allowed, guide


HEADLESS_SYSTEM_APPEND = (
    "你是被 headless 呼叫的工程執行者，負責規格明確的單點任務。把任務完整做完，"
    "結尾用繁體中文給精簡回報：做了什麼、動了哪些檔案（絕對路徑）、有什麼注意事項。"
    "查證類任務的每個結論都必須附：來源 URL、關鍵引文、發布日期；查不到就明說，禁止腦補。"
)


def which_cli(name: str) -> Optional[str]:
    """Locate a CLI; on Windows npm shims are .cmd files."""
    for cand in (name, f"{name}.cmd", f"{name}.exe"):
        p = shutil.which(cand)
        if p:
            return p
    return None


class HarnessAdapter(ABC):
    name: str = ""
    supports_resume: bool = False

    def __init__(self, settings: Any):
        self.settings = settings
        self._cancelled = False

    @abstractmethod
    def start(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        """Run the spec, yielding unified events; the last one is result/error."""
        ...

    async def cancel(self) -> None:
        self._cancelled = True

    @staticmethod
    def truncate(s: Any, limit: int = 4000) -> str:
        if s is None:
            return ""
        text = s if isinstance(s, str) else str(s)
        return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"
