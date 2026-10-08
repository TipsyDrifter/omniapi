"""Unit tests for the consolidated music tools and the 2026-09 model lists.

Two things are pinned here:

1. **Action routing** — the five MCP music tools take an ``action`` plus the
   union of every action's parameters, so the only thing standing between a
   client and a wrong upstream call is the router in ``MusicGenerationTool``.
   These tests check that each action reaches the right operation with the
   right arguments, and that a missing parameter fails with a message naming
   the tool, the action and the parameter.
2. **Model lists** — kie.ai discontinued the V4/V5 Suno families in favour of
   the V6 family; discontinued ids must stay callable but warn.

No network: providers are never constructed (settings declare none) and the
underlying operations are replaced with AsyncMocks.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omniapi_mcp.capabilities.music import (
    ElevenLabsMusicProvider,
    SunoProvider,
)
from omniapi_mcp.providers.base import ProviderConfig, ProviderError
from omniapi_mcp.tools.music_generation import MusicGenerationTool


@pytest.fixture
def tool(tmp_path):
    """A MusicGenerationTool with no providers configured (routing only)."""
    settings = SimpleNamespace(
        providers=SimpleNamespace(elevenlabs=None, kie=None),
        storage=SimpleNamespace(base_path=str(tmp_path)),
    )
    return MusicGenerationTool(settings=settings)


def _stub(tool, *names):
    """Replace the named operations with AsyncMocks and return them."""
    mocks = {}
    for name in names:
        mock = AsyncMock(return_value={"ok": name})
        setattr(tool, name, mock)
        mocks[name] = mock
    return mocks


class TestEditMusicRouting:
    """edit_music(action=...) reaches the right Suno operation."""

    async def test_extend_inherits_source_settings(self, tool):
        mocks = _stub(tool, "extend")
        assert await tool.edit(action="extend", audio_id="aud-1") == {"ok": "extend"}
        kwargs = mocks["extend"].call_args.kwargs
        assert kwargs["audio_id"] == "aud-1"
        # custom_mode maps onto Suno's defaultParamFlag.
        assert kwargs["default_param_flag"] is False

    async def test_extend_custom_mode_maps_to_default_param_flag(self, tool):
        mocks = _stub(tool, "extend")
        await tool.edit(
            action="extend",
            audio_id="aud-1",
            custom_mode=True,
            prompt="second verse",
            style="dream pop",
            title="Nightdrive",
            continue_at=42.0,
        )
        kwargs = mocks["extend"].call_args.kwargs
        assert kwargs["default_param_flag"] is True
        assert kwargs["continue_at"] == 42.0

    async def test_cover_routes_with_upload_url(self, tool):
        mocks = _stub(tool, "cover")
        await tool.edit(
            action="cover", upload_url="https://x/a.mp3", prompt="make it jazz"
        )
        kwargs = mocks["cover"].call_args.kwargs
        assert kwargs["upload_url"] == "https://x/a.mp3"
        assert kwargs["custom_mode"] is False

    async def test_upload_extend_routes(self, tool):
        mocks = _stub(tool, "upload_extend")
        await tool.edit(action="upload_extend", upload_url="https://x/a.mp3")
        assert mocks["upload_extend"].call_args.kwargs["upload_url"] == (
            "https://x/a.mp3"
        )

    async def test_add_instrumental_routes(self, tool):
        mocks = _stub(tool, "add_instrumental")
        await tool.edit(
            action="add_instrumental",
            upload_url="https://x/a.mp3",
            title="Backing",
            tags="lo-fi, jazzy",
            negative_tags="metal",
        )
        kwargs = mocks["add_instrumental"].call_args.kwargs
        assert kwargs["tags"] == "lo-fi, jazzy"
        assert kwargs["negative_tags"] == "metal"

    async def test_add_vocals_routes(self, tool):
        mocks = _stub(tool, "add_vocals")
        await tool.edit(
            action="add_vocals",
            upload_url="https://x/a.mp3",
            prompt="sing about rain",
            title="Rain",
            style="soulful pop",
            negative_tags="screamo",
        )
        assert mocks["add_vocals"].call_args.kwargs["style"] == "soulful pop"

    async def test_separate_vocals_routes(self, tool):
        mocks = _stub(tool, "separate_vocals")
        await tool.edit(
            action="separate_vocals",
            task_id="task-1",
            audio_id="aud-1",
            separation_type="split_stem",
        )
        kwargs = mocks["separate_vocals"].call_args.kwargs
        assert kwargs["task_id"] == "task-1"
        assert kwargs["separation_type"] == "split_stem"

    async def test_each_action_reaches_only_its_own_operation(self, tool):
        """No action leaks into a sibling operation."""
        ops = (
            "extend",
            "cover",
            "upload_extend",
            "add_instrumental",
            "add_vocals",
            "separate_vocals",
        )
        calls = {
            "extend": {"audio_id": "a"},
            "cover": {"upload_url": "u", "prompt": "p"},
            "upload_extend": {"upload_url": "u"},
            "add_instrumental": {
                "upload_url": "u",
                "title": "t",
                "tags": "g",
                "negative_tags": "n",
            },
            "add_vocals": {
                "upload_url": "u",
                "prompt": "p",
                "title": "t",
                "style": "s",
                "negative_tags": "n",
            },
            "separate_vocals": {"task_id": "t", "audio_id": "a"},
        }
        for action, params in calls.items():
            mocks = _stub(tool, *ops)
            await tool.edit(action=action, **params)
            called = [name for name, m in mocks.items() if m.called]
            assert called == [action], f"{action} routed to {called}"


class TestEditMusicValidation:
    """A malformed edit_music call fails synchronously with a usable message."""

    def test_unknown_action_lists_valid_actions(self, tool):
        with pytest.raises(ValueError) as exc:
            tool.edit(action="remix")
        msg = str(exc.value)
        assert "edit_music" in msg
        assert "remix" in msg
        assert "separate_vocals" in msg

    @pytest.mark.parametrize(
        "action,params,missing",
        [
            ("extend", {}, ["audio_id"]),
            ("cover", {"upload_url": "u"}, ["prompt"]),
            ("cover", {}, ["upload_url", "prompt"]),
            ("upload_extend", {}, ["upload_url"]),
            (
                "add_instrumental",
                {"upload_url": "u"},
                ["title", "tags", "negative_tags"],
            ),
            (
                "add_vocals",
                {"upload_url": "u", "prompt": "p"},
                ["title", "style", "negative_tags"],
            ),
            ("separate_vocals", {"audio_id": "a"}, ["task_id"]),
        ],
    )
    def test_missing_required_params_are_named(self, tool, action, params, missing):
        with pytest.raises(ValueError) as exc:
            tool.edit(action=action, **params)
        msg = str(exc.value)
        assert f"action={action!r}" in msg
        for name in missing:
            assert name in msg

    def test_blank_string_counts_as_missing(self, tool):
        with pytest.raises(ValueError, match="audio_id"):
            tool.edit(action="extend", audio_id="   ")

    def test_custom_mode_extras_are_required(self, tool):
        with pytest.raises(ValueError) as exc:
            tool.edit(action="extend", audio_id="a", custom_mode=True)
        msg = str(exc.value)
        assert "continue_at" in msg
        assert "custom_mode" in msg

    def test_bad_separation_type_is_rejected(self, tool):
        with pytest.raises(ValueError, match="separation_type"):
            tool.edit(
                action="separate_vocals",
                task_id="t",
                audio_id="a",
                separation_type="karaoke",
            )


class TestLyricsAndUtilityRouting:
    async def test_lyrics_generate(self, tool):
        mocks = _stub(tool, "generate_lyrics", "get_timestamped_lyrics")
        await tool.lyrics(action="generate", prompt="rainy tokyo night")
        assert mocks["generate_lyrics"].called
        assert not mocks["get_timestamped_lyrics"].called

    async def test_lyrics_timestamped(self, tool):
        mocks = _stub(tool, "generate_lyrics", "get_timestamped_lyrics")
        await tool.lyrics(action="timestamped", task_id="t", audio_id="a")
        assert mocks["get_timestamped_lyrics"].called
        assert not mocks["generate_lyrics"].called

    def test_lyrics_missing_params(self, tool):
        with pytest.raises(ValueError) as exc:
            tool.lyrics(action="timestamped", task_id="t")
        assert "music_lyrics" in str(exc.value)
        assert "audio_id" in str(exc.value)

    def test_lyrics_unknown_action(self, tool):
        with pytest.raises(ValueError, match="unknown action"):
            tool.lyrics(action="translate")

    async def test_utility_wav(self, tool):
        mocks = _stub(tool, "to_wav", "to_mp4")
        await tool.utility(action="convert_to_wav", task_id="t", audio_id="a")
        assert mocks["to_wav"].called
        assert not mocks["to_mp4"].called

    async def test_utility_music_video_passes_branding(self, tool):
        mocks = _stub(tool, "to_wav", "to_mp4")
        await tool.utility(
            action="create_music_video",
            task_id="t",
            audio_id="a",
            author="nagisa",
            domain_name="example.com",
        )
        kwargs = mocks["to_mp4"].call_args.kwargs
        assert kwargs["author"] == "nagisa"
        assert kwargs["domain_name"] == "example.com"

    def test_utility_missing_params(self, tool):
        with pytest.raises(ValueError) as exc:
            tool.utility(action="convert_to_wav")
        msg = str(exc.value)
        assert "music_utility" in msg
        assert "task_id" in msg and "audio_id" in msg


class TestComposeRouting:
    async def test_create_plan(self, tool):
        mocks = _stub(tool, "create_composition_plan", "compose_detailed")
        await tool.compose(action="create_plan", prompt="a slow waltz")
        assert mocks["create_composition_plan"].called
        assert not mocks["compose_detailed"].called

    async def test_compose_with_prompt(self, tool):
        mocks = _stub(tool, "create_composition_plan", "compose_detailed")
        await tool.compose(action="compose", prompt="a slow waltz")
        assert mocks["compose_detailed"].called

    async def test_compose_with_plan(self, tool):
        mocks = _stub(tool, "create_composition_plan", "compose_detailed")
        await tool.compose(
            action="compose", composition_plan={"sections": []}
        )
        assert mocks["compose_detailed"].call_args.kwargs["composition_plan"] == {
            "sections": []
        }

    def test_create_plan_requires_prompt(self, tool):
        with pytest.raises(ValueError) as exc:
            tool.compose(action="create_plan")
        assert "compose_music" in str(exc.value)
        assert "prompt" in str(exc.value)

    def test_compose_rejects_both_inputs(self, tool):
        with pytest.raises(ValueError, match="exactly one"):
            tool.compose(action="compose", prompt="x", composition_plan={"a": 1})

    def test_compose_rejects_neither_input(self, tool):
        with pytest.raises(ValueError, match="exactly one"):
            tool.compose(action="compose")


class TestModelLists:
    """2026-09 model catalogue: kie.ai V6 family current, V4/V5 discontinued."""

    def _suno(self):
        return SunoProvider(ProviderConfig(api_key="test-key"))

    def test_suno_current_models(self):
        assert SunoProvider.CURRENT_MODELS == {"V6", "V6_MINI", "V6_WILD"}
        assert SunoProvider.DEFAULT_MODEL == "V6"
        assert SunoProvider.DEFAULT_ADD_MODEL == "V6"

    def test_suno_deprecated_models_still_supported(self):
        for old in ("V4", "V4_5", "V4_5PLUS", "V4_5ALL", "V5", "V5_5"):
            assert old in SunoProvider.SUPPORTED_MODELS
            assert SunoProvider.MODEL_STATUS[old] == "deprecated"

    def test_model_status_covers_every_supported_model(self):
        assert set(SunoProvider.MODEL_STATUS) == SunoProvider.SUPPORTED_MODELS
        assert set(ElevenLabsMusicProvider.MODEL_STATUS) == (
            ElevenLabsMusicProvider.SUPPORTED_MODELS
        )

    def test_deprecated_model_is_allowed_but_warns(self, caplog):
        provider = self._suno()
        with caplog.at_level(logging.WARNING):
            assert (
                provider._validate_model(
                    "V5", SunoProvider.SUPPORTED_MODELS, operation="generate"
                )
                == "V5"
            )
        messages = [r.getMessage() for r in caplog.records]
        assert any("discontinued" in m for m in messages), messages
        assert any("V6" in m for m in messages), messages

    def test_current_model_does_not_warn(self, caplog):
        provider = self._suno()
        with caplog.at_level(logging.WARNING):
            provider._validate_model("V6", SunoProvider.SUPPORTED_MODELS)
        assert not caplog.records

    def test_unknown_model_raises(self):
        provider = self._suno()
        with pytest.raises(ProviderError) as exc:
            provider._validate_model("V7", SunoProvider.SUPPORTED_MODELS)
        assert exc.value.error_code == "UNSUPPORTED_MODEL"

    def test_stem_add_endpoints_prefer_v6_family(self):
        assert SunoProvider.STEM_ADD_MODELS == {"V6", "V6_MINI", "V6_WILD"}
        # The previously documented stem-add ids stay callable, but deprecated.
        for old in SunoProvider.STEM_ADD_DEPRECATED:
            assert SunoProvider.MODEL_STATUS[old] == "deprecated"

    def test_elevenlabs_models(self):
        assert ElevenLabsMusicProvider.SUPPORTED_MODELS == {
            "music_v1",
            "music_v2",
            "music_v2_5",
        }
        assert ElevenLabsMusicProvider.DEFAULT_MODEL == "music_v2_5"


class TestMusicToolSurface:
    """The MCP surface exposes five music tools, not thirteen."""

    async def test_five_music_tools_registered(self):
        from omniapi_mcp.server import mcp

        names = {t.name for t in await mcp.list_tools()}
        assert {
            "generate_music",
            "edit_music",
            "music_lyrics",
            "music_utility",
            "compose_music",
        } <= names

    async def test_old_music_tools_are_gone(self):
        from omniapi_mcp.server import mcp

        names = {t.name for t in await mcp.list_tools()}
        retired = {
            "extend_music",
            "cover_music",
            "upload_extend_music",
            "add_instrumental",
            "add_vocals",
            "separate_vocals",
            "generate_lyrics",
            "get_timestamped_lyrics",
            "convert_to_wav",
            "create_music_video",
            "create_composition_plan",
            "compose_music_detailed",
        }
        assert not (retired & names)

    async def test_action_enums_are_advertised(self):
        """MCP clients pick an action from the schema enum, so pin it."""
        from omniapi_mcp.server import mcp

        tools = {t.name: t for t in await mcp.list_tools()}
        expected = {
            "edit_music": [
                "extend",
                "cover",
                "upload_extend",
                "add_instrumental",
                "add_vocals",
                "separate_vocals",
            ],
            "music_lyrics": ["generate", "timestamped"],
            "music_utility": ["convert_to_wav", "create_music_video"],
            "compose_music": ["create_plan", "compose"],
        }
        for name, actions in expected.items():
            schema = tools[name].inputSchema
            assert schema["required"] == ["action"]
            assert schema["properties"]["action"]["enum"] == actions
