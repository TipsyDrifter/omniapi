"""The OmniAPI daemon app: one FastAPI process with three doors.

* ``/mcp``   — the MCP server over streamable-http (what Claude Code connects to)
* ``/api/*`` — REST for the GUI / CLI (status, models, calls, conversations, runs)
* ``/ws``    — WebSocket fan-out of the event bus (live board)
* ``/``      — the GUI (``gui/dist``) when built; a small status page otherwise

Everything shares the process-wide runtime (see ``runtime.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..catalog import catalog, data_home
from ..config.settings import Settings
from ..runtime import runtime

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7788


def pid_file() -> Path:
    return data_home() / "omniapi.pid"


def write_pid(host: str, port: int) -> None:
    pid_file().write_text(
        json.dumps({"pid": os.getpid(), "host": host, "port": port, "started_at": time.time(), "version": __version__}),
        encoding="utf-8",
    )


def read_pid() -> Optional[dict[str, Any]]:
    p = pid_file()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pid() -> None:
    try:
        pid_file().unlink()
    except FileNotFoundError:
        pass


_SKIP_DIRS = {"node_modules", "__pycache__", "$recycle.bin", "system volume information"}


def _subdirs(parent: Path, *, prefix: str = "", limit: int = 200) -> list[str]:
    """Names of visible sub-directories (dot-dirs and build junk skipped), sorted."""
    names: list[str] = []
    try:
        with os.scandir(parent) as it:
            for e in it:
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                n = e.name
                if n.startswith(".") or n.lower() in _SKIP_DIRS:
                    continue
                if prefix and not n.lower().startswith(prefix):
                    continue
                names.append(n)
    except (PermissionError, FileNotFoundError, OSError):
        return []
    names.sort(key=str.lower)
    return names[:limit]


HEARTBEAT_SECONDS = 300


def _in_job() -> Optional[bool]:
    """Is this process inside a Windows job object? (``None`` when unknown.)

    A daemon that lives in someone else's job dies silently when that job is
    closed — no shutdown log, stale pid file. Logged at startup so the next
    silent death can be told apart from a crash.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        result = wintypes.BOOL()
        if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(result)):
            return None
        return bool(result.value)
    except Exception:
        return None


async def _heartbeat() -> None:
    """One log line every few minutes. The daemon logs nothing while idle, so
    without this a silent death (2026-09-29, 2026-09-30) leaves no clue about
    when it happened; with it the last heartbeat bounds the time to minutes."""
    started = time.time()
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        c = runtime.context
        logger.info(
            "heartbeat: up %dm, live runs %s, live chats %s, ws subscribers %s",
            int((time.time() - started) // 60),
            c.runs.live_count if c else "?",
            c.chat.live_count if c and c.chat else 0,
            c.bus.subscriber_count if c else "?",
        )


def create_app(settings: Settings, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> FastAPI:
    # Import late: server.py builds the FastMCP instance at import time.
    from .. import server as mcp_server

    mcp = mcp_server.mcp
    mcp.settings.streamable_http_path = "/"
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mcp_server.settings = settings  # the tool handlers read this module global
        await runtime.acquire(settings, owner="daemon")
        write_pid(host, port)
        async with mcp.session_manager.run():
            logger.info("OmniAPI daemon v%s listening on http://%s:%s (mcp at /mcp)", __version__, host, port)
            logger.info("process: pid=%s ppid=%s in_job=%s exe=%s", os.getpid(), os.getppid(), _in_job(), Path(sys.executable).name)
            heartbeat = asyncio.create_task(_heartbeat(), name="daemon-heartbeat")
            try:
                yield
            finally:
                heartbeat.cancel()
                logger.info("OmniAPI daemon shutting down (pid=%s)", os.getpid())
                clear_pid()
                await runtime.release(owner="daemon")

    app = FastAPI(title="OmniAPI", version=__version__, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

    # ------------------------------------------------------------ helpers
    def ctx():
        c = runtime.context
        if c is None:
            raise HTTPException(503, "runtime not ready")
        return c

    # ------------------------------------------------------------ REST
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        c = runtime.context
        return {"status": "ok" if c else "starting", "version": __version__, "pid": os.getpid(), "ts": time.time()}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        c = ctx()
        await c.image_generation_tool.ensure_providers_registered()
        return {
            "version": __version__,
            "pid": c.pid,
            "mode": c.mode,
            "started_at": c.started_at,
            "uptime_s": round(time.time() - c.started_at, 1),
            "host": host,
            "port": port,
            "providers": {
                "configured": settings.providers.enabled_providers,
                "text": c.text_tool.available_providers(),
                "image": c.image_generation_tool.get_available_providers(),
            },
            "discovery": catalog.discovery_status(),
            "tiers": catalog.tiers,
            "store": await c.store.stats(),
            "bus_subscribers": c.bus.subscriber_count,
            "live_runs": c.runs.live_count,
            "live_chats": c.chat.live_count if c.chat else 0,
            "dev": os.environ.get("OMNIAPI_DEV") == "1",
            "offline": os.environ.get("OMNIAPI_OFFLINE") == "1",
            "data_home": str(data_home()),
        }

    @app.get("/api/models")
    async def models(
        modality: Optional[str] = None,
        include_retired: bool = False,
        include_snapshots: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if refresh:
            await catalog.refresh(settings, force=True)
        snap = catalog.snapshot(include_retired=include_retired, modality=modality)
        if include_snapshots:
            snap["models"] = {
                mod: [e.to_dict() for e in catalog.models(modality=mod, include_retired=include_retired, include_snapshots=True)]
                for mod in snap["models"]
            }
        if os.environ.get("OMNIAPI_DEV") == "1" and isinstance(snap.get("models"), dict) and modality in (None, "text"):
            # development daemon: the echo models (no vendor call, no billing) lead the text list
            echo = [
                {"id": mid, "provider": "echo", "modality": "text", "name": name, "status": "current", "online": True, "harness": None,
                 "pricing": {"input": 0, "output": 0, "note": "開發用：不呼叫供應商"}, "capabilities": {"reasoning": True}}
                for mid, name in (("echo", "回音（開發用）"), ("echo-fast", "回音・不延遲（開發用）"))
            ]
            snap["models"]["text"] = echo + list(snap["models"].get("text") or [])
        return snap

    @app.get("/api/calls")
    async def calls(limit: int = Query(50, le=500), tool: Optional[str] = None, since: Optional[float] = None) -> list[dict[str, Any]]:
        return await ctx().store.calls(limit=limit, tool=tool, since=since)

    @app.get("/api/calls/{call_id}")
    async def call(call_id: str) -> dict[str, Any]:
        row = await ctx().store.call(call_id)
        if not row:
            raise HTTPException(404, "call not found")
        return row

    @app.get("/api/costs")
    async def costs(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
        return await ctx().store.cost_summary(days=days)

    @app.get("/api/conversations")
    async def conversations(limit: int = Query(50, le=500), kind: Optional[str] = None) -> list[dict[str, Any]]:
        return await ctx().store.conversations(limit=limit, kind=kind)

    @app.get("/api/conversations/{cid}")
    async def conversation(cid: str) -> dict[str, Any]:
        c = ctx()
        row = await c.store.conversation(cid)
        if not row:
            raise HTTPException(404, "conversation not found")
        row["messages"] = await c.store.messages(cid)
        return row

    # ------------------------------------------------------------ chat (M6)
    def chat():
        m = ctx().chat
        if m is None:
            raise HTTPException(503, "chat is not available")
        return m

    def _chat_error(e: Exception) -> HTTPException:
        return HTTPException(getattr(e, "status", 400), str(e))

    def _check_fields(body: dict[str, Any], allowed: set[str]) -> None:
        bad = set(body) - allowed
        if bad:
            raise HTTPException(400, f"unknown fields: {sorted(bad)}")

    @app.get("/api/chat")
    async def chat_list(limit: int = Query(100, le=500), archived: bool = False) -> list[dict[str, Any]]:
        return await chat().list(limit=limit, include_archived=archived)

    @app.post("/api/chat")
    async def chat_create(body: dict[str, Any]) -> dict[str, Any]:
        """Create a conversation; with ``message`` the first turn starts right away."""
        from ..chat import ChatError

        _check_fields(body, {"model", "system", "title", "message", "params", "source"})
        try:
            conv = await chat().create(model=body.get("model"), system=body.get("system"), title=body.get("title"), source=body.get("source") or "gui")
            if body.get("message"):
                conv["turn"] = await chat().send(conv["id"], body["message"], model=body.get("model"), params=body.get("params"), source=body.get("source") or "gui")
            return conv
        except ChatError as e:
            raise _chat_error(e)

    @app.get("/api/chat/{cid}")
    async def chat_get(cid: str) -> dict[str, Any]:
        from ..chat import ChatError

        try:
            return await chat().get(cid)
        except ChatError as e:
            raise _chat_error(e)

    @app.patch("/api/chat/{cid}")
    async def chat_update(cid: str, body: dict[str, Any]) -> dict[str, Any]:
        from ..chat import ChatError

        _check_fields(body, {"title", "model", "system_prompt", "archived"})
        try:
            return await chat().update(cid, title=body.get("title"), model=body.get("model"), system_prompt=body.get("system_prompt"), archived=body.get("archived"))
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/messages")
    async def chat_send(cid: str, body: dict[str, Any], wait: bool = False) -> dict[str, Any]:
        """Send a message. The reply streams over /ws (chat.delta); ``?wait=true``
        holds the request until the reply is stored and returns it."""
        from ..chat import ChatError

        _check_fields(body, {"text", "model", "params", "source"})
        kw = {"model": body.get("model"), "params": body.get("params"), "source": body.get("source") or "gui"}
        try:
            if wait:
                return await chat().send_and_wait(cid, body.get("text") or "", **kw)
            return await chat().send(cid, body.get("text") or "", **kw)
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/cancel")
    async def chat_cancel(cid: str) -> dict[str, Any]:
        from ..chat import ChatError

        try:
            return await chat().cancel(cid)
        except ChatError as e:
            raise _chat_error(e)

    @app.get("/api/chat/{cid}/export")
    async def chat_export(cid: str, download: bool = True):
        """The whole conversation as markdown (``download=false`` shows it inline)."""
        from urllib.parse import quote

        from fastapi.responses import Response

        from ..chat import ChatError

        try:
            name, md = await chat().export_markdown(cid)
        except ChatError as e:
            raise _chat_error(e)
        headers = {}
        if download:
            # RFC 5987: the title is usually Chinese, plain filename= must stay ASCII
            headers["Content-Disposition"] = f"attachment; filename=\"chat-{cid}.md\"; filename*=UTF-8''{quote(name)}"
        return Response(content=md, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.post("/api/runs")
    async def start_run(body: dict[str, Any]) -> dict[str, Any]:
        from ..harness import RunSpec

        c = ctx()
        allowed = {"prompt", "model", "harness", "cwd", "title", "yolo", "search", "max_turns", "resume_run_id", "dispatcher", "system_append", "auth"}
        bad = set(body) - allowed
        if bad:
            raise HTTPException(400, f"unknown fields: {sorted(bad)}")
        if not body.get("prompt"):
            raise HTTPException(400, "prompt is required")
        try:
            return await c.runs.start(RunSpec(**body))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        return await ctx().runs.cancel(run_id)

    @app.get("/api/runs")
    async def runs(limit: int = Query(50, le=500), state: Optional[str] = None) -> list[dict[str, Any]]:
        return await ctx().runs.list(limit=limit, state=state)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str, after: int = 0, events: bool = True) -> dict[str, Any]:
        row = await ctx().runs.get(run_id, after_event=after, include_events=events)
        if not row:
            raise HTTPException(404, "run not found")
        return row

    @app.get("/api/runs/{run_id}/thread")
    async def run_thread(run_id: str) -> dict[str, Any]:
        """The resume chain (追問串) a run belongs to, root first, without events."""
        t = await ctx().runs.thread(run_id)
        if not t:
            raise HTTPException(404, "run not found")
        return t

    @app.get("/api/harnesses")
    async def harnesses() -> dict[str, Any]:
        """Which harnesses can actually run here (CLI found + credentials configured)."""
        from ..harness.base import which_cli

        reg = ctx().runs.registry
        endpoints = {k: reg._provider_configured(k) for k in ("anthropic", "anthropic-api", "deepseek", "openrouter")}
        out = {
            "claude": {"name": "Claude Code", "available": any(endpoints.values()), "endpoints": endpoints, "resume": True},
            "codex": {"name": "Codex", "available": bool(which_cli("codex")) and reg._provider_configured("openai"), "cli": bool(which_cli("codex")), "resume": True},
            "gemini": {"name": "Gemini CLI", "available": bool(which_cli("gemini")) and reg._provider_configured("google"), "cli": bool(which_cli("gemini")), "resume": True},
        }
        if os.environ.get("OMNIAPI_DEV") == "1":
            out["replay"] = {"name": "重播（開發用）", "available": True, "resume": True}
        return out

    @app.get("/api/cwds")
    async def cwds(limit: int = Query(30, le=200)) -> list[dict[str, Any]]:
        """Working directories past runs used (the 新對話 form offers them first)."""
        rows = await ctx().store.cwds(limit=limit)
        for r in rows:
            r["exists"] = os.path.isdir(r["cwd"])
        return rows

    @app.get("/api/fs/dirs")
    async def fs_dirs(path: str = "") -> dict[str, Any]:
        """List sub-directories of ``path`` for the cwd picker (directories only, names only).

        The daemon only listens on 127.0.0.1 and this never reads file contents.
        An empty path lists the drive roots (Windows) or the home directory.
        """
        if not path:
            if os.name == "nt":
                import string

                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
                return {"path": "", "parent": None, "exists": True, "dirs": drives, "home": str(Path.home())}
            path = str(Path.home())
        p = Path(path).expanduser()
        if not p.is_dir():
            # still typing: offer the siblings that match the last segment
            parent = p.parent
            if not parent.is_dir():
                return {"path": str(p), "parent": None, "exists": False, "dirs": [], "home": str(Path.home())}
            prefix = p.name.lower()
            names = _subdirs(parent, prefix=prefix)
            return {"path": str(p), "parent": str(parent), "exists": False, "dirs": [str(parent / n) for n in names], "home": str(Path.home())}
        parent = str(p.parent) if p.parent != p else None
        return {"path": str(p), "parent": parent, "exists": True, "dirs": [str(p / n) for n in _subdirs(p)], "home": str(Path.home())}

    @app.get("/api/events")
    async def events(limit: int = Query(100, le=500), since_seq: int = 0) -> list[dict[str, Any]]:
        return ctx().bus.recent(limit=limit, since_seq=since_seq)

    # ------------------------------------------------------------ WebSocket
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        c = ctx()
        q = c.bus.subscribe()
        try:
            await websocket.send_json({"type": "hello", "version": __version__, "ts": time.time()})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping", "ts": time.time()})
                    continue
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            c.bus.unsubscribe(q)

    # ------------------------------------------------------------ MCP + GUI
    app.mount("/mcp", mcp_app)

    class _McpWithoutSlash:
        """Clients are configured with ``…/mcp``; the mount only matches ``/mcp/``.
        Starlette would redirect, but the SPA catch-all below claims ``/mcp`` first
        and answers POST with 405 — so hand the bare path to the mount directly."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope.get("path") == "/mcp":
                scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
            await self.inner(scope, receive, send)

    app.add_middleware(_McpWithoutSlash)

    gui_dist = Path(__file__).resolve().parents[3] / "gui" / "dist"
    if gui_dist.exists() and (gui_dist / "index.html").exists():
        # SPA：/assets 走靜態檔；其他非 /api、/mcp、/ws 的路徑（/runs/:id、/costs）一律回 index.html，交給前端路由
        app.mount("/assets", StaticFiles(directory=str(gui_dist / "assets")), name="gui-assets")
        index_html = gui_dist / "index.html"

        @app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
        async def spa(path: str) -> Any:
            from fastapi.responses import FileResponse

            candidate = gui_dist / path
            if path and candidate.is_file() and candidate.resolve().is_relative_to(gui_dist.resolve()):
                return FileResponse(str(candidate))
            return FileResponse(str(index_html), media_type="text/html")

    else:

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            return f"""<!doctype html><meta charset="utf-8"><title>OmniAPI</title>
<body style="font-family:system-ui;margin:2rem;color:#222">
<h1>OmniAPI daemon v{__version__}</h1>
<p>MCP endpoint: <code>http://{host}:{port}/mcp</code> · REST docs: <a href="/api/docs">/api/docs</a> · status: <a href="/api/status">/api/status</a></p>
<p>The GUI is not built yet (M4). Recent calls: <a href="/api/calls">/api/calls</a></p></body>"""

    return app


def serve(settings: Settings, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, log_level: str = "info") -> None:
    """Run the daemon in the foreground (blocking)."""
    import uvicorn

    app = create_app(settings, host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level=log_level, ws="websockets", access_log=False)
