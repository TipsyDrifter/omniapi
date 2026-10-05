"""What the desktop app's daily new-version check found (1.3-M6, decision D45).

The desktop shell does the check (one anonymous GET of the public repo's latest
release) and writes ``<data home>/desktop-update.json``; the service only reads
that file so the web pages can show it (``/api/status`` → ``desktop_update``).
The service itself never goes to the network for this.

``None`` = no desktop app has checked (running from a checkout, or not yet).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import __version__
from .catalog.paths import data_home

FILE_NAME = "desktop-update.json"

_V = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")


def _key(v: str) -> Optional[tuple]:
    m = _V.match(v.strip())
    if not m:
        return None
    a, b, c, pre = m.groups()
    # a release sorts after its pre-releases; pre-release parts compare numerically when they can
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else ()
    return (int(a), int(b), int(c), 0 if pre else 1, parts)


def is_newer(latest: str, current: str) -> bool:
    lk, ck = _key(latest), _key(current)
    return bool(lk and ck and lk > ck)


def read(current: str = __version__) -> Optional[dict[str, Any]]:
    """The check result, with ``newer`` recomputed against the running version
    (a version found before an update is not new any more)."""
    path = data_home() / FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    latest = raw.get("latest") if isinstance(raw.get("latest"), str) else None
    url = raw.get("url") if isinstance(raw.get("url"), str) and str(raw.get("url")).startswith(("https://", "http://")) else None
    return {
        "enabled": bool(raw.get("enabled", True)),
        "current": current,
        "latest": latest,
        "newer": bool(latest and url and is_newer(latest, current)),
        "url": url,
        "checked_at_ms": raw.get("checked_at_ms") or None,
        "attempted_at_ms": raw.get("attempted_at_ms") or None,
        "error": raw.get("error"),
    }
