"""Claude Code's MCP entry for this service, in ``~/.claude.json``.

One place for what ``omni mcp-config --apply`` and the settings page's
"connect Claude Code" button do (1.3-M4): read whether the user-scope entry
``mcpServers["omniapi-mcp"]`` exists and where it points, and write it.

Writing changes a file that belongs to Claude Code, so it happens only when
asked (the CLI flag, the button): the previous entry, if different, is kept
in ``<data home>/claude-mcp-entry.backup.json`` (as the CLI always did), the
rest of the file is left as it was, and the new file replaces the old one in
a single rename. Claude Code reads the file when it starts — a session that is
already open does not see the change.

``OMNIAPI_CLAUDE_CONFIG`` points at another file (tests and sandboxes must
never touch the real one).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..catalog.paths import data_home

ENTRY_NAME = "omniapi-mcp"
CONFIG_ENV = "OMNIAPI_CLAUDE_CONFIG"
BACKUP_NAME = "claude-mcp-entry.backup.json"


class ClaudeConfigError(Exception):
    """The file cannot be read or written; ``reason`` is ``no_config`` (Claude
    Code has never run here), ``unreadable`` or ``write_failed``."""

    def __init__(self, message: str, reason: str, status: int = 409):
        super().__init__(message)
        self.reason = reason
        self.status = status


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".claude.json"


def entry(host: str, port: int) -> dict[str, Any]:
    """The entry that points Claude Code at this service's ``/mcp``."""
    return {"type": "http", "url": f"http://{host}:{port}/mcp"}


def backup_path() -> Path:
    return data_home() / BACKUP_NAME


def _load(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ClaudeConfigError(f"{path} does not exist (has Claude Code been started on this computer?)", "no_config") from None
    except OSError as e:
        raise ClaudeConfigError(f"cannot read {path}: {e.__class__.__name__}", "unreadable") from None
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise ClaudeConfigError(f"{path} is not valid JSON; left as it is", "unreadable") from None
    if not isinstance(data, dict):
        raise ClaudeConfigError(f"{path} does not hold a JSON object; left as it is", "unreadable")
    return data


def _describe_entry(e: Any) -> Optional[dict[str, Any]]:
    """What an existing entry is, without echoing anything secret it may hold
    (a stdio entry can carry ``env`` with keys)."""
    if not isinstance(e, dict):
        return None if e is None else {"type": "unknown"}
    kind = e.get("type") or ("stdio" if e.get("command") else "unknown")
    out: dict[str, Any] = {"type": kind}
    if e.get("url"):
        out["url"] = e["url"]
    if e.get("command"):
        out["command"] = e["command"]
    return out


def status(host: str, port: int, path: Optional[Path] = None) -> dict[str, Any]:
    """``{path, state, entry, expected, backup}``. ``state``:

    * ``connected``  the entry exists and points at this service
    * ``other``      an entry by that name points elsewhere (another port, the old stdio server)
    * ``missing``    the file has no such entry
    * ``no_config``  the file does not exist (Claude Code has not run here)
    * ``unreadable`` the file exists but is not readable JSON"""
    p = path or config_path()
    want = entry(host, port)
    out: dict[str, Any] = {"path": str(p), "name": ENTRY_NAME, "expected": want, "entry": None,
                           "backup": str(backup_path()) if backup_path().is_file() else None}
    try:
        data = _load(p)
    except ClaudeConfigError as e:
        return {**out, "state": e.reason, "message": str(e)}
    servers = data.get("mcpServers")
    current = servers.get(ENTRY_NAME) if isinstance(servers, dict) else None
    out["entry"] = _describe_entry(current)
    if current is None:
        out["state"] = "missing"
    elif current == want:
        out["state"] = "connected"
    else:
        out["state"] = "other"
    return out


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.omniapi.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def apply(host: str, port: int, path: Optional[Path] = None) -> dict[str, Any]:
    """Write the entry. Returns ``status()`` plus ``changed`` and ``backed_up``.
    Raises :class:`ClaudeConfigError` when the file is missing or unreadable
    (it is never created or overwritten blind)."""
    p = path or config_path()
    want = entry(host, port)
    data = _load(p)
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    elif not isinstance(servers, dict):
        raise ClaudeConfigError(f"{p}: mcpServers is not an object; left as it is", "unreadable")
    old = servers.get(ENTRY_NAME)
    if old == want:
        return {**status(host, port, p), "changed": False, "backed_up": False}
    backed_up = False
    if old:
        backup_path().write_text(json.dumps(old, indent=2), encoding="utf-8")
        backed_up = True
    servers[ENTRY_NAME] = want
    try:
        _write_atomic(p, data)
    except OSError as e:
        raise ClaudeConfigError(f"cannot write {p}: {e.__class__.__name__}", "write_failed", status=500) from None
    return {**status(host, port, p), "changed": True, "backed_up": backed_up}
