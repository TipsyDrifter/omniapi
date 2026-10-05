"""1.3-M5: which Claude Code executable agent runs use, and what /api/harnesses says when
there is none. Nothing here starts Claude: the lookup only looks at files, and the adapter
test stops at building the SDK options."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniapi_mcp.harness import claude as hc
from omniapi_mcp.harness.events import RunSpec
from omniapi_mcp.harness.registry import HarnessRegistry

EXE = "claude.exe" if os.name == "nt" else "claude"


def _exe(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"MZ")
    return p


def _settings(claude_cli: str = "", **enabled):
    provs = {
        n: SimpleNamespace(enabled=enabled.get(n, False), api_key="k-" + n if enabled.get(n) else "")
        for n in ("openai", "gemini", "deepseek", "anthropic", "openrouter", "elevenlabs", "kie")
    }
    return SimpleNamespace(
        providers=SimpleNamespace(**provs),
        search=SimpleNamespace(tavily_api_key="", enabled=True),
        harness=SimpleNamespace(claude_cli=claude_cli),
    )


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(hc.CLAUDE_CLI_ENV, raising=False)


def _nothing(_name):
    return None


# ------------------------------------------------------------------ lookup order


def test_configured_path_wins_over_everything(tmp_path):
    mine = _exe(tmp_path / "custom" / EXE)
    found = hc.find_claude_cli(_settings(str(mine)), which=lambda n: str(_exe(tmp_path / "path" / EXE)), home=tmp_path, bundled=lambda: tmp_path / "b")
    assert (found.path, found.source) == (str(mine), "setting")


def test_env_var_is_the_same_as_the_setting(tmp_path, monkeypatch):
    mine = _exe(tmp_path / "env" / EXE)
    monkeypatch.setenv(hc.CLAUDE_CLI_ENV, f'"{mine}"')  # quotes pasted from Explorer are tolerated
    assert hc.find_claude_cli(_settings(), which=_nothing, home=tmp_path, bundled=lambda: None).path == str(mine)


def test_a_configured_path_that_is_not_there_is_reported_not_replaced(tmp_path):
    _exe(tmp_path / ".local" / "bin" / EXE)
    found = hc.find_claude_cli(_settings(str(tmp_path / "gone" / EXE)), which=_nothing, home=tmp_path, bundled=lambda: None)
    assert found.path is None and "is not a file" in found.reason


def test_path_then_local_bin_then_bundled(tmp_path):
    on_path = str(_exe(tmp_path / "path" / EXE))
    local = _exe(tmp_path / ".local" / "bin" / EXE)
    bundled = _exe(tmp_path / "sdk" / "_bundled" / EXE)
    pick = lambda which: hc.find_claude_cli(_settings(), which=which, home=tmp_path, bundled=lambda: bundled)  # noqa: E731
    assert (pick(lambda n: on_path if n == "claude" else None).source) == "path"
    assert (pick(_nothing).path, pick(_nothing).source) == (str(local), "local-bin")
    local.unlink()
    assert (pick(_nothing).path, pick(_nothing).source) == (str(bundled), "sdk-bundled")


@pytest.mark.skipif(os.name != "nt", reason="the .cmd shim problem is Windows-only")
def test_npm_shim_alone_is_not_enough(tmp_path):
    shim = str(tmp_path / "npm" / "claude.cmd")
    found = hc.find_claude_cli(_settings(), which=lambda n: shim if n == "claude" else None, home=tmp_path, bundled=lambda: None)
    assert found.path is None and "shim" in found.reason
    # a native claude.exe later on PATH is preferred over the shim that shadows it
    exe = str(_exe(tmp_path / "native" / "claude.exe"))
    found = hc.find_claude_cli(_settings(), which=lambda n: {"claude": shim, "claude.exe": exe}.get(n), home=tmp_path, bundled=lambda: None)
    assert (found.path, found.source) == (exe, "path")


def test_nothing_found_says_claude_code_is_not_installed(tmp_path):
    found = hc.find_claude_cli(_settings(), which=_nothing, home=tmp_path, bundled=lambda: None)
    assert found.path is None and found.source == "none" and "not installed" in found.reason


# ------------------------------------------------------------------ registry and adapter


def _no_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "find_claude_cli", lambda settings=None, **kw: hc.ClaudeCli(None, "none", "Claude Code is not installed (test)"))


def test_availability_needs_the_cli_and_an_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # no subscription login on disk
    exe = _exe(tmp_path / "bin" / EXE)
    ok = HarnessRegistry(_settings(str(exe), deepseek=True)).claude_availability()
    assert ok["available"] is True and ok["cli"] is True and ok["cli_path"] == str(exe) and "reason" not in ok
    no_key = HarnessRegistry(_settings(str(exe))).claude_availability()
    assert no_key["available"] is False and "key" in no_key["reason"]
    _no_cli(monkeypatch, tmp_path)
    no_cli = HarnessRegistry(_settings(deepseek=True)).claude_availability()
    assert no_cli["available"] is False and no_cli["cli"] is False
    assert no_cli["reason"].startswith("沒有裝 Claude Code") and "test" in no_cli["cli_reason"]


def test_a_claude_run_is_refused_up_front_without_the_cli(tmp_path, monkeypatch):
    _no_cli(monkeypatch, tmp_path)
    r = HarnessRegistry(_settings(deepseek=True, openai=True))
    with pytest.raises(ValueError, match="claude harness: Claude Code is not installed"):
        r.resolve(RunSpec(prompt="x", model="cheap"))
    # the other harnesses do not care about claude.exe
    assert r.resolve(RunSpec(prompt="x", model="gpt-6-sol")).harness == "codex"


def test_build_options_passes_the_chosen_cli_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    exe = _exe(tmp_path / "bin" / EXE)
    h = hc.ClaudeHarness(_settings(str(exe), deepseek=True))
    opts = h.build_options(RunSpec(prompt="x", model="deepseek-flash", resolved_model="deepseek-flash", endpoint="deepseek"))
    assert str(opts.cli_path) == str(exe)


async def test_a_run_without_the_cli_ends_with_a_readable_error(tmp_path, monkeypatch):
    _no_cli(monkeypatch, tmp_path)
    h = hc.ClaudeHarness(_settings(deepseek=True))
    events = [e async for e in h.start(RunSpec(prompt="x", model="deepseek-flash", resolved_model="deepseek-flash", endpoint="deepseek"))]
    assert [e.type for e in events] == ["error"] and "not installed" in events[0].payload["message"]


# ------------------------------------------------------------------ the endpoint


def test_api_harnesses_explains_a_missing_claude_code(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.catalog import catalog
    from omniapi_mcp.config.settings import Settings
    from omniapi_mcp.daemon import app as dapp

    async def no_discovery(*a, **k):
        return {}

    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "TIERS__", "DEFAULTS__", "HARNESS__")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)
    monkeypatch.setattr(catalog, "refresh", no_discovery)
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    monkeypatch.setattr(srv, "settings", srv.settings)
    _no_cli(monkeypatch, tmp_path)
    app = dapp.create_app(Settings(_env_file=None), host="127.0.0.1", port=7799)
    with TestClient(app, base_url="http://127.0.0.1:7799") as client:
        hs = client.get("/api/harnesses").json()
    catalog.set_tier_overrides({})
    assert hs["claude"]["available"] is False and hs["claude"]["cli"] is False
    assert hs["claude"]["reason"] == "沒有裝 Claude Code（找不到 claude.exe）"
    for other in ("codex", "gemini"):
        assert hs[other]["available"] is False and hs[other]["reason"]  # no key in this sandbox, maybe no CLI either
