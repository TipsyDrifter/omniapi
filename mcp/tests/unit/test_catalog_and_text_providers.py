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


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "f",
            "description": "d",
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
    }
]


def _anthropic_on_fake_wire():
    """Anthropic provider whose client records the request body instead of calling out."""
    from types import SimpleNamespace as NS

    p = AnthropicTextProvider(CFG)
    sent: dict = {}

    async def create(**req):
        sent.update(req)
        return NS(content=[NS(type="text", text="ok")], usage=None, stop_reason="end_turn", model=req["model"])

    p.client = NS(messages=NS(create=create))
    return p, sent


async def test_anthropic_new_model_gets_adaptive_thinking_and_effort_on_the_wire():
    """4.7+ (here Opus 5.5): budget_tokens and temperature are HTTP 400 there."""
    p, sent = _anthropic_on_fake_wire()
    await p.complete(
        "claude-opus-5-5",
        [{"role": "user", "content": "go"}],
        tools=_TOOLS,
        tool_choice="auto",
        reasoning_effort="xhigh",
        temperature=0.9,
        top_p=0.5,
    )
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["extra_body"] == {"output_config": {"effort": "xhigh"}}
    assert "budget_tokens" not in json.dumps(sent, default=str)
    assert "temperature" not in sent and "top_p" not in sent
    assert sent["max_tokens"] == AnthropicTextProvider.ADAPTIVE_DEFAULT_MAX_TOKENS
    assert sent["tools"][0]["input_schema"]["properties"]["x"]["type"] == "string"


async def test_anthropic_new_model_without_effort_sends_no_thinking_and_still_drops_sampling():
    p, sent = _anthropic_on_fake_wire()
    await p.complete("claude-opus-4-7", [{"role": "user", "content": "go"}], temperature=0.3, max_completion_tokens=50)
    assert "thinking" not in sent and "extra_body" not in sent
    assert "temperature" not in sent and sent["max_tokens"] == 50


def test_anthropic_new_model_keeps_callers_max_tokens_and_merges_effort_with_json_schema():
    p = AnthropicTextProvider(CFG)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    req = p._build_request(
        "claude-sonnet-5",
        [{"role": "user", "content": "go"}],
        {
            "reasoning_effort": "low",
            "max_completion_tokens": 100,
            "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": schema}},
        },
    )
    assert req["thinking"] == {"type": "adaptive"}
    assert req["max_tokens"] == 100
    assert req["extra_body"]["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": schema},
    }


async def test_anthropic_old_model_keeps_budget_tokens_on_the_wire():
    """Pre-4.6 (here Haiku 4.5) only knows enabled + budget_tokens; effort would error."""
    p, sent = _anthropic_on_fake_wire()
    await p.complete(
        "claude-haiku-4-5-20251001",
        [{"role": "user", "content": "go"}],
        tools=_TOOLS,
        tool_choice="required",
        reasoning_effort="high",
        temperature=0.9,
        max_completion_tokens=100,
    )
    assert sent["tool_choice"] == {"type": "any"}
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 16000}
    assert sent["max_tokens"] > 16000  # headroom above the thinking budget
    assert "temperature" not in sent  # forbidden with extended thinking
    assert "extra_body" not in sent  # no output_config.effort on old models


def test_anthropic_4_6_gets_adaptive_with_xhigh_lowered_to_high_and_keeps_temperature():
    p = AnthropicTextProvider(CFG)
    req = p._build_request(
        "claude-opus-4-6",
        [{"role": "user", "content": "go"}],
        {"reasoning_effort": "xhigh", "temperature": 0.4},
    )
    assert req["thinking"] == {"type": "adaptive"}
    assert req["extra_body"] == {"output_config": {"effort": "high"}}
    assert req["temperature"] == 0.4  # sampling is still allowed on 4.6


async def test_anthropic_callers_own_thinking_dict_is_sent_as_given():
    p, sent = _anthropic_on_fake_wire()
    own = {"type": "adaptive", "display": "summarized"}
    await p.complete("claude-opus-5-5", [{"role": "user", "content": "go"}], thinking=own, reasoning_effort="low")
    assert sent["thinking"] == own
    assert "extra_body" not in sent


@pytest.mark.parametrize(
    "model, shape",
    [
        ("claude-fable-5-1", "adaptive"),
        ("claude-fable-5", "adaptive"),
        ("claude-mythos-5-1", "adaptive"),
        ("claude-opus-5-5", "adaptive"),
        ("claude-opus-5", "adaptive"),
        ("claude-opus-4-8", "adaptive"),
        ("claude-opus-4-7", "adaptive"),
        ("claude-sonnet-5-5", "adaptive"),
        ("claude-sonnet-5", "adaptive"),
        ("claude-opus-4-6", "adaptive-4.6"),
        ("claude-sonnet-4-6", "adaptive-4.6"),
        ("claude-sonnet-4-5-20250929", "budget"),
        ("claude-opus-4-5-20251101", "budget"),
        ("claude-haiku-4-5-20251001", "budget"),
        ("claude-sonnet-4-20250514", "budget"),
        ("claude-3-7-sonnet-20250219", "budget"),
        ("anthropic/claude-opus-4.7", "adaptive"),
        ("anthropic/claude-sonnet-4.5", "budget"),
        ("anthropic/claude-sonnet-4.6", "adaptive-4.6"),
        ("claude-something-new", "adaptive"),  # unreadable id -> newest rules
    ],
)
def test_claude_thinking_shape_rule(model, shape):
    from omniapi_mcp.capabilities.text import claude_thinking_shape

    assert claude_thinking_shape(model) == shape


def test_every_catalog_claude_id_has_a_readable_version():
    """A catalog id we cannot parse would silently get the 'new model' shape."""
    from pathlib import Path

    from omniapi_mcp.capabilities.text import claude_version

    path = Path(__file__).resolve().parents[2] / "omniapi_mcp" / "catalog" / "catalog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = [m["id"] for m in data["models"] if str(m.get("id", "")).startswith("claude-")]
    assert ids
    assert [i for i in ids if claude_version(i) is None] == []


def test_openrouter_claude_uses_unified_reasoning_and_drops_sampling_on_4_7_plus():
    p = OpenRouterTextProvider(CFG)
    req = p._build_request(
        "anthropic/claude-opus-4.7",
        [{"role": "user", "content": "go"}],
        {"reasoning_effort": "high", "temperature": 0.5},
    )
    assert "reasoning_effort" not in req
    assert req["extra_body"]["reasoning"] == {"effort": "high"}
    assert req["extra_body"]["usage"] == {"include": True}  # the provider's own extras survive
    assert "temperature" not in req
    assert "budget_tokens" not in json.dumps(req, default=str)


def test_openrouter_older_claude_keeps_temperature_and_non_claude_is_untouched():
    p = OpenRouterTextProvider(CFG)
    old = p._build_request(
        "anthropic/claude-sonnet-4.5", [{"role": "user", "content": "go"}], {"reasoning_effort": "none", "temperature": 0.5}
    )
    assert old["temperature"] == 0.5 and "reasoning" not in old["extra_body"] and "reasoning_effort" not in old
    other = p._build_request("moonshotai/kimi-k2", [{"role": "user", "content": "go"}], {"reasoning_effort": "low", "temperature": 0.5})
    assert other["reasoning_effort"] == "low" and other["temperature"] == 0.5


async def test_anthropic_new_model_without_effort_or_max_tokens_gets_room_to_think():
    """Fable / Opus 5.5 think even without reasoning_effort; thinking counts against max_tokens."""
    p, sent = _anthropic_on_fake_wire()
    await p.complete("claude-opus-5-5", [{"role": "user", "content": "go"}])
    assert "thinking" not in sent
    assert sent["max_tokens"] == AnthropicTextProvider.ADAPTIVE_DEFAULT_MAX_TOKENS


async def test_anthropic_old_model_without_max_tokens_keeps_the_old_default():
    p, sent = _anthropic_on_fake_wire()
    await p.complete("claude-haiku-4-5-20251001", [{"role": "user", "content": "go"}])
    assert sent["max_tokens"] == AnthropicTextProvider.DEFAULT_MAX_TOKENS


async def test_anthropic_forced_tool_choice_becomes_auto_with_a_warning_on_models_that_reject_it():
    p, sent = _anthropic_on_fake_wire()
    result = await p.complete(
        "claude-opus-5-5", [{"role": "user", "content": "go"}], tools=_TOOLS, tool_choice="required"
    )
    assert sent["tool_choice"] == {"type": "auto"}
    assert not any(k.startswith("_") for k in sent)  # the private warnings key never goes out
    assert len(result.metadata["warnings"]) == 1 and "tool_choice" in result.metadata["warnings"][0]


def test_anthropic_named_tool_choice_becomes_auto_on_sonnet_5_5():
    p = AnthropicTextProvider(CFG)
    req = p._build_request(
        "claude-sonnet-5-5",
        [{"role": "user", "content": "go"}],
        {"tools": _TOOLS, "tool_choice": {"type": "function", "function": {"name": "f"}}},
    )
    assert req["tool_choice"] == {"type": "auto"}
    assert "'f'" in req[AnthropicTextProvider._REQUEST_WARNINGS][0]


def test_anthropic_forced_tool_choice_kept_where_it_is_accepted():
    p = AnthropicTextProvider(CFG)
    named = {"type": "function", "function": {"name": "f"}}
    for model in ("claude-opus-5", "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8"):
        req = p._build_request(model, [{"role": "user", "content": "go"}], {"tools": _TOOLS, "tool_choice": named})
        assert req["tool_choice"] == {"type": "tool", "name": "f"}, model
        assert AnthropicTextProvider._REQUEST_WARNINGS not in req
    req = p._build_request("claude-opus-5-5", [{"role": "user", "content": "go"}], {"tools": _TOOLS, "tool_choice": "none"})
    assert req["tool_choice"] == {"type": "none"} and AnthropicTextProvider._REQUEST_WARNINGS not in req


async def test_anthropic_stream_carries_the_tool_choice_warning_on_done():
    from types import SimpleNamespace as NS

    p = AnthropicTextProvider(CFG)
    sent: dict = {}

    class _Events:
        def __init__(self, events):
            self._it = iter(events)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    async def create(**req):
        sent.update(req)
        return _Events([
            NS(type="content_block_start", index=0, content_block=NS(type="text", text="")),
            NS(type="content_block_delta", index=0, delta=NS(type="text_delta", text="hi")),
            NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=1)),
        ])

    p.client = NS(messages=NS(create=create))
    pieces = [x async for x in p.stream("claude-fable-5-1", [{"role": "user", "content": "x"}], tools=_TOOLS, tool_choice="required")]
    assert sent["tool_choice"] == {"type": "auto"} and not any(k.startswith("_") for k in sent)
    done = pieces[-1]["result"]
    assert done.metadata["warnings"] and "tool_choice" in done.metadata["warnings"][0]


@pytest.mark.parametrize(
    "model, rejects",
    [
        ("claude-fable-5-1", True),
        ("claude-mythos-5-1", True),
        ("claude-opus-5-5", True),
        ("claude-sonnet-5-5", True),
        ("anthropic/claude-sonnet-5.5", True),
        ("claude-something-new", True),  # unreadable id -> newest rules
        ("claude-fable-5", False),
        ("claude-opus-5", False),
        ("claude-sonnet-5", False),
        ("claude-opus-4-8", False),
        ("claude-opus-4-6", False),
        ("claude-haiku-4-5-20251001", False),
        ("claude-3-7-sonnet-20250219", False),
    ],
)
def test_claude_forced_tool_choice_rule(model, rejects):
    from omniapi_mcp.capabilities.text import claude_rejects_forced_tool_choice

    assert claude_rejects_forced_tool_choice(model) is rejects


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
