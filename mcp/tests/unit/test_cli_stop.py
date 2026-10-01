"""`omni stop --port X` must stop the daemon on port X — never the one whose
pid file happens to sit in the current data home (found 2026-09-30: with a
sandbox daemon on another port, the old code read the pid file first and would
have killed the owner's real daemon on 7788)."""

from __future__ import annotations

from omniapi_mcp.cli import stop_target

REAL = {"pid": 111, "host": "127.0.0.1", "port": 7788}


def test_the_daemon_answering_on_the_port_is_the_target():
    # sandbox on 7799 answers with its own pid; the pid file in this home is the real daemon's
    pid, clear, note = stop_target(REAL, {"pid": 222}, 7799, pid_alive=lambda p: True)
    assert (pid, clear, note) == (222, False, "")  # kill 222, leave the real daemon's pid file alone


def test_same_daemon_in_file_and_on_port_clears_the_file():
    assert stop_target(REAL, {"pid": 111}, 7788, pid_alive=lambda p: True) == (111, True, "")


def test_nothing_on_the_port_never_falls_back_to_another_ports_pid_file():
    pid, clear, note = stop_target(REAL, None, 7799, pid_alive=lambda p: True)
    assert pid == 0 and clear is False
    assert "port 7788" in note and "left alone" in note


def test_starting_daemon_on_the_same_port_is_stopped_via_the_pid_file():
    # up but not answering yet: the pid file is for this port and the process is alive
    assert stop_target(REAL, None, 7788, pid_alive=lambda p: True) == (111, True, "")


def test_stale_pid_file_is_removed_without_killing_anything():
    pid, clear, note = stop_target(REAL, None, 7788, pid_alive=lambda p: False)
    assert pid == 0 and clear is True and "stale" in note


def test_no_pid_file_and_no_answer_is_not_running():
    assert stop_target(None, None, 7788) == (0, False, "not running")
    assert stop_target({}, None, 7788) == (0, False, "not running")
