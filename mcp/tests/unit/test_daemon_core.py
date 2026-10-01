"""Unit tests for the M2 daemon core: store, event bus, call recorder."""

from __future__ import annotations

import asyncio

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.recorder import extract_call_meta, make_recorded, summarize_args
from omniapi_mcp.store.db import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.open()
    yield s
    await s.close()


# --------------------------------------------------------------------------- store


async def test_store_calls_roundtrip(store):
    cid = await store.call_started("complete_text", {"prompt": "hi", "model": "cheap"}, source="mcp")
    await store.call_finished(cid, status="ok", duration_ms=12, model="deepseek-flash", provider="deepseek",
                              cost_usd=0.00001, usage={"prompt_tokens": 3}, result={"text": "OK"})
    row = await store.call(cid)
    assert row["status"] == "ok" and row["model"] == "deepseek-flash"
    assert row["args"] == {"prompt": "hi", "model": "cheap"}
    assert row["usage"] == {"prompt_tokens": 3} and row["result"] == {"text": "OK"}
    rows = await store.calls(limit=10, tool="complete_text")
    assert [r["id"] for r in rows] == [cid]
    summary = await store.cost_summary(days=1)
    assert summary["total"]["n"] == 1 and summary["by_model"][0]["model"] == "deepseek-flash"


async def test_store_conversations_and_messages(store):
    conv = await store.create_conversation(kind="chat", title="t", model="gpt-6-sol", system_prompt="be brief")
    cid = conv["id"]
    m1 = await store.add_message(cid, role="user", content="hello")
    m2 = await store.add_message(cid, role="assistant", content="hi", model="gpt-6-sol", cost_usd=0.001)
    assert (m1["seq"], m2["seq"]) == (1, 2)
    msgs = await store.messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["content"] == "hi"
    await store.update_conversation(cid, title="renamed", meta={"x": 1})
    row = await store.conversation(cid)
    assert row["title"] == "renamed" and row["meta"] == {"x": 1}
    assert (await store.conversations(kind="chat"))[0]["id"] == cid


async def test_store_runs_and_events(store):
    await store.create_run("r1", harness="claude", model="deepseek-flash", cwd="C:/x", prompt="do", meta={"k": 1})
    await store.add_event("r1", "tool_call", {"name": "Read"})
    last = await store.add_event("r1", "text", {"t": "done"})
    await store.update_run("r1", state="done", turns=3, cost_usd=0.02)
    run = await store.run("r1")
    assert run["state"] == "done" and run["meta"] == {"k": 1}
    ev = await store.events("r1")
    assert [e["type"] for e in ev] == ["tool_call", "text"]
    assert await store.events("r1", after_id=last) == []
    stats = await store.stats()
    assert stats["runs"] == 1 and stats["events"] == 2


# --------------------------------------------------------------------------- bus


async def test_bus_publish_subscribe_and_history():
    bus = EventBus(history=3)
    q = bus.subscribe()
    for i in range(4):
        await bus.publish({"type": "t", "i": i})
    got = [q.get_nowait() for _ in range(4)]
    assert [g["i"] for g in got] == [0, 1, 2, 3] and got[0]["seq"] == 1 and "ts" in got[0]
    assert [e["i"] for e in bus.recent()] == [1, 2, 3]  # history capped at 3
    assert [e["i"] for e in bus.recent(since_seq=3)] == [3]
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


async def test_bus_drops_oldest_for_slow_consumer():
    bus = EventBus(queue_size=2)
    q = bus.subscribe()
    for i in range(3):
        await bus.publish({"type": "t", "i": i})
    assert [q.get_nowait()["i"] for _ in range(2)] == [1, 2]


# --------------------------------------------------------------------------- recorder


def test_summarize_args_hides_blobs_and_secrets():
    out = summarize_args({"image_data": "x" * 5000, "prompt": "p", "api_key": "sk-1", "skip": None,
                          "nested": {"token": "abc", "ok": 1}, "additional_images": ["a", "b"]})
    assert out["image_data"] == "<5000 chars>" and out["additional_images"] == "<2 images>"
    assert out["api_key"] == "sk-1"  # top-level arg names are not secrets by convention...
    assert out["nested"] == {"token": "***", "ok": 1}
    assert "skip" not in out


def test_extract_call_meta_variants():
    m = extract_call_meta({"text": "x", "model": "m", "provider": "p", "cost_usd": 0.5, "usage": {"a": 1}})
    assert (m["model"], m["provider"], m["cost_usd"], m["ticket"]) == ("m", "p", 0.5, False)
    m = extract_call_meta({"status": "running", "task_id": "job_1"})
    assert m["ticket"] is True
    m = extract_call_meta({"metadata": {"model": "gpt-image-2", "provider": "openai", "cost_estimate": {"estimated_cost_usd": 0.04}}})
    assert m["model"] == "gpt-image-2" and m["cost_usd"] == 0.04
    assert extract_call_meta("plain") == {}


async def test_recorder_records_ok_error_and_ticket(store):
    bus = EventBus()

    class Ctx:
        pass

    ctx = Ctx(); ctx.store = store; ctx.bus = bus
    recorded = make_recorded(lambda: ctx, source="test")

    @recorded
    async def tool_ok(prompt: str, model: str | None = None) -> dict:
        return {"text": "hi", "model": "m1", "provider": "p1", "cost_usd": 0.2}

    @recorded
    async def tool_ticket(prompt: str) -> dict:
        return {"status": "running", "task_id": "job_x"}

    @recorded
    async def tool_boom(prompt: str) -> dict:
        raise ValueError("nope")

    import inspect

    assert list(inspect.signature(tool_ok).parameters) == ["prompt", "model"]
    q = bus.subscribe()
    assert (await tool_ok(prompt="a"))["text"] == "hi"
    assert (await tool_ticket(prompt="b"))["status"] == "running"
    with pytest.raises(ValueError):
        await tool_boom(prompt="c")

    rows = await store.calls(limit=10)
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["tool_ok"]["status"] == "ok" and by_tool["tool_ok"]["cost_usd"] == 0.2
    assert by_tool["tool_ticket"]["status"] == "ticket"
    assert by_tool["tool_boom"]["status"] == "error" and "nope" in by_tool["tool_boom"]["error"]
    types = [q.get_nowait()["type"] for _ in range(6)]
    assert types == ["call.started", "call.finished"] * 3


async def test_recorder_survives_missing_context():
    recorded = make_recorded(lambda: None)

    @recorded
    async def tool(x: int) -> int:
        return x + 1

    assert await tool(x=1) == 2
