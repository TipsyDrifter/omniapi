"""The desktop test build shares nothing with the released app (1.4 release work).

The test shell's config (``desktop/config/shell.test.json``) and its ``omni.cmd``
(``desktop/config/omni.test.cmd``) set a few variables; without them every value
here is what the released app always used. Fake registry only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omniapi_mcp import cli
from omniapi_mcp import desktop_autostart as DA
from omniapi_mcp.daemon import app as daemon_app


class FakeReg:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], object] = {}
        self.writes: list[tuple] = []

    def get(self, key, name):
        return self.values.get((key, name))

    def key_exists(self, key):
        return True

    def set_string(self, key, name, value):
        self.writes.append(("sz", key, name, value))
        self.values[(key, name)] = value

    def set_binary(self, key, name, value):
        self.writes.append(("bin", key, name, value))
        self.values[(key, name)] = value

    def delete(self, key, name):
        self.writes.append(("del", key, name))
        self.values.pop((key, name), None)


def no_legacy():
    return None

DESKTOP = Path(__file__).resolve().parents[3] / "desktop"


def test_released_defaults_are_unchanged(monkeypatch):
    monkeypatch.delenv("OMNIAPI_DESKTOP_RUN_VALUE", raising=False)
    assert DA.run_value() == "OmniAPI" == DA.RUN_VALUE == cli.DESKTOP_RUN_VALUE
    assert daemon_app._default_port({}) == 7788
    assert daemon_app._default_port({"OMNIAPI_DEFAULT_PORT": ""}) == 7788


def test_default_port_from_the_environment():
    assert daemon_app._default_port({"OMNIAPI_DEFAULT_PORT": "7939"}) == 7939
    for bad in ("0", "70000", "x", "-1", "7939 "):
        want = 7939 if bad == "7939 " else 7788
        assert daemon_app._default_port({"OMNIAPI_DEFAULT_PORT": bad}) == want


def test_test_build_switch_writes_its_own_value(monkeypatch):
    monkeypatch.setenv("OMNIAPI_DESKTOP_RUN_VALUE", "OmniAPI-Test")
    reg = FakeReg()
    reg.values[(DA.RUN_KEY, "OmniAPI")] = r"C:\Users\u\AppData\Local\OmniAPI\OmniAPI.exe --background"
    exe = r"C:\T\OmniAPI-Test\OmniAPI-Test.exe"
    s = DA.status(reg, exe, no_legacy)
    assert s["enabled"] is False and s["other"] is None, "the released app's entry is not the test build's"
    DA.set_enabled(True, reg, exe, no_legacy)
    DA.set_enabled(False, reg, exe, no_legacy)
    names = {w[2] for w in reg.writes}
    assert names == {"OmniAPI-Test"}, reg.writes
    assert reg.values[(DA.RUN_KEY, "OmniAPI")].endswith("OmniAPI.exe --background"), "released entry untouched"


def test_cli_status_reads_the_test_value(monkeypatch):
    seen = []
    monkeypatch.setenv("OMNIAPI_DESKTOP_RUN_VALUE", "OmniAPI-Test")
    assert cli.desktop_autostart(lambda k, n: seen.append(n)) is None
    assert seen == ["OmniAPI-Test"]
    monkeypatch.delenv("OMNIAPI_DESKTOP_RUN_VALUE")
    seen.clear()
    cli.desktop_autostart(lambda k, n: seen.append(n))
    assert seen == ["OmniAPI"]


@pytest.mark.parametrize("cmd", [["autostart", "install"], ["autostart", "remove"]])
def test_test_build_refuses_the_shared_old_launchers(monkeypatch, cmd):
    monkeypatch.setenv("OMNIAPI_DESKTOP_RUN_VALUE", "OmniAPI-Test")
    monkeypatch.setattr(cli.os, "name", "nt")

    def boom(*a, **k):
        raise AssertionError("nothing may be run")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    monkeypatch.setattr(cli, "_startup_script", lambda: (_ for _ in ()).throw(AssertionError("startup folder touched")))
    r = CliRunner().invoke(cli.app, cmd)
    assert r.exit_code == 1 and "test build" in r.output


def test_shipped_test_config_is_separate():
    c = json.loads((DESKTOP / "config" / "shell.test.json").read_text(encoding="utf-8"))
    e = c["service"]["env"]
    assert c["port"] != 7788
    assert e["OMNIAPI_HOME"] == "{default_home}" and e["STORAGE__BASE_PATH"].startswith("{default_home}")
    assert e["OMNIAPI_DESKTOP_RUN_VALUE"] == "OmniAPI-Test"
    assert e["OMNIAPI_CLAUDE_CONFIG"].startswith("{default_home}")
    assert (e["OMNIAPI_DEV"], e["OMNIAPI_OFFLINE"]) == ("1", "1")
    assert c["update_check"]["enabled"] is False
    released = json.loads((DESKTOP / "config" / "shell.installed.json").read_text(encoding="utf-8"))
    assert released["port"] == 7788 and "OMNIAPI_DESKTOP_RUN_VALUE" not in released["service"]["env"]
    cmd = (DESKTOP / "config" / "omni.test.cmd").read_bytes()
    assert b"OMNIAPI_DEFAULT_PORT=7939" in cmd and b"OmniAPI-Test" in cmd and b"\r\n" in cmd
    assert all(b < 128 for b in cmd), "cmd.exe reads it in the OEM code page: ASCII only"
