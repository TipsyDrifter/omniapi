"""M5: resume chains (追問串), recent cwds, and the development-only replay harness."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.harness.events import RunSpec
from omniapi_mcp.harness.registry import HarnessRegistry
from omniapi_mcp.runs.manager import RunManager
from omniapi_mcp.store.db import Store


def _settings():
    prov = SimpleNamespace(enabled=True, api_key="k")
    return SimpleNamespace(providers=SimpleNamespace(deepseek=prov, openai=prov, gemini=prov, anthropic=prov, openrouter=prov))


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.open()
    yield s
    await s.close()


async def _mk(store: Store, rid: str, *, parent: str | None = None, ago: float = 0, state: str = "done", session: str | None = "s", cwd: str | None = None, harness: str = "claude"):
    await store.create_run(
        rid,
        title=rid,
        prompt="p",
        harness=harness,
        model="m",
        cwd=cwd,
        state=state,
        started_at=time.time() - ago,
        session_id=session,
        meta={"resume_run_id": parent},
    )


@pytest.mark.asyncio
async def test_thread_follows_the_branch_that_contains_the_run(store):
    mgr = RunManager(store, EventBus(), _settings())
    await _mk(store, "a", ago=500)
    await _mk(store, "b", parent="a", ago=400)
    await _mk(store, "c", parent="b", ago=300)
    await _mk(store, "d", parent="a", ago=100)  # a newer sibling branch of b

    t = await mgr.thread("c")
    assert [r["id"] for r in t["runs"]] == ["a", "b", "c"]
    assert (t["root_id"], t["leaf_id"]) == ("a", "c")
    assert t["resumable"] is True and t["reason"] is None

    # from the middle: up to the root, then down to the leaf
    assert [r["id"] for r in (await mgr.thread("b"))["runs"]] == ["a", "b", "c"]
    # from the root: below it, the most recent child wins
    assert [r["id"] for r in (await mgr.thread("a"))["runs"]] == ["a", "d"]
    assert await mgr.thread("nope") is None


@pytest.mark.asyncio
async def test_thread_resumable_reasons(store):
    mgr = RunManager(store, EventBus(), _settings())
    await _mk(store, "nosess", session=None)
    t = await mgr.thread("nosess")
    assert t["resumable"] is False and "session" in t["reason"]

    # a row still marked running with no task behind it is a dead run; it has a session so it can be resumed
    await _mk(store, "ghost", state="running")
    t = await mgr.thread("ghost")
    assert t["runs"][0]["state"] == "dead" and t["runs"][0]["live"] is False
    assert t["resumable"] is True


@pytest.mark.asyncio
async def test_thread_survives_a_cycle(store):
    mgr = RunManager(store, EventBus(), _settings())
    await _mk(store, "x", parent="y", ago=20)
    await _mk(store, "y", parent="x", ago=10)
    ids = [r["id"] for r in (await mgr.thread("x"))["runs"]]
    assert sorted(ids) == ["x", "y"]


@pytest.mark.asyncio
async def test_cwds_are_grouped_and_most_recent_first(store):
    await _mk(store, "r1", cwd="C:/old", ago=300)
    await _mk(store, "r2", cwd="C:/new", ago=10)
    await _mk(store, "r3", cwd="C:/old", ago=200)
    await _mk(store, "r4", cwd=None, ago=5)
    rows = await store.cwds()
    assert [(r["cwd"], r["n"]) for r in rows] == [("C:/new", 1), ("C:/old", 2)]


def test_replay_is_refused_unless_dev(monkeypatch):
    monkeypatch.delenv("OMNIAPI_DEV", raising=False)
    with pytest.raises(ValueError, match="development only"):
        HarnessRegistry(_settings()).resolve(RunSpec(prompt="x", harness="replay", model="replay"))


@pytest.mark.asyncio
async def test_replay_run_then_resume_builds_a_thread(store, monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_REPLAY_DELAY", "0")
    await _mk(store, "src", ago=900)
    await store.add_event("src", "session_start", {"session_id": "orig"})
    await store.add_event("src", "status", {"message": "hook_started"})
    await store.add_event("src", "text", {"text": "先看資料"})
    await store.add_event("src", "tool_call", {"id": "t1", "name": "Bash", "input": {"command": "ls"}})
    await store.add_event("src", "tool_result", {"id": "t1", "name": "Bash", "output": "a\nb", "is_error": False})
    await store.add_event("src", "result", {"text": "done", "cost_usd": 1.0, "num_turns": 1})

    bus = EventBus()
    mgr = RunManager(store, bus, _settings())
    first = await mgr.start(RunSpec(prompt="重播", harness="replay", model="replay:src", dispatcher="test"))
    await asyncio.wait_for(mgr._tasks[first["run_id"]], 5)
    row = await mgr.get(first["run_id"])
    assert row["state"] == "done" and row["harness"] == "replay" and row["cost_usd"] == 0.0
    types = [e["type"] for e in row["events"]]
    assert types == ["session_start", "text", "tool_call", "tool_result", "result"]  # status noise dropped
    assert row["turns"] == 1 and row["session_id"]

    second = await mgr.start(RunSpec(prompt="再補一點", resume_run_id=first["run_id"]))
    await asyncio.wait_for(mgr._tasks[second["run_id"]], 5)
    row2 = await mgr.get(second["run_id"])
    assert row2["state"] == "done" and row2["session_id"] == row["session_id"]
    assert any("再補一點" in (e["payload"].get("text") or "") for e in row2["events"] if e["type"] == "text")

    t = await mgr.thread(first["run_id"])
    assert [r["id"] for r in t["runs"]] == [first["run_id"], second["run_id"]]
    assert t["resumable"] is True


@pytest.mark.asyncio
async def test_replay_can_be_cancelled(store, monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_REPLAY_DELAY", "0.2")
    await _mk(store, "src", ago=900)
    for i in range(20):
        await store.add_event("src", "text", {"text": f"line {i}"})
    mgr = RunManager(store, EventBus(), _settings())
    r = await mgr.start(RunSpec(prompt="重播", harness="replay", model="replay:src"))
    await asyncio.sleep(0.5)
    out = await mgr.cancel(r["run_id"])
    assert out["cancelled"] is True
    row = await mgr.get(r["run_id"])
    assert row["state"] == "cancelled" and row["live"] is False
