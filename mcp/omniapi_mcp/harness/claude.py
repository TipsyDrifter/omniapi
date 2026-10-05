"""Claude Code harness via the official Python Agent SDK.

Runs Claude (native), DeepSeek (``/anthropic`` endpoint) or any OpenRouter
model (``/api`` Anthropic-compatible endpoint) as a fully tooled agent.

Isolation (inherited from dsk, proven over 56 runs): each endpoint gets its own
``CLAUDE_CONFIG_DIR`` under ``~/.omniapi/harness/claude/<endpoint>/`` with the
API key pre-approved in ``.claude.json`` (headless cannot answer the
"use this key?" dialog), no user settings/hooks are loaded, and parent
Claude Code session variables are scrubbed so the child never looks nested.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from ..catalog import catalog
from .base import HEADLESS_SYSTEM_APPEND, HarnessAdapter, clean_env, harness_home, search_mcp_servers
from .events import RunEvent, RunSpec
from .registry import DEEPSEEK_CLI_ALIAS

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ which claude.exe (1.3-M5)
# The SDK looks for the CLI itself, bundled copy first. The desktop build will not ship that
# 230 MB copy (Q7, prototype one), so we pick the executable ourselves and always pass
# ``cli_path``: the user's own install first (it is the one their login belongs to), the
# bundled copy last. Order: setting / env → PATH → ~/.local/bin/claude.exe → SDK bundled.

#: env var naming the executable directly (same meaning as the ``harness.claude_cli`` setting)
CLAUDE_CLI_ENV = "HARNESS__CLAUDE_CLI"


@dataclass(frozen=True)
class ClaudeCli:
    """Where the Claude Code executable is: ``path`` is ``None`` when none was found,
    and ``reason`` then says why in words a user can act on."""

    path: Optional[str]
    source: str  # setting | path | local-bin | sdk-bundled | none
    reason: str = ""


def _native(path: str) -> bool:
    """A file CreateProcess runs directly. npm's ``claude.cmd`` shim is not one: the SDK
    refuses batch files on Windows (quoting through cmd.exe is not safe)."""
    if os.name != "nt":
        return True
    return Path(path).suffix.lower() in (".exe", ".com")


def _sdk_bundled() -> Optional[Path]:
    """``claude_agent_sdk/_bundled/claude(.exe)`` when the installed wheel carries it
    (an sdist build does not). Located without importing the SDK."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or not spec.submodule_search_locations:
        return None
    name = "claude.exe" if os.name == "nt" else "claude"
    for loc in spec.submodule_search_locations:
        p = Path(loc) / "_bundled" / name
        if p.is_file():
            return p
    return None


def find_claude_cli(
    settings: Any = None,
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    home: Optional[Path] = None,
    bundled: Callable[[], Optional[Path]] = _sdk_bundled,
) -> ClaudeCli:
    """Pick the Claude Code executable for agent runs (see the comment above for the order).

    An explicitly configured path that does not exist is reported, not silently replaced by
    another copy: the user asked for that one."""
    configured = (getattr(getattr(settings, "harness", None), "claude_cli", "") or os.environ.get(CLAUDE_CLI_ENV, "")).strip().strip('"')
    if configured:
        p = Path(configured).expanduser()
        if p.is_file() and _native(str(p)):
            return ClaudeCli(str(p), "setting")
        why = "is not a file" if not p.is_file() else "is a script, not claude.exe"
        return ClaudeCli(None, "none", f"the configured Claude Code executable ({CLAUDE_CLI_ENV} / harness.claude_cli = {configured}) {why}")
    shim = None
    hit = which("claude")
    if hit and _native(hit):
        return ClaudeCli(hit, "path")
    if hit:
        shim = hit
        exe = which("claude.exe")
        if exe and _native(exe):
            return ClaudeCli(exe, "path")
    local = (home or Path.home()) / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    if local.is_file():
        return ClaudeCli(str(local), "local-bin")
    b = bundled()
    if b is not None:
        return ClaudeCli(str(b), "sdk-bundled")
    if shim:
        return ClaudeCli(None, "none", f"Claude Code is not installed as claude.exe (only npm's shim {shim}, which cannot be run headless on Windows); "
                         "install the native build: irm https://claude.ai/install.ps1 | iex")
    return ClaudeCli(None, "none", "Claude Code is not installed (no claude.exe on PATH or in ~/.local/bin); "
                     "install it: irm https://claude.ai/install.ps1 | iex — or set HARNESS__CLAUDE_CLI to its path")

# Claude Code emits these system subtypes by the thousand (or, for the hook
# pair, sixteen at the head of every run); they carry no information the
# board needs.
_NOISY_SYSTEM_SUBTYPES = {"thinking_tokens", "task_progress", "task_updated", "background_tasks_changed", "hook_started", "hook_response"}

ENDPOINTS = {
    # Claude models default to the owner's Claude subscription: the CLI runs
    # with the user's own login (~/.claude credentials), no API key, no
    # per-token billing. The Agent SDK loads no settings/hooks by default, so
    # the run still does not behave like a nested interactive session.
    "anthropic": {"base_url": None, "subscription": True},
    # Explicit opt-in to pay-per-token API billing (RunSpec.auth == "api").
    "anthropic-api": {"base_url": None},
    "deepseek": {"base_url": "https://api.deepseek.com/anthropic"},
    # OpenRouter: bearer token via ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY must be
    # explicitly empty, and model discovery lets vendor/model ids through.
    "openrouter": {"base_url": "https://openrouter.ai/api", "bearer": True, "extra_env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}},
}


def _prepare_config_dir(endpoint: str, api_key: str) -> Path:
    cfg_dir = harness_home("claude", endpoint)
    cfg_file = cfg_dir / ".claude.json"
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    except Exception:
        cfg = {}
    cfg["hasCompletedOnboarding"] = True
    responses = cfg.setdefault("customApiKeyResponses", {})
    approved = set(responses.get("approved") or [])
    approved.add(api_key[-20:])
    responses["approved"] = sorted(approved)
    cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_dir


class ClaudeHarness(HarnessAdapter):
    name = "claude"
    supports_resume = True

    def __init__(self, settings: Any):
        super().__init__(settings)
        self._client = None

    # ------------------------------------------------------------ options
    def _api_key(self, endpoint: str) -> str:
        cfg = getattr(self.settings.providers, endpoint)
        return cfg.api_key

    def _cli_model(self, spec: RunSpec) -> str:
        model = spec.resolved_model or spec.model
        if spec.endpoint == "deepseek":
            return DEEPSEEK_CLI_ALIAS.get(model, model)
        return model

    def build_options(self, spec: RunSpec):
        from claude_agent_sdk import ClaudeAgentOptions

        endpoint = spec.endpoint or "anthropic"
        ep = ENDPOINTS[endpoint]
        subscription = bool(ep.get("subscription"))

        env = clean_env()
        env.update({"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        if subscription:
            # Use the owner's own Claude Code login; leave CLAUDE_CONFIG_DIR at
            # its default (~/.claude) and send no API key at all.
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            key = self._api_key("anthropic" if endpoint == "anthropic-api" else endpoint)
            cfg_dir = _prepare_config_dir(endpoint, key)
            env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
            if ep.get("base_url"):
                env["ANTHROPIC_BASE_URL"] = ep["base_url"]
            if ep.get("bearer"):
                env["ANTHROPIC_AUTH_TOKEN"] = key
                env["ANTHROPIC_API_KEY"] = ""
            else:
                env["ANTHROPIC_API_KEY"] = key
        env.update(ep.get("extra_env") or {})

        allowed = ["Bash", "WebFetch"]
        disallowed: list[str] = []
        mcp_servers: dict[str, Any] = {}
        guide = ""
        if spec.search:
            mcp_servers, allowed_mcp, guide = search_mcp_servers(self.settings)
            allowed += allowed_mcp
        if endpoint not in ("anthropic", "anthropic-api"):
            # WebSearch is an Anthropic server-side tool; other endpoints lack it.
            disallowed.append("WebSearch")

        append = HEADLESS_SYSTEM_APPEND + guide
        if spec.system_append:
            append += "\n" + spec.system_append

        cli = find_claude_cli(self.settings)
        if cli.path is None:
            # the registry refuses such runs up front; this is the last line of defence
            raise RuntimeError(cli.reason)

        opts = ClaudeAgentOptions(
            cli_path=cli.path,
            model=self._cli_model(spec),
            cwd=spec.cwd,
            permission_mode="bypassPermissions" if spec.yolo else "acceptEdits",
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            env=env,
            max_turns=spec.max_turns,
            resume=spec.resume_session_id,
            include_partial_messages=False,
            # SDK default is 1 MiB per JSON message; a single big tool result
            # (screenshot dump, long file read) killed a run with
            # CLIJSONDecodeError (2026-09-25). 32 MiB is plenty.
            max_buffer_size=32 * 1024 * 1024,
            stderr=lambda line: logger.debug("claude[%s] %s", endpoint, line.rstrip()),
        )
        if spec.yolo:
            # bypassPermissions additionally needs the explicit CLI opt-in flag
            opts.extra_args = {"allow-dangerously-skip-permissions": None}
        return opts

    # ------------------------------------------------------------ run
    async def start(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        try:
            opts = self.build_options(spec)
        except RuntimeError as e:  # no Claude Code executable: say so as the run's error
            yield RunEvent("error", {"message": str(e)})
            return
        model_id = spec.resolved_model or spec.model
        tool_names: dict[str, str] = {}
        session_id: Optional[str] = None

        client = ClaudeSDKClient(options=opts)
        self._client = client
        try:
            await client.connect()
            await client.query(spec.prompt)
            async for msg in client.receive_response():
                if self._cancelled:
                    break
                if isinstance(msg, SystemMessage):
                    data = msg.data or {}
                    if msg.subtype == "init":
                        session_id = data.get("session_id")
                        yield RunEvent(
                            "session_start",
                            {
                                "session_id": session_id,
                                "model": data.get("model"),
                                "tools": data.get("tools", []),
                                "mcp_servers": [s.get("name") for s in data.get("mcp_servers", []) if isinstance(s, dict)],
                            },
                        )
                    elif msg.subtype in _NOISY_SYSTEM_SUBTYPES:
                        continue  # thousands per run (thinking token ticks, task progress)
                    else:
                        yield RunEvent("status", {"message": msg.subtype, "data": {k: v for k, v in data.items() if k not in ("tools",)}})
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield RunEvent("text", {"text": block.text})
                        elif isinstance(block, ThinkingBlock) and block.thinking:
                            yield RunEvent("thinking", {"text": block.thinking})
                        elif isinstance(block, ToolUseBlock):
                            tool_names[block.id] = block.name
                            yield RunEvent("tool_call", {"id": block.id, "name": block.name, "input": block.input})
                elif isinstance(msg, UserMessage):
                    content = msg.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                out = block.content
                                if isinstance(out, list):
                                    out = "\n".join(
                                        (b.get("text", "") if isinstance(b, dict) else str(b)) for b in out
                                    )
                                yield RunEvent(
                                    "tool_result",
                                    {
                                        "id": block.tool_use_id,
                                        "name": tool_names.get(block.tool_use_id),
                                        "output": self.truncate(out),
                                        "is_error": bool(block.is_error),
                                    },
                                )
                elif isinstance(msg, ResultMessage):
                    usage = msg.usage or {}
                    norm = {
                        "prompt_tokens": (usage.get("input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0),
                        "completion_tokens": usage.get("output_tokens") or 0,
                        "cached_tokens": usage.get("cache_read_input_tokens") or 0,
                        "raw": usage,
                    }
                    if spec.endpoint == "anthropic-api":
                        cost = msg.total_cost_usd
                    elif spec.endpoint == "anthropic":
                        cost = 0.0  # subscription: no per-token bill (usage still recorded)
                    else:
                        # SDK prices at Claude rates; re-price with the catalog.
                        cost = catalog.estimate_text_cost(model_id, norm)
                    yield RunEvent(
                        "result",
                        {
                            "text": msg.result or "",
                            "usage": norm,
                            "cost_usd": cost,
                            "num_turns": msg.num_turns,
                            "duration_ms": msg.duration_ms,
                            "session_id": msg.session_id or session_id,
                            "is_error": bool(msg.is_error),
                            "subtype": msg.subtype,
                            "errors": msg.errors,
                        },
                    )
                else:
                    # RateLimitEvent, StreamEvent, ConversationResetMessage …
                    yield RunEvent("status", {"message": type(msg).__name__})
            if self._cancelled:
                yield RunEvent("error", {"message": "cancelled"})
        except Exception as e:
            logger.exception("claude harness failed")
            yield RunEvent("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            self._client = None

    async def cancel(self) -> None:
        await super().cancel()
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:
                pass
