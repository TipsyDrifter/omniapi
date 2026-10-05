"""Request guard for the local daemon (1.3-M2).

The daemon listens on 127.0.0.1 without login. A web page on any other site
can still make the owner's browser send requests to it ("simple" form-style
POSTs need no CORS preflight), and DNS rebinding can make such a page look
same-origin. So every state-changing request — any ``/api/*`` call that is not
``GET`` / ``HEAD`` — and every WebSocket must:

* carry a ``Host`` that is a loopback name (``127.0.0.1``, ``localhost``,
  ``[::1]``; any port) — a rebound attacker domain fails here;
* when it carries an ``Origin`` (browsers always do on cross-site POSTs and
  WebSockets), have a loopback origin too — the Vite dev server
  (``http://localhost:5178``, proxied) passes.

Requests without ``Origin`` (curl, the CLI, MCP clients, the desktop shell
loading the page itself) pass. ``/mcp`` is left exactly as it was.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD"})


def _host_name(value: str) -> Optional[str]:
    """``127.0.0.1:7788`` → ``127.0.0.1``; ``[::1]:7788`` → ``::1``; malformed → None."""
    value = (value or "").strip().lower()
    if not value:
        return None
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def loopback_host(value: Optional[str]) -> bool:
    return _host_name(value or "") in LOOPBACK_HOSTS


def loopback_origin(value: Optional[str]) -> bool:
    v = (value or "").strip().lower()
    if not v or v == "null":
        return False
    try:
        parts = urlsplit(v)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS


def check(scope: dict[str, Any]) -> Optional[str]:
    """Why ``scope`` must be refused, or ``None`` when it may pass."""
    kind = scope.get("type")
    path = scope.get("path") or ""
    if path == "/mcp" or path.startswith("/mcp/"):
        return None
    if kind == "http":
        if not path.startswith("/api/") or scope.get("method", "GET").upper() in SAFE_METHODS:
            return None
    elif kind != "websocket":
        return None
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    if not loopback_host(headers.get("host")):
        return "host"
    origin = headers.get("origin")
    if origin is not None and not loopback_origin(origin):
        return "origin"
    return None


class LocalOnlyGuard:
    """ASGI middleware applying :func:`check`."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        reason = check(scope) if scope.get("type") in ("http", "websocket") else None
        if reason is None:
            await self.app(scope, receive, send)
            return
        logger.warning("refused %s %s: %s is not a loopback address", scope.get("method", "WS"), scope.get("path"), reason)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": f"{reason} not allowed"})
            return
        body = json.dumps({"detail": f"refused: the request's {reason} is not this computer (only local pages may change the daemon)"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
