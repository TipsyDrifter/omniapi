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

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..catalog import catalog, data_home
from ..config.settings import Settings
from ..runtime import runtime

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
#: the port when none is given: 7788, unless ``OMNIAPI_DEFAULT_PORT`` says otherwise (the
#: desktop test build's ``omni.cmd`` sets it, so its command line never reaches the released
#: app's service; nothing else sets it)
DEFAULT_PORT_ENV = "OMNIAPI_DEFAULT_PORT"


def _default_port(environ: Optional[dict[str, str]] = None) -> int:
    raw = ((os.environ if environ is None else environ).get(DEFAULT_PORT_ENV) or "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    return 7788


DEFAULT_PORT = _default_port()


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

#: upload caps follow what the vendors accept: 50 MB per reference image
#: (gpt-image edits), 25 MB per audio file (OpenAI transcription)
UPLOAD_MAX_IMAGE = 50 * 1024 * 1024
UPLOAD_MAX_AUDIO = 25 * 1024 * 1024
#: any other file attached to a chat (1.2-M5; the owner set 50 MB, 2026-10-03)
UPLOAD_MAX_FILE = 50 * 1024 * 1024
#: an upload's extraction is waited for this long before answering; a slower
#: one finishes in the background and its facts land on the upload (決策記錄 1.2-M5-h)
UPLOAD_EXTRACT_WAIT_S = 8.0


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


def _layout_info() -> dict[str, Any]:
    from ..layout import describe

    try:
        return describe()
    except Exception as e:  # pragma: no cover - informational only
        return {"error": str(e)}


def _service_info(settings: Settings) -> dict[str, Any]:
    """The settings page's "service" section in one place (1.3-M4): how it
    runs, where its data, works, ``.env`` and settings file are. Paths are
    absolute; ``env_file`` is ``None`` when there is none and
    ``env_file_suggested`` says where one would go (to set ``STORAGE__BASE_PATH``)."""
    from ..config.user_settings import settings_path
    from ..layout import env_file, layout_name, skip_repo_env, suggested_env_file

    try:
        env = env_file()
        storage = Path(settings.storage.base_path)
        return {
            "version": __version__,
            "layout": layout_name(),  # repo = run from a checkout; installed = the packaged app
            "data_home": str(data_home()),
            "storage": str(storage if storage.is_absolute() else storage.resolve()),
            "storage_env": "STORAGE__BASE_PATH",
            "env_file": str(env) if env else None,
            "env_file_suggested": str(suggested_env_file()),
            "repo_env_skipped": skip_repo_env(),  # a test sandbox that leaves the checkout's .env unread
            "settings_file": str(settings_path()),
            "logs": str(data_home() / "logs"),
        }
    except Exception as e:  # pragma: no cover - informational only
        return {"version": __version__, "error": str(e)}


def _desktop_update() -> Optional[dict[str, Any]]:
    """The desktop app's new-version check (1.3-M6); ``None`` without a desktop app."""
    from ..desktop_update import read

    try:
        return read()
    except Exception as e:  # pragma: no cover - informational only
        return {"error": str(e)}


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

    def live_settings() -> Settings:
        """The settings in effect: the runtime's (a settings change swaps them
        in place, 1.3-M2), or the ones the app was made with."""
        return getattr(runtime.context, "settings", None) or settings

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
                "configured": live_settings().providers.enabled_providers,
                "text": c.text_tool.available_providers(),
                "image": c.image_generation_tool.get_available_providers(),
            },
            "discovery": catalog.discovery_status(),
            "tiers": catalog.tiers,
            "store": await c.store.stats(),
            "bus_subscribers": c.bus.subscriber_count,
            "live_runs": c.runs.live_count,
            "live_chats": c.chat.live_count if c.chat else 0,
            "live_generations": c.generations.live_count if c.generations else 0,
            "dev": os.environ.get("OMNIAPI_DEV") == "1",
            "offline": os.environ.get("OMNIAPI_OFFLINE") == "1",
            "data_home": str(data_home()),
            "storage": live_settings().storage.base_path,
            "layout": _layout_info(),
            "service": _service_info(live_settings()),
            "desktop_update": _desktop_update(),
        }

    @app.get("/api/models")
    async def models(
        modality: Optional[str] = None,
        include_retired: bool = False,
        include_snapshots: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if refresh:
            await catalog.refresh(live_settings(), force=True)
        snap = catalog.snapshot(include_retired=include_retired, modality=modality)
        if include_snapshots:
            snap["models"] = {
                mod: [e.to_dict() for e in catalog.models(modality=mod, include_retired=include_retired, include_snapshots=True)]
                for mod in snap["models"]
            }
        if os.environ.get("OMNIAPI_DEV") == "1" and isinstance(snap.get("models"), dict) and modality in (None, "text"):
            # development daemon: the echo models (no vendor call, no billing) lead the text list
            from ..capabilities.echo import echo_catalog_entries

            echo = echo_catalog_entries()
            snap["models"]["text"] = echo + list(snap["models"].get("text") or [])
        enabled_list = getattr(getattr(live_settings(), "providers", None), "enabled_providers", None)
        if enabled_list is not None and isinstance(snap.get("providers"), dict):
            # 1.3-M2: whether each provider has a key and is switched on right now (changes without a restart)
            from ..config.user_settings import SLOT_OF, health_by_provider

            enabled = set(enabled_list)
            # 1.3-M4: the last listing / key test for the key in effect (same object as /api/settings)
            health = await asyncio.to_thread(health_by_provider, live_settings())
            snap["providers"] = {name: {**(p or {}), "configured": SLOT_OF.get(name, name) in enabled, "health": health.get(name)}
                                 for name, p in snap["providers"].items()}
        return snap

    @app.get("/api/calls")
    async def calls(limit: int = Query(50, le=500), tool: Optional[str] = None, since: Optional[float] = None) -> list[dict[str, Any]]:
        """Ledger rows, newest first; ``tool`` may list several. Each row names
        the works it produced (``artifact_ids``)."""
        store = ctx().store
        rows = await store.calls(limit=limit, tool=tool, since=since)
        made = await store.artifact_ids_by_call([r["id"] for r in rows])
        for r in rows:
            r["artifact_ids"] = made.get(r["id"], [])
        return rows

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

        _check_fields(body, {"model", "system", "title", "message", "attachments", "params", "source"})
        try:
            conv = await chat().create(model=body.get("model"), system=body.get("system"), title=body.get("title"), source=body.get("source") or "gui")
            if body.get("message") or body.get("attachments"):
                conv["turn"] = await chat().send(conv["id"], body.get("message") or "", model=body.get("model"), params=body.get("params"),
                                                 source=body.get("source") or "gui", attachments=body.get("attachments"))
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

        _check_fields(body, {"text", "attachments", "model", "params", "source"})
        kw = {"model": body.get("model"), "params": body.get("params"), "source": body.get("source") or "gui", "attachments": body.get("attachments")}
        try:
            started = await chat().send(cid, body.get("text") or "", **kw)
            return await chat().wait(started) if wait else started
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/messages/{mid}/regenerate")
    async def chat_regenerate(cid: str, mid: str, body: Optional[dict[str, Any]] = None, wait: bool = False) -> dict[str, Any]:
        """Answer again: a new version of reply ``mid`` under the same question
        (``model`` may differ). Same response and events as sending."""
        from ..chat import ChatError

        body = body or {}
        _check_fields(body, {"model", "params", "source"})
        try:
            started = await chat().regenerate(cid, mid, model=body.get("model"), params=body.get("params"), source=body.get("source") or "gui")
            return await chat().wait(started) if wait else started
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/messages/{mid}/edit")
    async def chat_edit(cid: str, mid: str, body: dict[str, Any], wait: bool = False) -> dict[str, Any]:
        """Send an edited version of your message ``mid``: it becomes a new
        version next to the old one and gets its own reply; the old branch
        stays. ``attachments`` left out keeps the original's images."""
        from ..chat import ChatError

        _check_fields(body, {"text", "attachments", "model", "params", "source"})
        try:
            started = await chat().edit(cid, mid, body.get("text") or "", attachments=body.get("attachments"), model=body.get("model"),
                                        params=body.get("params"), source=body.get("source") or "gui")
            return await chat().wait(started) if wait else started
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/switch")
    async def chat_switch(cid: str, body: dict[str, Any]) -> dict[str, Any]:
        """Show another version: ``{"message_id"}`` — the conversation moves to
        the newest branch under that message. Returns the conversation as GET."""
        from ..chat import ChatError

        _check_fields(body, {"message_id"})
        if not body.get("message_id"):
            raise HTTPException(400, "message_id is required")
        try:
            return await chat().switch(cid, str(body["message_id"]))
        except ChatError as e:
            raise _chat_error(e)

    @app.delete("/api/chat/{cid}")
    async def chat_delete(cid: str) -> dict[str, Any]:
        """Delete a chat and its messages for real (a streaming reply is
        cancelled first). Its ledger rows, works and uploads stay."""
        from ..chat import ChatError

        try:
            return await chat().delete(cid)
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/proposals/{tool_call_id}/accept")
    async def chat_proposal_accept(cid: str, tool_call_id: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """The owner pressed 生成 on a proposal card: ``{prompt?, model?, params?}``
        (their edits; left out = the proposal's). Starts a generation job
        (source ``chat``); the proposal moves to ``generating`` and follows the
        job. 409 when it is not pending (a failed or cancelled one may be retried)."""
        from ..chat import ChatError

        body = body or {}
        _check_fields(body, {"prompt", "model", "params"})
        try:
            return await chat().accept(cid, tool_call_id, prompt=body.get("prompt"), model=body.get("model"), params=body.get("params"))
        except ChatError as e:
            raise _chat_error(e)

    @app.post("/api/chat/{cid}/proposals/{tool_call_id}/decline")
    async def chat_proposal_decline(cid: str, tool_call_id: str) -> dict[str, Any]:
        """The owner pressed 不用了: the proposal is declined (409 unless pending)."""
        from ..chat import ChatError

        try:
            return await chat().decline(cid, tool_call_id)
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
        out: dict[str, Any] = {"claude": reg.claude_availability()}  # CLI found + a usable endpoint (1.3-M5)
        for key, name, cli_name, provider, key_name in (("codex", "Codex", "codex", "openai", "OpenAI"), ("gemini", "Gemini CLI", "gemini", "google", "Gemini")):
            has_cli = bool(which_cli(cli_name))
            has_key = reg._provider_configured(provider)
            entry: dict[str, Any] = {"name": name, "available": has_cli and has_key, "cli": has_cli, "resume": True}
            if not has_cli:
                entry["reason"] = f"沒有裝 {name}（找不到 {cli_name}）"
            elif not has_key:
                entry["reason"] = f"沒有設定 {key_name} 的 key"
            out[key] = entry
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

    # ------------------------------------------------------------ works library (v1.1)
    async def _artifact_or_404(artifact_id: str) -> dict[str, Any]:
        row = await ctx().store.artifact(artifact_id)
        if not row:
            raise HTTPException(404, "artifact not found")
        return row

    @app.get("/api/artifacts")
    async def artifacts(
        limit: int = Query(60, ge=1, le=500),
        kind: Optional[str] = None,
        model: Optional[str] = None,
        source: Optional[str] = None,
        q: Optional[str] = None,
        before: Optional[float] = None,
        hidden: bool = False,
        only_hidden: bool = False,
        since: Optional[float] = None,
        until: Optional[float] = None,
        call_id: Optional[str] = None,
        id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Generated works, newest first, from every door (MCP, GUI, CLI, backfill).

        ``kind`` may list several (``music,lyrics``); ``model=-`` means works
        without a model; ``since`` / ``until`` bound ``created_at``; ``id`` asks about one
        work — it comes back only if it passes the other filters (how the live
        wall decides whether a new arrival belongs on it).
        ``counts`` is the whole wall per kind, ``matching`` the same under the
        current filters (``kind`` aside) — what the kind tabs show."""
        from ..artifacts import public_row

        store = ctx().store
        filters = dict(model=model, source=source, q=q, include_hidden=hidden, only_hidden=only_hidden, since=since, until=until, call_id=call_id)
        rows = await store.artifacts(limit=limit, kind=kind, before=before, artifact_id=id, **filters)
        for r in rows:
            if r.get("text") and len(r["text"]) > 400:  # the list carries a preview; the single row has it all
                r["text"] = r["text"][:400] + "…"
        return {"items": [public_row(r) for r in rows], "counts": await store.artifact_counts(),
                "matching": await store.artifact_counts(**filters),
                "next_before": rows[-1]["created_at"] if len(rows) == limit else None}

    @app.get("/api/artifacts/facets")
    async def artifact_facets() -> dict[str, Any]:
        """What the wall can filter by: the models and doors that occur, how many are hidden, the date span."""
        return await ctx().store.artifact_facets()

    @app.post("/api/artifacts/backfill")
    async def artifacts_backfill(dry_run: bool = False) -> dict[str, Any]:
        """Re-scan the storage folder for files that are not indexed yet."""
        from ..artifacts import backfill

        c = ctx()
        return await backfill(c.store, live_settings().storage.base_path, dry_run=dry_run)

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(
        artifact_id: str,
        kind: Optional[str] = None,
        model: Optional[str] = None,
        source: Optional[str] = None,
        q: Optional[str] = None,
        only_hidden: bool = False,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> dict[str, Any]:
        """One work in full, with what it was made from (``parent``), what was
        made from it (``children``) and its neighbours on the wall under the
        given filters (``newer`` / ``older`` ids, for stepping through)."""
        from ..artifacts import public_row

        store = ctx().store
        row = await _artifact_or_404(artifact_id)
        out = public_row(row)
        slim = lambda r: {k: v for k, v in public_row(r).items() if k not in ("text", "params", "meta")}  # noqa: E731
        parent = await store.artifact(row["parent_id"]) if row.get("parent_id") else None
        out["parent"] = slim(parent) if parent else None
        out["children"] = [slim(c) for c in await store.artifact_children(artifact_id)]
        out.update(await store.artifact_neighbours(row["created_at"], kind=kind, model=model, source=source, q=q,
                                                   only_hidden=only_hidden, since=since, until=until))
        return out

    @app.patch("/api/artifacts/{artifact_id}")
    async def artifact_update(artifact_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``{"hidden": true}`` takes a work off the wall. The file stays."""
        from ..artifacts import public_row

        _check_fields(body, {"hidden"})
        await _artifact_or_404(artifact_id)
        if "hidden" in body:
            await ctx().store.set_artifact_hidden(artifact_id, bool(body["hidden"]))
        return public_row(await _artifact_or_404(artifact_id))

    def _send_file(path: str, mime: Optional[str], *, download: bool, name: Optional[str] = None):
        from urllib.parse import quote

        from fastapi.responses import FileResponse

        from ..artifacts import mime_for

        p = Path(path)
        if not p.is_file():
            raise HTTPException(410, "the file is no longer on disk")
        headers = {}
        if download:
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name or p.name)}"
        return FileResponse(str(p), media_type=mime or mime_for(p), headers=headers)

    @app.get("/api/artifacts/{artifact_id}/file")
    async def artifact_file(artifact_id: str, download: bool = False):
        """The work itself. Paths come from the index, never from the request,
        so this cannot be pointed at anything that was not generated here."""
        row = await _artifact_or_404(artifact_id)
        return _send_file(row["file_path"], row.get("mime"), download=download)

    @app.get("/api/artifacts/{artifact_id}/thumb")
    async def artifact_thumb(artifact_id: str, w: int = Query(480, ge=64, le=1600)):
        """A small WebP of an image work (cached under the data home). A video
        (1.4-M3): of its poster — one frame ffmpeg took when it was collected
        (or takes now). Without ffmpeg there is none: **204 No Content** with
        ``X-OmniAPI-Poster: none`` (not an error) — the page shows the video's
        own first frame instead."""
        from fastapi.responses import FileResponse, Response

        from ..modalities import MEDIA_OF

        row = await _artifact_or_404(artifact_id)
        media = MEDIA_OF.get(row["kind"])
        if media not in ("image", "video"):
            raise HTTPException(400, "only images and videos have thumbnails")
        src = Path(row["file_path"])
        if not src.is_file():
            raise HTTPException(410, "the file is no longer on disk")
        if media == "video":
            from ..video.media import extract_poster

            poster = data_home() / "cache" / "posters" / f"{artifact_id}.jpg"
            if not poster.is_file() and not await asyncio.to_thread(extract_poster, src, poster):
                return Response(status_code=204, headers={"X-OmniAPI-Poster": "none", "Cache-Control": "no-store"})
            src = poster
        width = min((240, 480, 960, 1600), key=lambda s: abs(s - w))
        out = data_home() / "cache" / "thumbs" / f"{artifact_id}_{width}.webp"
        if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:

            def render() -> None:
                from PIL import Image

                out.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(src) as im:
                    im.thumbnail((width, width * 4))
                    im.save(out, format="WEBP", quality=82)

            await asyncio.to_thread(render)
        return FileResponse(str(out), media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})

    def _upload_view(row: dict[str, Any]) -> dict[str, Any]:
        # 決策記錄 1.2-M1-e: the page names the file by id (/api/uploads/{id}/file), never by where it sits on disk
        meta = row.get("meta") or {}
        out = {k: v for k, v in row.items() if k not in ("file_path", "meta")}
        out["file_url"] = f"/api/uploads/{row['id']}/file"
        out["download_url"] = f"/api/uploads/{row['id']}/file?download=true"
        out["info"] = meta.get("info")
        if meta.get("info_pending"):
            out["info_pending"] = True
        return out

    @app.get("/api/uploads/limits")
    async def upload_limits() -> dict[str, Any]:
        """What the page must not hard-code: the size caps per kind and how
        many attachments a chat message carries (1.2-M5)."""
        from ..chat.manager import MAX_ATTACHMENTS

        return {"max_bytes": {"image": UPLOAD_MAX_IMAGE, "audio": UPLOAD_MAX_AUDIO, "file": UPLOAD_MAX_FILE},
                "max_attachments": MAX_ATTACHMENTS, "purposes": ["generate", "chat"]}

    @app.post("/api/uploads")
    async def upload(request: Request, filename: str = Query(..., min_length=1, max_length=255),
                     purpose: str = Query("generate", pattern="^(generate|chat)$")) -> dict[str, Any]:
        """Take a file (raw request body). ``purpose=generate`` (the default,
        the generate page): a reference image or an audio file only, as
        before. ``purpose=chat`` (1.2-M5): anything — an image or audio file by
        its extension, everything else as ``kind: file`` (50 MB). Size-capped,
        stored under ``uploads/<date>/`` in the storage folder with a name of
        our own. A chat file is extracted right away (決策記錄 1.2-M5-h) and
        the response carries ``info`` (pages, rows, readable…)."""
        import uuid
        from datetime import datetime, timezone

        import aiofiles

        from ..artifacts import mime_for
        from ..modalities import IMAGE_EXTS, UPLOAD_AUDIO_EXTS

        ext = Path(filename).suffix.lower()
        if not ext.isascii() or len(ext) > 16 or not all(ch.isalnum() or ch == "." for ch in ext):
            ext = ""  # only a plain extension ever reaches the disk; the name is display only
        if ext in IMAGE_EXTS:
            kind, cap = "image", UPLOAD_MAX_IMAGE
        elif ext in UPLOAD_AUDIO_EXTS:  # an .mp4 upload is a transcription input: its audio track is what counts
            kind, cap = "audio", UPLOAD_MAX_AUDIO
        elif purpose == "chat":
            kind, cap = "file", UPLOAD_MAX_FILE
        else:
            raise HTTPException(415, f"unsupported file type '{ext or filename}': images (png, jpg, webp, gif) and audio (mp3, wav, m4a, mp4, ogg, flac, webm) only")
        now = datetime.now(timezone.utc)
        upload_id = uuid.uuid4().hex[:16]
        out_dir = Path(live_settings().storage.base_path) / "uploads" / now.strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = (out_dir / f"upload_{now.strftime('%Y%m%d%H%M%S')}_{upload_id[:8]}{ext}").resolve()
        size = 0
        try:
            async with aiofiles.open(path, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap:
                        raise HTTPException(413, f"{kind} uploads are limited to {cap // (1024 * 1024)} MB")
                    await f.write(chunk)
            if size == 0:
                raise HTTPException(400, "empty upload")
            if kind == "image":

                def verify() -> None:
                    from PIL import Image

                    with Image.open(path) as im:
                        im.verify()

                try:
                    await asyncio.to_thread(verify)
                except Exception:
                    if purpose != "chat":
                        raise HTTPException(400, "the file is not a readable image")
                    kind = "file"  # a chat takes it anyway: as a file it is reported as unreadable, not refused
        except HTTPException:
            path.unlink(missing_ok=True)
            raise
        mime = mime_for(path)
        if mime == "application/octet-stream" and kind == "file":
            import mimetypes

            mime = mimetypes.guess_type(f"x{ext}")[0] or mime
        meta = await _file_facts(upload_id, kind, path, filename) if purpose == "chat" else None
        row = await ctx().store.add_upload(upload_id=upload_id, kind=kind, filename=Path(filename).name, file_path=str(path), mime=mime, size=size,
                                           meta=meta)
        return _upload_view(row)

    async def _file_facts(upload_id: str, kind: str, path: Path, filename: str) -> dict[str, Any]:
        """What a chat upload holds (``info``), found now when it is quick
        (決策記錄 1.2-M5-h). A slow extraction goes on in the background and
        writes its facts onto the upload when it ends (``info_pending`` until then)."""
        from ..chat import files as F

        if kind == "audio":
            return {"info": await asyncio.to_thread(F.audio_info, path)}
        if kind == "image":
            return {"info": {"type": "image", "type_name": F.TYPE_NAMES["image"]}}
        task = asyncio.create_task(F.aextract(path, Path(filename).name))
        done, _ = await asyncio.wait({task}, timeout=UPLOAD_EXTRACT_WAIT_S)
        if task in done:
            return {"info": F.public_info(task.result())}

        async def later() -> None:
            try:
                ex = await task
                await ctx().store.update_upload_meta(upload_id, info=F.public_info(ex), info_pending=False)
            except Exception as e:  # the facts are a convenience: the chat extracts again when it needs them
                logger.warning("upload %s: background extraction failed: %s", upload_id, e)

        asyncio.create_task(later())
        return {"info": None, "info_pending": True}

    @app.get("/api/uploads/{upload_id}")
    async def upload_row(upload_id: str) -> dict[str, Any]:
        """One upload as the POST answered it (with ``info`` once a slow extraction ended)."""
        row = await ctx().store.upload(upload_id)
        if not row:
            raise HTTPException(404, "upload not found")
        return _upload_view(row)

    @app.get("/api/uploads/{upload_id}/file")
    async def upload_file(upload_id: str, download: bool = False):
        """The uploaded file. A chat file that is not an image or audio is
        always sent as a download (``Content-Disposition: attachment``,
        ``nosniff``): an uploaded HTML or SVG must never render as a page of
        this origin."""
        row = await ctx().store.upload(upload_id)
        if not row:
            raise HTTPException(404, "upload not found")
        if row.get("kind") == "file":
            resp = _send_file(row["file_path"], "application/octet-stream", download=True, name=row.get("filename"))
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp
        return _send_file(row["file_path"], row.get("mime"), download=download, name=row.get("filename"))

    # ------------------------------------------------------------ generate page (v1.1)
    def generations():
        m = ctx().generations
        if m is None:
            raise HTTPException(503, "generation is not available")
        return m

    @app.get("/api/generate/options")
    async def generate_options(refresh_voices: bool = False) -> dict[str, Any]:
        """What the generate page can offer now: models per kind with whether
        each can be called (and why not), image request shapes, voices."""
        from ..generate import options

        return await options(ctx(), refresh_voices=refresh_voices)

    @app.post("/api/generate/estimate")
    async def generate_estimate(body: dict[str, Any]) -> dict[str, Any]:
        """The cost of a request before it is sent (see ``generate/estimate.py`` for ``basis``)."""
        from ..generate import GenerationError

        _check_fields(body, {"kind", "params", "sources", "duration_s"})
        try:
            return await generations().estimate(body.get("kind") or "", body.get("params"), body.get("sources"), duration_s=body.get("duration_s"))
        except GenerationError as e:
            raise _chat_error(e)

    @app.get("/api/generations")
    async def generation_list(limit: int = Query(30, ge=1, le=200), status: Optional[str] = None, kind: Optional[str] = None) -> list[dict[str, Any]]:
        return await generations().list(limit=limit, status=status, kind=kind)

    @app.post("/api/generations")
    async def generation_start(body: dict[str, Any]) -> dict[str, Any]:
        """Start a generation. Returns at once with ``status: running``; how it
        ends arrives on /ws (``generation.finished``) and stays readable here."""
        from ..generate import GenerationError

        _check_fields(body, {"kind", "params", "sources", "duration_s"})
        try:
            return await generations().start(body.get("kind") or "", body.get("params"), body.get("sources"), duration_s=body.get("duration_s"))
        except GenerationError as e:
            raise _chat_error(e)

    @app.get("/api/generations/{generation_id}")
    async def generation_get(generation_id: str) -> dict[str, Any]:
        from ..generate import GenerationError

        try:
            return await generations().get(generation_id)
        except GenerationError as e:
            raise _chat_error(e)

    @app.post("/api/generations/{generation_id}/cancel")
    async def generation_cancel(generation_id: str) -> dict[str, Any]:
        """Stop a generation. A video cannot be stopped at the provider: this is
        "stop waiting" there (see ``/stop-waiting``)."""
        from ..generate import GenerationError
        from ..video.jobs import VideoError

        try:
            return await generations().cancel(generation_id)
        except GenerationError as e:
            raise _chat_error(e)
        except VideoError as e:
            raise HTTPException(e.status, str(e))

    def _videos():
        v = getattr(ctx(), "videos", None)
        if v is None:
            raise HTTPException(503, "video jobs are not available")
        return v

    @app.post("/api/generations/{generation_id}/stop-waiting")
    async def generation_stop_waiting(generation_id: str) -> dict[str, Any]:
        """A video (1.4-M3): stop waiting. The provider has no cancel, so the video
        is probably still made and billed; with ``video.keep_collecting`` (on by
        default) it is still collected in the background (``status: detached``,
        later ``done`` with ``video.late``), otherwise ``abandoned``."""
        from ..video.jobs import VideoError

        try:
            return await _videos().stop_waiting(generation_id)
        except VideoError as e:
            raise HTTPException(e.status, str(e))

    @app.post("/api/generations/{generation_id}/recheck")
    async def generation_recheck(generation_id: str) -> dict[str, Any]:
        """A video we stopped waiting on (``gave_up`` / ``abandoned``): ask the
        provider once more by its job id — nothing is sent or billed again.
        Collected if done; otherwise waited on again."""
        from ..video.jobs import VideoError

        try:
            return await _videos().recheck(generation_id)
        except VideoError as e:
            raise HTTPException(e.status, str(e))

    @app.get("/api/events")
    async def events(limit: int = Query(100, le=500), since_seq: int = 0) -> list[dict[str, Any]]:
        return ctx().bus.recent(limit=limit, since_seq=since_seq)

    # ------------------------------------------------------------ settings (1.3-M2)
    settings_lock = asyncio.Lock()

    def _settings_error(e: Exception) -> HTTPException:
        return HTTPException(getattr(e, "status", 400), str(e))

    def _read_overlay() -> dict[str, Any]:
        from ..config import user_settings as US

        try:
            return US.normalize_overlay(US.read_overlay())
        except US.SettingsFileError as e:
            raise HTTPException(500, f"{e} — fix or delete the file, then try again")

    def _settings_view(overlay: dict[str, Any]) -> dict[str, Any]:
        from ..config import user_settings as US
        from ..layout import env_file

        _, env_only = US.build_settings(None, env_file=env_file())
        view = US.describe(live_settings(), env_only, overlay)
        view["restart_required"] = []  # everything here applies without a restart
        return view

    @app.get("/api/settings")
    async def settings_get() -> dict[str, Any]:
        """What the settings page edits: per provider whether a key is set (last
        four characters and where it comes from — never the key), on/off and
        configured; the tier aliases and the default models, each with its source.
        1.3-M4: per provider also ``key.shadowed`` (the .env key a settings value
        hides), ``health`` (last listing / key test of the key in effect),
        ``get_key`` (where to get one) and ``suggested_tiers``."""
        return _settings_view(_read_overlay())

    @app.patch("/api/settings")
    async def settings_patch(body: dict[str, Any]) -> dict[str, Any]:
        """Change settings.json and apply it at once (no restart). The body has
        the file's shape; ``null`` removes an entry (the env / built-in value
        shows through again); an ``api_key`` of ``""`` means "no key"."""
        from ..config import user_settings as US
        from ..layout import env_file

        async with settings_lock:
            overlay = _read_overlay()
            try:
                new_overlay, changed = US.apply_patch(overlay, body)
                effective, _ = US.build_settings(new_overlay, env_file=env_file())
            except US.SettingsError as e:
                raise _settings_error(e)
            providers: list[str] = []
            if changed:
                await asyncio.to_thread(US.write_overlay, new_overlay)
                providers = await runtime.apply_settings(effective)
                mcp_server.settings = runtime.context.settings if runtime.context else effective
                runtime.refresh_discovery(runtime.context.settings, providers)
                await ctx().bus.publish({"type": "settings.changed", "changed": changed, "providers": providers})
                logger.info("settings changed: %s", ", ".join(changed))  # field names only, never values
            view = _settings_view(new_overlay)
        return {**view, "changed": changed, "providers_changed": providers, "warnings": US.model_warnings(new_overlay)}

    @app.post("/api/settings/test-key")
    async def settings_test_key(body: dict[str, Any]) -> dict[str, Any]:
        """``{provider, api_key?}``: does the vendor accept this key? A free
        read-only call (list models); without ``api_key`` the key in effect is
        tested. An offline daemon sends nothing and says ``simulated``."""
        from ..config import user_settings as US

        _check_fields(body, {"provider", "api_key"})
        name = str(body.get("provider") or "")
        slot = US.SLOT_OF.get(name, name)
        if slot not in US.PROVIDERS:
            raise HTTPException(400, f"unknown provider '{name}' ({', '.join(US.PROVIDERS)})")
        if body.get("api_key") is not None:
            try:
                key = US._clean_key(body["api_key"], "api_key")
            except US.SettingsError as e:
                raise _settings_error(e)
        else:
            cfg = getattr(live_settings().providers, slot, None)
            key = getattr(cfg, "api_key", "") if cfg else ""
        return await US.test_key(slot, key, live_settings())

    # ------------------------------------------------------------ outside programs (1.3-M4)
    tools_cache: dict[str, Any] = {}
    tools_lock = asyncio.Lock()

    @app.get("/api/tools")
    async def external_tools(refresh: bool = False) -> dict[str, Any]:
        """Node / npx, ffmpeg, Claude Code (and its login), Codex and Gemini
        CLI: found or not, path, version when cheap, what is lost without it,
        how to install it (with the source). Looked up once and kept;
        ``?refresh=true`` looks again (read-only, nothing is installed)."""
        from ..utils.external_tools import detect

        async with tools_lock:
            if refresh or "result" not in tools_cache:
                tools_cache["result"] = await asyncio.to_thread(detect, live_settings())
            return tools_cache["result"]

    # ------------------------------------------------------------ Claude Code's MCP entry (1.3-M4)
    @app.get("/api/claude-mcp")
    async def claude_mcp_status() -> dict[str, Any]:
        """Is Claude Code pointed at this service? ``state``: connected / other /
        missing / no_config / unreadable; ``entry`` is what is there now,
        ``expected`` what connecting writes. Reads ``~/.claude.json`` only."""
        from ..config import claude_mcp

        return await asyncio.to_thread(claude_mcp.status, host, port)

    @app.post("/api/claude-mcp")
    async def claude_mcp_apply() -> dict[str, Any]:
        """Connect Claude Code: write ``mcpServers["omniapi-mcp"]`` in
        ``~/.claude.json`` (what ``omni mcp-config --apply`` does; the previous
        entry is backed up). Only on the owner's click; 409 when the file is
        missing or unreadable (it is never created or overwritten blind).
        Claude Code sees it the next time it starts."""
        from ..config import claude_mcp

        try:
            res = await asyncio.to_thread(claude_mcp.apply, host, port)
        except claude_mcp.ClaudeConfigError as e:
            raise HTTPException(e.status, {"reason": e.reason, "message": str(e)})
        if res.get("changed"):
            logger.info("Claude Code MCP entry written to %s (backup: %s)", res["path"], res.get("backed_up"))
            c = runtime.context
            if c is not None:
                await c.bus.publish({"type": "claude_mcp.changed", "state": res["state"]})
        return res

    # ------------------------------------------------------------ desktop app: start at logon
    @app.get("/api/desktop/autostart")
    async def desktop_autostart_status() -> dict[str, Any]:
        """The desktop app's logon entry (the tray's 開機時啟動): ``available`` only when
        the desktop app started this service; ``enabled`` is what the tray's check mark
        shows; ``legacy`` names an old ``omni autostart`` launcher; ``other`` is the
        entry's command when it points at a different exe."""
        from .. import desktop_autostart

        return await asyncio.to_thread(desktop_autostart.status)

    @app.api_route("/api/desktop/autostart", methods=["PUT", "POST"])
    async def desktop_autostart_set(body: dict[str, Any]) -> dict[str, Any]:
        """Turn it on or off: ``{"enabled": true|false}``. Writes or removes the same
        registry value the tray does, with the desktop app's own exe (the request
        carries no path). 409 when this service was not started by the desktop app."""
        from .. import desktop_autostart

        on = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(on, bool):
            raise HTTPException(422, {"reason": "bad_request", "message": "body must be {\"enabled\": true|false}"})
        try:
            res = await asyncio.to_thread(desktop_autostart.set_enabled, on)
        except desktop_autostart.AutostartUnavailable as e:
            raise HTTPException(409, {"reason": "unavailable", "message": str(e)})
        except OSError as e:
            raise HTTPException(500, {"reason": "write_failed", "message": str(e)})
        logger.info("desktop logon entry turned %s from the settings page", "on" if on else "off")
        c = runtime.context
        if c is not None:
            await c.bus.publish({"type": "desktop.autostart.changed", "enabled": res["enabled"]})
        return res

    # ------------------------------------------------------------ shutdown (1.3-M2)
    @app.post("/api/shutdown")
    async def shutdown_endpoint() -> dict[str, Any]:
        """Shut down cleanly: stop taking requests, close out replies and
        generations in flight (the usual shutdown path), remove the pid file,
        exit. Answers first, then goes."""
        server = getattr(app.state, "uvicorn_server", None)
        if server is None:
            raise HTTPException(503, "this process was not started by `omni serve`; stop it from where it was started")
        logger.info("shutdown requested over /api/shutdown (pid=%s)", os.getpid())
        c = runtime.context
        if c is not None:
            await c.bus.publish({"type": "daemon.stopping", "pid": os.getpid()})

        async def go() -> None:
            await asyncio.sleep(0.2)  # let this answer leave first
            server.should_exit = True

        app.state.shutdown_task = asyncio.create_task(go(), name="daemon-shutdown")
        return {"ok": True, "pid": os.getpid()}

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
    # 1.3-M2: state-changing /api calls and WebSockets only from this computer
    from .guard import LocalOnlyGuard

    app.add_middleware(LocalOnlyGuard)

    from ..layout import gui_dist as find_gui_dist

    gui_dist = find_gui_dist()  # OMNIAPI_GUI_DIST, then the repo's gui/dist
    if gui_dist is not None:
        logger.info("GUI served from %s", gui_dist)
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
    # a Server object (not uvicorn.run) so POST /api/shutdown can ask it to exit;
    # requests still open after 10 s are cut off so a stuck one cannot hold the exit
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level, ws="websockets", access_log=False,
                            timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()
