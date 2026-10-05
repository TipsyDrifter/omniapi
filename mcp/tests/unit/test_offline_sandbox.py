"""OMNIAPI_OFFLINE=1: a sandbox daemon must not be able to reach a paid vendor.

Regression for 2026-09-29: a test script that lost its conversation id fell
back to the default model and billed two real DeepSeek calls from the sandbox.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from omniapi_mcp.bus import EventBus
from omniapi_mcp.chat import ChatError, ChatManager
from omniapi_mcp.harness.events import RunSpec
from omniapi_mcp.harness.registry import HarnessRegistry
from omniapi_mcp.recorder import make_recorded
from omniapi_mcp.store.db import Store
from omniapi_mcp.tools.text import TextTool


def _settings(enabled: bool = True):
    p = NS(enabled=enabled, api_key="k" if enabled else "", organization=None, base_url=None, timeout=30, max_retries=0)
    return NS(providers=NS(openai=p, deepseek=p, anthropic=p, gemini=p, openrouter=p))


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_ECHO_DELAY", "0")


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "o.db")
    await s.open()
    yield s
    await s.close()


def test_route_refuses_every_real_model_even_with_keys_configured(sandbox):
    tool = TextTool(_settings())
    assert tool.available_providers()  # the keys are there — the lock is what stops it
    for model in ("cheap", "standard", "strong", "deepseek-flash", "gpt-6-sol", "vendor/some-model", None, ""):
        with pytest.raises(RuntimeError, match="offline sandbox"):
            tool.route(model)
    assert tool.route("echo")[0] == "echo"


@pytest.mark.asyncio
async def test_complete_and_stream_refuse_but_echo_works(sandbox):
    tool = TextTool(_settings())
    with pytest.raises(RuntimeError, match="offline sandbox"):
        await tool.complete(prompt="hi")  # no model given: the default would be a real one
    with pytest.raises(RuntimeError, match="offline sandbox"):
        await tool.complete(prompt="hi", model="cheap")
    with pytest.raises(RuntimeError, match="offline sandbox"):
        async for _ in tool.stream([{"role": "user", "content": "hi"}], model="cheap"):
            pass
    out = await tool.complete(prompt="hi", model="echo-fast")
    assert out["provider"] == "echo" and out["cost_usd"] == 0.0 and "hi" in out["text"]


@pytest.mark.asyncio
async def test_chat_without_a_model_cannot_fall_back_to_a_real_one(sandbox, store):
    mgr = ChatManager(store, EventBus(), TextTool(_settings()))
    with pytest.raises(ChatError, match="offline sandbox"):
        await mgr.create()  # default model is `cheap`
    conv = await mgr.create(model="echo-fast")
    with pytest.raises(ChatError, match="offline sandbox"):
        await mgr.send(conv["id"], "x", model="cheap")
    assert (await mgr.get(conv["id"]))["messages"] == []  # the refused send stored nothing
    assert (await mgr.send_and_wait(conv["id"], "x"))["state"] == "done"
    assert await store.calls(tool="chat") and all(c["model"].startswith("echo") for c in await store.calls(tool="chat"))


def test_agent_runs_are_refused_except_replay(sandbox):
    reg = HarnessRegistry(_settings())
    for spec in (RunSpec(prompt="x"), RunSpec(prompt="x", model="strong"), RunSpec(prompt="x", model="claude-sonnet-5", harness="claude")):
        with pytest.raises(ValueError, match="offline sandbox"):
            reg.resolve(spec)
    assert reg.resolve(RunSpec(prompt="x", harness="replay", model="replay")).harness == "replay"


@pytest.mark.asyncio
async def test_paid_tools_are_refused_before_they_run(sandbox):
    ran = []
    recorded = make_recorded(lambda: None)

    @recorded
    async def generate_image(prompt: str = "") -> dict:
        ran.append(prompt)
        return {"ok": True}

    @recorded
    async def server_info() -> dict:
        ran.append("info")
        return {"ok": True}

    out = await generate_image(prompt="cat")
    assert out["status"] == "refused" and "offline sandbox" in out["error"] and ran == []
    assert (await server_info()) == {"ok": True} and ran == ["info"]


def test_nothing_is_refused_when_the_switch_is_off(monkeypatch):
    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    tool = TextTool(_settings())
    model, provider = tool.route("deepseek-flash")
    assert model == "deepseek-flash" and provider.PROVIDER_KEY == "deepseek"


# ---------------------------------------------------------------- model listing
# Regression for 2026-10-04: a sandbox whose settings.json held keys sent
# GET /models to DeepSeek and OpenAI at startup.
_LISTING_KEYS = {"deepseek": "offline-deepseek-0123456789", "openai": "offline-openai-0123456789",
                 "anthropic": "offline-anthropic-0123456789", "gemini": "offline-gemini-0123456789abcd",
                 "elevenlabs": "offline-elevenlabs-0123456789"}


@pytest.fixture
def keyed_home(tmp_path, monkeypatch):
    """A fresh data home whose settings.json holds keys, and a wire that records instead of sending."""
    import json
    import os

    import httpx

    from omniapi_mcp.config import user_settings as US

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OMNIAPI_HOME", str(home))
    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "TIERS__", "DEFAULTS__", "IMAGES__")):
            monkeypatch.delenv(k)
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)
    overlay = {"providers": {slot: {"api_key": key} for slot, key in _LISTING_KEYS.items()}}
    (home / "settings.json").write_text(json.dumps(overlay), encoding="utf-8")
    sent: list[str] = []

    async def record_send(self, request, *a, **k):
        sent.append(f"{request.method} {request.url}")
        return httpx.Response(401, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", record_send)
    settings, _ = US.build_settings(overlay)
    return settings, sent


@pytest.mark.asyncio
async def test_no_model_listing_leaves_an_offline_sandbox_with_keys(sandbox, keyed_home):
    from omniapi_mcp.catalog import health
    from omniapi_mcp.catalog.catalog import ModelCatalog
    from omniapi_mcp.runtime import Runtime

    settings, sent = keyed_home
    cat = ModelCatalog()
    assert cat._fetcher_for("deepseek", settings.providers.deepseek)  # the keys are there — the lock is what stops it
    for kwargs in ({}, {"force": True}, {"force": True, "only": ["deepseek", "openai"]}):
        assert await cat.refresh(settings, **kwargs) == {}
    assert cat.discovery_status() == {} and cat.online_count("deepseek") is None

    # the startup path
    await Runtime._discover(settings, EventBus())
    assert sent == []
    # and no key is filed as working (or failing) by a listing that never happened
    for slot, key in _LISTING_KEYS.items():
        assert health.view(slot, key)["state"] == "untested"


@pytest.mark.asyncio
async def test_the_same_listing_goes_out_when_the_switch_is_off(monkeypatch, keyed_home):
    from omniapi_mcp.catalog.catalog import ModelCatalog

    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    settings, sent = keyed_home
    await ModelCatalog().refresh(settings, force=True, only=["deepseek", "openai"])
    assert any("api.deepseek.com" in s for s in sent) and any("api.openai.com" in s for s in sent)
