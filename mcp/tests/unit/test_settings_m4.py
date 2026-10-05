"""1.3-M4 backend: what the settings page, the models page and the first-run
guide need beyond 1.3-M2 — the shadowed .env key, each provider's last
connection result, the outside programs, the corrected key test (kie's credit
query, no prefix rules), the suggested tiers and key pages in the catalog,
Claude Code's MCP entry, and the service info. No vendor is ever called: every
HTTP answer here is faked."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from omniapi_mcp.catalog import catalog, health
from omniapi_mcp.catalog.catalog import ModelCatalog
from omniapi_mcp.catalog.discovery import DiscoveredModel, discover, load_cached, save_cached, DiscoveryResult
from omniapi_mcp.config import claude_mcp
from omniapi_mcp.config import user_settings as US
from omniapi_mcp.config.settings import Settings

KEY = "sk-m4-settings-0123456789abcd"
ENV_KEY = "sk-m4-environment-zyxw3a90"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIAPI_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "TIERS__", "DEFAULTS__", "IMAGES__")):
            monkeypatch.delenv(k)
    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)
    yield
    catalog.set_tier_overrides({})


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://vendor.example/models")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


# ---------------------------------------------------------------- health store
def test_health_is_filed_per_key_and_survives_a_new_reader():
    assert health.view("deepseek", "") is None
    assert health.view("deepseek", KEY)["state"] == "untested"
    health.record("deepseek", KEY, ok=True, source="discovery", listed=12, at=100.0)
    v = health.view("deepseek", KEY, online_models=5)
    assert v["state"] == "ok" and v["listed"] == 12 and v["online_models"] == 5 and v["checked_at"] == 100.0 and v["source"] == "discovery"
    # another key is another story
    assert health.view("deepseek", "sk-another-key-0000000000")["state"] == "untested"
    # a failure keeps when it last worked
    health.record("deepseek", KEY, ok=False, source="test", reason="rejected", status=401, message="no", at=200.0)
    v = health.view("deepseek", KEY)
    assert v["state"] == "failed" and v["reason"] == "rejected" and v["status"] == 401 and v["last_ok_at"] == 100.0
    # an older result never replaces a newer one
    health.record("deepseek", KEY, ok=True, source="discovery", at=150.0)
    assert health.view("deepseek", KEY)["state"] == "failed"
    # on disk, without the key
    text = health.health_path().read_text(encoding="utf-8")
    assert KEY not in text and health.fingerprint(KEY) in text


def test_health_keeps_a_few_keys_per_provider():
    for i in range(health.KEEP_PER_PROVIDER + 3):
        health.record("openai", f"sk-key-number-{i:02d}-xxxxxxxx", ok=True, source="test", at=float(i))
    data = json.loads(health.health_path().read_text(encoding="utf-8"))
    assert len(data["openai"]) == health.KEEP_PER_PROVIDER
    assert health.lookup("openai", f"sk-key-number-{health.KEEP_PER_PROVIDER + 2:02d}-xxxxxxxx")


def test_messages_never_carry_the_key():
    e = health.classify(RuntimeError(f"bad thing with {KEY}"), key=KEY)
    assert KEY not in json.dumps(e) and e["reason"] == "error"
    health.record("kie", KEY, ok=False, source="test", reason="error", message=f"echo {KEY}")
    assert KEY not in health.health_path().read_text(encoding="utf-8")


@pytest.mark.parametrize("code,reason", [(401, "rejected"), (403, "rejected"), (402, "payment"), (429, "rate_limited"),
                                         (503, "provider_error"), (404, "http_error")])
def test_classify_http_statuses(code, reason):
    out = health.classify(_status_error(code))
    assert out["reason"] == reason and out["status"] == code


def test_classify_network_failures():
    req = httpx.Request("GET", "https://vendor.example")
    assert health.classify(httpx.ConnectError("refused", request=req))["reason"] == "network"
    assert health.classify(httpx.ReadTimeout("slow", request=req))["reason"] == "network"
    assert health.classify(asyncio.TimeoutError())["reason"] == "network"


# ---------------------------------------------------------------- discovery feeds it
def test_a_cache_made_with_another_key_is_listed_again():
    calls = []

    async def fetch():
        calls.append(1)
        return [DiscoveredModel(id="m1")]

    save_cached(DiscoveryResult(provider="deepseek", models=[DiscoveredModel(id="old")], fetched_at=time.time(), key_fp="other"))
    res = asyncio.run(discover("deepseek", fetch, key_fp=health.fingerprint(KEY)))
    assert calls and not res.from_cache and res.key_fp == health.fingerprint(KEY)
    assert load_cached("deepseek").key_fp == health.fingerprint(KEY)
    # same key, fresh cache: no call
    res = asyncio.run(discover("deepseek", fetch, key_fp=health.fingerprint(KEY)))
    assert len(calls) == 1 and res.from_cache
    # no fingerprint given: the old behaviour (a fresh cache is used whatever key made it)
    save_cached(DiscoveryResult(provider="deepseek", models=[], fetched_at=time.time(), key_fp="other"))
    assert asyncio.run(discover("deepseek", fetch)).from_cache


def test_an_offline_sandbox_never_lists_models(monkeypatch):
    calls = []

    async def fetch():
        calls.append(1)
        return [DiscoveredModel(id="m1")]

    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    # nothing cached: an empty result, no call, no error
    res = asyncio.run(discover("deepseek", fetch, key_fp=health.fingerprint(KEY)))
    assert not calls and res.models == [] and not res.error
    # a cache made with another key, and a forced refresh: still no call, the cache is served
    save_cached(DiscoveryResult(provider="deepseek", models=[DiscoveredModel(id="old")], fetched_at=time.time(), key_fp="other"))
    res = asyncio.run(discover("deepseek", fetch, key_fp=health.fingerprint(KEY), force=True))
    assert not calls and [m.id for m in res.models] == ["old"]


def test_a_failed_listing_is_classified():
    async def fetch():
        raise _status_error(401)

    res = asyncio.run(discover("anthropic", fetch, key_fp="x"))
    assert res.error and res.error_info == {"reason": "rejected", "status": 401, "message": "the provider rejected the key"}


def test_refresh_files_each_providers_result(monkeypatch):
    import importlib

    catmod = importlib.import_module("omniapi_mcp.catalog.catalog")
    answers = {"deepseek": [DiscoveredModel(id="deepseek-flash"), DiscoveredModel(id="deepseek-v4-pro"), DiscoveredModel(id="deepseek-v4-flash-2026-04-01")]}  # a dated snapshot: listed, not counted

    async def fake_fetch(base_url, api_key, **k):
        if "deepseek" in base_url:
            return answers["deepseek"]
        raise _status_error(401)

    monkeypatch.setattr(catmod, "fetch_openai_compatible", fake_fetch)
    cat = ModelCatalog()
    eff, _ = US.build_settings({"providers": {"deepseek": {"api_key": KEY}, "openai": {"api_key": "sk-openai-wrong-000000000"}}})
    asyncio.run(cat.refresh(eff, force=True))
    ds = health.view("deepseek", KEY, online_models=cat.online_count("deepseek"))
    assert ds["state"] == "ok" and ds["listed"] == 3 and ds["online_models"] == 2 and ds["source"] == "discovery"
    oa = health.view("openai", "sk-openai-wrong-000000000")
    assert oa["state"] == "failed" and oa["reason"] == "rejected"
    assert cat.online_count("anthropic") is None  # never listed


# ---------------------------------------------------------------- test-key
def test_key_shape_rules_ignore_vendor_prefixes(monkeypatch):
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    s = Settings(_env_file=None)
    for slot, key in (("gemini", "AQ.Ab8RN6-new-auth-key-without-AIza"), ("deepseek", "0123456789abcdef0123"), ("kie", "a" * 32),
                      ("anthropic", "not-sk-ant-but-long-enough")):
        r = asyncio.run(US.test_key(slot, key, s))
        assert r["ok"] is True and r["simulated"] is True, (slot, r)
    for key, why in (("has space inside-0123456789", "whitespace"), ("line\nbreak-0123456789abc", "whitespace"),
                     ("short-key-123", "too short"), ("ｓｋ－全形字元的金鑰０１２３４５６７", "characters")):
        r = asyncio.run(US.test_key("openai", key, s))
        assert r["ok"] is False and r["reason"] == "format" and why in r["message"], (key, r)


def test_kie_is_tested_with_its_credit_query(monkeypatch):
    seen = []

    async def fake_send(self, request, *a, **k):
        auth = request.headers.get("authorization") or ""
        seen.append((request.method, str(request.url), auth))
        if "httpdeny" in auth:
            return httpx.Response(401, json={"code": 401, "msg": "nope"}, request=request)
        if "bodydeny" in auth:
            return httpx.Response(200, json={"code": 401, "msg": "You do not have access permissions"}, request=request)
        return httpx.Response(200, json={"code": 200, "msg": "success", "data": 1234.5}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    s = Settings(_env_file=None)
    ok = asyncio.run(US.test_key("kie", KEY, s))
    assert ok["ok"] is True and ok["credits"] == 1234.5 and "1234.5 credits" in ok["message"]
    assert seen[-1] == ("GET", "https://api.kie.ai/api/v1/chat/credit", f"Bearer {KEY}")
    for k in ("kie-httpdeny-0123456789", "kie-bodydeny-0123456789"):
        bad = asyncio.run(US.test_key("kie", k, s))
        assert bad["ok"] is False and bad["reason"] == "rejected" and bad["status"] == 401
    # the result is the provider's connection status for that key
    assert health.view("kie", KEY)["state"] == "ok" and health.view("kie", KEY)["credits"] == 1234.5
    assert health.view("kie", "kie-bodydeny-0123456789")["reason"] == "rejected"


def test_a_real_test_is_filed_and_a_simulated_one_is_not(monkeypatch):
    async def fake_send(self, request, *a, **k):
        return httpx.Response(429, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    r = asyncio.run(US.test_key("openai", KEY, Settings(_env_file=None)))
    assert r["ok"] is False and r["reason"] == "rate_limited" and r["status"] == 429
    assert health.view("openai", KEY)["reason"] == "rate_limited" and health.view("openai", KEY)["source"] == "test"
    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    asyncio.run(US.test_key("deepseek", KEY, Settings(_env_file=None)))
    assert health.view("deepseek", KEY)["state"] == "untested"


# ---------------------------------------------------------------- describe
def test_a_settings_key_names_the_env_key_it_hides(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"PROVIDERS__DEEPSEEK__API_KEY={ENV_KEY}\nPROVIDERS__KIE__API_KEY={ENV_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("PROVIDERS__OPENAI__API_KEY", ENV_KEY)
    overlay = {"providers": {"deepseek": {"api_key": KEY}, "kie": {"api_key": ""}, "openai": {"api_key": KEY}}}
    eff, env = US.build_settings(overlay, env_file=env_file)
    view = US.describe(eff, env, overlay)
    assert ENV_KEY not in json.dumps(view) and KEY not in json.dumps(view)
    assert view["providers"]["deepseek"]["key"] == {"set": True, "last4": KEY[-4:], "source": "settings",
                                                    "shadowed": {"set": True, "last4": "3a90", "source": "env", "where": "env_file"}}
    assert view["providers"]["openai"]["key"]["shadowed"]["where"] == "environment"
    assert view["providers"]["kie"]["key"]["shadowed"]["last4"] == "3a90"  # "" hides it too
    # nothing to hide: null
    eff, env = US.build_settings({}, env_file=env_file)
    v = US.describe(eff, env, {})
    assert v["providers"]["deepseek"]["key"]["source"] == "env" and v["providers"]["deepseek"]["key"]["shadowed"] is None
    assert v["providers"]["anthropic"]["key"]["shadowed"] is None


def test_describe_carries_health_key_pages_and_suggestions():
    overlay = {"providers": {"deepseek": {"api_key": KEY}}}
    eff, env = US.build_settings(overlay)
    view = US.describe(eff, env, overlay)
    assert view["providers"]["deepseek"]["health"]["state"] == "untested"
    assert view["providers"]["anthropic"]["health"] is None  # no key
    health.record("deepseek", KEY, ok=True, source="test", listed=4)
    assert US.describe(eff, env, overlay)["providers"]["deepseek"]["health"]["state"] == "ok"
    assert view["providers"]["anthropic"]["get_key"]["url"] == "https://platform.claude.com/settings/keys"
    assert view["providers"]["openai"]["get_key"]["url"] is None and view["providers"]["openai"]["get_key"]["docs"]
    assert view["providers"]["gemini"]["suggested_tiers"]["standard"] == "gemini-3.8-flash"
    assert view["providers"]["openrouter"]["suggested_tiers"] is None
    assert view["providers"]["kie"]["suggested_tiers"] is None


# ---------------------------------------------------------------- suggested tiers: the rule, checked
def _candidates(provider: str) -> list:
    out = []
    for e in catalog.models(provider=provider, modality="text"):
        note = (e.note or "").lower()
        if e.status != "current" or e.shutdown or e.snapshot:
            continue
        if "responses api only" in note or note.startswith("legacy") or "retirement not sooner" in note:
            continue
        if not (e.pricing or {}).get("output"):
            continue
        out.append(e)
    return out


def _price(e) -> tuple:
    return (e.pricing["output"], e.pricing["input"])


def test_suggested_tiers_follow_the_written_rule():
    with_text = {p for p, cfg in catalog.providers.items() if "text" in (cfg.get("modalities") or [])}
    suggested = {p for p, cfg in catalog.providers.items() if cfg.get("suggested_tiers")}
    assert suggested == with_text - {"openrouter"}
    assert catalog.providers["openrouter"]["suggested_tiers"] is None and catalog.providers["openrouter"]["suggested_tiers_note"]
    for p in suggested:
        tiers = catalog.providers[p]["suggested_tiers"]
        assert set(tiers) == {"cheap", "standard", "strong"}
        cands = _candidates(p)
        by_id = {e.id: e for e in cands}
        for tier, mid in tiers.items():
            assert mid in by_id, f"{p}.{tier}: {mid} is not a candidate (current, no shutdown, chat-capable, not legacy)"
        cheap, standard, strong = (by_id[tiers[t]] for t in ("cheap", "standard", "strong"))
        assert _price(cheap) == min(_price(e) for e in cands), p
        assert _price(strong) == max(_price(e) for e in cands), p
        assert _price(cheap) <= _price(standard) <= _price(strong), p


def test_key_pages_exist_for_every_provider_and_are_https():
    for p, cfg in catalog.providers.items():
        gk = cfg.get("get_key")
        assert isinstance(gk, dict) and (gk.get("url") or gk.get("docs")), p
        for k in ("url", "docs"):
            assert gk.get(k) is None or gk[k].startswith("https://"), (p, k)


# ---------------------------------------------------------------- outside programs
def _fake_which(table):
    return lambda name: table.get(name)


def test_detect_reports_found_missing_and_how_to_install(tmp_path):
    from omniapi_mcp.utils import external_tools as X

    home = tmp_path / "userhome"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    claude_exe = tmp_path / "claude.exe"
    claude_exe.write_bytes(b"MZ")
    ran = []

    def run(args):
        ran.append(args)
        return {"node": "v22.11.0", "ffmpeg": "ffmpeg version 7.1-full_build-www.gyan.dev Copyright (c)"}.get(Path(args[0]).stem)

    which = _fake_which({"node": "C:/n/node.exe", "npx": "C:/n/npx.cmd", "ffmpeg": "C:/f/ffmpeg.exe", "claude": str(claude_exe)})
    out = X.detect(None, which=which, fresh_path=None, run=run, home=home, bundled=lambda: None)
    tools = {t["id"]: t for t in out["tools"]}
    assert list(tools) == ["node", "ffmpeg", "claude_code", "claude_login", "codex", "gemini_cli"]
    assert tools["node"]["found"] and tools["node"]["version"] == "22.11.0" and tools["node"]["npx_path"] == "C:/n/npx.cmd"
    assert tools["ffmpeg"]["version"] == "7.1-full_build-www.gyan.dev"
    assert tools["claude_code"]["found"] and tools["claude_code"]["cli_source"] == "path" and tools["claude_code"]["version"] is None
    assert tools["claude_login"]["found"]
    assert not tools["codex"]["found"] and tools["codex"]["install"][0]["command"].startswith("irm https://chatgpt.com/codex")
    assert out["summary"] == {"found": 4, "missing": 2}
    for t in tools.values():
        assert t["affects"] and t["source"].startswith("https://") and t["install"]
    # versions only from node and ffmpeg (Codex / Gemini / Claude are never run)
    assert sorted(Path(a[0]).stem for a in ran) == ["ffmpeg", "node"]


def test_detect_without_npx_and_with_a_program_installed_after_start(tmp_path):
    from omniapi_mcp.utils import external_tools as X

    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    (fresh_dir / "codex.cmd").write_text("@echo off", encoding="utf-8")
    which = _fake_which({"node": "C:/n/node.exe"})
    out = X.detect(None, which=which, fresh_path=str(fresh_dir), run=lambda a: None, home=tmp_path, bundled=lambda: None, versions=False)
    tools = {t["id"]: t for t in out["tools"]}
    assert not tools["node"]["found"] and tools["node"]["detail"] == "找不到 npx"
    assert not tools["claude_login"]["found"] and not tools["claude_code"]["found"] and tools["claude_code"]["detail"]
    if os.name == "nt":  # shutil.which finds codex.cmd through PATHEXT on Windows only
        assert not tools["codex"]["found"] and tools["codex"]["installed_after_start"] and "重開服務" in tools["codex"]["detail"]


def test_version_parsers_are_strict():
    from omniapi_mcp.utils import external_tools as X

    assert X.node_version("node", lambda a: "v20.1.0") == "20.1.0"
    assert X.node_version("node", lambda a: "garbage") is None
    assert X.ffmpeg_version("ffmpeg", lambda a: None) is None


# ---------------------------------------------------------------- Claude Code's MCP entry
def test_claude_mcp_status_and_apply(tmp_path):
    cfg = tmp_path / "claude.json"
    assert claude_mcp.config_path() == cfg  # OMNIAPI_CLAUDE_CONFIG (never the real file in tests)
    assert claude_mcp.status("127.0.0.1", 7788)["state"] == "no_config"
    with pytest.raises(claude_mcp.ClaudeConfigError) as e:
        claude_mcp.apply("127.0.0.1", 7788)
    assert e.value.reason == "no_config" and not cfg.exists()  # never created blind

    cfg.write_text("{not json", encoding="utf-8")
    assert claude_mcp.status("127.0.0.1", 7788)["state"] == "unreadable"
    with pytest.raises(claude_mcp.ClaudeConfigError):
        claude_mcp.apply("127.0.0.1", 7788)
    assert cfg.read_text(encoding="utf-8") == "{not json"  # left as it was

    old = {"command": "uv", "args": ["run", "omniapi-mcp"], "env": {"PROVIDERS__OPENAI__API_KEY": "sk-secret-in-entry"}}
    cfg.write_text(json.dumps({"numStartups": 7, "projects": {"C:/x": {"a": 1}}, "mcpServers": {"omniapi-mcp": old, "other": {"type": "http", "url": "u"}}}),
                   encoding="utf-8")
    st = claude_mcp.status("127.0.0.1", 7788)
    assert st["state"] == "other" and st["entry"] == {"type": "stdio", "command": "uv"} and "sk-secret" not in json.dumps(st)
    res = claude_mcp.apply("127.0.0.1", 7788)
    assert res["changed"] and res["backed_up"] and res["state"] == "connected"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["omniapi-mcp"] == {"type": "http", "url": "http://127.0.0.1:7788/mcp"}
    assert data["numStartups"] == 7 and data["projects"] == {"C:/x": {"a": 1}} and data["mcpServers"]["other"] == {"type": "http", "url": "u"}
    assert json.loads(claude_mcp.backup_path().read_text(encoding="utf-8")) == old
    again = claude_mcp.apply("127.0.0.1", 7788)
    assert again["changed"] is False and again["state"] == "connected"
    # a different port reads as "other"
    assert claude_mcp.status("127.0.0.1", 7830)["state"] == "other"


def test_claude_mcp_missing_entry(tmp_path):
    (tmp_path / "claude.json").write_text(json.dumps({"numStartups": 1}), encoding="utf-8")
    assert claude_mcp.status("127.0.0.1", 7788)["state"] == "missing"
    res = claude_mcp.apply("127.0.0.1", 7788)
    assert res["changed"] and not res["backed_up"] and res["state"] == "connected"


def test_cli_mcp_config_uses_the_same_code(tmp_path):
    from typer.testing import CliRunner

    from omniapi_mcp.cli import app

    cfg = tmp_path / "claude.json"
    r = CliRunner().invoke(app, ["mcp-config", "--apply", "--port", "7830"])
    assert r.exit_code == 1 and not cfg.exists()
    cfg.write_text("{}", encoding="utf-8")
    r = CliRunner().invoke(app, ["mcp-config", "--apply", "--port", "7830"])
    assert r.exit_code == 0, r.output
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["omniapi-mcp"]["url"] == "http://127.0.0.1:7830/mcp"
    r = CliRunner().invoke(app, ["mcp-config"])
    assert r.exit_code == 0 and '"omniapi-mcp"' in r.output


# ---------------------------------------------------------------- the REST side
@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from omniapi_mcp import server as srv
    from omniapi_mcp.daemon import app as dapp

    async def _no_discovery(*a, **k):
        return {}

    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")
    monkeypatch.setenv("OMNIAPI_DEV", "1")
    monkeypatch.setattr(catalog, "refresh", _no_discovery)
    monkeypatch.setattr(srv.mcp, "_session_manager", None)
    monkeypatch.setattr(srv, "settings", srv.settings)
    app = dapp.create_app(Settings(_env_file=None), host="127.0.0.1", port=7799)
    with TestClient(app, base_url="http://127.0.0.1:7799") as client:
        yield client


def test_settings_and_models_carry_the_new_fields(daemon):
    daemon.patch("/api/settings", json={"providers": {"deepseek": {"api_key": KEY}}})
    health.record("deepseek", KEY, ok=False, source="test", reason="rejected", status=401, message="the provider rejected the key")
    v = daemon.get("/api/settings").json()
    ds = v["providers"]["deepseek"]
    assert ds["key"]["shadowed"] is None and ds["health"]["state"] == "failed" and ds["health"]["reason"] == "rejected"
    assert ds["get_key"]["url"] == "https://platform.deepseek.com/api_keys" and ds["suggested_tiers"]["cheap"] == "deepseek-flash"
    m = daemon.get("/api/models").json()
    assert m["providers"]["deepseek"]["health"]["state"] == "failed" and m["providers"]["anthropic"]["health"] is None
    assert m["providers"]["google"]["suggested_tiers"]["strong"] == "gemini-3.1-pro-preview"
    assert KEY not in json.dumps(v) and KEY not in json.dumps(m)


def test_status_has_the_service_section(daemon, tmp_path):
    s = daemon.get("/api/status").json()["service"]
    assert s["layout"] in ("repo", "installed") and s["data_home"] == str(tmp_path / "home")
    assert Path(s["storage"]).is_absolute() and s["settings_file"].endswith("settings.json")
    assert s["env_file"] is None and s["env_file_suggested"] and s["storage_env"] == "STORAGE__BASE_PATH"


def test_tools_endpoint_caches_and_refreshes(daemon, monkeypatch):
    from omniapi_mcp.utils import external_tools as X

    calls = []

    def fake_detect(settings):
        calls.append(1)
        return {"checked_at": len(calls), "tools": [], "summary": {"found": 0, "missing": 0}}

    monkeypatch.setattr(X, "detect", fake_detect)
    assert daemon.get("/api/tools").json()["checked_at"] == 1
    assert daemon.get("/api/tools").json()["checked_at"] == 1
    assert daemon.get("/api/tools?refresh=true").json()["checked_at"] == 2


def test_claude_mcp_endpoints(daemon, tmp_path):
    cfg = tmp_path / "claude.json"
    assert daemon.get("/api/claude-mcp").json()["state"] == "no_config"
    r = daemon.post("/api/claude-mcp")
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "no_config"
    cfg.write_text(json.dumps({"keep": True}), encoding="utf-8")
    assert daemon.get("/api/claude-mcp").json()["state"] == "missing"
    # a web page from elsewhere cannot do it
    assert daemon.post("/api/claude-mcp", headers={"Origin": "https://evil.example"}).status_code == 403
    assert "mcpServers" not in json.loads(cfg.read_text(encoding="utf-8"))
    r = daemon.post("/api/claude-mcp", headers={"Origin": "http://127.0.0.1:7799"})
    assert r.status_code == 200 and r.json()["state"] == "connected" and r.json()["changed"] is True
    assert r.json()["expected"]["url"] == "http://127.0.0.1:7799/mcp"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["keep"] is True and data["mcpServers"]["omniapi-mcp"]["url"] == "http://127.0.0.1:7799/mcp"
    assert any(e.get("type") == "claude_mcp.changed" for e in daemon.get("/api/events").json())
