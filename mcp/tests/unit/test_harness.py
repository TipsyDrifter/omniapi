"""Unit tests for the harness layer: routing, adapter request building,
Codex/Gemini/dsk event translation, RunManager persistence (fake adapter)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.harness.codex import CodexHarness
from omniapi_mcp.harness.events import RunEvent, RunSpec, new_run_id, short_tool_summary
from omniapi_mcp.harness.registry import HarnessRegistry
from omniapi_mcp.runs.import_dsk import estimate_cost, translate_stream_event
from omniapi_mcp.runs.manager import RunManager
from omniapi_mcp.store.db import Store


def _settings(**enabled):
    """Minimal settings double: providers with api keys for the given names."""
    provs = {}
    for name in ("openai", "gemini", "deepseek", "anthropic", "openrouter", "elevenlabs", "kie"):
        provs[name] = SimpleNamespace(enabled=enabled.get(name, False), api_key="k-" + name if enabled.get(name) else "")
    return SimpleNamespace(providers=SimpleNamespace(**provs), search=SimpleNamespace(tavily_api_key="", enabled=True))


# --------------------------------------------------------------------------- routing


def test_routing_by_provider():
    r = HarnessRegistry(_settings(openai=True, gemini=True, deepseek=True, anthropic=True, openrouter=True))
    cases = {
        "cheap": ("claude", "deepseek-flash", "deepseek"),
        "gpt-6-sol": ("codex", "gpt-6-sol", None),
        "gemini-3.8-flash": ("gemini", "gemini-3.8-flash", None),
        "claude-sonnet-5": ("claude", "claude-sonnet-5", "anthropic"),
        "moonshotai/kimi-k3": ("claude", "moonshotai/kimi-k3", "openrouter"),
    }
    for model, (h, m, ep) in cases.items():
        spec = r.resolve(RunSpec(prompt="x", model=model))
        assert (spec.harness, spec.resolved_model, spec.endpoint) == (h, m, ep), model


def test_routing_errors_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # no Claude login on disk
    r = HarnessRegistry(_settings(openai=True, deepseek=True))
    with pytest.raises(ValueError, match="Unknown model"):
        r.resolve(RunSpec(prompt="x", model="nope-model"))
    with pytest.raises(ValueError, match="not configured"):
        r.resolve(RunSpec(prompt="x", model="claude-sonnet-5"))  # no login, no API key
    # explicit API billing needs the key; subscription needs the login file
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    spec = r.resolve(RunSpec(prompt="x", model="claude-sonnet-5"))
    assert spec.endpoint == "anthropic"
    with pytest.raises(ValueError, match="not configured"):
        r.resolve(RunSpec(prompt="x", model="claude-sonnet-5", auth="api"))
    r_api = HarnessRegistry(_settings(anthropic=True))
    assert r_api.resolve(RunSpec(prompt="x", model="claude-sonnet-5", auth="api")).endpoint == "anthropic-api"
    with pytest.raises(ValueError, match="OpenRouter"):
        r.resolve(RunSpec(prompt="x", model="gpt-6-sol", harness="claude"))  # needs openrouter
    r2 = HarnessRegistry(_settings(openai=True, openrouter=True))
    spec = r2.resolve(RunSpec(prompt="x", model="gpt-6-sol", harness="claude"))
    # OpenRouter knows the model under a vendor-prefixed id and bills it: the run becomes an OpenRouter run
    assert (spec.endpoint, spec.resolved_model, spec.provider) == ("openrouter", "openai/gpt-6-sol", "openrouter")
    r3 = HarnessRegistry(_settings(openai=True, gemini=True, openrouter=True, deepseek=True))
    spec = r3.resolve(RunSpec(prompt="x", model="gemini-2.5-pro", harness="claude"))
    assert spec.resolved_model == "google/gemini-2.5-pro"
    # the other two harnesses only talk to their own vendor
    with pytest.raises(ValueError, match="codex harness cannot run 'gemini-3.8-flash'.*Use 'gemini' or 'claude'"):
        r3.resolve(RunSpec(prompt="x", model="gemini-3.8-flash", harness="codex"))
    with pytest.raises(ValueError, match="gemini harness cannot run 'deepseek-flash'.*Use 'claude'\\."):
        r3.resolve(RunSpec(prompt="x", model="deepseek-flash", harness="gemini"))
    assert r3.resolve(RunSpec(prompt="x", model="gpt-6-sol", harness="codex")).resolved_model == "gpt-6-sol"


def test_claude_via_openrouter_refuses_a_model_the_gateway_does_not_list(monkeypatch):
    from omniapi_mcp.harness import registry as reg

    listed = {"openai/gpt-6-sol", "moonshotai/kimi-k3"}
    monkeypatch.setattr(reg.catalog, "ids", lambda **kw: listed if kw.get("provider") == "openrouter" else set())
    r = HarnessRegistry(_settings(openai=True, openrouter=True))
    assert r.resolve(RunSpec(prompt="x", model="gpt-6-sol", harness="claude")).resolved_model == "openai/gpt-6-sol"
    with pytest.raises(ValueError, match="OpenRouter does not list 'openai/gpt-5.5-pro'"):
        r.resolve(RunSpec(prompt="x", model="gpt-5.5-pro", harness="claude"))


def test_codex_command_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path))
    monkeypatch.setattr("omniapi_mcp.harness.codex.which_cli", lambda n: "codex.cmd")
    s = _settings(openai=True)
    h = CodexHarness(s)
    args, env = h.build_command(RunSpec(prompt="x", model="gpt-6-sol", resolved_model="gpt-6-sol", cwd="C:/w"))
    assert args[:2] == ["codex.cmd", "exec"] and "--json" in args and args[-1] == "-"
    assert "-C" in args and "C:/w" in args
    assert env["OPENAI_API_KEY"] == "k-openai" and "CODEX_HOME" in env and "CLAUDECODE" not in env
    args, _ = h.build_command(RunSpec(prompt="x", model="gpt-6-sol", resolved_model="gpt-6-sol", yolo=True, resume_session_id="abc"))
    assert args[2:4] == ["resume", "abc"] and "--dangerously-bypass-approvals-and-sandbox" in args


# --------------------------------------------------------------------------- translation


def test_codex_item_events():
    ev = CodexHarness._item_events({"id": "i1", "type": "command_execution", "command": "ls", "aggregated_output": "a\nb", "exit_code": 0, "status": "completed"}, {})
    assert [e.type for e in ev] == ["tool_call", "tool_result"]
    assert ev[0].payload["name"] == "Bash" and ev[1].payload["is_error"] is False
    ev = CodexHarness._item_events({"id": "i2", "type": "agent_message", "text": "hi"}, {})
    assert ev[0].type == "text" and ev[0].payload["text"] == "hi"
    ev = CodexHarness._item_events({"id": "i3", "type": "file_change", "changes": [{"path": "a.py", "kind": "update"}], "status": "completed"}, {})
    assert ev[0].payload["name"] == "Edit" and "a.py" in ev[0].payload["input"]["file_path"]
    assert CodexHarness._item_events({"type": "todo_list"}, {}) == []


def test_claude_hook_chatter_is_dropped_at_the_adapter():
    """Sixteen hook_started / hook_response rows opened every run on the board."""
    from omniapi_mcp.harness.claude import _NOISY_SYSTEM_SUBTYPES

    assert {"hook_started", "hook_response", "thinking_tokens"} <= _NOISY_SYSTEM_SUBTYPES


def test_dsk_stream_translation_and_cost():
    names: dict[str, str] = {}
    init = {"type": "system", "subtype": "init", "session_id": "s1", "model": "m", "tools": ["Bash"], "mcp_servers": [{"name": "ddg"}]}
    assert translate_stream_event(init, names)[0] == ("session_start", {"session_id": "s1", "model": "m", "tools": ["Bash"], "mcp_servers": ["ddg"]})
    assert translate_stream_event({"type": "system", "subtype": "thinking_tokens"}, names) == []
    asst = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x"}}]}}
    out = translate_stream_event(asst, names)
    assert [o[0] for o in out] == ["text", "tool_call"] and names["t1"] == "Read"
    user = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "data"}]}]}}
    out = translate_stream_event(user, names)
    assert out[0][0] == "tool_result" and out[0][1]["name"] == "Read" and out[0][1]["output"] == "data"
    res = translate_stream_event({"type": "result", "result": "ok", "num_turns": 3, "is_error": False}, names)
    assert res[0][0] == "result" and res[0][1]["num_turns"] == 3
    # cost: 1M miss + 1M hit + 1M out off-peak (a Sunday 03:00 UTC)
    import calendar, time

    sunday = calendar.timegm((2026, 9, 27, 3, 0, 0, 6, 0, 0))
    assert estimate_cost("deepseek-flash", {"inMiss": 1e6, "inHit": 1e6, "out": 1e6}, sunday) == pytest.approx(0.22 + 0.007 + 0.66)
    assert estimate_cost("deepseek-v4-pro", {"inMiss": 1e6}, sunday) is None


def test_short_tool_summary_and_run_id():
    assert short_tool_summary("mcp__tavily__tavily_search", {"query": "a  b"}) == "tavily:tavily_search → a b"
    assert short_tool_summary("Bash", {"command": "ls\n-la"}) == "Bash → ls -la"
    assert len(new_run_id()) == 18 and new_run_id()[14] == "-"


# --------------------------------------------------------------------------- run manager with a fake adapter


class FakeAdapter:
    name = "fake"
    supports_resume = True

    def __init__(self, events):
        self._events = events

    async def start(self, spec):
        for e in self._events:
            await asyncio.sleep(0)
            yield e

    async def cancel(self):
        pass


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.db")
    await s.open()
    yield s
    await s.close()


async def test_run_manager_persists_events_and_state(store, monkeypatch):
    bus = EventBus()
    mgr = RunManager(store, bus, _settings(deepseek=True))
    events = [
        RunEvent("session_start", {"session_id": "sess-1", "model": "m"}),
        RunEvent("text", {"text": "working"}),
        RunEvent("tool_call", {"id": "t1", "name": "Bash", "input": {"command": "ls"}}),
        RunEvent("tool_result", {"id": "t1", "output": "x" * 10000, "is_error": False}),
        RunEvent("result", {"text": "all done", "usage": {"prompt_tokens": 10, "completion_tokens": 2}, "cost_usd": 0.01, "num_turns": 1, "session_id": "sess-1", "is_error": False}),
    ]
    monkeypatch.setattr(mgr.registry, "adapter", lambda h: FakeAdapter(events))
    q = bus.subscribe()
    ticket = await mgr.start(RunSpec(prompt="do the thing please", model="cheap"))
    assert ticket["harness"] == "claude" and ticket["state"] == "starting" and ticket["title"] == "do the thing please"
    for _ in range(50):
        await asyncio.sleep(0.02)
        if mgr.live_count == 0:
            break
    row = await mgr.get(ticket["run_id"])
    assert row["state"] == "done" and row["result"] == "all done" and row["cost_usd"] == 0.01
    assert row["session_id"] == "sess-1" and row["turns"] == 1
    types = [e["type"] for e in row["events"]]
    assert types == ["session_start", "text", "tool_call", "tool_result", "result"]
    assert len(row["events"][3]["payload"]["output"]) < 7000  # slimmed
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()["type"])
    assert kinds[0] == "run.started" and kinds[-1] == "run.finished" and "run.event" in kinds
    # incremental fetch
    last = row["events"][-1]["id"]
    assert (await mgr.get(ticket["run_id"], after_event=last))["events"] == []
    # resume builds on the parent session
    ticket2 = await mgr.start(RunSpec(prompt="more", resume_run_id=ticket["run_id"]))
    row2 = await store.run(ticket2["run_id"])
    assert row2["meta"]["resume_session_id"] == "sess-1" and row2["title"].startswith("↩")
    for _ in range(50):  # let the background drive finish before the store closes
        await asyncio.sleep(0.02)
        if mgr.live_count == 0:
            break


async def test_run_manager_error_and_cancel_paths(store, monkeypatch):
    bus = EventBus()
    mgr = RunManager(store, bus, _settings(deepseek=True))
    monkeypatch.setattr(mgr.registry, "adapter", lambda h: FakeAdapter([RunEvent("error", {"message": "boom"})]))
    t = await mgr.start(RunSpec(prompt="x"))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if mgr.live_count == 0:
            break
    row = await mgr.get(t["run_id"], include_events=False)
    assert row["state"] == "error" and row["error"] == "boom"
    with pytest.raises(ValueError, match="cwd does not exist"):
        await mgr.start(RunSpec(prompt="x", cwd="Z:/definitely/not/here"))
    assert (await mgr.cancel("nope"))["state"] == "not_found"
