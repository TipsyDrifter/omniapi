"""1.3-M2: `omni stop` asks the daemon to shut down first (POST /api/shutdown)
and kills it only when it has not gone after the grace period. Which process
it targets is unchanged (see test_cli_stop.py)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from omniapi_mcp import cli


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path))
    state = {"alive": {4242}, "posts": [], "killed": [], "accept": True, "exits": True}

    def post(url, timeout=None):
        state["posts"].append(url)
        if not state["accept"]:
            return NS(status_code=405, json=lambda: {})
        if state["exits"]:
            state["alive"].discard(4242)
        return NS(status_code=200, json=lambda: {"ok": True, "pid": 4242})

    def run(args, **kw):
        if args[0] == "taskkill":
            state["killed"].append(int(args[2]))
            state["alive"].discard(int(args[2]))
        return NS(stdout="", returncode=0)

    import httpx

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(cli, "_health", lambda host, port, timeout=1.5: {"pid": 4242} if 4242 in state["alive"] else None)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid in state["alive"])
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: run(["taskkill", "/PID", str(pid)]))
    return state


def test_stop_asks_first_and_does_not_kill_a_daemon_that_leaves(world):
    r = CliRunner().invoke(cli.app, ["stop", "--port", "7824", "--grace", "1"])
    assert r.exit_code == 0, r.output
    assert world["posts"] == ["http://127.0.0.1:7824/api/shutdown"]
    assert world["killed"] == [] and "stopped pid 4242" in r.output and "forced" not in r.output


def test_stop_kills_when_the_daemon_does_not_leave_in_time(world):
    world["exits"] = False
    r = CliRunner().invoke(cli.app, ["stop", "--port", "7824", "--grace", "0.5"])
    assert r.exit_code == 0, r.output
    assert world["posts"] and world["killed"] == [4242] and "(forced)" in r.output


def test_stop_kills_an_old_daemon_without_the_endpoint(world):
    world["accept"] = False
    r = CliRunner().invoke(cli.app, ["stop", "--port", "7824", "--grace", "5"])
    assert world["killed"] == [4242] and "(forced)" in r.output


def test_request_shutdown_checks_the_answering_pid(world):
    assert cli.request_shutdown("127.0.0.1", 7824, 4242) is True
    world["alive"].add(4242)
    assert cli.request_shutdown("127.0.0.1", 7824, 9999) is False  # another process answered: not ours to trust
