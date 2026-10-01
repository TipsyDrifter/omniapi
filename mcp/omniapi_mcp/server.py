"""Main MCP server implementation for OmniAPI.

One MCP server routing requests to multiple AI provider APIs: image
generation/editing, audio transcription, text/chat completion, speech
synthesis, and music generation.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional, Union

from mcp.server.fastmcp import FastMCP
from pydantic import Field, ValidationError

from . import __version__
from .catalog import catalog
from .config.settings import Settings
from .prompts.template_manager import template_manager
from .recorder import make_recorded
from .resources.model_registry import model_registry
from .resources.prompt_templates import prompt_template_resource_manager
from .runtime import ServerContext, runtime
from .utils.path_utils import find_existing_image_path
from .utils.validators import (
    load_image_file_as_data_url,
    sanitize_prompt,
    validate_background_type,
    validate_base64_image,
    validate_compression,
    validate_days,
    validate_image_quality,
    validate_image_size,
    validate_image_style,
    validate_limit,
    validate_moderation_level,
    validate_output_format,
)

# Initialize logging
logger = logging.getLogger(__name__)


# Global settings - will be initialized in main()
settings: Optional[Settings] = None


def configure_logging(log_level: str = "INFO") -> None:
    """Configure logging with the specified level."""
    level = getattr(logging, log_level.upper())

    # Configure root logger
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,  # Force reconfiguration
    )

    # Update all existing loggers to the new level
    for logger_name in logging.Logger.manager.loggerDict:
        logger_instance = logging.getLogger(logger_name)
        if not logger_instance.handlers:  # Only update if no custom handlers
            logger_instance.setLevel(level)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "OmniAPI MCP Server - Generate and edit images using multiple AI models "
            "(OpenAI, Gemini, etc.)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with default stdio transport for Claude Desktop
  python -m omniapi_mcp.server

  # Daemon (MCP + REST + WebSocket + GUI on one port) — preferred for Claude Code
  omni serve

  # Bare MCP over HTTP (no REST/GUI)
  python -m omniapi_mcp.server --transport streamable-http --port 3001

  # Run with custom config and debug logging
  python -m omniapi_mcp.server --config /path/to/config.env --log-level DEBUG

  # Run with SSE transport
  python -m omniapi_mcp.server --transport sse --port 8080
        """,
    )

    parser.add_argument(
        "--config", type=str, help="Path to configuration file (.env format)"
    )

    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set logging level (default: INFO)",
    )

    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport method (default: stdio for Claude Desktop)",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=3001,
        help="Port for HTTP transports (default: 3001)",
    )

    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address for HTTP transports (default: 127.0.0.1)",
    )

    parser.add_argument(
        "--cors", action="store_true", help="Enable CORS for web deployments"
    )

    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    return parser.parse_args()


def load_settings(
    config_path: Optional[str] = None, override_log_level: Optional[str] = None
) -> Settings:
    """Load settings from environment or config file."""
    global settings

    try:
        # Override config path if specified
        if config_path:
            settings_instance = Settings(_env_file=config_path)
        else:
            settings_instance = Settings()

        # Override log level from command line if specified
        if override_log_level:
            settings_instance.server.log_level = override_log_level

        settings = settings_instance
        return settings
    except ValidationError as e:
        logger.error(f"Failed to load settings due to validation error:\n{e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An unexpected error occurred while loading settings: {e}")
        sys.exit(1)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[ServerContext]:
    """Borrow the process-wide runtime for this MCP session.

    With streamable-http the SDK enters this once per client session; the
    runtime (providers, storage, cache, jobs, store, bus) is built once per
    process and shared — see ``runtime.py``. In stdio mode the single session
    owns it and tears it down on exit.
    """
    logger.info(f"Starting {settings.server.name} v{settings.server.version}")
    ctx = await runtime.acquire(settings, owner="session")
    try:
        yield ctx
    finally:
        await runtime.release(owner="session")


mcp = FastMCP(
    name="OmniAPI MCP Server",
    lifespan=server_lifespan,
    dependencies=[
        "mcp[cli]",
        "openai",
        "pillow",
        "python-dotenv",
        "pydantic",
        "httpx",
        "aiofiles",
        "google-genai",
        "google-auth",
    ],
)


# Every @mcp.tool handler is wrapped so each call lands in the store and on
# the event bus (see recorder.py). Signatures are preserved for the schema.
_recorded = make_recorded(lambda: runtime.context, source="mcp")
_original_tool = mcp.tool


def _recording_tool(*args, **kwargs):
    decorator = _original_tool(*args, **kwargs)

    def register(fn):
        return decorator(_recorded(fn))

    return register


mcp.tool = _recording_tool  # type: ignore[method-assign]


# Add image serving route for HTTP transports
@mcp.custom_route("/images/{image_id}", methods=["GET"])
async def serve_image(request):
    """Serve stored images via HTTP endpoint."""
    from starlette.responses import FileResponse, Response

    image_id = request.path_params["image_id"]

    try:
        # Access the global settings directly
        if not settings:
            return Response("Server not initialized", status_code=500)

        # Create storage manager instance to find the image
        storage_path = Path(settings.storage.base_path)
        image_path = find_existing_image_path(storage_path, image_id)

        if not image_path or not image_path.exists():
            return Response("Image not found", status_code=404)

        # Determine MIME type from file extension
        extension = image_path.suffix.lower()
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }

        media_type = mime_types.get(extension, "application/octet-stream")

        # Return image with proper headers
        return FileResponse(
            image_path,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=31536000",  # 1 year cache
                "ETag": f'"{image_id}"',
            },
        )

    except Exception as e:
        logger.error(f"Error serving image {image_id}: {e}")
        return Response("Internal server error", status_code=500)


# Default logging configuration - will be updated in main()
logger = logging.getLogger(__name__)


# Helper function to get server context
def get_server_context(ctx) -> ServerContext:
    """Get server context from MCP context."""
    return ctx.request_context.lifespan_context


@mcp.tool(
    title="Get Job Result",
    description=(
        "Fetch the result of a long-running generation that returned a ticket "
        "(a response with status='running' and a task_id). Long jobs — Suno "
        "music, high-quality / 4K images — come back as a ticket so the tool "
        "call stays under the client's ~60s timeout; the work keeps running in "
        "the background. Call this with that task_id to fetch the finished "
        "result; if it's still 'running', wait ~20-30s and call again."
    ),
)
async def get_job_result(
    task_id: str = Field(
        ..., description="The task_id returned by a generation tool's ticket."
    ),
) -> dict[str, Any]:
    """Fetch a previously started job's result (call-now / fetch-later)."""
    server_ctx = get_server_context(mcp.get_context())
    try:
        return await server_ctx.jobs.get(task_id)
    except Exception as e:
        logger.error(f"get_job_result failed: {e}", exc_info=True)
        raise


# Tool definitions
@mcp.tool(
    title="Run Agent",
    description=(
        "Dispatch a task to a headless coding agent (the vendor's own harness: "
        "Claude Code for Claude/DeepSeek/OpenRouter models, Codex CLI for OpenAI, "
        "Gemini CLI for Google) with Read/Write/Edit/Bash tools in a working "
        "directory. Returns immediately with a run_id — the agent keeps working in "
        "the background; poll get_run(run_id) for events and the final report. "
        "Use model tiers: cheap (deepseek-flash, default) / standard (gemini-3.8-flash) / "
        "strong (gpt-6-sol), or any model id. Set resume_run_id to continue a "
        "previous run's session with a follow-up instruction."
    ),
)
async def run_agent(
    task: str = Field(..., description="The task, written as a complete brief (goal, files, constraints, expected report)."),
    model: str = Field(default="cheap", description="Tier alias (cheap/standard/strong) or model id (deepseek-flash, gpt-6-sol, gemini-3.8-flash, claude-sonnet-5, moonshotai/kimi-k3 …)."),
    cwd: Optional[str] = Field(default=None, description="Working directory the agent operates in (absolute path). Defaults to the daemon's cwd."),
    title: Optional[str] = Field(default=None, description="Short title for the board (defaults to the first 40 chars of the task)."),
    yolo: bool = Field(default=False, description="Skip all permission prompts / sandbox (only for trusted mechanical tasks). Default: acceptEdits + Bash/WebFetch/search allowed."),
    search: bool = Field(default=True, description="Attach web search MCP servers (DuckDuckGo + Tavily) — claude harness only."),
    harness: Optional[str] = Field(default=None, description="Force a harness: claude | codex | gemini. Default routes by model's provider."),
    max_turns: Optional[int] = Field(default=None, ge=1, description="Cap agentic turns."),
    resume_run_id: Optional[str] = Field(default=None, description="Continue this earlier run's session; task becomes the follow-up message."),
    dispatcher: Optional[str] = Field(default=None, description="Who dispatched (e.g. the Claude Code conversation title) — shown on the board."),
    billing: Optional[str] = Field(default=None, description="Claude models only: 'subscription' (default — the owner's Claude login, no per-token cost) or 'api' (pay-per-token Anthropic API key; only when the owner explicitly asks)."),
) -> dict[str, Any]:
    """Start an agent run and return its ticket."""
    server_ctx = get_server_context(mcp.get_context())
    from .harness import RunSpec

    try:
        spec = RunSpec(
            prompt=task,
            model=model,
            cwd=cwd,
            title=title,
            yolo=yolo,
            search=search,
            harness=harness,
            max_turns=max_turns,
            resume_run_id=resume_run_id,
            dispatcher=dispatcher,
            auth=billing,
        )
        ticket = await server_ctx.runs.start(spec)
        ticket["message"] = (
            f"Agent started on the {ticket['harness']} harness with {ticket['model']}. "
            f"Poll get_run(run_id='{ticket['run_id']}') — typical tasks take 1-5 minutes."
        )
        return ticket
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"run_agent failed: {e}", exc_info=True)
        return {"error": f"run_agent failed: {e}"}


@mcp.tool(
    title="Get Run",
    description=(
        "Fetch an agent run: state (starting/running/done/error/cancelled/dead), "
        "turns, cost, the final report, and its event stream (text, tool_call, "
        "tool_result, …). Pass after_event to get only new events."
    ),
)
async def get_run(
    run_id: str = Field(..., description="The run_id returned by run_agent."),
    after_event: int = Field(default=0, ge=0, description="Only return events with id greater than this (incremental polling)."),
    include_events: bool = Field(default=True, description="Include the event stream (set false for a status-only check)."),
    max_events: int = Field(default=200, ge=1, le=2000, description="Cap on returned events."),
) -> dict[str, Any]:
    """Status + events of a run."""
    server_ctx = get_server_context(mcp.get_context())
    row = await server_ctx.runs.get(run_id, after_event=after_event, include_events=include_events, limit=max_events)
    if not row:
        return {"error": f"run '{run_id}' not found"}
    row.pop("prompt", None) if after_event else None
    return row


@mcp.tool(
    title="List Runs",
    description="List recent agent runs (newest first) with state, harness, model, cost and title.",
)
async def list_runs(
    limit: int = Field(default=15, ge=1, le=200),
    state: Optional[str] = Field(default=None, description="Filter: starting | running | done | error | cancelled | dead"),
) -> dict[str, Any]:
    server_ctx = get_server_context(mcp.get_context())
    rows = await server_ctx.runs.list(limit=limit, state=state)
    slim = [
        {k: r.get(k) for k in ("id", "title", "state", "harness", "model", "cwd", "started_at", "ended_at", "turns", "cost_usd", "dispatcher", "live")}
        for r in rows
    ]
    return {"runs": slim, "live": server_ctx.runs.live_count}


@mcp.tool(title="Cancel Run", description="Stop a running agent run.")
async def cancel_run(run_id: str = Field(..., description="The run to cancel.")) -> dict[str, Any]:
    server_ctx = get_server_context(mcp.get_context())
    return await server_ctx.runs.cancel(run_id)


def _chat_url(server_ctx: Any, cid: str) -> tuple[str, Optional[str]]:
    """GUI link for a conversation, and a note when the port is a guess.

    In the daemon the pid file carries the port this process listens on. In
    stdio mode there is no GUI in this process; the conversation still lands
    in the shared store, so point at the default daemon port and say so.
    """
    import os as _os

    from .chat.cli_support import gui_url
    from .daemon.app import DEFAULT_HOST, DEFAULT_PORT, read_pid

    if getattr(server_ctx, "mode", None) == "daemon":
        info = read_pid() or {}
        if int(info.get("pid") or 0) == _os.getpid() and info.get("port"):
            return gui_url(info.get("host") or DEFAULT_HOST, info["port"], cid), None
    return gui_url(DEFAULT_HOST, DEFAULT_PORT, cid), (
        "stdio mode: no GUI in this process — the link assumes the daemon (`omni serve`) on the default port."
    )


# Registered WITHOUT the call recorder: ChatManager already writes this turn to
# the tool-call ledger (calls row, tool='chat', source='mcp', with the model's
# cost). Recording the MCP call too would put a second row carrying the same
# cost_usd into the ledger and double the chat spend on /costs.
@_original_tool(
    title="Chat",
    description=(
        "Talk with an external text model over several turns (GPT-6 / GPT-5.x, Claude, "
        "Gemini, DeepSeek, OpenRouter; tiers cheap / standard / strong). The conversation "
        "is stored and shows up in the OmniAPI GUI under /chat, so the owner can read or "
        "continue it there. Start a new conversation by leaving conversation_id empty; "
        "for every later turn pass back the conversation_id this tool returned — the "
        "model then sees the whole history. Pick the tool by the job: chat = a back-and-"
        "forth with another model that should stay on record; complete_text = a one-off "
        "completion (structured output, function calling, full control of messages), "
        "nothing kept as a conversation; run_agent = the model must act — edit files, "
        "run commands in a working directory. Replies longer than ~45s come back as a "
        "ticket: fetch them with get_job_result (the conversation_id is in the ticket)."
    ),
)
async def chat(
    message: str = Field(..., description="What to say to the model (this turn's user message)."),
    conversation_id: Optional[str] = Field(default=None, description="Continue this conversation (the conversation_id a previous chat call returned). Leave empty to start a new one."),
    model: Optional[str] = Field(default=None, description="Tier alias (cheap/standard/strong) or model id. New conversation: defaults to cheap. Existing conversation: defaults to the model it used last; giving one switches models from this turn on."),
    system: Optional[str] = Field(default=None, description="System prompt. Sets it for a new conversation; on an existing one it replaces the conversation's system prompt."),
    title: Optional[str] = Field(default=None, description="Conversation title (defaults to the first message)."),
    reasoning_effort: Optional[str] = Field(default=None, description="Reasoning depth for reasoning models (e.g. low/medium/high)."),
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0, description="Sampling temperature; ignored by models that reject it."),
    max_completion_tokens: Optional[int] = Field(default=None, ge=1, description="Max output tokens for this reply."),
) -> dict[str, Any]:
    """One chat turn; returns the reply plus the conversation_id to continue with."""
    from .chat import ChatError
    from .chat.cli_support import reply_summary

    server_ctx = get_server_context(mcp.get_context())
    manager = getattr(server_ctx, "chat", None)
    if manager is None:
        return {"error": "chat is not available in this server"}
    params = {k: v for k, v in {"reasoning_effort": reasoning_effort, "temperature": temperature, "max_completion_tokens": max_completion_tokens}.items() if v is not None}
    cid = conversation_id
    try:
        if cid:
            if system is not None or title is not None:
                await manager.update(cid, title=title, system_prompt=system)
            else:
                await manager.get(cid)  # unknown id → ChatError(404) before anything is stored
        else:
            conv = await manager.create(model=model, system=system, title=title, source="mcp")
            cid = conv["id"]
        url, url_note = _chat_url(server_ctx, cid)

        async def turn() -> dict[str, Any]:
            res = await manager.send_and_wait(cid, message, model=model, params=params, source="mcp")
            conv_after = await manager.get(cid)
            out = reply_summary(conversation=conv_after, message=res.get("message"), state=res.get("state"),
                                requested_model=res.get("model"), resolved_model=res.get("resolved_model"), url=url)
            if url_note:
                out["url_note"] = url_note
            return out

        result = await server_ctx.jobs.run("chat", turn())
        if result.get("status") == "running" and result.get("task_id"):
            result.update({
                "conversation_id": cid,
                "url": url,
                "message": (f"The reply is still coming. Call get_job_result(task_id='{result['task_id']}') in a moment; "
                            f"the conversation is {cid} (also visible at {url})."),
            })
        return result
    except ChatError as e:
        out: dict[str, Any] = {"error": str(e), "status": e.status}
        if cid:
            out["conversation_id"] = cid
        return out
    except Exception as e:
        logger.error(f"chat failed: {e}", exc_info=True)
        return {"error": f"chat failed: {e}", **({"conversation_id": cid} if cid else {})}


@mcp.tool(title="Health Check", description="Check server health and status")
async def health_check() -> dict[str, Any]:
    """
    Check the health status of the MCP server and its dependencies.

    Returns health information including:
    - status: overall health status
    - timestamp: current server time
    - version: server version
    - services: status of dependent services
    """
    server_ctx = mcp.get_context().request_context.lifespan_context

    try:
        # Check providers by pinging their APIs (free endpoints)
        provider_details = {}
        try:
            await server_ctx.image_generation_tool.ensure_providers_registered()
            registry = server_ctx.image_generation_tool.provider_registry
            providers = registry.get_all_providers()
            if not providers:
                providers_status = "unhealthy"
            else:

                async def _check(provider):
                    try:
                        result = await asyncio.wait_for(
                            provider.check_health(), timeout=10
                        )
                        status = result.get("status", "unhealthy")
                        # Normalize to known values; providers return
                        # "healthy"/"unhealthy", "degraded" is aggregate-only.
                        if status not in ("healthy", "unhealthy"):
                            status = "unhealthy"
                        result["status"] = status
                        return provider.name, result
                    except Exception as e:
                        return provider.name, {
                            "status": "unhealthy",
                            "error": str(e),
                        }

                results = await asyncio.gather(
                    *(_check(p) for p in providers)
                )
                provider_details = dict(results)

                statuses = [
                    d["status"] for d in provider_details.values()
                ]
                if all(s == "healthy" for s in statuses):
                    providers_status = "healthy"
                elif any(s == "healthy" for s in statuses):
                    providers_status = "degraded"
                else:
                    providers_status = "unhealthy"
        except Exception:
            providers_status = "unhealthy"

        # Check storage
        storage_status = "healthy"
        try:
            if not server_ctx.storage_manager.base_path.exists():
                storage_status = "unhealthy"
        except Exception:
            storage_status = "unhealthy"

        # Check cache
        cache_status = "healthy" if server_ctx.cache_manager.enabled else "disabled"
        try:
            if server_ctx.cache_manager.enabled and not server_ctx.cache_manager.cache:
                cache_status = "unhealthy"
        except Exception:
            cache_status = "unhealthy"

        # No healthy providers means the server is useless
        if providers_status == "unhealthy":
            overall_status = "unhealthy"
        elif all(
            status in ["healthy", "disabled"]
            for status in [providers_status, storage_status, cache_status]
        ):
            overall_status = "healthy"
        else:
            overall_status = "degraded"

        return {
            "status": overall_status,
            "timestamp": time.time(),
            "version": settings.server.version if settings else "unknown",
            "services": {
                "providers": providers_status,
                "storage": storage_status,
                "cache": cache_status,
            },
            "provider_details": provider_details,
        }

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": time.time(),
            "error": str(e),
        }


@mcp.tool(
    title="Server Info", description="Get server configuration and runtime information"
)
async def server_info() -> dict[str, Any]:
    """
    Get detailed server information including configuration and capabilities.

    Returns:
    - server: server metadata
    - capabilities: available features
    - configuration: non-sensitive configuration details
    """
    server_ctx = mcp.get_context().request_context.lifespan_context

    try:
        return {
            "server": {
                "name": settings.server.name if settings else "OmniAPI MCP Server",
                "version": settings.server.version if settings else "unknown",
                "log_level": settings.server.log_level if settings else "INFO",
            },
            "capabilities": {
                "image_generation": True,
                "image_editing": True,
                "transcription": True,
                "text_completion": True,
                "speech_synthesis": True,
                "music_generation": True,
                "async_jobs": True,
                "caching": server_ctx.cache_manager.enabled,
                "storage": True,
                "prompt_templates": True,
                "model_registry": True,
            },
            "configuration": {
                "default_quality": settings.images.default_quality
                if settings
                else "auto",
                "default_size": settings.images.default_size
                if settings
                else "1536x1024",
                "default_style": settings.images.default_style if settings else "vivid",
                "storage_retention_days": settings.storage.retention_days
                if settings
                else 30,
                "cache_ttl_hours": settings.cache.ttl_hours
                if settings and settings.cache.enabled
                else None,
            },
        }

    except Exception as e:
        logger.error(f"Server info failed: {e}")
        return {"error": str(e)}


@mcp.tool(
    title="Generate Image",
    description=(
        "Generate images using multiple AI models from text descriptions. "
        "Use list_available_models first to see which models are currently available."
    ),
)
async def generate_image(
    prompt: str = Field(
        ...,
        description=(
            "The best practices for image generation prompt is to be highly specific "
            "and detailed about the subject, setting, style, mood, and visual elements "
            "you want, while using clear, unambiguous language to guide the AI's "
            "creative interpretation."
        ),
        min_length=1,
        max_length=4000,
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "AI model to use for image generation. Available models depend on "
            "configured providers. If not specified, uses the configured default model."
        ),
    ),
    quality: Optional[str] = Field(
        default="auto", description="Image quality: auto, high, medium, or low"
    ),
    size: Optional[str] = Field(
        default="auto",
        description="Image size: 1024x1024, 1536x1024, 1024x1536 or auto",
    ),
    style: Optional[str] = Field(
        default="vivid",
        description="Image style: vivid or natural (OpenAI models only)",
    ),
    moderation: Optional[str] = Field(
        default="auto",
        description="Content moderation level: auto or low (OpenAI models only)",
    ),
    output_format: Optional[str] = Field(
        default="png", description="Output format: png, jpeg, or webp"
    ),
    compression: Optional[int] = Field(
        default=100, ge=0, le=100, description="Compression level for JPEG/WebP (0-100)"
    ),
    background: Optional[str] = Field(
        default="auto",
        description="Background type: auto, transparent, opaque (OpenAI models only)",
    ),
    n: Optional[int] = Field(
        default=1,
        ge=1,
        le=10,
        description=(
            "Number of images to generate (gpt-image 1-10; Gemini Nano Banana is "
            "1 only). When >1 the result adds an 'images' list; n=1 keeps the "
            "single-image shape."
        ),
    ),
    user: Optional[str] = Field(
        default=None,
        description="OpenAI only: stable end-user identifier for abuse monitoring.",
    ),
    image_size: Optional[str] = Field(
        default=None,
        description=(
            "Gemini (Nano Banana) only: output resolution '512'/'1K'/'2K'/'4K' "
            "(UPPERCASE K; default 2K). Ignored by OpenAI (use size)."
        ),
    ),
    aspect_ratio: Optional[str] = Field(
        default=None,
        description=(
            "Gemini (Nano Banana) only: explicit aspect ratio (e.g. '16:9', "
            "'9:16', '21:9', '2:3', '4:5') — reaches ratios the WxH size cannot. "
            "Ignored by OpenAI."
        ),
    ),
    person_generation: Optional[str] = Field(
        default=None,
        description=(
            "Gemini only: 'dont_allow' / 'allow_adult' / 'allow_all' — whether "
            "people may be generated. Ignored by OpenAI."
        ),
    ),
    seed: Optional[int] = Field(
        default=None,
        description=(
            "Legacy (Imagen, retired 2026): accepted for backward compatibility "
            "and ignored by every current model."
        ),
    ),
    safety_filter_level: Optional[str] = Field(
        default=None,
        description=(
            "Legacy (Imagen, retired 2026): accepted for backward compatibility "
            "and ignored by every current model."
        ),
    ),
    enhance_prompt: Optional[bool] = Field(
        default=None,
        description=(
            "Legacy (Imagen, retired 2026): accepted for backward compatibility "
            "and ignored by every current model."
        ),
    ),
    guidance_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Legacy (Imagen, retired 2026): accepted for backward compatibility "
            "and ignored by every current model."
        ),
    ),
) -> dict[str, Any]:
    """
    Generate an image from a text prompt using multiple AI providers.

    RECOMMENDED WORKFLOW:
    1. First call list_available_models() to see which models are available
    2. Choose an appropriate model based on your needs and cost considerations
    3. Call this function with the chosen model

    Returns a dictionary containing:
    - task_id: Unique identifier for this generation task
    - image_id: Unique identifier for the generated image
    - image_url: Image access URL (format depends on transport and configuration):
      * STDIO transport: file:// URL for local file access
      * HTTP transport: http:// URL to MCP server endpoint
      * With base_host: full CDN/nginx URL with date path structure
    - resource_uri: MCP resource URI for future access
    - metadata: Generation details and parameters including model and provider info
    """
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    # Validate and sanitize inputs with fault tolerance
    validated_prompt = sanitize_prompt(prompt)
    validated_quality = validate_image_quality(quality)
    validated_size = validate_image_size(size)
    validated_style = validate_image_style(style)
    validated_moderation = validate_moderation_level(moderation)
    validated_output_format = validate_output_format(output_format)
    validated_compression = validate_compression(compression)
    validated_background = validate_background_type(background)

    try:
        return await server_ctx.jobs.run(
            "generate_image",
            server_ctx.image_generation_tool.generate(
                prompt=validated_prompt,
                model=model,  # Pass the model parameter
                quality=validated_quality,
                size=validated_size,
                style=validated_style,
                moderation=validated_moderation,
                output_format=validated_output_format,
                compression=validated_compression,
                background=validated_background,
                n=n or 1,
                user=user,
                image_size=image_size,
                aspect_ratio=aspect_ratio,
                person_generation=person_generation,
                seed=seed,
                safety_filter_level=safety_filter_level,
                enhance_prompt=enhance_prompt,
                guidance_scale=guidance_scale,
            ),
        )
    except Exception as e:
        logger.error(f"Image generation failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Transcribe Audio",
    description=(
        "Transcribe an audio file to text using the OpenAI Audio API "
        "(gpt-transcribe by default). Provide a local file path (preferred) "
        "or base64-encoded audio."
    ),
)
async def transcribe_audio(
    audio_path: Optional[str] = Field(
        default=None,
        description=(
            "Absolute path to a local audio file (mp3, wav, m4a, webm, flac, "
            "ogg, mp4...). Preferred over audio_data."
        ),
    ),
    audio_data: Optional[str] = Field(
        default=None,
        description="Base64-encoded audio (or data URL). Used if audio_path is omitted.",
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "Transcription model. Current: gpt-transcribe (default). "
            "DEPRECATED, still callable until they shut down on 2027-02-26: "
            "whisper-1 (only model documented for verbose_json/srt/vtt and "
            "word timestamps), gpt-4o-transcribe, gpt-4o-mini-transcribe, "
            "gpt-4o-transcribe-diarize (speaker labels via "
            "response_format='diarized_json')."
        ),
    ),
    language: Optional[str] = Field(
        default=None,
        description=(
            "Optional ISO-639-1 language code (e.g. 'en', 'zh', 'ja') to "
            "improve accuracy."
        ),
    ),
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Optional text to guide style or spelling of names/terms. Not "
            "supported by gpt-4o-transcribe-diarize (dropped for it)."
        ),
    ),
    response_format: Optional[str] = Field(
        default="text",
        description=(
            "Output format: text (default), json, verbose_json (adds timed "
            "segments), srt, or vtt. verbose_json/srt/vtt are documented for "
            "whisper-1 only; gpt-transcribe and gpt-4o-* are treated as "
            "text/json only; gpt-4o-transcribe-diarize adds 'diarized_json' "
            "(speaker segments). A format outside that range is passed through "
            "with a warning, not blocked."
        ),
    ),
    temperature: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Sampling temperature (0-1, default 0). Higher = more random.",
    ),
    timestamp_granularities: Optional[list[str]] = Field(
        default=None,
        description=(
            "Word-level timing. Pass ['word'] or ['word','segment']. Requires "
            "response_format='verbose_json' and model='whisper-1' (deprecated, "
            "shuts down 2027-02-26 — no replacement documented for word-level "
            "timestamps); adds a 'words' list (start/end seconds) to the result."
        ),
    ),
    chunking_strategy: Optional[Union[str, dict[str, Any]]] = Field(
        default=None,
        description=(
            "Server-side auto-chunking for long audio: 'auto' or a VAD config "
            "{'type':'server_vad','prefix_padding_ms':..,'silence_duration_ms':"
            "..,'threshold':..}. Auto-defaulted to 'auto' for the diarize model."
        ),
    ),
    include: Optional[list[str]] = Field(
        default=None,
        description=(
            "Extra fields to return, e.g. ['logprobs'] for per-token confidence "
            "(adds a 'logprobs' field). gpt-4o-transcribe / -mini + "
            "response_format='json' only."
        ),
    ),
    known_speaker_names: Optional[list[str]] = Field(
        default=None,
        description=(
            "gpt-4o-transcribe-diarize only: up to 4 speaker labels matched "
            "positionally with known_speaker_references."
        ),
    ),
    known_speaker_references: Optional[list[str]] = Field(
        default=None,
        description=(
            "gpt-4o-transcribe-diarize only: 2-10s audio samples (data URLs), "
            "one per name in known_speaker_names."
        ),
    ),
) -> dict[str, Any]:
    """
    Transcribe audio to text.

    Provide either audio_path (a local file, recommended) or audio_data
    (base64). Returns the transcribed text plus metadata (including
    model_status / model_shutdown).

    Defaults to gpt-transcribe. The 2026-08-26 OpenAI deprecation covers
    whisper-1, gpt-4o-transcribe, gpt-4o-mini-transcribe and
    gpt-4o-transcribe-diarize (shutdown 2027-02-26); they remain callable and
    are still the only route to some features: verbose_json timed segments and
    timestamp_granularities=['word'] (whisper-1), and
    response_format='diarized_json' speaker labels
    (gpt-4o-transcribe-diarize).
    """
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    try:
        return await server_ctx.jobs.run(
            "transcribe",
            server_ctx.transcription_tool.transcribe(
            audio_path=audio_path,
            audio_data=audio_data,
            model=model,
            language=language,
            prompt=prompt,
            response_format=response_format or "text",
            temperature=temperature,
            timestamp_granularities=timestamp_granularities,
            chunking_strategy=chunking_strategy,
            include=include,
            known_speaker_names=known_speaker_names,
            known_speaker_references=known_speaker_references,
            ),
        )
    except Exception as e:
        logger.error(f"Transcription failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Complete Text",
    description=(
        "Generate a text/chat completion with any configured text model: OpenAI "
        "(GPT-6 / GPT-5.x), Anthropic (Claude), Google (Gemini), DeepSeek, or the "
        "OpenRouter long tail. Accepts tier aliases cheap / standard / strong. "
        "Give a prompt plus an optional system message, or a full messages list."
    ),
)
async def complete_text(
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "The user prompt / question to complete. Either prompt or messages "
            "is required; ignored when messages is supplied."
        ),
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "Model id or tier alias. Tiers: 'cheap' (deepseek-flash, default), "
            "'standard' (gemini-3.8-flash), 'strong' (gpt-6-sol). Examples: "
            "gpt-6-astra, gpt-6-sol, gpt-5.4-mini, claude-opus-5-5, "
            "claude-sonnet-5, gemini-3.8-flash, deepseek-flash, deepseek-v4-pro, "
            "moonshotai/kimi-k3 (OpenRouter, vendor/model). Call "
            "list_available_models(modality='text') for the live roster."
        ),
    ),
    system: Optional[str] = Field(
        default=None,
        description=(
            "Optional system / developer instruction to steer behaviour. For "
            "GPT-5.x it is sent as a 'developer' message automatically. Ignored "
            "when messages is supplied."
        ),
    ),
    messages: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "Full chat history instead of prompt+system, e.g. [{'role':'user',"
            "'content':...}, {'role':'assistant','tool_calls':[...]}, {'role':"
            "'tool','tool_call_id':...,'content':...}]. Use this to feed tool "
            "results back for multi-turn function calling. Overrides prompt/system."
        ),
    ),
    temperature: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description=(
            "Sampling temperature (0-2). ⚠️ GPT-5.x reasoning models reject it "
            "(HTTP 400), so it is dropped automatically for them — use "
            "reasoning_effort / verbosity instead. Honoured by DeepSeek "
            "non-thinking mode and OpenAI *-chat-latest models."
        ),
    ),
    reasoning_effort: Optional[str] = Field(
        default=None,
        description=(
            "Reasoning depth. OpenAI GPT-5.x: none/low/medium/high/xhigh "
            "(default medium). DeepSeek: high/max. Replaces temperature as the "
            "main style knob for reasoning models."
        ),
    ),
    verbosity: Optional[str] = Field(
        default=None,
        description=(
            "Response length/detail: low/medium/high (OpenAI GPT-5.x only; "
            "ignored by DeepSeek)."
        ),
    ),
    max_completion_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Max output tokens. Includes hidden reasoning tokens for reasoning "
            "models, so leave headroom. Mapped to max_tokens for DeepSeek."
        ),
    ),
    response_format: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Structured output. e.g. {\"type\":\"json_object\"} or, for OpenAI, "
            "{\"type\":\"json_schema\",\"json_schema\":{\"name\":...,\"schema\":"
            "{...},\"strict\":true}}. DeepSeek supports only json_object."
        ),
    ),
    thinking: Optional[bool] = Field(
        default=None,
        description=(
            "DeepSeek only: true enables thinking (chain-of-thought in the "
            "'reasoning' field), false disables it. Ignored by OpenAI."
        ),
    ),
    tools: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "Function-calling tool definitions, each {'type':'function',"
            "'function':{'name','description','parameters':<JSON schema>,"
            "'strict'?}}. When the model calls a tool, the result is returned in "
            "the 'tool_calls' field (feed the outcome back via messages to continue)."
        ),
    ),
    tool_choice: Optional[Union[str, dict[str, Any]]] = Field(
        default=None,
        description=(
            "Control tool use: 'none' | 'auto' | 'required' or "
            "{'type':'function','function':{'name':...}} to force one. Needs tools."
        ),
    ),
    parallel_tool_calls: Optional[bool] = Field(
        default=None,
        description=(
            "Allow multiple tool calls in one turn (OpenAI only; default true). "
            "Set false to force one tool at a time. Only applies with tools."
        ),
    ),
    top_p: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Nucleus sampling (0-1). Like temperature, auto-dropped for GPT-5.x "
            "reasoning models; honoured by DeepSeek non-thinking / *-chat-latest."
        ),
    ),
    seed: Optional[int] = Field(
        default=None,
        description=(
            "Best-effort reproducibility seed. Auto-dropped for GPT-5.x reasoning "
            "models (400 otherwise); forwarded best-effort to DeepSeek."
        ),
    ),
    stop: Optional[list[str]] = Field(
        default=None,
        description=(
            "Up to 4 (OpenAI) / 16 (DeepSeek) stop sequences; generation halts "
            "when one is hit. Auto-dropped for GPT-5.x reasoning models (they "
            "400 on it, like temperature/top_p/seed); honoured by DeepSeek and "
            "OpenAI *-chat-latest."
        ),
    ),
    store: Optional[bool] = Field(
        default=None,
        description=(
            "OpenAI only: store this completion for evals/distillation (default "
            "false). Ignored by DeepSeek."
        ),
    ),
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "OpenAI only: up to 16 key-value string tags attached to a stored "
            "completion. Ignored by DeepSeek."
        ),
    ),
    service_tier: Optional[str] = Field(
        default=None,
        description=(
            "OpenAI only: 'auto' | 'default' | 'flex' (cheaper) | 'priority' "
            "(faster). Latency/price trade-off. Ignored by DeepSeek."
        ),
    ),
    prompt_cache_key: Optional[str] = Field(
        default=None,
        description=(
            "OpenAI only: cache-routing hint to raise prompt-cache hit rate. "
            "DeepSeek caches automatically (no key). Ignored by DeepSeek."
        ),
    ),
    safety_identifier: Optional[str] = Field(
        default=None,
        description=(
            "OpenAI only: stable end-user identifier for abuse monitoring. "
            "Ignored by DeepSeek."
        ),
    ),
) -> dict[str, Any]:
    """
    Complete a text prompt (or full messages list) with a chosen LLM.

    Returns the generated text plus model / provider / usage metadata. Reasoning
    models also return a 'reasoning' field (chain-of-thought); usage may include
    reasoning_tokens / cache hit-miss. When the model invokes a function, the
    'tool_calls' field carries the calls for the caller to execute and feed back
    via messages.
    """
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    if not prompt and not messages:
        raise ValueError("complete_text requires either 'prompt' or 'messages'.")

    # Friendly bool -> DeepSeek's {"type": "enabled"|"disabled"} body shape.
    thinking_obj = (
        {"type": "enabled" if thinking else "disabled"}
        if thinking is not None
        else None
    )

    try:
        return await server_ctx.jobs.run(
            "complete",
            server_ctx.text_tool.complete(
            prompt=prompt,
            messages=messages,
            model=model,
            system=system,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
            max_completion_tokens=max_completion_tokens,
            response_format=response_format,
            thinking=thinking_obj,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            top_p=top_p,
            seed=seed,
            stop=stop,
            store=store,
            metadata=metadata,
            service_tier=service_tier,
            prompt_cache_key=prompt_cache_key,
            safety_identifier=safety_identifier,
            ),
        )
    except Exception as e:
        logger.error(f"Text completion failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Generate Speech",
    description=(
        "Synthesize speech from text using ElevenLabs, OpenAI or Gemini TTS. "
        "Saves an audio file and returns its path. ElevenLabs needs "
        "PROVIDERS__ELEVENLABS__API_KEY; OpenAI TTS reuses "
        "PROVIDERS__OPENAI__API_KEY; Gemini TTS needs the Gemini slot to hold "
        "a Developer-API key."
    ),
)
async def generate_speech(
    text: str = Field(
        ...,
        description="The text to speak.",
        min_length=1,
    ),
    voice: Optional[str] = Field(
        default=None,
        description=(
            "Voice for the chosen model's provider: an ElevenLabs voice_id "
            "(GET /v1/voices), an OpenAI voice name (alloy, echo, fable, onyx, "
            "nova, shimmer...), or a Gemini prebuilt voice name (e.g. Kore). "
            "Falls back to that provider's preset voice if omitted."
        ),
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "TTS model. ElevenLabs: eleven_flash_v2_5 (default, ~75ms), "
            "eleven_flash_v2, eleven_multilingual_v2 (most lifelike), "
            "eleven_v3 (most advanced), eleven_v3_conversational (expressive "
            "realtime), eleven_turbo_v2_5 (DEPRECATED -> eleven_flash_v2_5). "
            "OpenAI: gpt-4o-mini-tts, tts-1, tts-1-hd. "
            "Gemini: gemini-3.8-flash-tts, gemini-3.8-flash-lite-tts, "
            "gemini-3.1-flash-tts-preview (legacy)."
        ),
    ),
    output_format: Optional[str] = Field(
        default="mp3_44100_128",
        description=(
            "Audio format, e.g. mp3_44100_128, mp3_44100_192, wav_44100, "
            "pcm_16000. Gemini TTS only emits PCM, so it returns WAV (or raw "
            "PCM when a pcm_* format is asked for) whatever you request."
        ),
    ),
    instructions: Optional[str] = Field(
        default=None,
        description=(
            "OpenAI gpt-4o-mini-tts only: natural-language tone/emotion/accent "
            "control, e.g. 'Speak in a warm, reassuring tone with pauses.' "
            "Ignored by tts-1/tts-1-hd and ElevenLabs."
        ),
    ),
    speed: Optional[float] = Field(
        default=None,
        ge=0.25,
        le=4.0,
        description=(
            "OpenAI tts-1 / tts-1-hd playback speed (0.25-4.0, default 1.0). "
            "gpt-4o-mini-tts ignores it (put pace in instructions). For "
            "ElevenLabs use voice_settings.speed instead."
        ),
    ),
    voice_settings: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "ElevenLabs only: fine voice tuning, e.g. {\"stability\":0.5,"
            "\"similarity_boost\":0.75,\"style\":0.0,\"use_speaker_boost\":true,"
            "\"speed\":1.0}. Ignored by OpenAI TTS."
        ),
    ),
    language_code: Optional[str] = Field(
        default=None,
        description=(
            "ElevenLabs + Gemini: force output language (ISO-639-1, e.g. "
            "'ja','zh'). Enforced on ElevenLabs turbo/flash v2.5. OpenAI "
            "auto-detects, so it ignores this."
        ),
    ),
    seed: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "ElevenLabs only: best-effort determinism (same seed+text+settings -> "
            "same audio). 0..4294967295. Ignored by OpenAI."
        ),
    ),
    previous_text: Optional[str] = Field(
        default=None,
        description=(
            "ElevenLabs only: text preceding this chunk, for prosody continuity "
            "across stitched segments. Ignored by OpenAI."
        ),
    ),
    next_text: Optional[str] = Field(
        default=None,
        description=(
            "ElevenLabs only: text following this chunk, for prosody continuity. "
            "Ignored by OpenAI."
        ),
    ),
    apply_text_normalization: Optional[str] = Field(
        default=None,
        description=(
            "ElevenLabs only: 'auto' | 'on' | 'off' — spell out numbers/dates/"
            "abbreviations. 'on' unsupported on turbo/flash v2.5. Ignored by OpenAI."
        ),
    ),
    enable_logging: Optional[bool] = Field(
        default=None,
        description=(
            "ElevenLabs only: false enables zero-retention mode (request not "
            "stored; needs an eligible plan). Ignored by OpenAI."
        ),
    ),
) -> dict[str, Any]:
    """
    Synthesize speech from text via ElevenLabs, OpenAI or Gemini TTS.

    Saves the audio under storage/audio/<date>/ and returns the file path plus
    metadata (including model_status / model_shutdown). ElevenLabs needs
    PROVIDERS__ELEVENLABS__API_KEY; OpenAI TTS (gpt-4o-mini-tts / tts-1*)
    reuses the OpenAI key; Gemini TTS (gemini-3.8-flash-tts and friends) uses
    the Gemini Developer-API key and returns WAV.

    Deprecated but still callable: eleven_turbo_v2_5 (use eleven_flash_v2_5).
    """
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    try:
        return await server_ctx.jobs.run(
            "synthesize",
            server_ctx.speech_tool.synthesize(
            text=text,
            voice=voice,
            model=model,
            output_format=output_format or "mp3_44100_128",
            instructions=instructions,
            speed=speed,
            voice_settings=voice_settings,
            language_code=language_code,
            seed=seed,
            previous_text=previous_text,
            next_text=next_text,
            apply_text_normalization=apply_text_normalization,
            enable_logging=enable_logging,
            ),
        )
    except Exception as e:
        logger.error(f"Speech synthesis failed: {e}", exc_info=True)
        raise


# ``Literal`` is imported here, beside the music tools, rather than added to
# the module-level typing import, so this whole section stays a self-contained
# diff while other tool sections are edited in parallel. Fold it into the
# top-of-file import once those branches have merged.


@mcp.tool(
    title="Generate Music",
    description=(
        "Generate music from a text description. Two backends, chosen by "
        "'model': ElevenLabs Music (music_v1 / music_v2 / music_v2_5, "
        "synchronous) reuses PROVIDERS__ELEVENLABS__API_KEY; Suno via kie.ai "
        "(V6 / V6_MINI / V6_WILD, supports vocals and custom mode) needs "
        "PROVIDERS__KIE__API_KEY. Saves an audio file and returns its path. "
        "To transform an existing track use edit_music; for lyrics use "
        "music_lyrics; for WAV/MP4 rendering use music_utility."
    ),
)
async def generate_music(
    prompt: str = Field(
        ...,
        description=(
            "Description of the music (or the lyrics, in Suno custom mode), e.g. "
            "'a dreamy lo-fi hip-hop beat for late-night studying'."
        ),
        min_length=1,
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "Model/backend. ElevenLabs: music_v1 (default), music_v2, "
            "music_v2_5 (highest quality). Suno (via kie.ai): V6 (default), "
            "V6_MINI (fast/lightweight), V6_WILD (experimental). The older Suno "
            "ids V4, V4_5, V4_5PLUS, V4_5ALL, V5 and V5_5 are discontinued "
            "upstream: still accepted for old presets, but they log a warning "
            "and may stop working without notice. Omit to use the first "
            "configured backend."
        ),
    ),
    instrumental: bool = Field(
        default=False,
        description="If true, generate instrumental music with no vocals.",
    ),
    output_format: Optional[str] = Field(
        default="mp3",
        description=(
            "ElevenLabs only: e.g. mp3_44100_128, mp3_44100_192, pcm_44100. "
            "Suno always returns mp3."
        ),
    ),
    music_length_ms: Optional[int] = Field(
        default=None,
        ge=3000,
        le=600000,
        description=(
            "ElevenLabs only: total length in milliseconds (3000-600000). "
            "Ignored by Suno."
        ),
    ),
    style: Optional[str] = Field(
        default=None,
        description=(
            "Suno custom mode only: genre/mood, e.g. 'melodic techno, 120 BPM'. "
            "Required together with title when custom_mode=true."
        ),
    ),
    title: Optional[str] = Field(
        default=None,
        description="Suno custom mode only: song title (max 80 chars).",
    ),
    custom_mode: bool = Field(
        default=False,
        description=(
            "Suno only: enable custom mode for precise style/title control "
            "(requires style and title)."
        ),
    ),
    vocal_gender: Optional[str] = Field(
        default=None,
        description="Suno custom mode only: 'm' or 'f' vocal gender preference.",
    ),
    negative_tags: Optional[str] = Field(
        default=None,
        description=(
            "Suno only: styles/traits to avoid, e.g. 'heavy metal, distortion'."
        ),
    ),
) -> dict[str, Any]:
    """
    Generate music from a text description via ElevenLabs Music or Suno (kie.ai).

    Saves the audio under storage/music/<date>/ and returns the file path plus
    metadata. ElevenLabs is synchronous; Suno is polled to completion internally,
    so a single call blocks until the track is ready (or times out).
    """
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    try:
        return await server_ctx.jobs.run(
            "generate",
            server_ctx.music_generation_tool.generate(
            prompt=prompt,
            model=model,
            instrumental=instrumental,
            output_format=output_format or "mp3",
            music_length_ms=music_length_ms,
            style=style,
            title=title,
            custom_mode=custom_mode,
            vocal_gender=vocal_gender,
            negative_tags=negative_tags,
            ),
        )
    except Exception as e:
        logger.error(f"Music generation failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Edit Music",
    description=(
        "Transform existing audio with Suno via kie.ai (requires "
        "PROVIDERS__KIE__API_KEY). Pick one 'action'; each needs a different "
        "subset of the parameters below.\n"
        "- extend: lengthen a track from an earlier generate_music call. "
        "Needs audio_id. With custom_mode=true it also needs prompt, style, "
        "title and continue_at; with custom_mode=false it inherits the source "
        "track's settings.\n"
        "- cover: re-style an uploaded audio file into a new track. Needs "
        "upload_url and prompt. With custom_mode=true it also needs style and "
        "title.\n"
        "- upload_extend: lengthen an uploaded audio file (one Suno did not "
        "generate). Needs upload_url. With custom_mode=true it also needs "
        "prompt, style, title and continue_at.\n"
        "- add_instrumental: add instrumental backing to an uploaded file. "
        "Needs upload_url, title, tags and negative_tags.\n"
        "- add_vocals: sing over an uploaded instrumental. Needs upload_url, "
        "prompt, title, style and negative_tags.\n"
        "- separate_vocals: split a generated track into stems. Needs task_id "
        "and audio_id from that generation, plus separation_type.\n"
        "upload_url must be a publicly reachable audio URL of at most 8 "
        "minutes. Saves the result(s) and returns the file path(s)."
    ),
)
async def edit_music(
    action: Literal[
        "extend",
        "cover",
        "upload_extend",
        "add_instrumental",
        "add_vocals",
        "separate_vocals",
    ] = Field(
        ...,
        description=(
            "Which transformation to run. See the tool description for the "
            "parameters each action requires."
        ),
    ),
    audio_id: Optional[str] = Field(
        default=None,
        description=(
            "Required for extend and separate_vocals: audio_id of a track from "
            "a prior generate_music / edit_music call."
        ),
    ),
    task_id: Optional[str] = Field(
        default=None,
        description=(
            "Required for separate_vocals: task_id from the generation that "
            "produced audio_id."
        ),
    ),
    upload_url: Optional[str] = Field(
        default=None,
        description=(
            "Required for cover, upload_extend, add_instrumental and "
            "add_vocals: public URL of the source audio (<=8 min)."
        ),
    ),
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Required for cover and add_vocals (description, or lyrics in "
            "custom mode). Also required by extend / upload_extend when "
            "custom_mode=true."
        ),
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "Suno model. V6 (default), V6_MINI, V6_WILD. add_instrumental and "
            "add_vocals accept only the V6 family (plus the discontinued "
            "V4_5PLUS / V5 / V5_5). The other discontinued ids (V4, V4_5, "
            "V4_5ALL) still work for the remaining actions but log a warning."
        ),
    ),
    custom_mode: bool = Field(
        default=False,
        description=(
            "extend / upload_extend / cover only: take manual control of "
            "prompt, style, title (and continue_at for the extend actions) "
            "instead of inheriting the source track's settings."
        ),
    ),
    instrumental: bool = Field(
        default=False,
        description="cover / upload_extend only: produce audio with no vocals.",
    ),
    style: Optional[str] = Field(
        default=None,
        description=(
            "Genre/vocal style. Required for add_vocals; required by extend / "
            "upload_extend / cover when custom_mode=true."
        ),
    ),
    title: Optional[str] = Field(
        default=None,
        description=(
            "Title for the result. Required for add_instrumental and "
            "add_vocals; required by extend / upload_extend / cover when "
            "custom_mode=true."
        ),
    ),
    tags: Optional[str] = Field(
        default=None,
        description=(
            "Required for add_instrumental: style tags to include, e.g. "
            "'lo-fi, jazzy'."
        ),
    ),
    negative_tags: Optional[str] = Field(
        default=None,
        description=(
            "Required for add_instrumental and add_vocals: styles to exclude."
        ),
    ),
    continue_at: Optional[float] = Field(
        default=None,
        description=(
            "extend / upload_extend with custom_mode=true: seconds into the "
            "source track to continue from."
        ),
    ),
    separation_type: str = Field(
        default="separate_vocal",
        description=(
            "separate_vocals only: 'separate_vocal' = vocals + accompaniment; "
            "'split_stem' = per-instrument stems (drums, bass, guitar, ...)."
        ),
    ),
) -> dict[str, Any]:
    """Run one Suno transformation on existing audio; save and return the output."""
    server_ctx = get_server_context(mcp.get_context())
    # Validate and route first: the router is synchronous, so a bad call raises
    # here instead of becoming a background job whose error only surfaces later.
    coro = server_ctx.music_generation_tool.edit(
        action=action,
        audio_id=audio_id,
        task_id=task_id,
        upload_url=upload_url,
        prompt=prompt,
        model=model,
        custom_mode=custom_mode,
        instrumental=instrumental,
        style=style,
        title=title,
        tags=tags,
        negative_tags=negative_tags,
        continue_at=continue_at,
        separation_type=separation_type,
    )
    try:
        return await server_ctx.jobs.run(action, coro)
    except Exception as e:
        logger.error(f"edit_music({action}) failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Music Lyrics",
    description=(
        "Song lyrics via Suno (kie.ai); requires PROVIDERS__KIE__API_KEY. "
        "Pick one 'action':\n"
        "- generate: write lyrics from a theme/mood/style. Needs prompt "
        "(<=200 chars). Returns the text and also saves it as a .txt file.\n"
        "- timestamped: word-level timings (alignedWords) plus waveform data "
        "for a track you already generated. Needs task_id and audio_id from "
        "that generation. Synchronous — no lyrics are written."
    ),
)
async def music_lyrics(
    action: Literal["generate", "timestamped"] = Field(
        ...,
        description=(
            "'generate' writes new lyrics from a prompt; 'timestamped' returns "
            "word-level timing for an existing track (task_id + audio_id)."
        ),
    ),
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Required for action='generate': theme/mood/style of the lyrics "
            "(<=200 chars)."
        ),
        max_length=200,
    ),
    task_id: Optional[str] = Field(
        default=None,
        description=(
            "Required for action='timestamped': taskId from a prior "
            "generate_music / edit_music call."
        ),
    ),
    audio_id: Optional[str] = Field(
        default=None,
        description=(
            "Required for action='timestamped': audio_id of the specific track."
        ),
    ),
) -> dict[str, Any]:
    """Generate lyrics, or fetch word-level timings for an existing track."""
    server_ctx = get_server_context(mcp.get_context())
    coro = server_ctx.music_generation_tool.lyrics(
        action=action, prompt=prompt, task_id=task_id, audio_id=audio_id
    )
    try:
        return await server_ctx.jobs.run(f"lyrics_{action}", coro)
    except Exception as e:
        logger.error(f"music_lyrics({action}) failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Music Utility",
    description=(
        "Render a Suno track you already generated into another format "
        "(requires PROVIDERS__KIE__API_KEY). Both actions need task_id and "
        "audio_id from that generation.\n"
        "- convert_to_wav: high-quality WAV file.\n"
        "- create_music_video: MP4 music video; optionally author (signature "
        "on the cover, <=50 chars) and domain_name (watermark at the bottom, "
        "<=50 chars).\n"
        "Saves the file and returns its path."
    ),
)
async def music_utility(
    action: Literal["convert_to_wav", "create_music_video"] = Field(
        ...,
        description=(
            "'convert_to_wav' renders a WAV; 'create_music_video' renders an "
            "MP4 video. Both need task_id + audio_id."
        ),
    ),
    task_id: Optional[str] = Field(
        default=None,
        description=(
            "Required: taskId from a prior generate_music / edit_music call."
        ),
    ),
    audio_id: Optional[str] = Field(
        default=None, description="Required: audio_id of the specific track."
    ),
    author: Optional[str] = Field(
        default=None,
        description=(
            "create_music_video only: signature on the cover (<=50 chars)."
        ),
    ),
    domain_name: Optional[str] = Field(
        default=None,
        description=(
            "create_music_video only: watermark at the bottom (<=50 chars)."
        ),
    ),
) -> dict[str, Any]:
    """Convert a generated track to WAV, or render it as an MP4 music video."""
    server_ctx = get_server_context(mcp.get_context())
    coro = server_ctx.music_generation_tool.utility(
        action=action,
        task_id=task_id,
        audio_id=audio_id,
        author=author,
        domain_name=domain_name,
    )
    try:
        return await server_ctx.jobs.run(action, coro)
    except Exception as e:
        logger.error(f"music_utility({action}) failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Compose Music",
    description=(
        "ElevenLabs composition-plan workflow — section-level control over a "
        "song (requires PROVIDERS__ELEVENLABS__API_KEY, paid plan). Models: "
        "music_v1 (default), music_v2, music_v2_5.\n"
        "- create_plan: turn a prompt into an editable composition plan "
        "(sections, styles, lyrics, durations) and return it as JSON. Needs "
        "prompt; optional music_length_ms and source_composition_plan (pass an "
        "existing plan to revise it).\n"
        "- compose: render audio and return it together with the plan used and "
        "song metadata. Needs exactly one of prompt or composition_plan (an "
        "edited plan from create_plan).\n"
        "For a plain one-shot track use generate_music instead."
    ),
)
async def compose_music(
    action: Literal["create_plan", "compose"] = Field(
        ...,
        description=(
            "'create_plan' returns an editable plan (JSON, no audio); "
            "'compose' renders audio with detailed metadata."
        ),
    ),
    prompt: Optional[str] = Field(
        default=None,
        description=(
            "Required for create_plan. For compose it is one of the two "
            "mutually exclusive inputs (prompt or composition_plan)."
        ),
    ),
    composition_plan: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "compose only: a plan from action='create_plan', optionally edited. "
            "Mutually exclusive with prompt."
        ),
    ),
    source_composition_plan: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "create_plan only: an existing plan to revise instead of starting "
            "from scratch."
        ),
    ),
    model: Optional[str] = Field(
        default=None,
        description="music_v1 (default), music_v2, or music_v2_5.",
    ),
    instrumental: bool = Field(
        default=False,
        description="compose only: instrumental, no vocals (prompt mode).",
    ),
    output_format: Optional[str] = Field(
        default="mp3",
        description=(
            "compose only: e.g. mp3_44100_128, mp3_44100_192, pcm_44100."
        ),
    ),
    music_length_ms: Optional[int] = Field(
        default=None,
        ge=3000,
        le=600000,
        description="Total length in ms (prompt mode, both actions).",
    ),
    with_timestamps: bool = Field(
        default=False,
        description="compose only: include word-level timing in the metadata.",
    ),
) -> dict[str, Any]:
    """Create or render an ElevenLabs composition plan."""
    server_ctx = get_server_context(mcp.get_context())
    coro = server_ctx.music_generation_tool.compose(
        action=action,
        prompt=prompt,
        composition_plan=composition_plan,
        source_composition_plan=source_composition_plan,
        model=model,
        instrumental=instrumental,
        output_format=output_format or "mp3",
        music_length_ms=music_length_ms,
        with_timestamps=with_timestamps,
    )
    try:
        return await server_ctx.jobs.run(f"compose_{action}", coro)
    except Exception as e:
        logger.error(f"compose_music({action}) failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="Edit Image",
    description=(
        "Edit existing images using multiple AI models (OpenAI, Gemini, etc.) "
        "with text instructions"
    ),
)
async def edit_image(
    image_data: Optional[str] = Field(
        default=None,
        description=(
            "Base64 encoded image or data URL. Optional when image_path is "
            "given."
        ),
    ),
    prompt: str = Field(
        ...,
        description="Text instructions for editing the image",
        min_length=1,
        max_length=4000,
    ),
    image_path: Optional[str] = Field(
        default=None,
        description=(
            "Local filesystem path to the source image — preferred over "
            "image_data on STDIO transport (server shares the client's "
            "filesystem): full-resolution reference with no base64 inlining. "
            "Ignored if image_data is provided."
        ),
    ),
    mask_data: Optional[str] = Field(
        default=None,
        description="Optional base64 encoded mask image for targeted editing",
    ),
    model: Optional[str] = Field(
        default=None,
        description=(
            "Edit model. OpenAI: gpt-image-2.5-sunburst / gpt-image-2.5-flare "
            "(top of the edit leaderboard), gpt-image-2 (custom/large sizes), "
            "gpt-image-1.5 / gpt-image-1-mini (input_fidelity), gpt-image-1 "
            "(shuts down 2026-10-23). Google: gemini-3.1-flash-image "
            "(nano-banana-2), gemini-3-pro-image (nano-banana-pro) — reference "
            "images via additional_images, no mask. If omitted, uses the "
            "configured default image model (IMAGES__DEFAULT_MODEL, gpt-image-2)."
        ),
    ),
    size: Optional[str] = Field(
        default="auto",
        description="Output image size: 1024x1024, 1536x1024, or 1024x1536",
    ),
    quality: Optional[str] = Field(
        default="auto", description="Image quality: auto, high, medium, or low"
    ),
    output_format: Optional[str] = Field(
        default="png", description="Output format: png, jpeg, or webp"
    ),
    compression: Optional[int] = Field(
        default=100, ge=0, le=100, description="Compression level for JPEG/WebP (0-100)"
    ),
    background: Optional[str] = Field(
        default="auto", description="Background type: auto, transparent, or opaque"
    ),
    input_fidelity: Optional[str] = Field(
        default=None,
        description=(
            "'high' keeps the source image's faces/style/identity close to the "
            "original (best for inpainting/edits). gpt-image-1 family only "
            "(gpt-image-1 / 1.5 / 1-mini); ignored by gpt-image-2."
        ),
    ),
    additional_images: Optional[list[str]] = Field(
        default=None,
        description=(
            "Extra reference images (base64 / data URLs) to composite alongside "
            "image_data — gpt-image edits accept up to 16 total. A mask applies "
            "to the first image only."
        ),
    ),
    additional_image_paths: Optional[list[str]] = Field(
        default=None,
        description=(
            "Local filesystem paths for extra reference images (STDIO "
            "transport); appended after additional_images."
        ),
    ),
    user: Optional[str] = Field(
        default=None,
        description="OpenAI only: stable end-user identifier for abuse monitoring.",
    ),
) -> dict[str, Any]:
    """
    Edit an existing image with text instructions.

    Returns a dictionary containing:
    - task_id: Unique identifier for this editing task
    - image_id: Unique identifier for the edited image
    - image_url: Image access URL (format depends on transport and configuration):
      * STDIO transport: file:// URL for local file access
      * HTTP transport: http:// URL to MCP server endpoint
      * With base_host: full CDN/nginx URL with date path structure
    - resource_uri: MCP resource URI for future access
    - operation: "edit" to indicate this was an edit operation
    - metadata: Edit details and parameters
    """
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    # Validate inputs. The source image comes from inline data or a local
    # file path (STDIO transport shares the client's filesystem).
    if image_data:
        validated_image_data = validate_base64_image(image_data)
    elif image_path:
        validated_image_data = load_image_file_as_data_url(image_path)
    else:
        raise ValueError("Provide either image_data or image_path")
    validated_prompt = sanitize_prompt(prompt)
    validated_mask_data = validate_base64_image(mask_data) if mask_data else None
    validated_size = validate_image_size(size)
    validated_quality = validate_image_quality(quality)
    validated_output_format = validate_output_format(output_format)
    validated_compression = validate_compression(compression)
    validated_background = validate_background_type(background)
    validated_extra_images = [
        validate_base64_image(img) for img in (additional_images or [])
    ]
    validated_extra_images.extend(
        load_image_file_as_data_url(p) for p in (additional_image_paths or [])
    )
    validated_extra_images = validated_extra_images or None

    try:
        return await server_ctx.jobs.run(
            "edit_image",
            server_ctx.image_editing_tool.edit(
                image_data=validated_image_data,
                prompt=validated_prompt,
                mask_data=validated_mask_data,
                model=model,
                size=validated_size,
                quality=validated_quality,
                output_format=validated_output_format,
                compression=validated_compression,
                background=validated_background,
                input_fidelity=input_fidelity,
                additional_images=validated_extra_images,
                user=user,
            ),
        )
    except Exception as e:
        logger.error(f"Image editing failed: {e}", exc_info=True)
        raise


@mcp.tool(
    title="List Available Models",
    description=(
        "List every model OmniAPI can call, across all modalities (text, image, "
        "transcription, speech, music), with provider, online status, pricing, "
        "deprecation/shutdown info and the tier aliases (cheap/standard/strong). "
        "Rosters are live: providers are asked what is online at startup. "
        "Filter with modality='text' etc."
    ),
)
async def list_available_models(
    modality: Optional[str] = Field(
        default=None,
        description=(
            "Restrict to one modality: text | image | transcription | speech | "
            "music. Omit for everything."
        ),
    ),
    include_retired: bool = Field(
        default=False,
        description="Also list models the provider has retired (shut down).",
    ),
    include_snapshots: bool = Field(
        default=False,
        description=(
            "Also list dated snapshot ids (e.g. gpt-5.5-2026-04-23). Hidden by "
            "default; they stay callable."
        ),
    ),
    refresh: bool = Field(
        default=False,
        description="Force a fresh discovery round instead of the 24h cache.",
    ),
) -> dict[str, Any]:
    """Return the model catalog (curated overlay merged with live discovery)."""
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)
    try:
        if refresh:
            await catalog.refresh(server_ctx.settings, force=True)
        snap = catalog.snapshot(include_retired=include_retired, modality=modality)
        if include_snapshots:
            snap["models"] = {
                mod: [
                    e.to_dict()
                    for e in catalog.models(
                        modality=mod,
                        include_retired=include_retired,
                        include_snapshots=True,
                    )
                ]
                for mod in snap["models"]
            }
            snap["counts"] = {k: len(v) for k, v in snap["models"].items()}

        # Image models carry request-shape capabilities from the providers
        # (sizes, qualities, formats) that the catalog does not duplicate.
        if modality in (None, "image"):
            await server_ctx.image_generation_tool.ensure_providers_registered()
            registry = server_ctx.image_generation_tool.provider_registry
            caps: dict[str, Any] = {}
            for model_id in registry.get_supported_models():
                info = registry.get_model_info(model_id)
                if not info:
                    continue
                c = info["capabilities"]
                caps[model_id] = {
                    "provider": info["provider"],
                    "sizes": c.supported_sizes,
                    "qualities": c.supported_qualities,
                    "formats": c.supported_formats,
                    "max_images": c.max_images_per_request,
                    "supports_background": c.supports_background,
                    "features": c.custom_parameters,
                }
            snap["image_capabilities"] = caps
            snap["default_image_model"] = server_ctx.settings.images.default_model

        snap["configured_providers"] = {
            "text": server_ctx.text_tool.available_providers(),
            "image": server_ctx.image_generation_tool.get_available_providers(),
        }
        snap["default_text_model"] = server_ctx.text_tool._default_model()
        return snap
    except Exception as e:
        logger.error(f"Failed to list available models: {e}", exc_info=True)
        return {"error": str(e), "models": {}, "counts": {}}


@mcp.resource(
    "generated-images://{image_id}",
    name="get_generated_image",
    title="Generated Image Access",
    description=(
        "Access a specific generated image by its unique identifier. "
        "Returns the full image data as a base64-encoded data URL for MCP "
        "resource access."
    ),
    mime_type="text/plain",
)
async def get_generated_image(
    image_id: str = Field(..., description="Unique image identifier"),
) -> str:
    """Access a generated image by its unique ID."""
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)
    return await server_ctx.resource_manager.get_image_resource(image_id)


@mcp.resource(
    "image-history://recent/{limit}/{days}",
    name="get_recent_images",
    title="Image Generation History",
    description=(
        "Retrieve recent image generation history with customizable limits and "
        "time range. Returns JSON with image metadata, generation parameters, "
        "and access URIs."
    ),
    mime_type="application/json",
)
async def get_recent_images(
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of images to return (1-100)",
    ),
    days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="Number of days to look back (1-365)",
    ),
) -> str:
    """Get recent image generation history."""
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    # Validate parameters
    validated_limit = validate_limit(limit, 100)
    validated_days = validate_days(days, 365)

    return await server_ctx.resource_manager.get_recent_images(
        limit=validated_limit, days=validated_days
    )


@mcp.resource(
    "storage-stats://overview",
    name="get_storage_stats",
    title="Storage Statistics",
    description=(
        "Get comprehensive storage usage statistics including total images stored, "
        "disk usage, cache status, and cleanup information."
    ),
    mime_type="application/json",
)
async def get_storage_stats() -> str:
    """Get storage statistics and management information."""
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)
    return await server_ctx.resource_manager.get_storage_stats()


@mcp.resource(
    "model-info://{model_id}",
    name="get_model_info",
    title="Model Documentation",
    description=(
        "Complete API documentation for AI models including capabilities, pricing, "
        "rate limits, available resources, and usage examples. Support for multiple "
        "models via model_id parameter."
    ),
    mime_type="text/markdown",
)
async def get_model_info(
    model_id: str = Field(
        ..., description="Model identifier (e.g., 'gpt-image-1', 'dalle-3')"
    ),
) -> str:
    """Get model capabilities and pricing information for specified model."""
    return await model_registry.get_model_documentation(model_id)


@mcp.resource(
    "models://list",
    name="list_models",
    title="Available Models",
    description=(
        "List all available AI models with their basic information and capabilities."
    ),
    mime_type="application/json",
)
async def list_models() -> str:
    """List all available AI models."""
    import json

    models = []
    for model_id in await model_registry.list_models():
        model_info = await model_registry.get_model_info(model_id)
        if model_info:
            models.append(
                {
                    "model_id": model_info.model_id,
                    "name": model_info.name,
                    "version": model_info.version,
                    "capabilities": model_info.capabilities,
                    "resource_uri": f"model-info://{model_id}",
                }
            )

    return json.dumps(
        {
            "models": models,
            "total": len(models),
            "usage": {
                "description": (
                    "Use model-info://{model_id} to get detailed information about "
                    "specific models"
                ),
                "example": "model-info://gpt-image-1",
            },
        },
        indent=2,
    )


@mcp.resource(
    "prompt-templates://list",
    name="list_prompt_templates",
    title="Available Prompt Templates",
    description=(
        "List all available prompt templates with their categories, descriptions, "
        "and usage information. Templates are organized by category for easy discovery."
    ),
    mime_type="application/json",
)
async def list_prompt_templates() -> str:
    """List all available prompt templates.

    This resource provides discovery and documentation for templates.
    Users can browse available templates and understand their parameters
    before using them with mcp.prompt functions.
    """
    import json

    return json.dumps(prompt_template_resource_manager.list_templates(), indent=2)


@mcp.resource(
    "prompt-templates://{template_name}",
    name="get_prompt_template",
    title="Prompt Template Details",
    description=(
        "Get detailed information about a specific prompt template including "
        "parameters, examples, and usage instructions. Returns comprehensive "
        "documentation for the template."
    ),
    mime_type="application/json",
)
async def get_prompt_template(
    template_name: str = Field(
        ...,
        description=(
            "ID of the prompt template (e.g., 'creative_image', 'product_photography')"
        ),
    ),
) -> str:
    """Get detailed information about a specific prompt template."""
    import json

    template_details = prompt_template_resource_manager.get_template_details(
        template_name
    )

    if template_details is None:
        # Return helpful error message with suggestions
        error_response = (
            prompt_template_resource_manager.get_template_not_found_response(
                template_name
            )
        )
        return json.dumps(error_response, indent=2)

    return json.dumps(template_details, indent=2)


# ===================================================================
# MCP PROMPT TEMPLATES - Direct Image Generation
# ===================================================================
#
# All prompt functions now directly generate images instead of
# returning prompt messages. This provides a complete end-to-end
# workflow where users input parameters and receive generated images.
#
# The system uses templates.json to define prompts and automatically
# generates parameter definitions to avoid manual errors.
# ===================================================================


async def _generate_from_template(template_id: str, **kwargs) -> dict[str, Any]:
    """Helper function to generate images from templates.

    Args:
        template_id: ID of the template to use
        **kwargs: Template parameters

    Returns:
        Image generation result with template information
    """
    # Get server context
    ctx = mcp.get_context()
    server_ctx = get_server_context(ctx)

    # Render the template
    prompt_text, metadata = template_manager.render_template(template_id, **kwargs)

    # Generate the image with template information
    result = await server_ctx.image_generation_tool.generate(
        prompt=prompt_text,
        quality=metadata.get("quality", "high"),
        size=metadata.get("recommended_size", "1024x1024"),
        style=metadata.get("style", "vivid"),
    )

    # Add template information
    result["template_used"] = template_id
    result["prompt_text"] = prompt_text

    return result


@mcp.prompt(
    name="creative_image",
    title="Creative Image Generation",
    description=(
        "Generate creative images with expert art direction. Combines subject, "
        "artistic style, mood, and color palette to create vivid, artistic images."
    ),
)
async def creative_image(
    subject: str = Field(
        ..., description="Main subject of the image - be specific and detailed"
    ),
    style: str = Field(default="digital art", description="Artistic style or medium"),
    setting: str = Field(
        default="dramatic environment", description="Environmental setting or location"
    ),
    mood: str = Field(default="vibrant", description="Desired mood or emotional tone"),
    lighting: str = Field(default="dramatic", description="Lighting style"),
    color_palette: str = Field(
        default="rich and vibrant", description="Color scheme preference"
    ),
    composition: str = Field(default="dynamic", description="Compositional approach"),
) -> dict[str, Any]:
    """Generate a creative image directly from parameters."""
    return await _generate_from_template(
        "creative_image",
        subject=subject,
        style=style,
        setting=setting,
        mood=mood,
        lighting=lighting,
        color_palette=color_palette,
        composition=composition,
    )


@mcp.prompt(
    name="product_photography",
    title="Product Photography",
    description=(
        "Generate professional product photography with commercial specifications. "
        "Optimized for e-commerce, catalogs, and marketing materials."
    ),
)
async def product_photography(
    product: str = Field(
        ..., description="Detailed product description with key features"
    ),
    background: str = Field(
        default="clean white studio",
        description="Background setting with texture details",
    ),
    lighting: str = Field(
        default="soft diffused", description="Professional lighting setup"
    ),
    angle: str = Field(default="hero shot", description="Camera angle and perspective"),
    detail_focus: str = Field(
        default="product features highlighted",
        description="Specific details to emphasize",
    ),
) -> dict[str, Any]:
    """Generate professional product photography directly."""
    return await _generate_from_template(
        "product_photography",
        product=product,
        background=background,
        lighting=lighting,
        angle=angle,
        detail_focus=detail_focus,
    )


@mcp.prompt(
    name="social_media",
    title="Social Media Graphics",
    description=(
        "Generate platform-optimized social media graphics with engagement best "
        "practices."
    ),
)
async def social_media(
    platform: str = Field(..., description="Target social media platform"),
    content_type: str = Field(..., description="Type of social media post"),
    topic: str = Field(..., description="Main topic or subject of the post"),
    brand_style: str = Field(
        default="modern and clean", description="Brand visual aesthetic"
    ),
    visual_elements: str = Field(
        default="geometric shapes and icons",
        description="Specific visual elements to include",
    ),
    color_scheme: str = Field(
        default="brand-aligned", description="Color palette for the design"
    ),
    layout: str = Field(default="balanced", description="Compositional layout"),
    call_to_action: bool = Field(
        default=False, description="Include call-to-action element"
    ),
) -> dict[str, Any]:
    """Generate social media graphics directly."""
    return await _generate_from_template(
        "social_media",
        platform=platform,
        content_type=content_type,
        topic=topic,
        brand_style=brand_style,
        visual_elements=visual_elements,
        color_scheme=color_scheme,
        layout=layout,
        call_to_action=call_to_action,
    )


@mcp.prompt(
    name="artistic_style",
    title="Artistic Style Generation",
    description=(
        "Generate images in specific artistic styles and periods. Emulates famous "
        "artists, art movements, and traditional mediums."
    ),
)
async def artistic_style(
    subject: str = Field(..., description="Main subject with specific details"),
    setting: str = Field(
        default="appropriate to style", description="Environmental context"
    ),
    artist_style: str = Field(
        default="impressionist", description="Specific artist or art movement style"
    ),
    medium: str = Field(default="oil painting", description="Traditional art medium"),
    era: str = Field(
        default="appropriate to style", description="Historical artistic period"
    ),
    atmosphere: str = Field(default="evocative", description="Emotional atmosphere"),
    technique: str = Field(
        default="masterful brushwork", description="Specific artistic technique"
    ),
) -> dict[str, Any]:
    """Generate artistic style images directly."""
    return await _generate_from_template(
        "artistic_style",
        subject=subject,
        setting=setting,
        artist_style=artist_style,
        medium=medium,
        era=era,
        atmosphere=atmosphere,
        technique=technique,
    )


@mcp.prompt(
    name="og_image",
    title="Open Graph Images",
    description=(
        "Generate social media preview images optimized for sharing. Creates "
        "engaging thumbnails for websites and blog posts."
    ),
)
async def og_image(
    title: str = Field(..., description="Main title text to display prominently"),
    brand_name: Optional[str] = Field(
        default=None, description="Website or brand name"
    ),
    background_style: str = Field(
        default="modern gradient", description="Background visual style"
    ),
    visual_elements: str = Field(
        default="subtle design accents", description="Supporting visual elements"
    ),
    text_layout: str = Field(default="centered", description="Typography arrangement"),
    color_scheme: str = Field(
        default="professional", description="Color palette theme"
    ),
) -> dict[str, Any]:
    """Generate Open Graph images directly."""
    return await _generate_from_template(
        "og_image",
        title=title,
        brand_name=brand_name,
        background_style=background_style,
        visual_elements=visual_elements,
        text_layout=text_layout,
        color_scheme=color_scheme,
    )


@mcp.prompt(
    name="blog_header",
    title="Blog Header Images",
    description=(
        "Generate header images for blog posts and articles with optional space "
        "for text overlays."
    ),
)
async def blog_header(
    topic: str = Field(..., description="Blog post topic or main theme"),
    style: str = Field(default="modern editorial", description="Visual design style"),
    visual_metaphor: str = Field(
        default="abstract concept visualization",
        description="Visual representation of the topic",
    ),
    mood: str = Field(default="engaging", description="Emotional tone"),
    lighting: str = Field(
        default="bright and optimistic", description="Lighting atmosphere"
    ),
    color_palette: str = Field(default="complementary", description="Color scheme"),
    include_text_space: bool = Field(
        default=True, description="Reserve space for text overlay"
    ),
) -> dict[str, Any]:
    """Generate blog header images directly."""
    return await _generate_from_template(
        "blog_header",
        topic=topic,
        style=style,
        visual_metaphor=visual_metaphor,
        mood=mood,
        lighting=lighting,
        color_palette=color_palette,
        include_text_space=include_text_space,
    )


@mcp.prompt(
    name="hero_banner",
    title="Website Hero Banners",
    description=(
        "Generate hero section banners for websites with impactful landing page "
        "visuals."
    ),
)
async def hero_banner(
    website_type: str = Field(..., description="Type of website"),
    main_theme: str = Field(
        ..., description="Core theme or main subject of the hero banner"
    ),
    industry: Optional[str] = Field(
        default=None, description="Industry or market sector"
    ),
    message: Optional[str] = Field(
        default=None, description="Key value proposition or message"
    ),
    visual_style: str = Field(
        default="modern professional", description="Design aesthetic approach"
    ),
    hero_elements: str = Field(
        default="abstract technology patterns", description="Main visual elements"
    ),
    atmosphere: str = Field(
        default="innovative and dynamic", description="Overall feeling and mood"
    ),
) -> dict[str, Any]:
    """Generate website hero banners directly."""
    return await _generate_from_template(
        "hero_banner",
        website_type=website_type,
        main_theme=main_theme,
        industry=industry,
        message=message,
        visual_style=visual_style,
        hero_elements=hero_elements,
        atmosphere=atmosphere,
    )


@mcp.prompt(
    name="thumbnail",
    title="Video Thumbnails",
    description=(
        "Generate engaging thumbnails for video content optimized for high "
        "click-through rates."
    ),
)
async def thumbnail(
    content_type: str = Field(..., description="Type of video content"),
    topic: str = Field(..., description="Specific video topic or subject"),
    style: str = Field(default="bold and dynamic", description="Visual design style"),
    focal_element: str = Field(
        default="eye-catching central subject", description="Main visual focus"
    ),
    emotion: str = Field(default="exciting", description="Emotional hook"),
    color_scheme: str = Field(
        default="vibrant high-contrast", description="Color approach for visibility"
    ),
) -> dict[str, Any]:
    """Generate video thumbnails directly."""
    return await _generate_from_template(
        "thumbnail",
        content_type=content_type,
        topic=topic,
        style=style,
        focal_element=focal_element,
        emotion=emotion,
        color_scheme=color_scheme,
    )


@mcp.prompt(
    name="infographic",
    title="Infographic Images",
    description=(
        "Generate information graphics and data visualizations that effectively "
        "communicate complex data."
    ),
)
async def infographic(
    data_type: str = Field(..., description="Type of data or information"),
    topic: str = Field(..., description="Subject matter of the infographic"),
    visual_approach: str = Field(
        default="modern clean", description="Design style approach"
    ),
    chart_types: str = Field(
        default="mixed visualization elements",
        description="Types of data visualizations",
    ),
    layout: str = Field(
        default="vertical flow", description="Information organization"
    ),
    color_scheme: str = Field(
        default="professional palette", description="Color coding approach"
    ),
) -> dict[str, Any]:
    """Generate infographic images directly."""
    return await _generate_from_template(
        "infographic",
        data_type=data_type,
        topic=topic,
        visual_approach=visual_approach,
        chart_types=chart_types,
        layout=layout,
        color_scheme=color_scheme,
    )


@mcp.prompt(
    name="email_header",
    title="Email Newsletter Headers",
    description=(
        "Generate header images for email newsletters with branded designs and "
        "seasonal themes."
    ),
)
async def email_header(
    newsletter_type: str = Field(..., description="Type of newsletter content"),
    main_topic: str = Field(
        ..., description="Main topic or focus of this newsletter edition"
    ),
    brand_name: Optional[str] = Field(
        default=None, description="Company or brand name"
    ),
    theme: Optional[str] = Field(
        default=None, description="Newsletter theme or campaign"
    ),
    season: Optional[str] = Field(default=None, description="Seasonal context"),
    visual_style: str = Field(
        default="clean and modern", description="Design aesthetic"
    ),
    header_elements: str = Field(
        default="brand elements and patterns", description="Visual components"
    ),
) -> dict[str, Any]:
    """Generate email newsletter headers directly."""
    return await _generate_from_template(
        "email_header",
        newsletter_type=newsletter_type,
        main_topic=main_topic,
        brand_name=brand_name,
        theme=theme,
        season=season,
        visual_style=visual_style,
        header_elements=header_elements,
    )


@mcp.prompt(
    name="pencil_drawing",
    title="Drawing Reference Generator",
    description=(
        "Generate clear structural references an artist can use to pencil draw. "
        "Creates clean line art and construction references that artists can use. "
        "Shows form, proportions, and structure clearly."
    ),
)
async def pencil_drawing(
    subject: str = Field(
        ...,
        description=(
            "What you want to draw - creates clear structural reference material"
        ),
        examples=[
            "cat",
            "human hand",
            "standing figure",
            "geometric shapes",
            "still life objects",
            "draped fabric",
            "tree",
            "portrait head",
        ],
    ),
    complexity_level: str = Field(
        default="moderate",
        description="Amount of structural detail to show",
        examples=["simple", "moderate", "detailed", "complex"],
    ),
    study_type: str = Field(
        default="observational",
        description="Type of reference needed",
        examples=[
            "observational",
            "gesture",
            "contour",
            "value",
            "form",
            "anatomy",
            "proportions",
        ],
    ),
    drawing_style: str = Field(
        default="clean line art with clear structure",
        description="Type of reference material that shows form clearly",
        examples=[
            "clean line art with clear structure",
            "construction breakdown showing basic shapes",
            "contour drawing with essential edges",
            "simplified form study with proportions",
        ],
    ),
    form_clarity: str = Field(
        default="clear proportions and structure",
        description="What structural information to emphasize",
        examples=[
            "clear proportions and structure",
            "basic shape construction",
            "form and volume relationships",
            "essential contours and edges",
        ],
    ),
    composition: str = Field(
        default="standard",
        description="How the subject is framed",
        examples=[
            "close-up detail view",
            "full subject in frame",
            "three-quarter view",
            "standard framing",
        ],
    ),
) -> dict[str, Any]:
    """Generate drawing references an artist can use to draw"""
    return await _generate_from_template(
        "pencil_drawing",
        subject=subject,
        complexity_level=complexity_level,
        study_type=study_type,
        drawing_style=drawing_style,
        form_clarity=form_clarity,
        composition=composition,
    )


@mcp.prompt(
    name="gesture_drawing",
    title="Gesture Drawing Practice",
    description=(
        "Generate clean gesture drawing references for capturing movement and "
        "essence. Creates simplified line drawings perfect for quick gesture "
        "practice and building upon."
    ),
)
async def gesture_drawing(
    subject: str = Field(
        ...,
        description="Subject for gesture practice",
        examples=[
            "figure in motion",
            "dancer in pose",
            "animal running",
            "person sitting",
            "tree in wind",
        ],
    ),
    line_quality: str = Field(
        default="flowing expressive",
        description="Quality and character of lines",
        examples=[
            "flowing expressive",
            "bold confident",
            "loose gestural",
            "varied weight",
        ],
    ),
    capture_focus: str = Field(
        default="overall movement and energy",
        description="What aspect to emphasize",
        examples=[
            "overall movement and energy",
            "weight and balance",
            "rhythm and flow",
            "proportional relationships",
        ],
    ),
    construction_visibility: str = Field(
        default="subtle",
        description="How visible construction and guidelines should be",
        examples=[
            "clearly visible",
            "subtle underlying",
            "minimal structural",
            "no construction lines",
        ],
    ),
    movement_emphasis: str = Field(
        default="natural flow",
        description="Type of movement to emphasize",
        examples=[
            "dynamic action",
            "natural flow",
            "weight shift",
            "directional force",
        ],
    ),
) -> dict[str, Any]:
    """Generate gesture drawing references for movement and essence practice."""
    return await _generate_from_template(
        "gesture_drawing",
        subject=subject,
        line_quality=line_quality,
        capture_focus=capture_focus,
        construction_visibility=construction_visibility,
        movement_emphasis=movement_emphasis,
    )


@mcp.prompt(
    name="shapes_study",
    title="Basic Shapes Form Study",
    description=(
        "Generate clean line drawings of basic geometric shapes for form study. "
        "Perfect for learning 3D construction, volume, and shading fundamentals."
    ),
)
async def shapes_study(
    primary_shape: str = Field(
        ...,
        description="Main geometric form to study",
        examples=[
            "cube",
            "sphere",
            "cylinder",
            "cone",
            "pyramid",
        ],
    ),
    secondary_shapes: str = Field(
        default="none",
        description="Additional shapes for composition",
        examples=[
            "none",
            "smaller cubes",
            "intersecting cylinders",
            "stacked spheres",
        ],
    ),
    lighting_setup: str = Field(
        default="single strong",
        description="Lighting approach for form study",
        examples=[
            "single strong",
            "soft diffused",
            "dramatic directional",
        ],
    ),
    light_direction: str = Field(
        default="upper left",
        description="Direction of primary light source",
        examples=[
            "upper left",
            "upper right",
            "directly above",
            "side lighting",
        ],
    ),
    shading_technique: str = Field(
        default="smooth blending",
        description="Shading method to practice",
        examples=[
            "smooth blending",
            "hatching lines",
            "cross-hatching",
            "stippling dots",
        ],
    ),
    construction_visibility: str = Field(
        default="clearly visible",
        description="How visible construction lines should be",
        examples=[
            "clearly visible",
            "lightly indicated",
            "minimal guidelines",
            "clean finished",
        ],
    ),
    learning_objective: str = Field(
        default="form and volume understanding",
        description="Primary learning goal",
        examples=[
            "form and volume understanding",
            "shading technique",
            "construction method",
            "spatial relationships",
        ],
    ),
) -> dict[str, Any]:
    """Generate basic shapes references for fundamental form study."""
    return await _generate_from_template(
        "shapes_study",
        primary_shape=primary_shape,
        secondary_shapes=secondary_shapes,
        lighting_setup=lighting_setup,
        light_direction=light_direction,
        shading_technique=shading_technique,
        construction_visibility=construction_visibility,
        learning_objective=learning_objective,
    )


@mcp.prompt(
    name="contour_drawing",
    title="Contour Drawing Exercise",
    description=(
        "Generate clean contour line drawings for observation skill development. "
        "Creates simplified line drawings focused on edges and form "
        "relationships."
    ),
)
async def contour_drawing(
    subject: str = Field(
        ...,
        description="Subject for contour drawing practice",
        examples=[
            "still life object",
            "plant with complex leaves",
            "crumpled paper",
            "hand in various positions",
            "household object",
        ],
    ),
    contour_type: str = Field(
        default="modified blind",
        description="Type of contour drawing technique",
        examples=[
            "blind contour",
            "modified blind",
            "pure contour",
            "cross-contour",
        ],
    ),
    line_weight: str = Field(
        default="varied expressive",
        description="Line weight approach",
        examples=[
            "consistent thin",
            "varied expressive",
            "bold confident",
            "delicate precise",
        ],
    ),
    observation_focus: str = Field(
        default="edge relationships",
        description="What to focus observation on",
        examples=[
            "edge relationships",
            "form transitions",
            "negative spaces",
            "surface contours",
        ],
    ),
    drawing_speed: str = Field(
        default="slow deliberate",
        description="Pace of drawing for different learning goals",
        examples=[
            "slow deliberate",
            "moderate steady",
            "quick gestural",
            "varied rhythm",
        ],
    ),
) -> dict[str, Any]:
    """Generate contour drawing references for observation practice."""
    return await _generate_from_template(
        "contour_drawing",
        subject=subject,
        contour_type=contour_type,
        line_weight=line_weight,
        observation_focus=observation_focus,
        drawing_speed=drawing_speed,
    )


@mcp.prompt(
    name="value_study",
    title="Value Study Exercise",
    description=(
        "Generate clean value study references for light and shadow practice. "
        "Creates simplified drawings focused on value relationships and form "
        "rendering."
    ),
)
async def value_study(
    subject: str = Field(
        ...,
        description="Subject for value study",
        examples=[
            "simple still life",
            "single object with strong lighting",
            "geometric forms",
            "draped fabric",
            "portrait head",
        ],
    ),
    shading_method: str = Field(
        default="smooth blending",
        description="Shading technique approach",
        examples=[
            "smooth blending",
            "hatching patterns",
            "stippling dots",
            "block shading",
        ],
    ),
    light_source: str = Field(
        default="single directional",
        description="Lighting setup for value study",
        examples=[
            "single directional",
            "soft window light",
            "dramatic spot light",
            "overcast diffused",
        ],
    ),
    study_emphasis: str = Field(
        default="form definition",
        description="Primary focus of the value study",
        examples=[
            "form definition",
            "light pattern",
            "cast shadows",
            "reflected light",
            "value relationships",
        ],
    ),
    simplification_level: str = Field(
        default="moderate",
        description="How simplified the study should be",
        examples=[
            "highly simplified",
            "moderate detail",
            "refined finish",
            "quick study",
        ],
    ),
) -> dict[str, Any]:
    """Generate value study references for light and shadow practice."""
    return await _generate_from_template(
        "value_study",
        subject=subject,
        shading_method=shading_method,
        light_source=light_source,
        study_emphasis=study_emphasis,
        simplification_level=simplification_level,
    )


def main():
    """Main entry point for FastMCP server."""
    # Parse command line arguments
    args = parse_arguments()

    # Load settings first (pass CLI log level to override)
    app_settings = load_settings(args.config, args.log_level)

    # Configure logging with final log level (from settings, which includes CLI
    # override)
    configure_logging(app_settings.server.log_level)

    logger.info(f"Starting {app_settings.server.name} v{app_settings.server.version}")
    logger.info(f"Transport: {args.transport}")

    # Configure FastMCP settings based on command line arguments
    if args.transport in ["sse", "streamable-http"]:
        logger.info(f"Server will run on {args.host}:{args.port}")

        # Configure host and port through FastMCP settings system
        mcp.settings.host = args.host
        mcp.settings.port = args.port

        # Configure CORS if requested
        if hasattr(args, "cors") and args.cors:
            logger.info("CORS enabled for web deployments")
            # Note: CORS configuration depends on FastMCP implementation
            # This may need adjustment based on actual FastMCP CORS settings

    try:
        # Run server with specified transport
        if args.transport == "stdio":
            logger.info("Running with stdio transport for Claude Desktop integration")
            mcp.run(transport="stdio")
        elif args.transport == "sse":
            logger.info("Running with Server-Sent Events (SSE) transport")
            mcp.run(transport="sse")
        elif args.transport == "streamable-http":
            logger.info("Running with streamable HTTP transport for web deployment")
            mcp.run(transport="streamable-http")
        else:
            logger.error(f"Unsupported transport: {args.transport}")
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Server stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
