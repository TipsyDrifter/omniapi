"""M6: provider streaming, the echo model, ChatManager turns, export, store migration."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace as NS

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.capabilities.text import AnthropicTextProvider, DeepSeekTextProvider, OpenAITextProvider, TextProvider, TextResult
from omniapi_mcp.chat import ChatError, ChatManager
from omniapi_mcp.providers.base import ProviderConfig
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.text import TextTool


# ---------------------------------------------------------------- helpers
class _AsyncIter:
    def __init__(self, items, fail_after: int | None = None):
        self._items = list(items)
        self._fail_after = fail_after
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._fail_after is not None and self._i >= self._fail_after:
            raise RuntimeError("connection dropped")
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


def _cfg() -> ProviderConfig:
    return ProviderConfig(api_key="k", enabled=True)


def _settings_without_providers():
    off = NS(enabled=False, api_key="")
    return NS(providers=NS(openai=off, deepseek=off, anthropic=off, gemini=off, openrouter=off))


async def _collect(agen):
    return [x async for x in agen]


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "chat.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0")


@pytest.fixture
async def chat(store, dev):
    bus = EventBus()
    mgr = ChatManager(store, bus, TextTool(_settings_without_providers()))
    yield mgr
    await mgr.close()


async def _wait(mgr: ChatManager, cid: str):
    task = mgr._tasks.get(cid)
    if task:
        await asyncio.wait_for(task, 5)


# ---------------------------------------------------------------- provider streaming
@pytest.mark.asyncio
async def test_openai_stream_yields_deltas_then_result():
    p = OpenAITextProvider(_cfg())
    seen = {}

    def chunk(content=None, reasoning=None, finish=None, usage=None):
        delta = NS(content=content, reasoning_content=reasoning, model_extra={})
        return NS(model="gpt-5.4-mini-2026", choices=[NS(delta=delta, finish_reason=finish)], usage=usage)

    usage = NS(prompt_tokens=12, completion_tokens=5, total_tokens=17, completion_tokens_details=None, prompt_tokens_details=None, model_extra={})

    async def create(**req):
        seen.update(req)
        return _AsyncIter([chunk(reasoning="想一下"), chunk(content="你"), chunk(content="好"), chunk(finish="stop"), NS(model="gpt-5.4-mini-2026", choices=[], usage=usage)])

    p.client = NS(chat=NS(completions=NS(create=create)))
    out = await _collect(p.stream("gpt-5.4-mini", [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], temperature=0.3))

    assert seen["stream"] is True and seen["stream_options"] == {"include_usage": True}
    assert seen["messages"][0]["role"] == "developer"  # same dialect rules as complete()
    assert "temperature" not in seen  # reasoning model: sampling params dropped
    assert [(o["type"], o.get("delta")) for o in out[:-1]] == [("reasoning", "想一下"), ("text", "你"), ("text", "好")]
    res = out[-1]["result"]
    assert out[-1]["type"] == "done" and isinstance(res, TextResult)
    assert (res.text, res.reasoning, res.finish_reason, res.model) == ("你好", "想一下", "stop", "gpt-5.4-mini-2026")
    assert res.usage["total_tokens"] == 17


@pytest.mark.asyncio
async def test_openai_family_stream_keeps_dialect_and_wraps_errors():
    p = DeepSeekTextProvider(_cfg())
    seen = {}

    async def create(**req):
        seen.update(req)
        return _AsyncIter([NS(model="deepseek-flash", choices=[NS(delta=NS(content="a", model_extra={}), finish_reason=None)], usage=None)], fail_after=1)

    p.client = NS(chat=NS(completions=NS(create=create)))
    got = []
    with pytest.raises(Exception, match="text stream failed"):
        async for piece in p.stream("deepseek-flash", [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], max_completion_tokens=50):
            got.append(piece)
    assert got == [{"type": "text", "delta": "a"}]  # what arrived before the drop was delivered
    assert seen["messages"][0]["role"] == "system" and seen["max_tokens"] == 50


@pytest.mark.asyncio
async def test_anthropic_stream_reads_raw_events():
    p = AnthropicTextProvider(_cfg())
    seen = {}
    events = [
        NS(type="message_start", message=NS(model="claude-sonnet-5", usage=NS(input_tokens=20, cache_read_input_tokens=4))),
        NS(type="content_block_start", index=0),
        NS(type="content_block_delta", delta=NS(type="thinking_delta", thinking="先想")),
        NS(type="content_block_delta", delta=NS(type="text_delta", text="哈")),
        NS(type="content_block_delta", delta=NS(type="text_delta", text="囉")),
        NS(type="content_block_delta", delta=NS(type="signature_delta", signature="x")),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=7)),
        NS(type="message_stop"),
    ]

    async def create(**req):
        seen.update(req)
        return _AsyncIter(events)

    p.client = NS(messages=NS(create=create))
    out = await _collect(p.stream("claude-sonnet-5", [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]))
    assert seen["stream"] is True and seen["system"] and seen["messages"][0]["role"] == "user"
    assert [(o["type"], o.get("delta")) for o in out[:-1]] == [("reasoning", "先想"), ("text", "哈"), ("text", "囉")]
    res = out[-1]["result"]
    assert (res.text, res.reasoning, res.finish_reason) == ("哈囉", "先想", "stop")
    assert res.usage == {"prompt_tokens": 20, "cached_tokens": 4, "completion_tokens": 7, "total_tokens": 27}


@pytest.mark.asyncio
async def test_provider_without_stream_falls_back_to_one_piece():
    class Plain(TextProvider):
        PROVIDER_KEY = "plain"

        async def complete(self, model, messages, **kw):
            return TextResult(text="一次回完", model=model, metadata={"provider": "plain"})

    out = await _collect(Plain(_cfg()).stream("m", [{"role": "user", "content": "x"}]))
    assert [o["type"] for o in out] == ["text", "done"] and out[0]["delta"] == "一次回完"


# ---------------------------------------------------------------- routing / echo
def test_echo_is_refused_unless_dev(monkeypatch):
    monkeypatch.delenv("OMNIAPI_DEV", raising=False)
    with pytest.raises(RuntimeError, match="development only"):
        TextTool(_settings_without_providers()).route("echo")


def test_route_without_any_provider_says_so(dev):
    tool = TextTool(_settings_without_providers())
    assert tool.route("echo")[0] == "echo"
    with pytest.raises(RuntimeError, match="No text provider"):
        tool.route("gpt-6-sol")


# ---------------------------------------------------------------- bus
@pytest.mark.asyncio
async def test_ephemeral_events_reach_subscribers_but_not_history():
    bus = EventBus()
    q = bus.subscribe()
    await bus.publish({"type": "chat.delta", "delta": "a"}, ephemeral=True)
    await bus.publish({"type": "chat.finished"})
    assert [q.get_nowait()["type"], q.get_nowait()["type"]] == ["chat.delta", "chat.finished"]
    assert [e["type"] for e in bus.recent()] == ["chat.finished"]
    assert bus.recent()[0]["seq"] == 2  # seq still counts every event


# ---------------------------------------------------------------- store migration
@pytest.mark.asyncio
async def test_store_adds_messages_meta_to_an_old_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,"
        " content_json TEXT NOT NULL, created_at REAL NOT NULL, model TEXT, usage_json TEXT, cost_usd REAL, reasoning TEXT, tool_calls_json TEXT);"
        "INSERT INTO messages VALUES ('m1','c1',1,'user','\"舊訊息\"',1.0,NULL,NULL,NULL,NULL,NULL);"
    )
    con.commit()
    con.close()
    s = Store(path)
    await s.open()
    try:
        rows = await s.messages("c1")
        assert rows[0]["content"] == "舊訊息" and rows[0]["meta"] is None
        await s.create_conversation(kind="chat", conversation_id="c2")
        m = await s.add_message("c2", role="assistant", content="x", meta={"state": "done"})
        assert (await s.messages("c2"))[0]["meta"] == {"state": "done"} and m["meta"] == {"state": "done"}
    finally:
        await s.close()
    s2 = Store(path)  # opening twice must not try to add the column again
    await s2.open()
    await s2.close()


# ---------------------------------------------------------------- ChatManager
@pytest.mark.asyncio
async def test_turn_streams_stores_and_bills_the_call_ledger(chat, store):
    q = chat.bus.subscribe()
    conv = await chat.create(model="echo-fast", system="你是測試助手")
    started = await chat.send(conv["id"], "  第一個問題  ")
    assert started["state"] == "streaming" and started["user_message"]["content"] == "第一個問題"
    with pytest.raises(ChatError) as busy:
        await chat.send(conv["id"], "插隊")
    assert busy.value.status == 409
    await _wait(chat, conv["id"])

    events = []
    while not q.empty():
        events.append(q.get_nowait())
    types = [e["type"] for e in events]
    assert types[0] == "chat.updated" and "chat.started" in types and types.count("chat.finished") == 1
    deltas = [e for e in events if e["type"] == "chat.delta"]
    assert {d["kind"] for d in deltas} == {"text", "reasoning"}
    finished = next(e for e in events if e["type"] == "chat.finished")
    assert finished["state"] == "done" and "".join(d["delta"] for d in deltas if d["kind"] == "text") == finished["message"]["content"]

    got = await chat.get(conv["id"])
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    reply = got["messages"][1]
    assert reply["model"] == "echo-fast" and reply["cost_usd"] == 0.0 and reply["reasoning"]
    assert reply["meta"]["state"] == "done" and reply["meta"]["requested_model"] == "echo-fast"
    assert got["title"] == "第一個問題" and got["status"] == "open" and got["live"] is None
    assert "system prompt | 有" in reply["content"]  # the system prompt reached the model

    calls = await store.calls(tool="chat")
    assert len(calls) == 1 and calls[0]["status"] == "ok" and calls[0]["model"] == "echo-fast" and calls[0]["conversation_id"] == conv["id"]


@pytest.mark.asyncio
async def test_switching_model_mid_conversation_is_recorded_per_reply(chat):
    conv = await chat.create(model="echo-fast")
    await chat.send_and_wait(conv["id"], "一")
    second = await chat.send_and_wait(conv["id"], "二", model="echo")
    assert second["state"] == "done" and second["message"]["model"] == "echo"
    assert "第 **2** 輪" in second["message"]["content"]  # history was sent along
    got = await chat.get(conv["id"])
    assert [m.get("model") for m in got["messages"] if m["role"] == "assistant"] == ["echo-fast", "echo"]
    assert got["model"] == "echo"  # the conversation remembers the last one
    assert got["n_messages"] == 4 and got["unpriced"] == 0

    listed = await chat.list()
    assert listed[0]["id"] == conv["id"] and listed[0]["n_messages"] == 4 and listed[0]["live"] is False


@pytest.mark.asyncio
async def test_cancel_keeps_the_partial_reply(chat, monkeypatch):
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0.05")
    conv = await chat.create(model="echo")
    await chat.send(conv["id"], "會被取消")
    await asyncio.sleep(0.3)
    live = (await chat.get(conv["id"]))["live"]
    assert live and live["text"]  # a page opened mid-reply can show what has arrived
    assert (await chat.cancel(conv["id"]))["cancelled"] is True
    got = await chat.get(conv["id"])
    reply = got["messages"][-1]
    assert reply["role"] == "assistant" and reply["meta"]["state"] == "cancelled"
    assert 0 < len(reply["content"]) < 400 and got["live"] is None and got["status"] == "open"
    assert (await chat.cancel(conv["id"]))["cancelled"] is False
    # and the conversation goes on
    assert (await chat.send_and_wait(conv["id"], "繼續", model="echo-fast"))["state"] == "done"


@pytest.mark.asyncio
async def test_provider_failure_is_stored_and_skipped_in_history(store, dev):
    class Flaky:
        def __init__(self):
            self.histories = []

        def route(self, model):
            if model == "nope":
                raise RuntimeError("No text provider for model 'nope'.")
            return model, NS(PROVIDER_KEY="fake")

        async def stream(self, messages, model=None, **params):
            self.histories.append(messages)
            if len(self.histories) == 1:
                raise RuntimeError("HTTP 429 rate limited")
            yield {"type": "text", "delta": "好了"}
            yield {"type": "done", "text": "好了", "model": model, "provider": "fake", "usage": {"prompt_tokens": 3, "completion_tokens": 2}, "cost_usd": None, "finish_reason": "stop"}

    tool = Flaky()
    mgr = ChatManager(store, EventBus(), tool)
    with pytest.raises(ChatError, match="No text provider"):
        await mgr.create(model="nope")
    conv = await mgr.create(model="m1")
    first = await mgr.send_and_wait(conv["id"], "問")
    assert first["state"] == "error" and "429" in first["message"]["meta"]["error"] and first["message"]["content"] == ""
    second = await mgr.send_and_wait(conv["id"], "再問")
    assert second["state"] == "done"
    # the failed, empty reply is not sent back to the model as an assistant turn
    assert [m["role"] for m in tool.histories[1]] == ["user", "user"]
    got = await mgr.get(conv["id"])
    assert got["unpriced"] == 1  # the good reply has no price; the empty failure is not counted
    with pytest.raises(ChatError) as bad:
        await mgr.send(conv["id"], "x", model="nope")
    assert bad.value.status == 400 and len((await mgr.get(conv["id"]))["messages"]) == 4  # nothing stored for a refused send


@pytest.mark.asyncio
async def test_update_archive_and_export(chat):
    conv = await chat.create(model="echo-fast", system="第一行\n第二行")
    await chat.send_and_wait(conv["id"], "匯出測試")
    await chat.update(conv["id"], title="我的 對話/標題")
    name, md = await chat.export_markdown(conv["id"])
    assert name.startswith("我的_對話_標題-") and name.endswith(".md")
    assert md.startswith("# 我的 對話/標題\n") and "## System prompt" in md and "> 第二行" in md
    assert "## 你 · " in md and "## echo-fast · " in md and "· $0.0000" in md
    assert "<details><summary>思考</summary>" in md and "```python" in md

    with pytest.raises(ChatError):
        await chat.update(conv["id"], model="gpt-6-sol")  # no provider configured in this test
    await chat.update(conv["id"], archived=True)
    assert await chat.list() == [] and len(await chat.list(include_archived=True)) == 1
    with pytest.raises(ChatError) as arch:
        await chat.send(conv["id"], "封存後")
    assert arch.value.status == 409
    await chat.update(conv["id"], archived=False)
    assert len(await chat.list()) == 1
    with pytest.raises(ChatError) as missing:
        await chat.get("nope")
    assert missing.value.status == 404
