"""OmniAPI data home (``~/.omniapi``).

Everything OmniAPI persists outside the repo lives here: discovery caches,
the conversation/run database (M2), and per-harness isolated config dirs
(M3). Override with ``OMNIAPI_HOME`` for tests or portable installs.

Deliberately *not* under ``%LOCALAPPDATA%``: the Claude desktop app is an
MSIX package, and writes to AppData\Local from inside it get redirected
into the package container, which other processes cannot read (lesson from
the dsk worker, 2026-08).
"""

from __future__ import annotations

import os
from pathlib import Path


def data_home() -> Path:
    """Return (and create) the OmniAPI data home directory."""
    raw = os.environ.get("OMNIAPI_HOME")
    home = Path(raw).expanduser() if raw else Path.home() / ".omniapi"
    home.mkdir(parents=True, exist_ok=True)
    return home


def cache_dir() -> Path:
    d = data_home() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d
