"""Unit tests: model catalog (curated + discovery merge) and the text
provider dialects added in v1.0 (Anthropic conversion, Gemini/OpenRouter
request shapes, tier aliases)."""

from __future__ import annotations

import json
import time

import pytest

from omniapi_mcp.capabilities.text import (
    AnthropicTextProvider,
    GeminiTextProvider,
    OpenAITextProvider,
    OpenRouterTextProvider,
)
from omniapi_mcp.catalog.catalog import ModelCatalog
from omniapi_mcp.catalog.discovery import DiscoveredModel, DiscoveryResult
from omniapi_mcp.providers.base import ProviderConfig


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAPI_HOME", str(tmp_path / "home"))
    return ModelCatalog()


def test_catalog_loads_curated_entries(cat):
    assert cat.get("gpt-6-sol").provider == "openai"
    assert cat.get("claude-opus-5-5").modality == "text"
    assert cat.get("gemini-3.1-flash-image").modality == "image"
    assert cat.get("V5").status == "retired"


def test_tier_and_alias_resolution(cat):
    assert cat.resolve("cheap") == "deepseek-flash"
    assert cat.resolve("strong") == "gpt-6-sol"
    assert cat.resolve("deepseek-v4-flash") == "deepseek-flash"
    assert cat.resolve("unknown-model") == "unknown-model"
    assert cat.resolve(None) is None


def test_retired_hidden_by_default(cat):
    ids = cat.ids(modality="music", provider="kie")
    assert "V6" in ids and "V5" not in ids
    assert "V5" in cat.ids(modality="music", provider="kie", include_retired=True)


def test_classify_discovered_ids(cat):
    assert cat.classify("openai", "gpt-7-nova") == "text"
    assert cat.classify("openai", "gpt-image-3") == "image"
    assert cat.classify("openai", "text-embedding-3-large") is None
    assert cat.classify("openai", "gpt-live-1") is None
    assert cat.classify("google", "gemini-4-flash-tts") == "speech"
    assert cat.classify("elevenlabs", "eleven_english_sts_v2") is None
    assert cat.classify("openrouter", "moonshotai/kimi-k3") == "text"


def test_merge_marks_online_and_adds_discovered(cat):
    res = DiscoveryResult(
        provider="openai",
        models=[
            DiscoveredModel(id="gpt-6-sol"),
            DiscoveredModel(id="gpt-7-nova", display_name="GPT-7 Nova"),
            DiscoveredModel(id="gpt-5.5-2026-04-23"),
            DiscoveredModel(id="text-embedding-3-large"),
        ],
        fetched_at=time.time(),
    )
    cat._merge("openai", res)
    assert cat.get("gpt-6-sol").online is True
    assert cat.get("gpt-6-astra").online is False  # curated but not listed
    nova = cat.get("gpt-7-nova", provider="openai")
    assert nova and nova.status == "discovered" and nova.modality == "text"
    snap = cat.get("gpt-5.5-2026-04-23", provider="openai")
    assert snap.snapshot is True
    assert "gpt-5.5-2026-04-23" not in cat.ids(provider="openai")
    assert "gpt-5.5-2026-04-23" in cat.ids(provider="openai", include_snapshots=True)
    assert cat.get("text-embedding-3-large", provider="openai") is None


def test_merge_respects_discovery_modalities(cat):
    # ElevenLabs /models lists TTS only — music entries must not go offline
    res = DiscoveryResult(
        provider="elevenlabs",
        models=[DiscoveredModel(id="eleven_v3")],
        fetched_at=time.time(),
    )
    cat._merge("elevenlabs", res)
    assert cat.get("eleven_v3").online is True
    assert cat.get("eleven_flash_v2_5").online is False
    assert cat.get("music_v2_5").online is None


def test_merge_with_error_and_no_models_changes_nothing(cat):
    res = DiscoveryResult(provider="openai", models=[], fetched_at=0.0, error="boom")
    cat._merge("openai", res)
    assert cat.get("gpt-6-sol").online is None


def test_estimate_text_cost(cat):
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}
    assert cat.estimate_text_cost("gpt-6-sol", usage) == pytest.approx(2.0 + 1.0)
    cached = {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cached_tokens": 500_000}
    assert cat.estimate_text_cost("gpt-6-sol", cached) == pytest.approx(0.5 * 2.0 + 0.5 * 0.2)
    assert cat.estimate_text_cost("gpt-5-chat-latest", usage) is None  # retired: no pricing kept
    assert cat.estimate_text_cost("gpt-6-sol", None) is None


def test_deepseek_costs_double_in_peak_hours(cat):
    """Peak = 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday (official pricing page)."""
    import calendar

    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "cached_tokens": 0}
    off = 0.15 + 0.6
    at = lambda *ymdhm: calendar.timegm((*ymdhm, 0, 0, 0, 0))  # noqa: E731
    cases = {
        at(2026, 9, 30, 2, 0): 2,    # Wed 02:00 UTC — peak
        at(2026, 9, 30, 3, 59): 2,
        at(2026, 9, 30, 4, 0): 1,    # the gap between the two windows
        at(2026, 9, 30, 5, 30): 1,
        at(2026, 9, 30, 6, 0): 2,
        at(2026, 9, 30, 9, 59): 2,
        at(2026, 9, 30, 10, 0): 1,
        at(2026, 9, 30, 0, 59): 1,
        at(2026, 10, 3, 2, 0): 1,    # Saturday
        at(2026, 10, 4, 7, 0): 1,    # Sunday
    }
    for when, factor in cases.items():
        assert cat.estimate_text_cost("deepseek-flash", usage, at=when) == pytest.approx(off * factor), when
    assert cat.estimate_text_cost("deepseek-v4-pro", usage, at=at(2026, 9, 30, 2, 0)) == pytest.approx((0.66 + 1.98) * 2)
    # a model without a peak rule is the same price at any hour
    assert cat.estimate_text_cost("gpt-6-sol", usage, at=at(2026, 9, 30, 2, 0)) == pytest.approx(2.0 + 10.0)
    # a listed holiday is off-peak all day
    cat.get("deepseek-flash").pricing["peak"]["except_dates"] = ["2026-09-30"]
    assert cat.estimate_text_cost("deepseek-flash", usage, at=at(2026, 9, 30, 2, 0)) == pytest.approx(off)


def test_every_listed_text_model_is_priced_and_dated(cat):
    """The curated list is what people pick from; discovery is only the safety
    net for models that appeared since. So a curated text model that is still on
    sale must carry a price, and a deprecated one its announced shutdown date."""
    listed = [m for m in cat.models(modality="text") if m.status in ("current", "deprecated")]
    assert len(listed) > 50
    unpriced = [m.id for m in listed if not (m.pricing and "input" in m.pricing and "output" in m.pricing)]
    assert unpriced == []
    undated = [m.id for m in listed if m.status == "deprecated" and not m.shutdown]
    assert undated == []
    assert {"gemini-2.5-pro", "claude-sonnet-5-5", "gpt-4o", "gpt-5.6-sol"} <= {m.id for m in listed}


def test_uncurated_snapshots_and_aliases_stay_callable_but_unlisted(cat):
    res = DiscoveryResult(
        provider="openai",
        models=[DiscoveredModel(id="gpt-7-nova-0613"), DiscoveredModel(id="gpt-7-nova")],
        fetched_at=time.time(),
    )
    cat._merge("openai", res)
    cat._merge("google", DiscoveryResult(provider="google", models=[DiscoveredModel(id="gemini-flash-latest"), DiscoveredModel(id="gemini-robotics-er-2-preview")], fetched_at=time.time()))
    listed = cat.ids(modality="text")
    assert "gpt-7-nova" in listed and "gpt-7-nova-0613" not in listed and "gemini-flash-latest" not in listed
    callable_ids = cat.ids(modality="text", include_snapshots=True)
    assert {"gpt-7-nova-0613", "gemini-flash-latest"} <= callable_ids
    assert cat.get("gemini-robotics-er-2-preview", provider="google") is None  # special-purpose endpoint: ignored
    assert "gpt-5.1-codex" not in callable_ids  # shut down upstream even if /models still lists it
    # a gateway's vendor/model ids end in digits for ordinary models — those stay listed
    cat._merge("openrouter", DiscoveryResult(provider="openrouter", models=[DiscoveredModel(id="mistralai/mistral-large-2411")], fetched_at=time.time()))
    assert "mistralai/mistral-large-2411" in cat.ids(modality="text")


def test_snapshot_shape(cat):
    snap = cat.snapshot(modality="image")
    assert set(snap["models"]) == {"image"}
    assert snap["tiers"]["cheap"] == "deepseek-flash"
    ids = {m["id"] for m in snap["models"]["image"]}
    assert {"gpt-image-2.5-sunburst", "gemini-3-pro-image"} <= ids


# --------------------------------------------------------------------------
# text providers (pure request building, no network)
# --------------------------------------------------------------------------

CFG = ProviderConfig(api_key="test-key", enabled=True)


def test_openai_dynamic_roster_includes_catalog_models():
    p = OpenAITextProvider(CFG)
    models = p.get_supported_models()
    assert "gpt-6-astra" in models and "gpt-5.4-mini" in models


def test_gemini_dialect_keeps_sampling_and_system():
    p = GeminiTextProvider(CFG)
    req = p._build_request(
        "gemini-3.8-flash",
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        {"temperature": 0.2, "max_completion_tokens": 10, "reasoning_effort": "low"},
    )
    assert req["messages"][0]["role"] == "system"
    assert req["temperature"] == 0.2
    assert req["max_tokens"] == 10 and "max_completion_tokens" not in req
    assert req["reasoning_effort"] == "low"
    assert str(p.client.base_url).startswith("https://generativelanguage.googleapis.com")


def test_gemini_thinking_tokens_are_billed_as_output(cat):
    """Gemini's OpenAI-compatible endpoint reports thinking only inside total_tokens.
    Figures from a real call (2026-10-01): 720 in, 110 visible out, 1480 total."""
    from types import SimpleNamespace as NS

    resp = NS(usage=NS(prompt_tokens=720, completion_tokens=110, total_tokens=1480, completion_tokens_details=None, prompt_tokens_details=None))
    usage = GeminiTextProvider._extract_usage(resp)
    assert usage["reasoning_tokens"] == 650 and usage["completion_tokens"] == 760 and usage["total_tokens"] == 1480
    assert cat.estimate_text_cost("gemini-3.8-flash", usage) == pytest.approx((720 * 0.75 + 760 * 3.75) / 1e6)
    # nothing hidden → nothing changes; and the OpenAI dialect never invents a gap
    plain = NS(usage=NS(prompt_tokens=10, completion_tokens=5, total_tokens=15, completion_tokens_details=None, prompt_tokens_details=None))
    assert GeminiTextProvider._extract_usage(plain) == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert OpenAITextProvider._extract_usage(resp)["completion_tokens"] == 110


def test_gemini_cli_stats_count_thinking_and_the_cache_discount(cat):
    """`gemini -o stream-json` stats from a real run: input includes the cached part,
    thinking is the unlisted remainder of total."""
    from omniapi_mcp.capabilities.text import fold_unreported_output

    stats = {"total_tokens": 30894, "input_tokens": 30302, "output_tokens": 356, "cached": 8060}
    norm = fold_unreported_output({"prompt_tokens": stats["input_tokens"], "completion_tokens": stats["output_tokens"],
                                   "total_tokens": stats["total_tokens"], "cached_tokens": stats["cached"]})
    assert norm["completion_tokens"] == 356 + 236 and norm["reasoning_tokens"] == 236
    expected = ((30302 - 8060) * 0.75 + 8060 * 0.075 + 592 * 3.75) / 1e6
    assert cat.estimate_text_cost("gemini-3.8-flash", norm) == pytest.approx(expected)


def test_openrouter_accepts_unknown_ids_and_requests_cost():
    p = OpenRouterTextProvider(CFG)
    assert p.supports_model("some-vendor/brand-new-model")
    req = p._build_request("moonshotai/kimi-k3", [{"role": "user", "content": "hi"}], {})
    assert req["extra_body"] == {"usage": {"include": True}}


def test_anthropic_message_conversion_roundtrip():
    system, msgs = AnthropicTextProvider._convert_messages(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Taipei"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            {"role": "user", "content": "thanks"},
        ]
    )
    assert system == "You are terse."
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {"city": "Taipei"},
    }
    # tool_result and the following user text merge into one user turn
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][1] == {"type": "text", "text": "thanks"}


def test_anthropic_tools_and_thinking_request():
    p = AnthropicTextProvider(CFG)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "f",
                "description": "d",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        }
    ]
    req = p._build_request(
        "claude-sonnet-5",
        [{"role": "user", "content": "go"}],
        {
            "tools": tools,
            "tool_choice": "required",
            "reasoning_effort": "high",
            "temperature": 0.9,
            "max_completion_tokens": 100,
        },
    )
    assert req["tools"][0]["input_schema"]["properties"]["x"]["type"] == "string"
    assert req["tool_choice"] == {"type": "any"}
    assert req["thinking"] == {"type": "enabled", "budget_tokens": 16000}
    assert req["max_tokens"] > 16000  # headroom above the thinking budget
    assert "temperature" not in req  # forbidden with extended thinking


def test_anthropic_json_schema_goes_to_output_config():
    p = AnthropicTextProvider(CFG)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    req = p._build_request(
        "claude-sonnet-5",
        [{"role": "user", "content": "go"}],
        {"response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": schema}}},
    )
    assert req["extra_body"]["output_config"]["format"]["schema"] == schema


def test_anthropic_response_parsing():
    class B:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    resp = B(
        content=[
            B(type="thinking", thinking="hmm"),
            B(type="text", text="Hello"),
            B(type="tool_use", id="toolu_1", name="f", input={"x": "1"}),
        ],
        usage=B(input_tokens=10, output_tokens=5, cache_read_input_tokens=2),
        stop_reason="tool_use",
        model="claude-sonnet-5",
    )
    r = AnthropicTextProvider._parse_response(resp, "claude-sonnet-5", "anthropic")
    assert r.text == "Hello" and r.reasoning == "hmm"
    assert r.finish_reason == "tool_calls"
    assert json.loads(r.tool_calls[0]["function"]["arguments"]) == {"x": "1"}
    assert r.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cached_tokens": 2,
        "total_tokens": 15,
    }
