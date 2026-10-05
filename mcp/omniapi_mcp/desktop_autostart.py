"""The desktop app's "start at logon" switch, for the settings page.

The desktop shell's own switch (tray menu 開機時啟動, tauri-plugin-autostart) is
one registry value: ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
``OmniAPI`` = ``<OmniAPI.exe> --background`` (the exe path unquoted, one space,
the argument). The service reads and writes that same value, byte for byte the
way the plugin does, so the tray's check mark and the page always show one and
the same thing (the shell re-reads it when the pointer reaches the tray icon).

Only a service the desktop app started may do this: the shell passes its own
exe in ``OMNIAPI_DESKTOP_EXE`` (``{exe}`` in the installed shell config). A
service run from a checkout or a zip has no such variable, so ``available`` is
false and nothing can be written. The path written is always that one; a
request never carries a path.

The older launchers from ``omni autostart install`` (Startup-folder .vbs, the
scheduled task) are only reported, never removed (decision 1.3-M5: no double
logon start is forced away; ``omni autostart remove`` removes them).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

ENV_VAR = "OMNIAPI_DESKTOP_EXE"
RUN_VALUE = "OmniAPI"  # same as cli.DESKTOP_RUN_VALUE and the shell's autostart::RUN_VALUE
ARG = "--background"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
#: what the plugin writes into StartupApproved on enable (Task Manager's "enabled")
APPROVED_ON = bytes([0x02] + [0x00] * 11)
LEGACY_VBS = "OmniAPI Daemon.vbs"
LEGACY_TASK = "OmniAPI Daemon"


class AutostartUnavailable(Exception):
    """This service was not started by the desktop app (or this is not Windows)."""


class Registry(Protocol):
    """The few HKCU operations needed; the real one is :class:`WinReg`, tests use a fake."""

    def get(self, key: str, name: str) -> Any: ...  # value or None

    def key_exists(self, key: str) -> bool: ...

    def set_string(self, key: str, name: str, value: str) -> None: ...

    def set_binary(self, key: str, name: str, value: bytes) -> None: ...

    def delete(self, key: str, name: str) -> None: ...  # missing value = no error


class WinReg:
    """HKEY_CURRENT_USER through ``winreg`` (Windows only)."""

    def __init__(self) -> None:
        import winreg

        self._w = winreg

    def get(self, key: str, name: str) -> Any:
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, key) as k:
                return w.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def key_exists(self, key: str) -> bool:
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, key):
                return True
        except OSError:
            return False

    def set_string(self, key: str, name: str, value: str) -> None:
        w = self._w
        with w.OpenKey(w.HKEY_CURRENT_USER, key, 0, w.KEY_SET_VALUE) as k:
            w.SetValueEx(k, name, 0, w.REG_SZ, value)

    def set_binary(self, key: str, name: str, value: bytes) -> None:
        w = self._w
        with w.OpenKey(w.HKEY_CURRENT_USER, key, 0, w.KEY_SET_VALUE) as k:
            w.SetValueEx(k, name, 0, w.REG_BINARY, value)

    def delete(self, key: str, name: str) -> None:
        w = self._w
        try:
            with w.OpenKey(w.HKEY_CURRENT_USER, key, 0, w.KEY_SET_VALUE) as k:
                w.DeleteValue(k, name)
        except FileNotFoundError:
            pass


def shell_exe(environ: Optional[dict[str, str]] = None) -> Optional[str]:
    """The desktop shell's exe as the shell gave it, or ``None`` (not started by the desktop app)."""
    env = os.environ if environ is None else environ
    v = (env.get(ENV_VAR) or "").strip().strip('"')
    if not v or not os.path.isabs(v):
        return None
    return v


def command_for(exe: str) -> str:
    """Exactly what tauri-plugin-autostart 2.5.1 (auto-launch 0.5.0) writes on Windows:
    ``format!("{} {}", app_path, args.join(" "))``."""
    return f"{exe} {ARG}"


def _approved(raw: Any) -> bool:
    """Task Manager's switch, read the way the plugin reads it: no value, or a short one,
    counts as on; otherwise on when the last eight bytes are all zero."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 8:
        return True
    return all(b == 0 for b in raw[-8:])


def legacy_text(vbs: bool, task: bool) -> Optional[str]:
    if vbs and task:
        return "以前用 omni autostart 設的開機啟動也還在（啟動資料夾與工作排程）。兩邊都開不會壞，但用 omni autostart remove 拿掉舊的比較乾淨。"
    if vbs:
        return f"以前用 omni autostart 設的開機啟動也還在（啟動資料夾的 {LEGACY_VBS}）。兩邊都開不會壞，但用 omni autostart remove 拿掉舊的比較乾淨。"
    if task:
        return f"以前用 omni autostart 設的開機啟動也還在（工作排程 {LEGACY_TASK}）。兩邊都開不會壞，但用 omni autostart remove 拿掉舊的比較乾淨。"
    return None


def detect_legacy() -> Optional[str]:
    """The old launchers (Startup-folder .vbs, scheduled task); runs ``schtasks /Query`` (~0.1 s)."""
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    vbs = (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / LEGACY_VBS).is_file()
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", LEGACY_TASK], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        task = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        task = False
    return legacy_text(vbs, task)


def _default_registry() -> Optional[Registry]:
    return WinReg() if os.name == "nt" else None


def status(reg: Optional[Registry] = None, exe: Optional[str] = None, legacy: Optional[Callable[[], Optional[str]]] = None,
           *, use_env: bool = True) -> dict[str, Any]:
    """``{available, enabled, legacy, other}``.

    ``enabled`` is what the tray's check mark shows (the plugin's rule: the value is there
    and Task Manager has not switched it off). ``other`` is the command when the value
    points at a different exe (an older install, a copy elsewhere): turning the switch on
    rewrites it to this one."""
    if exe is None and use_env:
        exe = shell_exe()
    if reg is None:
        reg = _default_registry()
    if not exe or reg is None:
        return {"available": False, "enabled": False, "legacy": None, "other": None}
    command = reg.get(RUN_KEY, RUN_VALUE)
    command = command if isinstance(command, str) and command else None
    enabled = bool(command) and _approved(reg.get(APPROVED_KEY, RUN_VALUE))
    other = command if command and command.strip().lower() != command_for(exe).lower() else None
    return {
        "available": True,
        "enabled": enabled,
        "legacy": (legacy or detect_legacy)(),
        "other": other,
    }


def set_enabled(on: bool, reg: Optional[Registry] = None, exe: Optional[str] = None, legacy: Optional[Callable[[], Optional[str]]] = None,
                *, use_env: bool = True) -> dict[str, Any]:
    """Turn the logon entry on or off, the way the plugin's enable / disable do, and
    return the new :func:`status`. Raises :class:`AutostartUnavailable` without a desktop app."""
    if exe is None and use_env:
        exe = shell_exe()
    if reg is None:
        reg = _default_registry()
    if not exe or reg is None:
        raise AutostartUnavailable("this service was not started by the OmniAPI desktop app")
    if on:
        reg.set_string(RUN_KEY, RUN_VALUE, command_for(exe))
        # the plugin also marks it enabled for Task Manager, but only when that key exists
        if reg.key_exists(APPROVED_KEY):
            reg.set_binary(APPROVED_KEY, RUN_VALUE, APPROVED_ON)
    else:
        reg.delete(RUN_KEY, RUN_VALUE)
    return status(reg, exe, legacy, use_env=False)
