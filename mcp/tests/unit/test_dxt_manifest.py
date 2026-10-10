"""The Claude Desktop extension's manifest (mcp/manifest.json): every key field the user is asked for
reaches the environment variable the service reads, and a blank field leaves its provider off."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from omniapi_mcp.config.settings import Settings

MANIFEST = Path(__file__).resolve().parents[2] / "manifest.json"
if not MANIFEST.is_file():
    pytest.skip(f"{MANIFEST} not found", allow_module_level=True)

M = json.loads(MANIFEST.read_text(encoding="utf-8"))
ENV: dict[str, str] = M["server"]["mcp_config"]["env"]
FIELDS: dict[str, dict] = M["user_config"]
REF = re.compile(r"^\$\{user_config\.([a-z_]+)\}$")

#: the key fields and the provider slot each one feeds
KEY_FIELDS = {
    "openai_api_key": "openai",
    "elevenlabs_api_key": "elevenlabs",
    "kie_api_key": "kie",
    "openrouter_api_key": "openrouter",
    "google_api_key": "gemini",
    "anthropic_api_key": "anthropic",
    "deepseek_api_key": "deepseek",
}


def _refs() -> dict[str, str]:
    """env var -> user_config field, for the variables the manifest fills from a field."""
    return {k: REF.match(v).group(1) for k, v in ENV.items() if REF.match(v)}


def test_every_field_a_variable_names_exists_and_every_key_field_is_wired():
    refs = _refs()
    assert set(refs.values()) <= set(FIELDS), set(refs.values()) - set(FIELDS)
    for field, slot in KEY_FIELDS.items():
        assert field in FIELDS and FIELDS[field]["sensitive"] is True, field
        assert refs.get(f"PROVIDERS__{slot.upper()}__API_KEY") == field, field
        assert ENV.get(f"PROVIDERS__{slot.upper()}__ENABLED") == "true", slot


def test_the_new_key_fields_are_optional_and_blank_by_default():
    for field in ("google_api_key", "anthropic_api_key", "deepseek_api_key"):
        assert FIELDS[field]["required"] is False and FIELDS[field]["default"] == ""
        assert "optional" in FIELDS[field]["title"].lower()


def _settings_from(values: dict[str, str], monkeypatch) -> Settings:
    import os

    for k in list(os.environ):
        if k.upper().startswith("PROVIDERS__"):
            monkeypatch.delenv(k)
    for k, v in ENV.items():
        if not k.startswith("PROVIDERS__"):
            continue
        m = REF.match(v)
        monkeypatch.setenv(k, values.get(m.group(1), "") if m else v)
    return Settings(_env_file=None)


def test_the_variables_are_the_ones_the_service_reads(monkeypatch):
    fake = {field: f"manifest-test-not-a-key-{i}" for i, field in enumerate(KEY_FIELDS)}
    s = _settings_from(fake, monkeypatch)
    for i, (field, slot) in enumerate(KEY_FIELDS.items()):
        cfg = getattr(s.providers, slot)
        assert cfg is not None and cfg.api_key == f"manifest-test-not-a-key-{i}" and cfg.enabled, slot


def test_blank_optional_fields_leave_their_providers_unconfigured(monkeypatch):
    # the OpenAI key is the one required field; every other one may stay blank
    s = _settings_from({"openai_api_key": "manifest-test-not-a-key-openai"}, monkeypatch)
    for slot in ("gemini", "anthropic", "deepseek", "elevenlabs", "kie", "openrouter"):
        cfg = getattr(s.providers, slot)
        assert cfg is None or not cfg.api_key, slot
    assert s.providers.enabled_providers == ["openai"]
