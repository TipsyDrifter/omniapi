"""The settings page's "start at logon" switch (desktop app only).

Everything here runs against a fake registry: no test writes the real HKCU.
"""

from __future__ import annotations

import pytest

from omniapi_mcp import desktop_autostart as DA
from omniapi_mcp.config.settings import Settings

EXE = r"C:\Apps\OmniAPI\OmniAPI.exe"


class FakeReg:
    def __init__(self, approved_key: bool = True) -> None:
        self.values: dict[tuple[str, str], object] = {}
        self.keys = {DA.RUN_KEY} | ({DA.APPROVED_KEY} if approved_key else set())
        self.writes: list[tuple] = []

    def get(self, key, name):
        return self.values.get((key, name))

    def key_exists(self, key):
        return key in self.keys

    def set_string(self, key, name, value):
        assert isinstance(value, str)
        self.writes.append(("sz", key, name, value))
        self.values[(key, name)] = value

    def set_binary(self, key, name, value):
        assert isinstance(value, bytes)
        self.writes.append(("bin", key, name, value))
        self.values[(key, name)] = value

    def delete(self, key, name):
        self.writes.append(("del", key, name))
        self.values.pop((key, name), None)


class NoReg:
    """Fails the test if anything reaches the registry."""

    def __getattr__(self, name):
        raise AssertionError(f"registry touched: {name}")


def no_legacy():
    return None


def test_command_is_byte_for_byte_what_the_plugin_writes():
    # tauri-plugin-autostart 2.5.1 / auto-launch 0.5.0 on Windows:
    #   format!("{} {}", app_path, args.join(" "))  with app_path = current_exe().display(), args = ["--background"]
    assert DA.command_for(EXE) == r"C:\Apps\OmniAPI\OmniAPI.exe --background"
    assert DA.RUN_VALUE == "OmniAPI" and DA.RUN_KEY == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert DA.APPROVED_ON == bytes([2] + [0] * 11)


def test_on_writes_the_run_value_and_task_manager_enabled():
    reg = FakeReg()
    s = DA.set_enabled(True, reg, EXE, no_legacy)
    assert reg.values[(DA.RUN_KEY, "OmniAPI")] == EXE + " --background"
    assert reg.values[(DA.APPROVED_KEY, "OmniAPI")] == DA.APPROVED_ON
    assert s == {"available": True, "enabled": True, "legacy": None, "other": None}
    # the CLI's `omni autostart status` reads the same entry
    from omniapi_mcp.cli import desktop_autostart

    assert desktop_autostart(reg.get) == {"command": EXE + " --background", "enabled": True}


def test_on_without_the_startup_approved_key_writes_only_the_run_value():
    reg = FakeReg(approved_key=False)
    DA.set_enabled(True, reg, EXE, no_legacy)
    assert [w[0] for w in reg.writes] == ["sz"]


def test_off_removes_the_run_value_and_is_idempotent():
    reg = FakeReg()
    DA.set_enabled(True, reg, EXE, no_legacy)
    s = DA.set_enabled(False, reg, EXE, no_legacy)
    assert (DA.RUN_KEY, "OmniAPI") not in reg.values and s["enabled"] is False
    assert DA.set_enabled(False, reg, EXE, no_legacy)["enabled"] is False


def test_task_manager_disabled_reads_as_off_like_the_tray():
    reg = FakeReg()
    reg.values[(DA.RUN_KEY, "OmniAPI")] = EXE + " --background"
    # Task Manager "disabled": 03 00 00 00 + a FILETIME (last eight bytes not zero)
    reg.values[(DA.APPROVED_KEY, "OmniAPI")] = bytes([3, 0, 0, 0, 0x10, 0x20, 0x30, 0x40, 1, 2, 3, 4])
    assert DA.status(reg, EXE, no_legacy)["enabled"] is False
    assert DA.set_enabled(True, reg, EXE, no_legacy)["enabled"] is True


def test_an_entry_for_another_exe_is_reported_and_replaced_on_enable():
    reg = FakeReg()
    reg.values[(DA.RUN_KEY, "OmniAPI")] = r"D:\old\OmniAPI.exe --background"
    s = DA.status(reg, EXE, no_legacy)
    assert s["enabled"] is True and s["other"] == r"D:\old\OmniAPI.exe --background"
    s = DA.set_enabled(True, reg, EXE, no_legacy)
    assert s["other"] is None and reg.values[(DA.RUN_KEY, "OmniAPI")] == EXE + " --background"


def test_without_the_desktop_app_nothing_is_read_or_written(monkeypatch):
    monkeypatch.delenv(DA.ENV_VAR, raising=False)
    assert DA.status(NoReg(), legacy=no_legacy) == {"available": False, "enabled": False, "legacy": None, "other": None}
    with pytest.raises(DA.AutostartUnavailable):
        DA.set_enabled(True, NoReg(), legacy=no_legacy)


@pytest.mark.parametrize("value,expected", [
    (EXE, EXE),
    (f'"{EXE}"', EXE),
    ("", None),
    ("   ", None),
    ("OmniAPI.exe", None),  # not a full path: refused
])
def test_shell_exe_comes_only_from_the_variable(value, expected):
    assert DA.shell_exe({DA.ENV_VAR: value}) == expected
    assert DA.shell_exe({}) is None


def test_legacy_text():
    assert DA.legacy_text(False, False) is None
    assert "OmniAPI Daemon.vbs" in DA.legacy_text(True, False)
    assert "工作排程" in DA.legacy_text(False, True) and "omni autostart remove" in DA.legacy_text(True, True)


# ---------------------------------------------------------------- the REST side
@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.catalog import catalog
    from omniapi_mcp.daemon import app as dapp

    async def _no_discovery(*a, **k):
        return {}

    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)
    monkeypatch.setattr(catalog, "refresh", _no_discovery)
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    monkeypatch.setattr(srv, "settings", srv.settings)
    monkeypatch.setattr(DA, "detect_legacy", lambda: "legacy here")
    app = dapp.create_app(Settings(_env_file=None), host="127.0.0.1", port=7799)
    with TestClient(app, base_url="http://127.0.0.1:7799") as client:
        yield client


def test_endpoints_refuse_without_the_desktop_app(daemon, monkeypatch):
    monkeypatch.delenv(DA.ENV_VAR, raising=False)
    monkeypatch.setattr(DA, "_default_registry", lambda: NoReg())
    assert daemon.get("/api/desktop/autostart").json() == {"available": False, "enabled": False, "legacy": None, "other": None}
    r = daemon.put("/api/desktop/autostart", json={"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "unavailable"


def test_endpoints_turn_it_on_and_off(daemon, monkeypatch):
    reg = FakeReg()
    monkeypatch.setenv(DA.ENV_VAR, EXE)
    monkeypatch.setattr(DA, "_default_registry", lambda: reg)
    s = daemon.get("/api/desktop/autostart").json()
    assert s == {"available": True, "enabled": False, "legacy": "legacy here", "other": None}
    # a web page from elsewhere cannot do it
    assert daemon.put("/api/desktop/autostart", json={"enabled": True}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert reg.writes == []
    # the request cannot choose the path; a bad body is refused
    assert daemon.put("/api/desktop/autostart", json={"enabled": "yes"}).status_code == 422
    r = daemon.put("/api/desktop/autostart", json={"enabled": True, "path": r"C:\evil.exe"}, headers={"Origin": "http://127.0.0.1:7799"})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert reg.values[(DA.RUN_KEY, "OmniAPI")] == EXE + " --background"
    assert any(e.get("type") == "desktop.autostart.changed" for e in daemon.get("/api/events").json())
    r = daemon.post("/api/desktop/autostart", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and (DA.RUN_KEY, "OmniAPI") not in reg.values
