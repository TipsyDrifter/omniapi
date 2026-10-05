"""``daemon.log`` with a size cap and a few old copies kept (1.3-M2).

A background daemon writes everything — its logging, uvicorn's, ``print``,
tracebacks — to its stdout / stderr. A file another handle holds open cannot
be renamed on Windows, and the venv's ``pythonw.exe`` is a shim that keeps
the real interpreter's std handles open for its whole life — so the launcher
points the child's raw stdout / stderr at ``daemon.stderr.log`` (what comes
out before Python is set up: an import error, an interpreter crash), and the
daemon itself owns ``daemon.log``: :func:`install` swaps ``sys.stdout`` /
``sys.stderr`` for one :class:`RotatingLogStream` (one writer, one lock —
stdout and stderr share the file as before).

Rollover: ``daemon.log`` → ``daemon.log.1`` → … → ``daemon.log.<backups>``
(the oldest is dropped). If a rename fails (someone has the file open), the
stream keeps appending and tries again a while later instead of losing lines.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUPS = 5
_RETRY_AFTER_S = 60.0


def limits() -> tuple[int, int]:
    """``(max bytes, backups)`` — ``OMNIAPI_LOG_MAX_MB`` / ``OMNIAPI_LOG_BACKUPS`` override."""
    try:
        max_bytes = int(float(os.environ.get("OMNIAPI_LOG_MAX_MB", "")) * 1024 * 1024)
    except ValueError:
        max_bytes = DEFAULT_MAX_BYTES
    try:
        backups = int(os.environ.get("OMNIAPI_LOG_BACKUPS", ""))
    except ValueError:
        backups = DEFAULT_BACKUPS
    return (max_bytes if max_bytes > 0 else DEFAULT_MAX_BYTES), max(1, backups)


class RotatingLogStream(io.TextIOBase):
    """A text stream (usable as ``sys.stdout`` / ``sys.stderr`` and by logging
    handlers) that appends to ``path`` and rolls it over past ``max_bytes``."""

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS):
        super().__init__()
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.RLock()
        self._retry_at = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)

    # -- the TextIOBase surface
    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:  # type: ignore[override]
        # faulthandler & co. want a real descriptor
        return self._fh.fileno()

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        with self._lock:
            if self._fh.closed:
                return 0
            self._fh.write(s)
            if "\n" in s:
                self._fh.flush()
                self._maybe_rollover()
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()
        super().close()

    # -- rotation
    def _size(self) -> int:
        try:
            return self._fh.tell()
        except (OSError, ValueError):
            return 0

    def _maybe_rollover(self) -> None:
        if self._size() < self.max_bytes or time.monotonic() < self._retry_at:
            return
        self.rollover()

    def rollover(self) -> bool:
        """Rotate now. ``False`` when a rename failed (the file stays in use)."""
        with self._lock:
            self._fh.close()
            ok = True
            try:
                oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
                if oldest.exists():
                    oldest.unlink()
                for i in range(self.backups - 1, 0, -1):
                    src = self.path.with_name(f"{self.path.name}.{i}")
                    if src.exists():
                        os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
                os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
            except OSError:
                ok = False
                self._retry_at = time.monotonic() + _RETRY_AFTER_S
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
            if not ok:
                self._fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - omni.logfile - WARNING - could not rotate {self.path.name} (in use); retrying later\n")
            return ok


_installed: Optional[RotatingLogStream] = None


def install(path: Path, *, max_bytes: Optional[int] = None, backups: Optional[int] = None, release_fds: bool = True) -> RotatingLogStream:
    """Route this process's stdout / stderr into a rotating ``path``.

    Call before logging and uvicorn are configured (their handlers bind to
    ``sys.stderr`` / ``sys.stdout`` when they are set up). ``release_fds``
    re-points descriptors 1 and 2 at ``<name>.stderr.log`` so no inherited
    handle keeps ``path`` open (which would block the rename on Windows)."""
    global _installed
    if _installed is not None:
        return _installed
    mb, bk = limits()
    stream = RotatingLogStream(path, max_bytes=max_bytes or mb, backups=backups or bk)
    if release_fds:
        low = Path(path).with_name(Path(path).stem + ".stderr.log")
        try:
            if low.exists() and low.stat().st_size > stream.max_bytes:
                os.replace(low, low.with_name(low.name + ".1"))
        except OSError:
            pass
        try:
            fd = os.open(str(low), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            for target in (1, 2):
                try:
                    os.dup2(fd, target)
                except OSError:
                    pass
            os.close(fd)
        except OSError:
            pass
    sys.stdout = stream  # type: ignore[assignment]
    sys.stderr = stream  # type: ignore[assignment]
    _installed = stream
    return stream
