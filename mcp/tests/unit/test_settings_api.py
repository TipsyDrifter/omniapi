"""1.3-M2 REST: /api/settings (read, change without restart, test a key),
/api/shutdown, the local-only guard and the rotated daemon log — against the
real app and a real (offline) runtime."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from omniapi_mcp.catalog import catalog
from omniapi_mcp.config.settings import Settings

KEY = "sk-deepseek-test-0123456789wxyz"


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.daemon import app as dapp

    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "TIERS__", "DEFAULTS__")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)
    monkeypatch.setattr(catalog, "refresh", _no_discovery)
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    monkeypatch.setattr(srv.mcp.settings, "streamable_http_path", srv.mcp.settings.streamable_http_path)
    monkeypatch.setattr(srv, "settings", srv.settings)
    app = dapp.create_app(Settings(_env_file=None), host="127.0.0.1", port=7799)
    with TestClient(app, base_url="http://127.0.0.1:7799") as client:
        client.app_ref = app
        yield client
    catalog.set_tier_overrides({})


async def _no_discovery(*a, **k):
    return {}


def test_get_settings_never_carries_a_key(daemon):
    v = daemon.get("/api/settings").json()
    assert set(v) >= {"path", "providers", "tiers", "defaults", "restart_required"}
    assert v["providers"]["deepseek"] == {**v["providers"]["deepseek"], "configured": False, "key": {"set": False, "last4": None, "source": None, "shadowed": None}}
    assert v["tiers"]["cheap"]["model"] == catalog.catalog_tiers["cheap"]
    assert v["defaults"]["chat"] == {"model": "cheap", "source": "default"}


def test_a_key_set_over_the_api_works_at_once(daemon, tmp_path):
    from omniapi_mcp.runtime import runtime

    events_before = daemon.get("/api/events").json()
    old_text_tool = runtime.context.text_tool
    assert "deepseek" not in daemon.get("/api/status").json()["providers"]["configured"]
    assert daemon.get("/api/models").json()["providers"]["deepseek"]["configured"] is False

    r = daemon.patch("/api/settings", json={"providers": {"deepseek": {"api_key": KEY}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert KEY not in r.text
    assert body["changed"] == ["providers.deepseek.api_key"] and body["providers_changed"] == ["deepseek"]
    assert body["providers"]["deepseek"]["key"] == {"set": True, "last4": KEY[-4:], "source": "settings", "shadowed": None}

    # no restart: status, the model list and the generate options see it now
    st = daemon.get("/api/status").json()
    assert "deepseek" in st["providers"]["configured"] and "deepseek" in st["providers"]["text"]
    assert daemon.get("/api/models").json()["providers"]["deepseek"]["configured"] is True
    assert daemon.get("/api/generate/options").status_code == 200
    assert runtime.context.text_tool is not old_text_tool  # rebuilt and swapped in
    assert runtime.context.chat.text is runtime.context.text_tool
    assert old_text_tool in runtime._retired  # work started on it finishes on it

    # stored in settings.json, with the key, in the data home only
    saved = json.loads((tmp_path / "home" / "settings.json").read_text(encoding="utf-8"))
    assert saved["providers"]["deepseek"]["api_key"] == KEY

    # the page hears about it, without the key
    new_events = [e for e in daemon.get("/api/events").json() if e not in events_before]
    changed = [e for e in new_events if e.get("type") == "settings.changed"]
    assert changed and changed[-1]["changed"] == ["providers.deepseek.api_key"] and KEY not in json.dumps(new_events)

    # and back: null removes it from settings.json
    r = daemon.patch("/api/settings", json={"providers": {"deepseek": {"api_key": None}}})
    assert r.json()["providers"]["deepseek"]["configured"] is False
    assert "deepseek" not in daemon.get("/api/status").json()["providers"]["configured"]


def test_tiers_and_defaults_apply_without_restart(daemon):
    from omniapi_mcp.runtime import runtime

    r = daemon.patch("/api/settings", json={"tiers": {"cheap": "gpt-5.4-mini"}, "defaults": {"chat": "echo", "dispatch": "replay", "image": "gpt-image-1.5"}})
    assert r.status_code == 200, r.text
    assert catalog.resolve("cheap") == "gpt-5.4-mini"
    assert daemon.get("/api/status").json()["tiers"]["cheap"] == "gpt-5.4-mini"
    assert daemon.get("/api/models").json()["tiers"]["cheap"] == "gpt-5.4-mini"
    assert runtime.context.settings.images.default_model == "gpt-image-1.5"
    # a new chat without a model takes the chat default
    conv = daemon.post("/api/chat", json={}).json()
    assert conv["model"] == "echo"
    # an agent run without a model takes the dispatch default
    run = daemon.post("/api/runs", json={"prompt": "x", "cwd": str(Path.cwd())})
    assert run.status_code == 200, run.text
    assert run.json()["harness"] == "replay"
    daemon.post(f"/api/runs/{run.json()['run_id']}/cancel")


def test_bad_patches_are_refused_and_change_nothing(daemon, tmp_path):
    for body in ({"providers": {"acme": {"api_key": "x"}}}, {"tiers": {"cheap": "strong"}}, {"oops": 1},
                 {"providers": {"openai": {"api_key": "has space"}}}):
        r = daemon.patch("/api/settings", json=body)
        assert r.status_code == 400, (body, r.text)
    assert not (tmp_path / "home" / "settings.json").exists()


def test_test_key_offline_is_simulated(daemon):
    r = daemon.post("/api/settings/test-key", json={"provider": "deepseek", "api_key": KEY}).json()
    assert r["ok"] is True and r["simulated"] is True and KEY not in json.dumps(r)
    r = daemon.post("/api/settings/test-key", json={"provider": "deepseek"}).json()  # the key in effect: none
    assert r["ok"] is False and r["reason"] == "missing_key"
    assert daemon.post("/api/settings/test-key", json={"provider": "acme"}).status_code == 400


def test_shutdown_asks_the_server_to_exit(daemon):
    class FakeServer:
        should_exit = False

    daemon.app_ref.state.uvicorn_server = FakeServer()
    r = daemon.post("/api/shutdown")
    assert r.status_code == 200 and r.json() == {"ok": True, "pid": os.getpid()}
    for _ in range(100):
        if FakeServer.should_exit or daemon.app_ref.state.uvicorn_server.should_exit:
            break
        time.sleep(0.02)
    assert daemon.app_ref.state.uvicorn_server.should_exit is True
    assert any(e.get("type") == "daemon.stopping" for e in daemon.get("/api/events").json())


def test_shutdown_without_a_server_says_so(daemon):
    assert daemon.post("/api/shutdown").status_code == 503


# ---------------------------------------------------------------- guard
def test_guard_blocks_foreign_origins_and_hosts(daemon):
    ok = daemon.patch("/api/settings", json={}, headers={"Origin": "http://localhost:5178"})  # the Vite dev server
    assert ok.status_code == 200
    assert daemon.patch("/api/settings", json={}, headers={"Origin": "http://127.0.0.1:7799"}).status_code == 200
    assert daemon.patch("/api/settings", json={}, headers={"Origin": "http://[::1]:7799"}).status_code == 200
    for origin in ("https://evil.example", "null", "http://127.0.0.1.evil.example", "file://"):
        r = daemon.post("/api/settings/test-key", json={"provider": "deepseek"}, headers={"Origin": origin})
        assert r.status_code == 403, origin
    r = daemon.post("/api/shutdown", headers={"Host": "evil.example:7799"})  # DNS rebinding
    assert r.status_code == 403
    r = daemon.post("/api/chat", json={}, headers={"Host": "attacker.test"})
    assert r.status_code == 403
    # reads are not affected; curl-style calls without Origin pass
    assert daemon.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200
    assert daemon.post("/api/settings/test-key", json={"provider": "deepseek"}).status_code == 200


def test_guard_on_websockets(daemon):
    from starlette.websockets import WebSocketDisconnect

    with daemon.websocket_connect("ws://127.0.0.1:7799/ws", headers={"Origin": "http://localhost:5178"}) as ws:
        assert ws.receive_json()["type"] == "hello"
    with pytest.raises(WebSocketDisconnect) as e:
        with daemon.websocket_connect("ws://127.0.0.1:7799/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 1008
    with pytest.raises(WebSocketDisconnect):
        with daemon.websocket_connect("ws://evil.example:7799/ws") as ws:
            ws.receive_json()


def test_guard_check_rules():
    from omniapi_mcp.daemon.guard import check

    def scope(kind="http", method="POST", path="/api/x", host="127.0.0.1:7788", origin=None):
        h = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin is not None else [])
        return {"type": kind, "method": method, "path": path, "headers": h}

    assert check(scope()) is None
    assert check(scope(host="localhost")) is None and check(scope(host="[::1]:1")) is None
    assert check(scope(host="192.168.1.5:7788")) == "host"
    assert check(scope(origin="http://evil.example")) == "origin"
    assert check(scope(method="GET", host="evil.example")) is None  # reads unchanged
    assert check(scope(path="/mcp", host="evil.example", origin="http://evil.example")) is None  # /mcp untouched
    assert check(scope(path="/mcp/", host="evil.example")) is None
    assert check(scope(path="/assets/x.js", host="evil.example")) is None
    assert check(scope(kind="websocket", path="/ws", host="evil.example")) == "host"
    assert check(scope(host="")) == "host"


# ---------------------------------------------------------------- log rotation
def test_daemon_log_rotates_and_keeps_n_copies(tmp_path):
    from omniapi_mcp.daemon.logfile import RotatingLogStream

    log = tmp_path / "daemon.log"
    s = RotatingLogStream(log, max_bytes=200, backups=2)
    for i in range(40):
        s.write(f"line {i:03d} " + "x" * 20 + "\n")
    s.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["daemon.log", "daemon.log.1", "daemon.log.2"]
    assert all(p.stat().st_size <= 260 for p in tmp_path.iterdir())
    assert "line 039" in log.read_text(encoding="utf-8")


def test_a_rename_blocked_by_another_reader_does_not_lose_lines(tmp_path, monkeypatch):
    from omniapi_mcp.daemon import logfile

    log = tmp_path / "daemon.log"
    s = logfile.RotatingLogStream(log, max_bytes=100, backups=2)
    real = os.replace

    def blocked(src, dst):
        if Path(src) == log:
            raise PermissionError(13, "in use")
        return real(src, dst)

    monkeypatch.setattr(logfile.os, "replace", blocked)
    for i in range(10):
        s.write(f"line {i}\n" + "y" * 30 + "\n")
    s.close()
    text = log.read_text(encoding="utf-8")
    assert all(f"line {i}" in text for i in range(10)) and "could not rotate" in text
    assert not (tmp_path / "daemon.log.1").exists()


def test_logging_and_prints_share_the_rotating_stream(tmp_path):
    import logging

    from omniapi_mcp.daemon.logfile import RotatingLogStream

    s = RotatingLogStream(tmp_path / "daemon.log", max_bytes=10_000, backups=1)
    h = logging.StreamHandler(s)
    lg = logging.getLogger("omni.test.rotate")
    lg.addHandler(h)
    lg.propagate = False
    try:
        lg.warning("from logging")
        print("from print", file=s)
    finally:
        lg.removeHandler(h)
        s.close()
    text = (tmp_path / "daemon.log").read_text(encoding="utf-8")
    assert "from logging" in text and "from print" in text
