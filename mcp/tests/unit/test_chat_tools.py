"""1.2-M4 聊天裡的生成 (backend): tool calls on the streaming path of every
vendor family, the history that carries them back, and the life of a
``propose_generation`` proposal — pending, accepted, generating, done /
failed / cancelled, declined (by hand or automatically) and reopened, a restart in
between. No vendor is called: the providers get fake clients replaying
recorded-shape event sequences, the chat runs on the echo models or a
capturing text tool, and the generation jobs run a stand-in tool."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace as NS

import pytest
from openai.types.chat import ChatCompletionChunk
from PIL import Image

from omniapi_mcp.bus import EventBus
from omniapi_mcp.capabilities.text import (
    AnthropicTextProvider,
    GeminiTextProvider,
    OpenAITextProvider,
    ToolCallAssembler,
)
from omniapi_mcp.catalog.catalog import ModelEntry, discovered_tools
from omniapi_mcp.chat import ChatError, ChatManager
from omniapi_mcp.chat.tools import ProposeGeneration, ToolRegistry, describe
from omniapi_mcp.generate import GenerationManager
from omniapi_mcp.providers.base import ProviderConfig
from omniapi_mcp.recorder import call_scope
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.text import TextTool


# --------------------------------------------------------------------------- helpers
def _settings_without_providers():
    off = NS(enabled=False, api_key="")
    return NS(providers=NS(openai=off, deepseek=off, anthropic=off, gemini=off, openrouter=off))


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 120, 40)).save(buf, format="PNG")
    return buf.getvalue()


async def _menu(_ctx):
    return {
        "image": {"models": [{"id": "gpt-image-2", "name": "GPT Image 2", "pricing": {"unit": "per_1m_tokens", "image_output": 30}},
                             {"id": "gemini-3.1-flash-image", "name": "Nano Banana 2", "pricing": {"unit": "per_image", "1K": 0.067, "4K": 0.151}}],
                  "default": "gpt-image-2", "sandbox": False},
        "speech": {"models": [{"id": "tts-1", "name": "TTS-1", "pricing": {"unit": "per_1m_chars", "text": 15}}], "default": "tts-1", "sandbox": False},
    }


class FakeGenerations(GenerationManager):
    """The real job manager around a stand-in tool: it opens a ledger row under
    the caller's door (as the call recorder does), writes a file, indexes it."""

    def __init__(self, store, bus, tmp_path):
        super().__init__(store, bus)
        self.tmp = tmp_path
        self.gate: asyncio.Event | None = None
        self.fail: str | None = None
        self.cost = 0.0
        self.n = 0

    def _validate(self, tool, args):
        return None

    def _tool(self, name):
        mgr = self

        async def run(args, convert_result=False):
            scope = call_scope.get()
            scope.call_id = await mgr.store.call_started(name, args, source=scope.source, conversation_id=scope.links.get("conversation_id"))
            if mgr.gate is not None:
                await mgr.gate.wait()
            if mgr.fail:
                return {"error": mgr.fail}
            mgr.n += 1
            f = mgr.tmp / f"work{mgr.n}.png"
            f.write_bytes(_png())
            row = await mgr.store.add_artifact(kind="image" if name == "generate_image" else "speech", tool=name, model=args.get("model"),
                                               prompt=args.get("prompt") or args.get("text"), file_path=str(f), source=scope.source,
                                               call_id=scope.call_id, meta=dict(scope.links))
            scope.artifacts.append(row)
            return {"model": args.get("model"), "cost_usd": mgr.cost}

        return NS(run=run)

    async def settle(self):
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


class Scripted:
    """A text tool that answers every turn with ``calls`` (and records the
    history and params it was given). Routed as an echo model, so the chat
    believes it takes tools."""

    def __init__(self, calls):
        self.calls = calls
        self.seen: list[tuple[list, dict]] = []
        self.gate: asyncio.Event | None = None

    def route(self, model):
        return model, NS(PROVIDER_KEY="echo")

    async def stream(self, messages, model=None, **params):
        self.seen.append((messages, params))
        if self.gate is not None:
            await self.gate.wait()
        yield {"type": "text", "delta": "好"}
        yield {"type": "done", "text": "好", "model": model, "provider": "echo", "usage": None, "cost_usd": 0.0, "finish_reason": "tool_calls",
               "tool_calls": self.calls}


def _call(cid, kind="image", prompt="貓", **extra):
    return {"id": cid, "type": "function", "function": {"name": "propose_generation", "arguments": json.dumps({"kind": kind, "prompt": prompt, **extra})}}


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0")
    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "chat.db")
    await s.open()
    yield s
    await s.close()


def _registry():
    reg = ToolRegistry()
    reg.register(ProposeGeneration(menu=_menu))
    return reg


async def _manager(store, bus, tool, gens):
    mgr = ChatManager(store, bus, tool)
    mgr.tools = _registry()
    await mgr.attach(gens)
    return mgr


@pytest.fixture
async def env(store, dev, tmp_path):
    bus = EventBus()
    gens = FakeGenerations(store, bus, tmp_path)
    mgr = await _manager(store, bus, TextTool(_settings_without_providers()), gens)
    yield NS(chat=mgr, gens=gens, bus=bus, store=store)
    await mgr.close()
    await gens.close()


async def _turn(chat, cid, text, **kw):
    return await chat.wait(await chat.send(cid, text, **kw))


def _tool_rows(rows):
    return [r for r in rows if r["role"] == "tool"]


# --------------------------------------------------------------------------- OpenAI family: streamed tool calls
def _chunk(delta, finish=None, usage=None):
    return ChatCompletionChunk.model_validate({
        "id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5.4-mini",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **({"usage": usage} if usage else {})})


class _Stream:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        async def gen():
            for i in self.items:
                yield i
        return gen()


def _openai(cls, chunks):
    p = cls(ProviderConfig(api_key="k"))
    sent = {}

    async def create(**req):
        sent.update(req)
        return _Stream(chunks)

    p.client = NS(chat=NS(completions=NS(create=create)))
    return p, sent


async def _drain(gen):
    pieces = [x async for x in gen]
    return pieces, pieces[-1]["result"]


async def test_openai_stream_puts_fragmented_tool_calls_back_together_next_to_text():
    p, sent = _openai(OpenAITextProvider, [
        _chunk({"role": "assistant", "content": "我來畫"}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "propose_generation", "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"kind": "ima'}}]}),
        _chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "propose_generation", "arguments": '{"kind":"speech","prompt":"嗨"}'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ge", "prompt": "貓"}'}}]}),
        _chunk({}, finish="tool_calls"),
    ])
    tools = [{"type": "function", "function": {"name": "propose_generation", "parameters": {"type": "object"}}}]
    pieces, result = await _drain(p.stream("gpt-5.4-mini", [{"role": "user", "content": "畫貓"}], tools=tools))
    assert sent["tools"] == tools and sent["stream"] is True
    assert [x["delta"] for x in pieces if x["type"] == "text"] == ["我來畫"]
    assert result.text == "我來畫" and result.finish_reason == "tool_calls"
    assert [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in result.tool_calls] == [
        ("call_a", "propose_generation", {"kind": "image", "prompt": "貓"}),
        ("call_b", "propose_generation", {"kind": "speech", "prompt": "嗨"}),
    ]


async def test_a_gemini_style_whole_call_without_index_keeps_its_extra_fields():
    # the SDK builds stream chunks without validating them, so an index-less fragment does reach us
    whole = NS(model="gemini-3.8-flash", usage=None, choices=[NS(finish_reason=None, delta=NS(
        content=None, model_extra={},
        tool_calls=[{"id": "g1", "type": "function", "function": {"name": "propose_generation", "arguments": '{"kind":"image","prompt":"山"}'},
                     "extra_content": {"google": {"thought_signature": "sig=="}}}]))])
    p, _ = _openai(GeminiTextProvider, [_chunk({"content": "好"}), whole, _chunk({}, finish="tool_calls")])
    _, result = await _drain(p.stream("gemini-3.8-flash", [{"role": "user", "content": "x"}]))
    assert result.tool_calls == [{"id": "g1", "type": "function", "function": {"name": "propose_generation", "arguments": '{"kind":"image","prompt":"山"}'},
                                  "extra_content": {"google": {"thought_signature": "sig=="}}}]


def test_the_assembler_names_a_call_that_came_without_an_id_and_drops_nameless_fragments():
    a = ToolCallAssembler()
    a.add({"index": 0, "function": {"name": "propose_generation", "arguments": "{}"}})
    a.add({"index": 1, "function": {"arguments": "{}"}})
    calls = a.calls()
    assert len(calls) == 1 and calls[0]["id"].startswith("call_") and len(calls[0]["id"]) > 8


async def test_a_stream_without_tool_calls_has_none():
    p, _ = _openai(OpenAITextProvider, [_chunk({"content": "hi"}), _chunk({}, finish="stop")])
    _, result = await _drain(p.stream("gpt-5.4-mini", [{"role": "user", "content": "x"}]))
    assert result.tool_calls is None


# --------------------------------------------------------------------------- Anthropic: tool_use blocks in the stream
def _anthropic(events):
    p = AnthropicTextProvider(ProviderConfig(api_key="k"))
    sent = {}

    async def create(**req):
        sent.update(req)
        return _Stream(events)

    p.client = NS(messages=NS(create=create))
    return p, sent


def _ev(type_, **kw):
    return NS(type=type_, **kw)


async def test_anthropic_stream_joins_input_json_deltas_and_keeps_signed_thinking():
    p, sent = _anthropic([
        _ev("message_start", message=NS(model="claude-sonnet-5", usage=NS(input_tokens=10, cache_read_input_tokens=None))),
        _ev("content_block_start", index=0, content_block=NS(type="thinking", thinking="", signature="")),
        _ev("content_block_delta", index=0, delta=NS(type="thinking_delta", thinking="想一下")),
        _ev("content_block_delta", index=0, delta=NS(type="signature_delta", signature="SIG")),
        _ev("content_block_stop", index=0),
        _ev("content_block_start", index=1, content_block=NS(type="text", text="")),
        _ev("content_block_delta", index=1, delta=NS(type="text_delta", text="我提議")),
        _ev("content_block_start", index=2, content_block=NS(type="tool_use", id="toolu_1", name="propose_generation", input={})),
        _ev("content_block_delta", index=2, delta=NS(type="input_json_delta", partial_json='{"kind": "image", ')),
        _ev("content_block_delta", index=2, delta=NS(type="input_json_delta", partial_json='"prompt": "戴耳機的貓"}')),
        _ev("content_block_stop", index=2),
        _ev("message_delta", delta=NS(stop_reason="tool_use"), usage=NS(output_tokens=20)),
    ])
    tools = [{"type": "function", "function": {"name": "propose_generation", "description": "d", "parameters": {"type": "object"}}}]
    pieces, result = await _drain(p.stream("claude-sonnet-5", [{"role": "user", "content": "畫貓"}], tools=tools, reasoning_effort="low"))
    assert sent["tools"] == [{"name": "propose_generation", "description": "d", "input_schema": {"type": "object"}}]
    assert [x["type"] for x in pieces[:-1]] == ["reasoning", "text"]
    assert result.text == "我提議" and result.finish_reason == "tool_calls"
    assert result.tool_calls == [{"id": "toolu_1", "type": "function",
                                  "function": {"name": "propose_generation", "arguments": json.dumps({"kind": "image", "prompt": "戴耳機的貓"}, ensure_ascii=False)}}]
    assert result.metadata["replay"] == {"_anthropic_thinking": [{"type": "thinking", "thinking": "想一下", "signature": "SIG"}]}


async def test_anthropic_text_only_stream_has_no_calls_and_no_replay():
    p, _ = _anthropic([_ev("content_block_start", index=0, content_block=NS(type="text", text="")),
                       _ev("content_block_delta", index=0, delta=NS(type="text_delta", text="hi")),
                       _ev("message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=1))])
    _, result = await _drain(p.stream("claude-sonnet-5", [{"role": "user", "content": "x"}]))
    assert result.tool_calls is None and "replay" not in result.metadata


# --------------------------------------------------------------------------- the history goes back in each vendor's form
_HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "畫貓"},
    {"role": "assistant", "content": None, "tool_calls": [_call("toolu_1")], "_anthropic_thinking": [{"type": "thinking", "thinking": "t", "signature": "S"}]},
    {"role": "tool", "tool_call_id": "toolu_1", "content": "主人說不用了，沒有生成。"},
    {"role": "user", "content": "那改畫狗"},
]


def test_openai_family_gets_tool_calls_and_tool_messages_without_private_keys():
    p = OpenAITextProvider(ProviderConfig(api_key="k"))
    req = p._build_request("gpt-5.4-mini", _HISTORY, {})
    msgs = req["messages"]
    assert msgs[0]["role"] == "developer"
    assert msgs[2] == {"role": "assistant", "content": None, "tool_calls": [_call("toolu_1")]}
    assert msgs[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "主人說不用了，沒有生成。"}
    assert all(not k.startswith("_") for m in msgs for k in m)


def test_anthropic_gets_tool_use_after_its_signed_thinking_and_the_result_ahead_of_the_next_question():
    p = AnthropicTextProvider(ProviderConfig(api_key="k"))
    req = p._build_request("claude-sonnet-5", _HISTORY, {})
    assert req["system"] == "sys"
    asst, user = req["messages"][1], req["messages"][2]
    assert asst["content"] == [{"type": "thinking", "thinking": "t", "signature": "S"},
                               {"type": "tool_use", "id": "toolu_1", "name": "propose_generation", "input": {"kind": "image", "prompt": "貓"}}]
    assert user["role"] == "user" and user["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "主人說不用了，沒有生成。"}, {"type": "text", "text": "那改畫狗"}]


# --------------------------------------------------------------------------- the tools flag
def test_text_models_always_say_whether_they_take_tools():
    assert ModelEntry(id="m", provider="openai", modality="text").to_dict()["capabilities"]["tools"] is False
    assert ModelEntry(id="m", provider="openai", modality="text", capabilities={"tools": True}).to_dict()["capabilities"]["tools"] is True
    assert "tools" not in ModelEntry(id="i", provider="openai", modality="image").to_dict()["capabilities"]


def test_openrouter_tools_come_from_supported_parameters():
    assert discovered_tools({"supported_parameters": ["max_tokens", "tools", "tool_choice"]}) is True
    assert discovered_tools({"supported_parameters": ["max_tokens"]}) is False
    assert discovered_tools({"architecture": {}}) is None  # a listing cached before the field was kept: unknown


async def test_the_description_lists_models_with_prices_and_defaults():
    text = describe(await _menu(None))
    assert "gpt-image-2" in text and "輸出每百萬 token $30" in text and "每張 $0.067（1K）～$0.151（4K）" in text
    assert "預設模型：image=gpt-image-2；speech=tts-1" in text
    empty = describe({"image": {"models": []}, "speech": {"models": []}})
    assert "不要提議" in empty


async def test_no_tool_is_offered_when_nothing_can_be_generated():
    async def none(_):
        return {"image": {"models": []}, "speech": {"models": []}}

    reg = ToolRegistry()
    reg.register(ProposeGeneration(menu=none))
    assert await reg.specs(NS(ctx=None)) == []


# --------------------------------------------------------------------------- proposals in a chat
async def test_a_proposal_appears_pending_and_costs_nothing_until_accepted(env):
    conv = await env.chat.create(model="echo-fast")
    turn = await _turn(env.chat, conv["id"], "幫我畫一隻戴耳機的貓")
    assert turn["tools"] is True and turn["state"] == "done"
    msg = turn["message"]
    (p,) = msg["proposals"]
    assert p["state"] == "pending" and p["kind"] == "image" and p["model"] == "gpt-image-2" and "戴耳機的貓" in p["prompt"]
    assert msg["tool_calls"][0]["id"] == p["id"] and "replay" not in msg["meta"]
    got = await env.chat.get(conv["id"])
    assert got["status"] == "open" and got["messages"][-1]["proposals"][0]["state"] == "pending"
    events = [e for e in env.bus.recent(200) if e["type"] == "chat.proposal"]
    assert [(e["action"], e["proposal"]["state"]) for e in events] == [("created", "pending")]
    # nothing but the chat turn is on the ledger, and no generation job exists
    assert {c["tool"] for c in await env.store.calls(limit=50)} == {"chat"}
    assert await env.store.generations() == []


async def test_two_proposals_in_one_reply(env):
    conv = await env.chat.create(model="echo-fast")
    turn = await _turn(env.chat, conv["id"], "畫兩張不同風格的海")
    assert [p["state"] for p in turn["message"]["proposals"]] == ["pending", "pending"]
    assert len({p["id"] for p in turn["message"]["proposals"]}) == 2


async def test_a_model_without_tools_is_offered_none_and_proposes_nothing(env):
    conv = await env.chat.create(model="echo-blind")
    turn = await _turn(env.chat, conv["id"], "幫我畫一隻貓")
    assert turn["tools"] is False and turn["message"]["proposals"] == [] and not turn["message"].get("tool_calls")
    started = [e for e in env.bus.recent(50) if e["type"] == "chat.started" and e["conversation_id"] == conv["id"]]
    assert started[-1]["tools"] is False


async def test_only_a_gui_turn_is_offered_tools(store, dev, tmp_path):
    tool = Scripted([])
    mgr = await _manager(store, EventBus(), tool, FakeGenerations(store, EventBus(), tmp_path))
    conv = await mgr.create(model="echo-fast")
    await _turn(mgr, conv["id"], "畫", source="mcp")
    assert "tools" not in tool.seen[-1][1]
    await _turn(mgr, conv["id"], "畫")
    assert tool.seen[-1][1]["tools"][0]["function"]["name"] == "propose_generation"
    await mgr.close()


async def test_at_most_three_proposals_and_bad_calls_are_ignored_and_noted(store, dev, tmp_path):
    calls = [_call(f"c{i}") for i in range(4)] + [_call("bad", kind="video"), {"id": "x", "type": "function", "function": {"name": "rm_rf", "arguments": "{}"}}]
    mgr = await _manager(store, EventBus(), Scripted(calls), FakeGenerations(store, EventBus(), tmp_path))
    conv = await mgr.create(model="echo-fast")
    msg = (await _turn(mgr, conv["id"], "畫"))["message"]
    assert [p["id"] for p in msg["proposals"]] == ["c0", "c1", "c2"]
    assert [c["id"] for c in msg["tool_calls"]] == ["c0", "c1", "c2"]  # only kept calls are stored: none is left without a result
    reasons = [i["reason"] for i in msg["meta"]["tool_calls_ignored"]]
    assert reasons[0] == "over 3 per reply" and "kind must be" in reasons[1] and reasons[2] == "unknown tool"
    await mgr.close()


async def test_accept_runs_a_chat_sourced_generation_and_the_work_lands_on_the_reply(env):
    conv = await env.chat.create(model="echo-fast")
    msg = (await _turn(env.chat, conv["id"], "畫一隻貓"))["message"]
    pid = msg["proposals"][0]["id"]
    acc = await env.chat.accept(conv["id"], pid, prompt="一隻戴耳機的橘貓")
    assert acc["state"] == "generating" and acc["generation_id"] and acc["prompt"] == "一隻戴耳機的橘貓" and acc["attempts"] == 1
    await env.gens.settle()
    p = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert p["state"] == "done" and p["artifact"]["kind"] == "image" and p["artifact"]["file_url"].endswith("/file")
    art = await env.store.artifact(p["artifact"]["id"])
    assert art["source"] == "chat" and art["meta"]["conversation_id"] == conv["id"] and art["meta"]["message_id"] == msg["id"]
    gen = await env.store.generation(acc["generation_id"])
    assert gen["source"] == "chat" and gen["meta"] == {"conversation_id": conv["id"], "message_id": msg["id"], "tool_call_id": pid}
    gen_calls = [c for c in await env.store.calls(limit=50) if c["tool"] == "generate_image"]
    assert len(gen_calls) == 1 and gen_calls[0]["source"] == "chat" and gen_calls[0]["conversation_id"] == conv["id"]
    # the model's result message follows the reply on the branch, and is not a message to the owner
    rows = await env.store.messages(conv["id"], limit=None)
    (tool,) = _tool_rows(rows)
    assert tool["parent_id"] == msg["id"] and "做好了" in tool["content"] and p["artifact"]["id"] in tool["content"]
    got = await env.chat.get(conv["id"])
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"] and got["n_messages"] == 2
    assert (await env.store.chats())[0]["n_messages"] == 2
    states = [e["proposal"]["state"] for e in env.bus.recent(300) if e["type"] == "chat.proposal"]
    assert states == ["pending", "generating", "done"]


async def test_the_same_proposal_cannot_be_decided_twice(env):
    conv = await env.chat.create(model="echo-fast")
    a, b = (await _turn(env.chat, conv["id"], "畫兩張貓"))["message"]["proposals"]
    await env.chat.accept(conv["id"], a["id"])
    for again in (env.chat.accept(conv["id"], a["id"]), env.chat.decline(conv["id"], a["id"])):
        with pytest.raises(ChatError) as e:
            await again
        assert e.value.status == 409
    assert (await env.chat.decline(conv["id"], b["id"]))["state"] == "declined"
    with pytest.raises(ChatError) as e:
        await env.chat.decline(conv["id"], b["id"])
    assert e.value.status == 409
    with pytest.raises(ChatError) as e:
        await env.chat.accept(conv["id"], "nope")
    assert e.value.status == 404
    await env.gens.settle()
    # two results, chained after the reply in the order they were settled
    rows = await env.store.messages(conv["id"], limit=None)
    t1, t2 = _tool_rows(rows)
    assert t2["parent_id"] == t1["id"] and "不用了" in t2["content"]


async def test_a_failed_generation_is_classified_and_may_be_tried_again(env):
    conv = await env.chat.create(model="echo-fast")
    pid = (await _turn(env.chat, conv["id"], "畫"))["message"]["proposals"][0]["id"]
    env.gens.fail = "Error code: 429 - insufficient_quota"
    await env.chat.accept(conv["id"], pid)
    await env.gens.settle()
    p = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert p["state"] == "failed" and p["error_kind"] == "quota"
    env.gens.fail = None
    again = await env.chat.accept(conv["id"], pid)
    assert again["state"] == "generating" and again["attempts"] == 2 and "error" not in again
    await env.gens.settle()
    rows = await env.store.messages(conv["id"], limit=None)
    (tool,) = _tool_rows(rows)  # the same result message, rewritten
    assert "做好了" in tool["content"]


async def test_a_declined_proposal_may_be_reopened_and_accepted(env):
    conv = await env.chat.create(model="echo-fast")
    msg = (await _turn(env.chat, conv["id"], "畫一隻貓"))["message"]
    pid = msg["proposals"][0]["id"]
    await env.chat.decline(conv["id"], pid)
    rows = await env.store.messages(conv["id"], limit=None)
    (tool,) = _tool_rows(rows)
    assert "不用了" in tool["content"]
    again = await env.chat.accept(conv["id"], pid, prompt="還是畫一隻橘貓")
    assert again["state"] == "generating" and again["attempts"] == 1 and again["prompt"] == "還是畫一隻橘貓" and "auto" not in again
    await env.gens.settle()
    p = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert p["state"] == "done" and p["artifact"]["kind"] == "image"
    rows = await env.store.messages(conv["id"], limit=None)
    (tool2,) = _tool_rows(rows)  # the same result message, rewritten
    assert tool2["id"] == tool["id"] and "做好了" in tool2["content"]
    # done is final: neither accept nor decline
    for again in (env.chat.accept(conv["id"], pid), env.chat.decline(conv["id"], pid)):
        with pytest.raises(ChatError) as e:
            await again
        assert e.value.status == 409


async def test_an_automatically_declined_proposal_may_be_accepted_later(env):
    conv = await env.chat.create(model="echo-fast")
    first = (await _turn(env.chat, conv["id"], "畫一隻貓"))["message"]
    await _turn(env.chat, conv["id"], "算了，聊聊天")
    pid = first["proposals"][0]["id"]
    p = (await env.chat.get(conv["id"]))["messages"][1]["proposals"][0]
    assert p["state"] == "declined" and p["auto"] is True
    acc = await env.chat.accept(conv["id"], pid)
    assert acc["state"] == "generating" and "auto" not in acc
    await env.gens.settle()
    got = await env.chat.get(conv["id"])
    assert got["messages"][1]["proposals"][0]["state"] == "done"
    assert [m["role"] for m in got["messages"]] == ["user", "assistant", "user", "assistant"]  # the branch did not move
    rows = await env.store.messages(conv["id"], limit=None)
    (tool,) = _tool_rows(rows)
    assert tool["parent_id"] == first["id"] and "做好了" in tool["content"]


async def test_cancelling_the_generation_cancels_the_proposal(env):
    conv = await env.chat.create(model="echo-fast")
    pid = (await _turn(env.chat, conv["id"], "畫"))["message"]["proposals"][0]["id"]
    env.gens.gate = asyncio.Event()
    acc = await env.chat.accept(conv["id"], pid)
    await asyncio.sleep(0.05)
    await env.gens.cancel(acc["generation_id"])
    p = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert p["state"] == "cancelled"
    rows = await env.store.messages(conv["id"], limit=None)
    assert "中止" in _tool_rows(rows)[0]["content"]


async def test_a_failed_or_cancelled_proposal_may_be_declined(env):
    conv = await env.chat.create(model="echo-fast")
    a, b = (await _turn(env.chat, conv["id"], "畫兩張貓"))["message"]["proposals"]
    env.gens.fail = "Error code: 400 - content blocked"
    await env.chat.accept(conv["id"], a["id"])
    await env.gens.settle()
    env.gens.fail = None
    env.gens.gate = asyncio.Event()
    acc = await env.chat.accept(conv["id"], b["id"])
    await asyncio.sleep(0.05)
    await env.gens.cancel(acc["generation_id"])
    env.gens.gate = None
    states = [p["state"] for p in (await env.chat.get(conv["id"]))["messages"][-1]["proposals"]]
    assert states == ["failed", "cancelled"]
    assert (await env.chat.decline(conv["id"], a["id"]))["state"] == "declined"
    assert (await env.chat.decline(conv["id"], b["id"]))["state"] == "declined"
    rows = await env.store.messages(conv["id"], limit=None)
    assert all("不用了" in t["content"] for t in _tool_rows(rows))  # the results the model reads say so too
    # declined is still reopenable; a done one cannot be declined
    await env.chat.accept(conv["id"], a["id"])
    await env.gens.settle()
    with pytest.raises(ChatError) as e:
        await env.chat.decline(conv["id"], a["id"])
    assert e.value.status == 409


async def test_the_proposal_keeps_what_the_model_proposed_after_it_is_accepted(env):
    conv = await env.chat.create(model="echo-fast")
    msg = (await _turn(env.chat, conv["id"], "畫一隻貓"))["message"]
    p = msg["proposals"][0]
    assert p["proposed"] == {"prompt": p["prompt"], "model": "gpt-image-2"}
    acc = await env.chat.accept(conv["id"], p["id"], prompt="一隻戴耳機的橘貓", model="gemini-3.1-flash-image")
    assert acc["prompt"] == "一隻戴耳機的橘貓" and acc["model"] == "gemini-3.1-flash-image"
    assert acc["proposed"] == {"prompt": p["prompt"], "model": "gpt-image-2"}
    await env.gens.settle()
    done = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert done["state"] == "done" and done["prompt"] == "一隻戴耳機的橘貓" and done["proposed"]["prompt"] == p["prompt"]
    events = [e["proposal"] for e in env.bus.recent(300) if e["type"] == "chat.proposal"]
    assert all(e["proposed"]["model"] == "gpt-image-2" for e in events)
    # a record stored before ``proposed`` was kept: read back from the call's arguments
    row = await env.store.message(msg["id"])
    meta = dict(row["meta"])
    meta["tools"] = {k: {kk: vv for kk, vv in r.items() if kk != "proposed"} for k, r in meta["tools"].items()}
    await env.store.update_message(msg["id"], meta=meta)
    legacy = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert legacy["proposed"] == {"prompt": p["prompt"], "model": "gpt-image-2"} and legacy["prompt"] == "一隻戴耳機的橘貓"


async def test_the_chat_totals_carry_what_its_generations_cost(env):
    conv = await env.chat.create(model="echo-fast")
    assert conv["generation_cost_usd"] is None
    other = await env.chat.create(model="echo-fast")
    a, b = (await _turn(env.chat, conv["id"], "畫兩張貓"))["message"]["proposals"]
    assert (await env.chat.get(conv["id"]))["generation_cost_usd"] is None  # nothing generated yet
    env.gens.cost = 0.04
    await env.chat.accept(conv["id"], a["id"])
    await env.gens.settle()
    env.gens.cost = 0.0125
    await env.chat.accept(conv["id"], b["id"])
    await env.gens.settle()
    # a generation on another branch counts too: what was spent stays spent
    await env.chat.wait(await env.chat.regenerate(conv["id"], (await env.chat.get(conv["id"]))["messages"][-1]["id"]))
    got = await env.chat.get(conv["id"])
    assert got["messages"][-1]["versions"]["index"] == 2
    assert got["generation_cost_usd"] == pytest.approx(0.0525)
    listed = {c["id"]: c for c in await env.chat.list()}
    assert listed[conv["id"]]["generation_cost_usd"] == pytest.approx(0.0525) and listed[other["id"]]["generation_cost_usd"] is None


async def test_moving_on_declines_a_waiting_proposal_and_the_next_turn_knows(env):
    conv = await env.chat.create(model="echo-fast")
    first = (await _turn(env.chat, conv["id"], "畫一隻貓"))["message"]
    second = (await _turn(env.chat, conv["id"], "算了，聊聊天"))["message"]
    p = (await env.chat.get(conv["id"]))["messages"][1]["proposals"][0]
    assert p["state"] == "declined" and p["auto"] is True
    assert "沒有回應這個提議就繼續對話" in second["content"]  # the echo model quotes the tool result it was sent
    rows = await env.store.messages(conv["id"], limit=None)
    tool = _tool_rows(rows)[0]
    user2 = next(r for r in rows if r["role"] == "user" and r["content"] == "算了，聊聊天")
    assert tool["parent_id"] == first["id"] and user2["parent_id"] == tool["id"]
    got = await env.chat.get(conv["id"])
    assert [m["role"] for m in got["messages"]] == ["user", "assistant", "user", "assistant"]
    assert got["messages"][2]["versions"] == {"count": 1, "index": 1, "ids": [user2["id"]]}


async def test_regenerating_or_editing_also_settles_a_waiting_proposal(env):
    conv = await env.chat.create(model="echo-fast")
    msg = (await _turn(env.chat, conv["id"], "畫一隻貓"))["message"]
    r2 = await env.chat.wait(await env.chat.regenerate(conv["id"], msg["id"]))
    old = await env.store.message(msg["id"])
    assert old["meta"]["tools"][msg["proposals"][0]["id"]]["state"] == "declined"
    got = await env.chat.get(conv["id"])
    assert got["messages"][-1]["id"] == r2["message"]["id"] and got["messages"][-1]["versions"]["count"] == 2  # the result message is no version
    q = got["messages"][0]
    ed = await env.chat.wait(await env.chat.edit(conv["id"], q["id"], "改畫狗"))
    new = await env.store.message(r2["message"]["id"])
    assert all(r["state"] == "declined" for r in new["meta"]["tools"].values())
    assert ed["state"] == "done"


async def test_a_generating_proposal_does_not_block_the_chat_and_its_result_is_updated_later(store, dev, tmp_path):
    tool = Scripted([_call("c1")])
    gens = FakeGenerations(store, EventBus(), tmp_path)
    mgr = await _manager(store, EventBus(), tool, gens)
    conv = await mgr.create(model="echo-fast")
    await _turn(mgr, conv["id"], "畫")
    gens.gate = asyncio.Event()
    await mgr.accept(conv["id"], "c1")
    tool.calls = []
    await _turn(mgr, conv["id"], "生成時繼續聊")
    history = tool.seen[-1][0]
    asst = next(m for m in history if m.get("tool_calls"))
    res = history[history.index(asst) + 1]
    assert res["role"] == "tool" and res["tool_call_id"] == "c1" and "生成中" in res["content"]
    gens.gate.set()
    await gens.settle()
    await _turn(mgr, conv["id"], "好了嗎")
    res = next(m for m in tool.seen[-1][0] if m.get("role") == "tool")
    assert "做好了" in res["content"]
    await mgr.close()


async def test_a_turn_without_tools_reads_calls_and_results_as_text(store, dev, tmp_path):
    tool = Scripted([_call("c1", prompt="貓")])
    mgr = await _manager(store, EventBus(), tool, FakeGenerations(store, EventBus(), tmp_path))
    conv = await mgr.create(model="echo-fast")
    await _turn(mgr, conv["id"], "畫")
    await mgr.decline(conv["id"], "c1")
    tool.calls = []
    await _turn(mgr, conv["id"], "再說", source="mcp")  # no tools offered on this turn
    history = tool.seen[-1][0]
    assert all(m["role"] != "tool" and not m.get("tool_calls") for m in history)
    asst = [m for m in history if m["role"] == "assistant"][0]
    assert "propose_generation" in asst["content"] and "主人說不用了" in asst["content"]
    await mgr.close()


async def test_a_call_without_a_result_still_gets_one_in_the_history(store, dev, tmp_path):
    tool = Scripted([])
    mgr = await _manager(store, EventBus(), tool, FakeGenerations(store, EventBus(), tmp_path))
    conv = await mgr.create(model="echo-fast")
    u = await store.add_message(conv["id"], role="user", content="q")
    await store.add_message(conv["id"], role="assistant", content="", tool_calls=[_call("orphan")], meta={"tools": {}})
    history, _ = await mgr._history(conv["id"], tools=True)
    assert history[-1] == {"role": "tool", "tool_call_id": "orphan", "content": "（這個工具呼叫沒有結果）"}
    assert u
    await mgr.close()


async def test_pending_survives_a_restart_and_generating_becomes_retryable(store, dev, tmp_path):
    bus = EventBus()
    gens = FakeGenerations(store, bus, tmp_path)
    mgr = await _manager(store, bus, TextTool(_settings_without_providers()), gens)
    conv = await mgr.create(model="echo-fast")
    a, b = (await _turn(mgr, conv["id"], "畫兩張貓"))["message"]["proposals"]
    gens.gate = asyncio.Event()
    acc = await mgr.accept(conv["id"], a["id"])
    await mgr.close()
    # the process "dies": its job never finishes (left hanging on the gate); the next start marks it interrupted
    dead = list(gens._tasks.values())
    assert (await store.generation(acc["generation_id"]))["status"] == "running"
    assert await store.interrupt_running_generations() == 1

    gens2 = FakeGenerations(store, EventBus(), tmp_path)
    mgr2 = await _manager(store, EventBus(), TextTool(_settings_without_providers()), gens2)
    p1, p2 = (await mgr2.get(conv["id"]))["messages"][-1]["proposals"]
    assert p2["state"] == "pending"
    assert p1["state"] == "failed" and p1["error_kind"] == "interrupted"
    again = await mgr2.accept(conv["id"], a["id"])
    await gens2.settle()
    assert again["attempts"] == 2
    assert (await mgr2.get(conv["id"]))["messages"][-1]["proposals"][0]["state"] == "done"
    for t in dead:  # the old job, should it ever report, is an older attempt and changes nothing
        t.cancel()
    await asyncio.gather(*dead, return_exceptions=True)
    assert (await mgr2.get(conv["id"]))["messages"][-1]["proposals"][0]["state"] == "done"
    await mgr2.close()
    await gens2.close()


async def test_a_shutdown_cancel_is_not_the_owners_cancel(env):
    conv = await env.chat.create(model="echo-fast")
    pid = (await _turn(env.chat, conv["id"], "畫"))["message"]["proposals"][0]["id"]
    env.gens.gate = asyncio.Event()
    await env.chat.accept(conv["id"], pid)
    await asyncio.sleep(0.05)
    await env.gens.close()
    p = (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]
    assert p["state"] == "failed" and p["error_kind"] == "interrupted"


async def test_deleting_the_chat_leaves_a_running_generation_alone(env):
    conv = await env.chat.create(model="echo-fast")
    pid = (await _turn(env.chat, conv["id"], "畫"))["message"]["proposals"][0]["id"]
    env.gens.gate = asyncio.Event()
    acc = await env.chat.accept(conv["id"], pid)
    await env.chat.delete(conv["id"])
    env.gens.gate.set()
    await env.gens.settle()
    gen = await env.store.generation(acc["generation_id"])
    assert gen["status"] == "done" and gen["artifact_ids"]
    assert (await env.store.artifact(gen["artifact_ids"][0]))["source"] == "chat"


async def test_accept_with_an_unusable_model_falls_back_to_the_default(env):
    conv = await env.chat.create(model="echo-fast")
    pid = (await _turn(env.chat, conv["id"], "唸一段早安"))["message"]["proposals"][0]["id"]
    acc = await env.chat.accept(conv["id"], pid, model="no-such-model")
    assert acc["model"] == "tts-1" and acc["kind"] == "speech"
    await env.gens.settle()
    gen = await env.store.generation(acc["generation_id"])
    assert gen["tool"] == "generate_speech" and gen["params"]["text"] == acc["prompt"]


async def test_bad_generation_params_leave_the_proposal_pending(env):
    conv = await env.chat.create(model="echo-fast")
    pid = (await _turn(env.chat, conv["id"], "畫"))["message"]["proposals"][0]["id"]
    with pytest.raises(ChatError) as e:
        await env.chat.accept(conv["id"], pid, params={"image_path": "C:/Windows/win.ini"})
    assert e.value.status == 400
    assert (await env.chat.get(conv["id"]))["messages"][-1]["proposals"][0]["state"] == "pending"


async def test_the_live_reply_carries_its_place_among_versions(store, dev, tmp_path):
    tool = Scripted([])
    mgr = await _manager(store, EventBus(), tool, FakeGenerations(store, EventBus(), tmp_path))
    conv = await mgr.create(model="echo-fast")
    first = (await _turn(mgr, conv["id"], "q"))["message"]
    tool.gate = asyncio.Event()
    started = await mgr.regenerate(conv["id"], first["id"])
    await asyncio.sleep(0.02)
    live = (await mgr.get(conv["id"]))["live"]
    assert live["versions"] == {"count": 2, "index": 2, "ids": [first["id"], f"live-{started['turn_id']}"]}
    tool.gate.set()
    await mgr.wait(started)
    await mgr.close()


async def test_the_export_marks_proposals_and_how_they_ended(env):
    conv = await env.chat.create(model="echo-fast")
    a, b = (await _turn(env.chat, conv["id"], "畫兩張貓"))["message"]["proposals"]
    await env.chat.accept(conv["id"], a["id"])
    await env.chat.decline(conv["id"], b["id"])
    await env.gens.settle()
    _, md = await env.chat.export_markdown(conv["id"])
    assert md.count("> 提議生成圖片（gpt-image-2）") == 2
    assert "> 結果：做好了，作品 `" in md and "> 結果：不用了" in md
    assert "- 訊息數：2" in md
