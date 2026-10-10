"""Where things are: the one place that knows the service's file layout (1.3-M2).

Two layouts:

* **repo** — the package sits in a checkout (``<repo>/mcp/omniapi_mcp``, with
  ``mcp/pyproject.toml`` next to it). Everything behaves as it always did:
  the service runs in ``mcp/``, works go to ``./storage`` (``mcp/storage``),
  the GUI is ``<repo>/gui/dist`` and ``mcp/.env`` is read first.
* **installed** — anything else (a packaged app, a copied folder, a wheel in
  site-packages). The service runs in the data home (``OMNIAPI_HOME`` or
  ``~/.omniapi``), works go to the user's Documents folder (``Documents\\OmniAPI``)
  unless ``STORAGE__BASE_PATH`` says otherwise, and the GUI comes from
  ``OMNIAPI_GUI_DIST`` (else, in the desktop install, ``gui`` next to the
  bundled ``python`` folder).

Every "where is X" question goes through here; nothing else should compute a
path from ``__file__``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .catalog.paths import data_home

logger = logging.getLogger(__name__)

#: the package folder (``.../omniapi_mcp``)
PACKAGE_DIR = Path(__file__).resolve().parent

#: env var naming a built GUI folder (one holding ``index.html``)
GUI_DIST_ENV = "OMNIAPI_GUI_DIST"


def project_dir(package_dir: Optional[Path] = None) -> Optional[Path]:
    """The checkout's ``mcp/`` folder when running from a repo, else ``None``.

    The marker is ``pyproject.toml`` next to the package: a checkout has it,
    an installed package (site-packages, a packaged app) does not."""
    parent = (package_dir or PACKAGE_DIR).parent
    return parent if (parent / "pyproject.toml").is_file() else None


def is_repo_layout(package_dir: Optional[Path] = None) -> bool:
    return project_dir(package_dir) is not None


def layout_name(package_dir: Optional[Path] = None) -> str:
    return "repo" if is_repo_layout(package_dir) else "installed"


# ---------------------------------------------------------------- GUI
def _usable_dist(p: Path) -> bool:
    return p.is_dir() and (p / "index.html").is_file()


def gui_dist(package_dir: Optional[Path] = None) -> Optional[Path]:
    """The built GUI: ``OMNIAPI_GUI_DIST`` first, then the repo's ``gui/dist``;
    ``None`` means the daemon shows its small status page instead."""
    raw = os.environ.get(GUI_DIST_ENV)
    if raw:
        p = Path(raw).expanduser()
        if _usable_dist(p):
            return p.resolve()
        logger.warning("%s=%s has no index.html; ignoring it", GUI_DIST_ENV, raw)
    proj = project_dir(package_dir)
    if proj is not None:
        p = proj.parent / "gui" / "dist"
        if _usable_dist(p):
            return p
        return None
    # the desktop install (1.3-M6): <install>\python is this Python, <install>\gui the GUI --
    # so `omni serve` from <install>\omni.cmd serves the same pages as the desktop app
    p = Path(sys.prefix).parent / "gui"
    if _usable_dist(p):
        return p.resolve()
    return None


# ---------------------------------------------------------------- .env
#: env var that decides whether the checkout's ``mcp/.env`` and the working
#: directory's ``.env`` are left unread. ``1`` skips them, ``0`` reads them;
#: unset, an offline sandbox (``OMNIAPI_OFFLINE=1``) skips and everything else reads.
SKIP_REPO_ENV = "OMNIAPI_SKIP_REPO_ENV"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def skip_repo_env() -> bool:
    """Should the service ignore the repo / working-directory ``.env``?

    A sandbox for tests must not pick up the real keys that sit in ``mcp/.env``
    of the checkout it is started from: they turn providers on and change the
    model lists a test expects. The data home's own ``.env`` (the sandbox's
    home, chosen explicitly through ``OMNIAPI_HOME``) is always read.
    An offline sandbox never reaches a vendor whatever it is given, so
    skipping by default there costs nothing; ``OMNIAPI_SKIP_REPO_ENV=0`` reads
    the file anyway (e.g. an offline run that wants the owner's model lists)."""
    from .devmode import offline

    raw = os.environ.get(SKIP_REPO_ENV, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return offline()


def env_file_candidates(package_dir: Optional[Path] = None) -> list[Path]:
    """Where a ``.env`` may be, in the order both entry points (daemon and
    stdio) look: the checkout's ``mcp/.env``, the working directory's ``.env``
    (what the stdio server always read), then ``<data home>/.env``. With
    ``skip_repo_env()`` only the last one is left."""
    out: list[Path] = []
    if not skip_repo_env():
        proj = project_dir(package_dir)
        if proj is not None:
            out.append(proj / ".env")
        out.append(Path.cwd() / ".env")
    out.append(data_home() / ".env")
    seen: set[str] = set()
    unique = []
    for p in out:
        key = os.path.normcase(str(p.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def env_file(package_dir: Optional[Path] = None) -> Optional[Path]:
    for cand in env_file_candidates(package_dir):
        if cand.is_file():
            return cand
    return None


def suggested_env_file(package_dir: Optional[Path] = None) -> Path:
    """Where to tell a user to create a ``.env`` when there is none."""
    proj = project_dir(package_dir)
    return (proj / ".env") if proj is not None else data_home() / ".env"


# ---------------------------------------------------------------- service dirs
def service_workdir(package_dir: Optional[Path] = None) -> Path:
    """The working directory of a background service: ``mcp/`` in a repo
    (unchanged), the data home when installed (the install folder may be
    read-only and is replaced on update)."""
    proj = project_dir(package_dir)
    return proj if proj is not None else data_home()


def documents_dir() -> Path:
    """The user's Documents folder from the OS (it may be redirected, e.g. to
    OneDrive); ``~/Documents`` when the OS cannot say."""
    if os.name == "nt":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")  # FOLDERID_Documents
            guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            out = ctypes.c_wchar_p()
            shell32 = ctypes.WinDLL("shell32")
            ole32 = ctypes.WinDLL("ole32")
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
            try:
                if hr == 0 and out.value:
                    return Path(out.value)
            finally:
                ole32.CoTaskMemFree(out)
        except Exception as e:  # pragma: no cover - depends on the OS
            logger.debug("SHGetKnownFolderPath(Documents) failed: %s", e)
    return Path.home() / "Documents"


def default_storage_dir(package_dir: Optional[Path] = None) -> Path:
    """Where works go when ``STORAGE__BASE_PATH`` is not set."""
    proj = project_dir(package_dir)
    if proj is not None:
        return Path("./storage")  # relative to the service's cwd (mcp/), as always
    return documents_dir() / "OmniAPI"


def apply_storage_default(settings: Any, package_dir: Optional[Path] = None) -> Optional[str]:
    """Installed layout without an explicit ``STORAGE__BASE_PATH``: point the
    works folder at ``Documents\\OmniAPI``. Returns the new path, or ``None``
    when nothing changed (repo layout, or the path was set)."""
    if is_repo_layout(package_dir):
        return None
    storage = settings.storage
    if "base_path" in getattr(storage, "model_fields_set", set()):
        return None
    target = str(default_storage_dir(package_dir))
    storage.base_path = target
    return target


def describe() -> dict[str, Any]:
    """For ``/api/status``: which layout and where things resolved to."""
    dist = gui_dist()
    env = env_file()
    return {
        "layout": layout_name(),
        "package_dir": str(PACKAGE_DIR),
        "gui_dist": str(dist) if dist else None,
        "env_file": str(env) if env else None,
        "repo_env_skipped": skip_repo_env(),
        "workdir": str(Path.cwd()),
    }
