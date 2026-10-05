"""M6-k: the MCP ``chat`` tool and ``omni chat`` (its pure helpers plus the
REST + WebSocket path against the real daemon app, with the echo model)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.chat import ChatManager
from omniapi_mcp.chat.cli_support import (
    ChatApi,
    ChatSession,
    clean_input,
    Command,
    TurnRenderer,
    WsConn,
    export_filename,
    export_target,
    parse_line,
    reply_summary,
    stream_turn,
    summary_line,
)
from omniapi_mcp.core.job_manager import JobManager
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.text import TextTool

# ChatApi passes per-request timeouts (right for httpx, merely ignored by TestClient)
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


def _settings_without_providers():
    off = NS(enabled=False, api_key="")
    return NS(providers=NS(openai=off, deepseek=off, anthropic=off, gemini=off, openrouter=off))


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0")


# ================================================================ MCP tool
@pytest.fixture
async def mcp_chat(tmp_path, dev):
    store = Store(tmp_path / "chat.db")
    await store.open()
    mgr = ChatManager(store, EventBus(), TextTool(_settings_without_providers()))
    server_ctx = NS(chat=mgr, jobs=JobManager(), mode="stdio", store=store)
    from omniapi_mcp.server import chat as chat_tool, mcp

    mock_ctx = NS(request_context=NS(lifespan_context=server_ctx))

    async def call(**kw):
        full = {"conversation_id": None, "model": None, "system": None, "title": None,
                "reasoning_effort": None, "temperature": None, "max_completion_tokens": None, **kw}
        with patch.object(mcp, "get_context", return_value=mock_ctx):
            return await chat_tool(**full)

    call.store = store
    call.manager = mgr
    yield call
    await mgr.close()
    await store.close()


async def test_tool_new_conversation_returns_reply_and_id(mcp_chat):
    r = await mcp_chat(message="你好", model="echo-fast", system="你是測試助手")
    assert "error" not in r
    assert r["state"] == "done" and r["conversation_id"]
    assert "收到第 **1** 輪" in r["text"] and "system prompt | 有" in r["text"]
    assert r["model"] == "echo-fast" and r["requested_model"] == "echo-fast"
    assert r["reasoning"] and r["cost_usd"] == 0.0 and r["usage"]["total_tokens"] > 0
    assert r["n_messages"] == 2 and r["title"] == "你好"
    assert r["url"] == f"http://127.0.0.1:7788/chat/{r['conversation_id']}" and "url_note" in r
    conv = await mcp_chat.store.conversation(r["conversation_id"])
    assert conv["meta"]["source"] == "mcp" and conv["meta"]["leaf_id"]  # 1.2-M1-b: the current branch tip lives in meta too
    calls = await mcp_chat.store.calls(tool="chat")
    assert len(calls) == 1 and calls[0]["source"] == "mcp"  # one ledger row per turn, not two


async def test_tool_second_turn_carries_history_and_keeps_model(mcp_chat):
    first = await mcp_chat(message="一", model="echo-fast")
    second = await mcp_chat(message="二", conversation_id=first["conversation_id"])
    assert second["conversation_id"] == first["conversation_id"]
    assert "第 **2** 輪" in second["text"]  # the history went along
    assert second["model"] == "echo-fast"  # no model given → the conversation's last one
    assert second["n_messages"] == 4


async def test_tool_switches_model_and_updates_system(mcp_chat):
    first = await mcp_chat(message="一", model="echo-fast")
    cid = first["conversation_id"]
    assert "system prompt | 無" in first["text"]
    second = await mcp_chat(message="二", conversation_id=cid, model="echo", system="新的規則")
    assert second["model"] == "echo" and "system prompt | 有" in second["text"]
    got = await mcp_chat.manager.get(cid)
    assert got["model"] == "echo" and got["system_prompt"] == "新的規則"
    assert [m["model"] for m in got["messages"] if m["role"] == "assistant"] == ["echo-fast", "echo"]


async def test_tool_unknown_conversation_is_an_error(mcp_chat):
    r = await mcp_chat(message="哈囉", conversation_id="nope")
    assert r["status"] == 404 and "not found" in r["error"] and r["conversation_id"] == "nope"
    assert await mcp_chat.store.chats() == []


async def test_tool_unknown_model_is_an_error_and_creates_nothing(mcp_chat):
    r = await mcp_chat(message="哈囉", model="no-such-model-xyz")
    assert "error" in r and r["status"] == 400 and "conversation_id" not in r
    assert await mcp_chat.store.chats() == []
    # an existing conversation refuses it too, without storing the message
    ok = await mcp_chat(message="一", model="echo-fast")
    bad = await mcp_chat(message="二", conversation_id=ok["conversation_id"], model="no-such-model-xyz")
    assert bad["status"] == 400 and bad["conversation_id"] == ok["conversation_id"]
    assert (await mcp_chat.manager.get(ok["conversation_id"]))["n_messages"] == 2


async def test_tool_slow_reply_comes_back_as_a_ticket(mcp_chat, monkeypatch):
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0.01")
    from omniapi_mcp.server import chat as chat_tool, mcp

    jobs = JobManager(soft_timeout=0.01)
    server_ctx = NS(chat=mcp_chat.manager, jobs=jobs, mode="stdio")

    with patch.object(mcp, "get_context", return_value=NS(request_context=NS(lifespan_context=server_ctx))):
        ticket = await chat_tool(message="慢一點", conversation_id=None, model="echo", system=None, title=None,
                                 reasoning_effort=None, temperature=None, max_completion_tokens=None)
    assert ticket["status"] == "running" and ticket["conversation_id"] and "get_job_result" in ticket["message"]
    import asyncio

    for _ in range(200):
        res = await jobs.get(ticket["task_id"])
        if res.get("status") != "running":
            break
        await asyncio.sleep(0.05)
    assert res["state"] == "done" and res["conversation_id"] == ticket["conversation_id"]


def test_chat_tool_is_registered_with_its_parameters():
    import asyncio

    from omniapi_mcp.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert len(tools) == 19 and "chat" in tools
    props = tools["chat"].inputSchema["properties"]
    assert set(props) == {"message", "conversation_id", "model", "system", "title", "reasoning_effort", "temperature", "max_completion_tokens"}
    assert tools["chat"].inputSchema["required"] == ["message"]


def test_manifest_lists_chat():
    from pathlib import Path

    manifest = json.loads((Path(__file__).resolve().parents[2] / "manifest.json").read_text(encoding="utf-8"))
    assert "chat" in {t["name"] for t in manifest["tools"]}


# ================================================================ pure helpers
@pytest.mark.parametrize(
    "line,expected",
    [
        (None, Command("exit")),
        ("", Command("skip")),
        ("   ", Command("skip")),
        ("你好", Command("send", "你好")),
        ("  你好  ", Command("send", "你好")),
        ("//model 是什麼", Command("send", "/model 是什麼")),
        ("/model gpt-6-sol", Command("model", "gpt-6-sol")),
        ("/MODEL  echo ", Command("model", "echo")),
        ("/model", Command("model", "")),
        ("/system 你是 翻譯", Command("system", "你是 翻譯")),
        ("/export", Command("export", "")),
        ("/export out/a.md", Command("export", "out/a.md")),
        ("/id", Command("id", "")),
        ("/exit", Command("exit")),
        ("/quit", Command("exit")),
        ("/help", Command("help")),
        ("/foo bar", Command("unknown", "foo")),
    ],
)
def test_parse_line(line, expected):
    assert parse_line(line) == expected


def test_export_filename_prefers_rfc5987():
    cd = "attachment; filename=\"chat-abc.md\"; filename*=UTF-8''%E6%88%91%E7%9A%84%E5%B0%8D%E8%A9%B1-20260929-1200.md"
    assert export_filename(cd, "abc") == "我的對話-20260929-1200.md"
    assert export_filename('attachment; filename="chat-abc.md"', "abc") == "chat-abc.md"
    assert export_filename(None, "abc") == "chat-abc.md"
    assert export_filename("attachment; filename*=UTF-8''..%2F..%2Fevil.md", "abc") == "evil.md"  # no path escape


def test_export_target(tmp_path):
    assert export_target("", "a.md", cwd=str(tmp_path)) == tmp_path / "a.md"
    assert export_target("x.md", "a.md", cwd=str(tmp_path)) == tmp_path / "x.md"
    assert export_target(str(tmp_path), "a.md", cwd="C:/elsewhere") == tmp_path / "a.md"
    assert export_target("sub/", "a.md", cwd=str(tmp_path)) == tmp_path / "sub" / "a.md"


class _Sink:
    def __init__(self):
        self.parts: list[str] = []

    def __call__(self, s: str) -> None:
        self.parts.append(s)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def test_renderer_prints_only_this_turn():
    out, err = _Sink(), _Sink()
    r = TurnRenderer("c1", out, err)
    events = [
        {"type": "hello"},
        {"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "text", "delta": "早"},  # before the POST answered
        {"type": "chat.started", "conversation_id": "c1", "turn_id": "t1"},
        {"type": "chat.delta", "conversation_id": "c2", "turn_id": "x", "kind": "text", "delta": "別人的"},
        {"type": "chat.delta", "conversation_id": "c1", "turn_id": "t0", "kind": "text", "delta": "舊的"},
        {"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "reasoning", "delta": "想一下"},
        {"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "text", "delta": "安"},
        {"type": "call.finished", "tool": "chat"},
    ]
    assert not any(r.feed(e) for e in events)
    assert r.feed({"type": "chat.finished", "conversation_id": "c1", "turn_id": "t1", "state": "done", "message": {"content": "早安"}})
    assert out.text == "早安\n" and err.text == ""  # thinking hidden by default
    assert r.finished["state"] == "done"


def test_renderer_shows_thinking_on_stderr():
    out, err = _Sink(), _Sink()
    r = TurnRenderer("c1", out, err, show_thinking=True, dim=lambda s: f"<{s}>")
    r.expect("t1")
    r.feed({"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "reasoning", "delta": "嗯"})
    r.feed({"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "reasoning", "delta": "…"})
    r.feed({"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "text", "delta": "好\n"})
    r.feed({"type": "chat.finished", "conversation_id": "c1", "turn_id": "t1", "state": "done"})
    assert err.text == "<〔思考〕><嗯><…>\n" and out.text == "好\n"


def test_reply_summary_and_summary_line():
    conv = {"id": "c1", "title": "標題", "n_messages": 2}
    msg = {"content": "答", "model": "gpt-6-sol", "usage": {"prompt_tokens": 10, "completion_tokens": 3}, "cost_usd": 0.0012,
           "reasoning": None, "meta": {"state": "done", "requested_model": "strong"}}
    s = reply_summary(conversation=conv, message=msg, state="done", url="http://h:1/chat/c1")
    assert s == {"conversation_id": "c1", "title": "標題", "state": "done", "text": "答", "model": "gpt-6-sol",
                 "requested_model": "strong", "usage": msg["usage"], "cost_usd": 0.0012, "n_messages": 2, "url": "http://h:1/chat/c1"}
    line = summary_line(s)
    assert line.startswith("── gpt-6-sol") and "輸入 10／輸出 3 tokens" in line and "$0.0012" in line and "對話 c1" in line
    err = reply_summary(conversation=conv, message={"content": "", "meta": {"state": "error", "error": "boom"}}, state="error")
    assert err["error"] == "boom" and summary_line(err).startswith("── 錯誤")
    assert reply_summary(conversation=conv, message=None, state="error")["error"]


class _FakeApi:
    """Records calls; replays scripted results."""

    def __init__(self):
        self.cancelled = 0

    def send(self, cid, text, *, model=None, wait=False):
        return {"conversation_id": cid, "turn_id": "t1", "model": model or "echo", "resolved_model": "echo"}

    def cancel(self, cid):
        self.cancelled += 1
        return {"cancelled": True}


class _ScriptedWs(WsConn):
    def __init__(self, items):
        self.items = list(items)
        self.closed = False

    def recv(self, timeout):
        if not self.items:
            raise TimeoutError()
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    def close(self):
        self.closed = True


def test_ctrl_c_during_a_reply_cancels_that_turn():
    api, out = _FakeApi(), _Sink()
    ws = _ScriptedWs([
        {"type": "chat.delta", "conversation_id": "c1", "turn_id": "t1", "kind": "text", "delta": "一半"},
        KeyboardInterrupt(),
        {"type": "chat.finished", "conversation_id": "c1", "turn_id": "t1", "state": "cancelled",
         "message": {"content": "一半", "meta": {"state": "cancelled"}}},
    ])
    r = TurnRenderer("c1", out, _Sink())
    outcome = stream_turn(api, "c1", "問", model=None, renderer=r, ws=ws)
    assert api.cancelled == 1 and ws.closed
    assert outcome.state == "cancelled" and out.text == "一半\n"


# ================================================================ against the daemon app
@pytest.fixture
def daemon(tmp_path, monkeypatch, dev):
    """The real FastAPI app (REST + /ws) over a tmp store, echo models on."""
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.daemon import app as dapp
    from omniapi_mcp.runtime import runtime

    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(srv.mcp, "_session_manager", None)  # a session manager runs only once
    monkeypatch.setattr(srv.mcp.settings, "streamable_http_path", srv.mcp.settings.streamable_http_path)
    monkeypatch.setattr(srv, "settings", srv.settings)
    held: dict = {}

    async def acquire(settings, owner="daemon"):
        store = Store(tmp_path / "daemon.db")
        await store.open()
        bus = EventBus()
        mgr = ChatManager(store, bus, TextTool(_settings_without_providers()))
        runtime.context = NS(chat=mgr, bus=bus, store=store, mode="daemon")
        held.update(store=store, chat=mgr)
        return runtime.context

    async def release(owner="daemon"):
        await held["chat"].close()
        await held["store"].close()
        runtime.context = None

    monkeypatch.setattr(runtime, "acquire", acquire)
    monkeypatch.setattr(runtime, "release", release)
    monkeypatch.setattr(runtime, "context", None)
    app = dapp.create_app(NS(), host="127.0.0.1", port=7799)
    with TestClient(app, base_url="http://127.0.0.1:7799") as client:
        yield client


class _TestClientWs(WsConn):
    def __init__(self, client):
        self._cm = client.websocket_connect("ws://127.0.0.1:7799/ws")  # TestClient defaults to Host testserver; the 1.3-M2 guard wants loopback
        self._ws = self._cm.__enter__()

    def recv(self, timeout):
        return self._ws.receive_text()

    def close(self):
        self._cm.__exit__(None, None, None)


def _session(client, *, ws_factory, model="echo-fast", **kw):
    out, err = _Sink(), _Sink()
    s = ChatSession(ChatApi(client), host="127.0.0.1", port=7799, model=model, system=kw.pop("system", None),
                    out=out, err=err, ws_factory=ws_factory, **kw)
    return s, out, err


def test_cli_streams_the_reply_then_prints_the_summary(daemon):
    opened = []

    def factory(url):
        assert url == "ws://127.0.0.1:7799/ws"
        opened.append(url)
        return _TestClientWs(daemon)

    s, out, err = _session(daemon, ws_factory=factory, system="你是測試助手")
    summary = s.ask("第一問")
    assert opened and summary["state"] == "done"
    assert out.text.startswith("收到第 **1** 輪：「第一問」") and out.text.endswith("\n")
    assert out.text.rstrip("\n") == summary["text"]
    assert "── echo-fast" in err.text and f"對話 {s.cid}" in err.text and f"http://127.0.0.1:7799/chat/{s.cid}" in err.text
    conv = daemon.get(f"/api/chat/{s.cid}").json()
    assert conv["meta"]["source"] == "cli" and conv["system_prompt"] == "你是測試助手"

    out.parts.clear()
    s.ask("第二問")
    assert "第 **2** 輪" in out.text


def test_cli_falls_back_to_wait_when_the_socket_is_down(daemon):
    s, out, err = _session(daemon, ws_factory=lambda url: None)
    summary = s.ask("你好")
    assert "WebSocket 連不上" in err.text
    assert summary["state"] == "done" and "收到第 **1** 輪" in out.text
    s.ask("再一次")
    assert err.text.count("WebSocket 連不上") == 1  # said once per session


def test_cli_json_mode_prints_nothing_but_returns_the_result(daemon):
    s, out, err = _session(daemon, ws_factory=lambda url: pytest.fail("--json must not open the socket"), stream=False, quiet=True)
    summary = s.ask("你好")
    assert out.text == "" and err.text == ""
    assert summary["state"] == "done" and summary["url"].endswith(summary["conversation_id"])


def test_cli_repl_commands(daemon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = iter(["/id", "/model echo", "/system 規則一", "", "哈囉", "/model no-such-model-xyz", "/export", "/export sub/", "/nope", "/exit", "不會送出"])
    s, out, err = _session(daemon, ws_factory=lambda url: _TestClientWs(daemon), model=None)
    s.repl(lambda prompt: next(lines))
    assert "還沒開始對話" in err.text
    conv = daemon.get(f"/api/chat/{s.cid}").json()
    assert conv["model"] == "echo" and conv["system_prompt"] == "規則一" and conv["n_messages"] == 2
    assert "錯誤（HTTP 400）" in err.text and s.model == "echo"  # a bad /model is refused and not kept
    exported = list(tmp_path.glob("*.md"))
    assert len(exported) == 1 and "## System prompt" in exported[0].read_text(encoding="utf-8")
    assert len(list((tmp_path / "sub").glob("*.md"))) == 1
    assert "不認得 /nope" in err.text


def test_cli_resume_continues_the_conversation(daemon):
    s1, _, _ = _session(daemon, ws_factory=lambda url: None)
    s1.ask("一")
    s2, out, _ = _session(daemon, ws_factory=lambda url: _TestClientWs(daemon), model=None, cid=s1.cid)
    s2.ask("二")
    assert "第 **2** 輪" in out.text
    assert daemon.get(f"/api/chat/{s1.cid}").json()["n_messages"] == 4


def test_cli_ctrl_d_and_ctrl_c_at_the_prompt_leave(daemon):
    for exc in (EOFError, KeyboardInterrupt):
        s, _, _ = _session(daemon, ws_factory=lambda url: None)

        def boom(prompt, exc=exc):
            raise exc()

        s.repl(boom)  # returns instead of raising
        assert s.cid is None


def test_mcp_endpoint_answers_with_and_without_the_trailing_slash(daemon):
    """Clients are configured with ``…/mcp``. With the GUI built, the SPA catch-all
    used to claim that path and answer POST with 405 (GET only)."""
    # No MCP session is opened (the fixture has no real settings): a POST the MCP
    # transport itself turns down (406, wrong Accept) proves the request reached it.
    for path in ("/mcp", "/mcp/"):
        r = daemon.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Accept": "text/plain"}, follow_redirects=False)
        assert r.status_code == 406, (path, r.status_code)
        assert "Not Acceptable" in r.text
    assert daemon.get("/runs/abc").status_code in (200, 404)  # the GUI routes are untouched


def test_clean_input_recovers_utf8_from_a_code_page_pipe():
    utf8 = "REPL 第一句".encode("utf-8")
    garbled = utf8.decode("cp950", "surrogateescape")
    assert garbled != "REPL 第一句"
    assert clean_input(garbled, "cp950") == "REPL 第一句"
    assert clean_input("正常", "cp950") == "正常" and clean_input(None) is None


def test_cli_rejects_an_empty_resume_before_touching_the_daemon():
    from typer.testing import CliRunner

    from omniapi_mcp.cli import app

    r = CliRunner().invoke(app, ["chat", "x", "--resume", "", "--port", "1"])
    assert r.exit_code == 2 and "--resume needs a conversation id" in r.output
