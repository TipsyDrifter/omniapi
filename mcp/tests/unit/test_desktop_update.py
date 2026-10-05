"""1.3-M6: the desktop app's new-version check, as /api/status shows it."""

from __future__ import annotations

import json

import pytest

from omniapi_mcp import desktop_update as du


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path))


def _write(tmp_path, **kw):
    (tmp_path / du.FILE_NAME).write_text(json.dumps(kw), encoding="utf-8")


def test_no_file_means_no_desktop_app():
    assert du.read("1.2.0") is None


def test_newer_is_recomputed_against_the_running_version(tmp_path):
    _write(tmp_path, enabled=True, current="1.2.0", latest="1.3.0", url="https://github.com/x/releases/tag/v1.3.0", newer=True, checked_at_ms=5)
    r = du.read("1.2.0")
    assert r["newer"] is True and r["latest"] == "1.3.0" and r["url"].startswith("https://")
    assert du.read("1.3.0")["newer"] is False  # after the update it is not new any more


def test_failed_check_and_turned_off(tmp_path):
    _write(tmp_path, enabled=True, attempted_at_ms=9, error="winhttp error 12007")
    r = du.read("1.2.0")
    assert r["newer"] is False and r["error"] and r["checked_at_ms"] is None
    _write(tmp_path, enabled=False)
    assert du.read("1.2.0")["enabled"] is False


def test_bad_url_and_garbage(tmp_path):
    _write(tmp_path, latest="9.0.0", url="javascript:alert(1)")
    assert du.read("1.2.0")["newer"] is False and du.read("1.2.0")["url"] is None
    (tmp_path / du.FILE_NAME).write_text("not json", encoding="utf-8")
    assert du.read("1.2.0") is None


@pytest.mark.parametrize("latest,current,newer", [
    ("1.3.0", "1.2.0", True), ("v1.2.1", "1.2.0", True), ("1.2.0", "1.2.0", False), ("1.1.0", "1.2.0", False),
    ("1.3.0", "1.3.0-rc.1", True), ("1.3.0-rc.1", "1.3.0", False), ("1.3.0-rc.10", "1.3.0-rc.9", True), ("x", "1.2.0", False),
])
def test_versions(latest, current, newer):
    assert du.is_newer(latest, current) is newer
