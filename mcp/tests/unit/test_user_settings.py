"""1.3-M2: the writable settings layer (settings.json over .env over defaults)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from omniapi_mcp.catalog import catalog
from omniapi_mcp.config import user_settings as US
from omniapi_mcp.config.settings import Settings

KEY = "sk-settingsjson-0123456789abcd"
ENV_KEY = "sk-environment-zyxwvut98765"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    for k in list(os.environ):
        if k.upper().startswith(("PROVIDERS__", "TIERS__", "DEFAULTS__", "IMAGES__")):
            monkeypatch.delenv(k)
    monkeypatch.setattr("omniapi_mcp.layout.env_file", lambda *a, **k: None)
    yield
    catalog.set_tier_overrides({})


# ---------------------------------------------------------------- file
def test_write_is_atomic_and_reads_back(tmp_path):
    p = US.write_overlay({"tiers": {"cheap": "gpt-5.4-mini"}})
    assert p == tmp_path / "home" / "settings.json"
    assert US.read_overlay() == {"tiers": {"cheap": "gpt-5.4-mini"}, "version": 1}
    assert [f.name for f in p.parent.iterdir() if f.name.endswith(".tmp")] == []  # no temp file left behind


def test_missing_or_empty_file_is_an_empty_overlay(tmp_path):
    assert US.read_overlay() == {}
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "settings.json").write_text("  ", encoding="utf-8")
    assert US.read_overlay() == {}


def test_reader_retries_while_the_writer_renames(tmp_path, monkeypatch):
    US.write_overlay({"tiers": {"cheap": "gpt-5.4-mini"}})
    real = Path.read_text
    fails = {"n": 2}

    def flaky(self, *a, **k):
        if self.name == "settings.json" and fails["n"]:
            fails["n"] -= 1
            raise PermissionError(13, "in use")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr(US, "READ_RETRY_DELAY", 0.001)
    assert US.read_overlay()["tiers"] == {"cheap": "gpt-5.4-mini"}
    assert fails["n"] == 0


def test_a_corrupt_file_raises_for_the_api_and_is_ignored_at_startup(tmp_path, monkeypatch, caplog):
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "settings.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(US, "READ_RETRY_DELAY", 0.001)
    with pytest.raises(US.SettingsFileError):
        US.read_overlay()
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__API_KEY", ENV_KEY)
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__ENABLED", "true")
    s = US.load_settings(env_file=None)  # the service still starts, on the environment alone
    assert s.providers.deepseek.api_key == ENV_KEY


# ---------------------------------------------------------------- precedence
def test_settings_json_beats_env_beats_defaults(monkeypatch):
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__API_KEY", ENV_KEY)
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__ENABLED", "true")
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__TIMEOUT", "77")
    monkeypatch.setenv("DEFAULTS__DISPATCH", "standard")
    eff, env = US.build_settings({"providers": {"deepseek": {"api_key": KEY}}, "defaults": {"chat": "strong", "image": "gpt-image-1.5"}})
    assert env.providers.deepseek.api_key == ENV_KEY
    assert eff.providers.deepseek.api_key == KEY  # settings.json wins
    assert eff.providers.deepseek.timeout == 77  # the rest of the env block is kept
    assert eff.defaults.chat == "strong" and eff.defaults.dispatch == "standard"  # json, then env
    assert eff.defaults.speech is None  # built-in
    assert eff.images.default_model == "gpt-image-1.5"
    assert US.default_model(eff, "image") == "gpt-image-1.5"
    assert US.default_model(Settings(_env_file=None), "chat") == "cheap"
    assert US.default_model(Settings(_env_file=None), "transcript") == "gpt-transcribe"
    assert US.default_model(None, "dispatch") == "cheap"


def test_a_key_saved_in_settings_is_switched_on():
    eff, _ = US.build_settings({"providers": {"kie": {"api_key": KEY}}})
    assert eff.providers.kie.enabled and "kie" in eff.providers.enabled_providers


def test_disabling_and_clearing_keys(monkeypatch):
    monkeypatch.setenv("PROVIDERS__OPENAI__API_KEY", ENV_KEY)
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__API_KEY", ENV_KEY)
    monkeypatch.setenv("PROVIDERS__DEEPSEEK__ENABLED", "true")
    eff, env = US.build_settings({"providers": {"openai": {"api_key": ""}, "deepseek": {"enabled": False}}})
    assert "openai" in env.providers.enabled_providers
    assert eff.providers.openai is None  # "" = no key, whatever the environment says
    assert eff.providers.deepseek.api_key == ENV_KEY and not eff.providers.deepseek.enabled
    assert eff.providers.enabled_providers == []


def test_switching_on_a_provider_without_any_key_is_kept_for_later():
    # OpenAI's block needs a key; a switch alone must not break validation
    eff, _ = US.build_settings({"providers": {"openai": {"enabled": True}}})
    assert eff.providers.openai is None


# ---------------------------------------------------------------- patch
def test_patch_sets_and_null_removes():
    o, changed = US.apply_patch({}, {"providers": {"google": {"api_key": f"  {KEY}  "}}, "tiers": {"cheap": "gpt-5.4-mini"}})
    assert o == {"providers": {"gemini": {"api_key": KEY}}, "tiers": {"cheap": "gpt-5.4-mini"}}
    assert sorted(changed) == ["providers.gemini.api_key", "tiers.cheap"]
    o2, changed = US.apply_patch(o, {"tiers": {"cheap": None}, "providers": {"gemini": {"api_key": None}}})
    assert o2 == {} and sorted(changed) == ["providers.gemini.api_key", "tiers.cheap"]
    _, changed = US.apply_patch(o, {"tiers": {"cheap": "gpt-5.4-mini"}})
    assert changed == []  # same value: nothing to do


@pytest.mark.parametrize("body", [
    {"nope": 1},
    {"providers": {"acme": {"api_key": "x"}}},
    {"providers": {"openai": {"base_url": "http://evil"}}},
    {"providers": {"openai": {"enabled": "yes"}}},
    {"providers": {"openai": {"api_key": "two words"}}},
    {"tiers": {"premium": "x"}},
    {"tiers": {"cheap": "strong"}},
    {"tiers": {"cheap": ""}},
    {"defaults": {"hologram": "x"}},
    {"video": {"max_wait_minutes": 0}},
    {"video": {"mcp_max_usd": "1"}},
    {"video": {"keep_collecting": "yes"}},
    {"video": {"poll_every": 5}},
    [],
])
def test_patch_rejects_what_it_does_not_know(body):
    with pytest.raises(US.SettingsError):
        US.apply_patch({}, body)


def test_validation_errors_never_echo_the_key(monkeypatch):
    monkeypatch.setenv("PROVIDERS__OPENAI__API_KEY", ENV_KEY)
    monkeypatch.setenv("PROVIDERS__DEFAULT_PROVIDER", "openai")  # must stay an enabled provider
    with pytest.raises(US.SettingsError) as e:
        US.build_settings({"providers": {"openai": {"api_key": KEY, "enabled": False}}})
    assert KEY not in str(e.value) and KEY[-12:] not in str(e.value)


def test_unknown_or_wrong_kind_models_are_saved_with_a_warning():
    w = US.model_warnings({"tiers": {"cheap": "no-such-model", "strong": "moonshotai/kimi-k3"},
                           "defaults": {"chat": "standard", "dispatch": "echo", "image": "gpt-transcribe", "speech": "gpt-image-2",
                                        "transcript": "gpt-transcribe"}})
    assert any(x.startswith("tiers.cheap") for x in w)
    assert not any(x.startswith(("tiers.strong", "defaults.chat", "defaults.dispatch", "defaults.transcript")) for x in w)
    assert any(x.startswith("defaults.image") and "transcription" in x for x in w)
    assert any(x.startswith("defaults.speech") for x in w)


# ---------------------------------------------------------------- describe
def test_describe_masks_keys_and_names_sources(monkeypatch):
    monkeypatch.setenv("PROVIDERS__OPENAI__API_KEY", ENV_KEY)
    overlay = {"providers": {"deepseek": {"api_key": KEY}, "kie": {"api_key": ""}}, "tiers": {"strong": "claude-opus-5-5"},
               "defaults": {"chat": "standard"}}
    eff, env = US.build_settings(overlay)
    view = US.describe(eff, env, overlay)
    text = json.dumps(view)
    assert KEY not in text and ENV_KEY not in text
    assert view["providers"]["deepseek"]["key"] == {"set": True, "last4": KEY[-4:], "source": "settings", "shadowed": None}
    assert view["providers"]["deepseek"]["configured"] and view["providers"]["deepseek"]["enabled_source"] == "default"
    assert view["providers"]["openai"]["key"] == {"set": True, "last4": ENV_KEY[-4:], "source": "env", "shadowed": None}
    assert view["providers"]["kie"]["key"] == {"set": False, "last4": None, "source": "settings", "shadowed": None}  # cleared on purpose
    assert view["providers"]["anthropic"]["key"] == {"set": False, "last4": None, "source": None, "shadowed": None}
    assert view["providers"]["gemini"]["provider"] == "google"
    assert view["tiers"]["strong"] == {"model": "claude-opus-5-5", "source": "settings", "catalog": catalog.catalog_tiers["strong"]}
    assert view["tiers"]["cheap"]["source"] == "catalog"
    assert view["defaults"]["chat"] == {"model": "standard", "source": "settings"}
    assert view["defaults"]["dispatch"] == {"model": "cheap", "source": "default"}
    assert view["defaults"]["image"]["model"] == "gpt-image-2"
    assert view["defaults"]["transcript"]["model"] == "gpt-transcribe"


def test_short_keys_show_no_last_four():
    assert US.mask("abc") == {"set": True, "last4": None}
    assert US.mask("") == {"set": False, "last4": None}


# ---------------------------------------------------------------- tiers in the catalog
def test_tier_overrides_lay_over_catalog_json():
    shipped = catalog.catalog_tiers
    assert catalog.set_tier_overrides({"cheap": "gpt-5.4-mini", "premium": "x"})["cheap"] == "gpt-5.4-mini"
    assert catalog.resolve("cheap") == "gpt-5.4-mini"
    assert catalog.tiers["standard"] == shipped["standard"]
    assert "premium" not in catalog.tiers  # only the catalog's own tiers
    catalog.set_tier_overrides({})
    assert catalog.resolve("cheap") == shipped["cheap"] and catalog.tiers == shipped
    catalog.set_tier_overrides({"cheap": "gpt-5.4-mini"})
    catalog.load()  # a reload keeps the owner's choice
    assert catalog.resolve("cheap") == "gpt-5.4-mini"


def test_defaults_reach_the_tools():
    from omniapi_mcp.tools.text import TextTool

    eff, _ = US.build_settings({"providers": {"openai": {"api_key": KEY}}, "defaults": {"chat": "gpt-5.4-mini"}})
    assert TextTool(eff)._default_model() == "gpt-5.4-mini"
    eff, _ = US.build_settings({"providers": {"openai": {"api_key": KEY}}})
    tool = TextTool(eff)  # cheap → deepseek-flash, not configured: the first provider's own default
    assert tool._default_model() == tool._providers[0].DEFAULT_MODEL


# ---------------------------------------------------------------- test-key
def test_test_key_offline_sends_nothing(monkeypatch):
    import httpx

    monkeypatch.setenv("OMNIAPI_OFFLINE", "1")

    def boom(*a, **k):
        raise AssertionError("an offline daemon must not connect")

    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    r = asyncio.run(US.test_key("deepseek", KEY, Settings(_env_file=None)))
    assert r["ok"] is True and r["simulated"] is True and r["key"]["last4"] == KEY[-4:]
    assert asyncio.run(US.test_key("deepseek", "short", Settings(_env_file=None)))["ok"] is False
    assert asyncio.run(US.test_key("deepseek", "", Settings(_env_file=None)))["reason"] == "missing_key"


def test_test_key_online_reads_the_vendors_answer(monkeypatch):
    import httpx

    monkeypatch.delenv("OMNIAPI_OFFLINE", raising=False)
    seen = []

    async def fake_send(self, request, *a, **k):
        seen.append((str(request.url), request.headers.get("authorization")))
        code = 401 if "bad" in (request.headers.get("authorization") or "") else 200
        return httpx.Response(code, json={"data": [{"id": "m1"}, {"id": "m2"}]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    s = Settings(_env_file=None)
    ok = asyncio.run(US.test_key("deepseek", KEY, s))
    assert ok["ok"] is True and ok["models"] == 2 and seen[-1][0] == "https://api.deepseek.com/models"
    bad = asyncio.run(US.test_key("openrouter", "bad-key-123456789", s))
    assert bad["ok"] is False and bad["reason"] == "rejected" and seen[-1][0] == "https://openrouter.ai/api/v1/key"
    assert "bad-key" not in json.dumps(bad)
    kie = asyncio.run(US.test_key("kie", KEY, s))  # 1.3-M4: the read-only credit query (the fake answers without kie's code)
    assert seen[-1][0] == "https://api.kie.ai/api/v1/chat/credit" and kie["ok"] is False and kie["reason"] == "error"
