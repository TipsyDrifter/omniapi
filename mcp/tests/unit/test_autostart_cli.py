"""1.3-M5: `omni autostart` knows about the desktop app's logon entry, so the service is
never started twice at logon. The registry and the Startup folder are faked: nothing here
touches the real logon settings."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omniapi_mcp import cli

RUN, APPROVED = cli._RUN_KEY, cli._APPROVED_KEY
CMD = r"C:\Users\u\AppData\Local\OmniAPI\OmniAPI.exe --background"


def _reg(values: dict):
    return lambda key, name: values.get((key, name))


def test_no_entry_means_no_desktop_autostart():
    assert cli.desktop_autostart(_reg({})) is None


def test_entry_is_read_with_its_command():
    assert cli.desktop_autostart(_reg({(RUN, "OmniAPI"): CMD})) == {"command": CMD, "enabled": True}


@pytest.mark.parametrize(
    "approved, enabled",
    [
        (bytes([2] + [0] * 11), True),  # what tauri-plugin-autostart writes
        (bytes([3] + [1] * 11), False),  # switched off in Task Manager
        (bytes([6] + [0] * 11), True),
        (bytes([7] + [9] * 11), False),
        (None, True),  # no StartupApproved value: Windows runs it
    ],
)
def test_task_manager_switch_is_respected(approved, enabled):
    vals = {(RUN, "OmniAPI"): CMD}
    if approved is not None:
        vals[(APPROVED, "OmniAPI")] = approved
    assert cli.desktop_autostart(_reg(vals))["enabled"] is enabled


@pytest.fixture
def fake_logon(tmp_path, monkeypatch):
    """Startup folder in tmp, schtasks answering 'not installed', desktop entry switchable."""
    state = {"desk": None, "task": False}
    monkeypatch.setattr(cli, "_startup_dir", lambda: tmp_path / "Startup")
    monkeypatch.setattr(cli, "desktop_autostart", lambda read=None: state["desk"])
    monkeypatch.setattr(cli.os, "name", "nt")

    def fake_run(args, **kw):
        assert args[0] == "schtasks", args  # nothing else may run
        if args[1] == "/Query":
            return SimpleNamespace(returncode=0 if state["task"] else 1, stdout="", stderr="")
        raise AssertionError(f"unexpected schtasks call {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


def test_status_says_the_desktop_app_has_taken_over(fake_logon):
    fake_logon["desk"] = {"command": CMD, "enabled": True}
    out = CliRunner().invoke(cli.app, ["autostart", "status"]).output
    assert "desktop app has taken over" in out and CMD in out and "note:" not in out


def test_status_warns_when_both_start_at_logon(fake_logon, tmp_path):
    fake_logon["desk"] = {"command": CMD, "enabled": True}
    (tmp_path / "Startup").mkdir()
    (tmp_path / "Startup" / "OmniAPI Daemon.vbs").write_text("x")
    out = CliRunner().invoke(cli.app, ["autostart", "status"]).output
    assert "note: both the desktop app and the old launcher start at logon" in out


def test_status_without_the_desktop_app(fake_logon):
    out = CliRunner().invoke(cli.app, ["autostart", "status"]).output
    assert "desktop app:    not set to start at logon" in out


def test_install_is_refused_while_the_desktop_app_starts_at_logon(fake_logon, tmp_path):
    fake_logon["desk"] = {"command": CMD, "enabled": True}
    r = CliRunner().invoke(cli.app, ["autostart", "install"])
    assert r.exit_code == 1 and "already starts at logon" in r.output
    assert not (tmp_path / "Startup" / "OmniAPI Daemon.vbs").exists()


def test_install_goes_ahead_when_the_desktop_entry_is_disabled_or_forced(fake_logon, tmp_path):
    fake_logon["desk"] = {"command": CMD, "enabled": False}
    assert CliRunner().invoke(cli.app, ["autostart", "install"]).exit_code == 0
    script = tmp_path / "Startup" / "OmniAPI Daemon.vbs"
    assert script.exists()
    script.unlink()
    fake_logon["desk"] = {"command": CMD, "enabled": True}
    assert CliRunner().invoke(cli.app, ["autostart", "install", "--force"]).exit_code == 0 and script.exists()
