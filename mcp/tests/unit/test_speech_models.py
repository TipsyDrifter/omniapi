"""Roster / lifecycle / request-assembly tests for TTS and STT providers.

Covers the 2026-09 model refresh:
  * OpenAI STT gained `gpt-transcribe` (now the default) and the four legacy
    models are deprecated-but-callable (shutdown 2027-02-26).
  * ElevenLabs gained `eleven_v3_conversational`; `eleven_turbo_v2_5` is
    deprecated-but-callable.
  * Gemini TTS is new.

Everything here is offline: the HTTP/SDK layers are mocked, but the Gemini
request is assembled with the *real* google-genai types so the field names are
checked against the installed SDK rather than against memory.
"""

import io
import logging
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omniapi_mcp.capabilities.speech import (
    ElevenLabsProvider,
    GeminiSpeechProvider,
    OpenAITTSProvider,
)
from omniapi_mcp.capabilities.transcription import OpenAIWhisperProvider
from omniapi_mcp.providers.base import ProviderConfig, ProviderError


def _cfg(**kwargs) -> ProviderConfig:
    return ProviderConfig(api_key=kwargs.pop("api_key", "test-key"), **kwargs)


# --------------------------------------------------------------------------
# Rosters
# --------------------------------------------------------------------------


class TestTranscriptionRoster:
    def test_gpt_transcribe_is_present_and_default(self):
        assert "gpt-transcribe" in OpenAIWhisperProvider.SUPPORTED_MODELS
        assert OpenAIWhisperProvider.DEFAULT_MODEL == "gpt-transcribe"

    def test_legacy_models_still_selectable(self):
        legacy = {
            "whisper-1",
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe-diarize",
        }
        assert legacy <= OpenAIWhisperProvider.SUPPORTED_MODELS

    def test_legacy_models_marked_deprecated_with_shutdown_date(self):
        provider = OpenAIWhisperProvider(_cfg())
        assert provider.model_status("gpt-transcribe") == "current"
        for model in (
            "whisper-1",
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe-diarize",
        ):
            assert provider.model_status(model) == "deprecated"
            assert provider.MODEL_SHUTDOWN[model] == "2027-02-26"
            assert provider.MODEL_REPLACEMENT[model] == "gpt-transcribe"

    def test_realtime_only_models_are_not_listed(self):
        # These live on the Realtime API, not /v1/audio/transcriptions.
        for model in (
            "gpt-live-transcribe",
            "gpt-realtime-whisper",
            "gpt-realtime-translate",
        ):
            assert model not in OpenAIWhisperProvider.SUPPORTED_MODELS


class TestElevenLabsRoster:
    def test_expected_models_present(self):
        expected = {
            "eleven_v3",
            "eleven_v3_conversational",
            "eleven_flash_v2_5",
            "eleven_flash_v2",
            "eleven_multilingual_v2",
            "eleven_turbo_v2_5",
        }
        assert expected == ElevenLabsProvider.SUPPORTED_MODELS

    def test_turbo_deprecated_with_flash_replacement(self):
        provider = ElevenLabsProvider(_cfg())
        assert provider.model_status("eleven_turbo_v2_5") == "deprecated"
        assert provider.MODEL_REPLACEMENT["eleven_turbo_v2_5"] == "eleven_flash_v2_5"
        # No shutdown date has been announced by ElevenLabs.
        assert "eleven_turbo_v2_5" not in provider.MODEL_SHUTDOWN
        assert provider.deprecated_models() == {"eleven_turbo_v2_5"}

    def test_default_model_is_current(self):
        provider = ElevenLabsProvider(_cfg())
        assert provider.DEFAULT_MODEL == "eleven_flash_v2_5"
        assert provider.model_status(provider.DEFAULT_MODEL) == "current"


class TestOpenAITTSRoster:
    def test_roster_unchanged_and_all_current(self):
        provider = OpenAITTSProvider(_cfg())
        assert provider.SUPPORTED_MODELS == {"gpt-4o-mini-tts", "tts-1", "tts-1-hd"}
        assert provider.deprecated_models() == set()


# --------------------------------------------------------------------------
# Deprecated models stay callable, but warn
# --------------------------------------------------------------------------


class TestDeprecationWarnings:
    @pytest.mark.asyncio
    async def test_elevenlabs_turbo_still_synthesizes_and_warns(self, caplog):
        provider = ElevenLabsProvider(_cfg())
        http = MagicMock()
        http.post = AsyncMock(
            return_value=MagicMock(status_code=200, content=b"fake-mp3")
        )
        provider._http = lambda: http  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            result = await provider.synthesize("hi", model="eleven_turbo_v2_5")

        # Still callable — no exception, real audio bytes returned.
        assert result.audio_data == b"fake-mp3"
        assert http.post.call_args.kwargs["json"]["model_id"] == "eleven_turbo_v2_5"
        assert any(
            "eleven_turbo_v2_5" in r.getMessage() and "deprecated" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_elevenlabs_current_model_does_not_warn(self, caplog):
        provider = ElevenLabsProvider(_cfg())
        http = MagicMock()
        http.post = AsyncMock(
            return_value=MagicMock(status_code=200, content=b"fake-mp3")
        )
        provider._http = lambda: http  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            await provider.synthesize("hi", model="eleven_v3_conversational")

        assert not [r for r in caplog.records if "deprecated" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_whisper1_still_transcribes_and_warns(self, caplog):
        provider = OpenAIWhisperProvider(_cfg())
        provider.client.audio.transcriptions.create = AsyncMock(return_value="hello")

        with caplog.at_level(logging.WARNING):
            result = await provider.transcribe("whisper-1", b"bytes", "a.mp3")

        assert result.text == "hello"
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "whisper-1" in m and "deprecated" in m and "2027-02-26" in m
            for m in messages
        )

    @pytest.mark.asyncio
    async def test_gpt_transcribe_does_not_warn(self, caplog):
        provider = OpenAIWhisperProvider(_cfg())
        provider.client.audio.transcriptions.create = AsyncMock(return_value="hello")

        with caplog.at_level(logging.WARNING):
            await provider.transcribe("gpt-transcribe", b"bytes", "a.mp3")

        assert not [r for r in caplog.records if "deprecated" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_gpt_transcribe_warns_on_unverified_format_but_passes_through(
        self, caplog
    ):
        """gpt-transcribe is conservatively assumed json/text-only (UNVERIFIED),
        so verbose_json warns — but is still forwarded to the API."""
        provider = OpenAIWhisperProvider(_cfg())
        create = AsyncMock(return_value=SimpleNamespace(text="hi"))
        provider.client.audio.transcriptions.create = create

        with caplog.at_level(logging.WARNING):
            await provider.transcribe(
                "gpt-transcribe", b"bytes", "a.mp3", response_format="verbose_json"
            )

        assert create.call_args.kwargs["response_format"] == "verbose_json"
        assert any(
            "verbose_json" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unknown_model_still_rejected(self):
        provider = OpenAIWhisperProvider(_cfg())
        with pytest.raises(ProviderError):
            await provider.transcribe("gpt-9-transcribe", b"bytes", "a.mp3")


# --------------------------------------------------------------------------
# Gemini TTS
# --------------------------------------------------------------------------


PCM = b"\x00\x01" * 100


def _gemini_response(mime_type="audio/L16;codec=pcm;rate=24000", data=PCM):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(text="ignored", inline_data=None),
                        SimpleNamespace(
                            inline_data=SimpleNamespace(
                                data=data, mime_type=mime_type
                            )
                        ),
                    ]
                )
            )
        ]
    )


def _make_gemini_provider(response=None, timeout=30.0):
    """Build a GeminiSpeechProvider with only `genai.Client` mocked, so the
    request is assembled with the real SDK types."""
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(
        return_value=response if response is not None else _gemini_response()
    )
    with patch("google.genai.Client", return_value=client) as ctor:
        provider = GeminiSpeechProvider(_cfg(timeout=timeout))
    return provider, client, ctor


class TestGeminiRoster:
    def test_models_and_default(self):
        assert GeminiSpeechProvider.SUPPORTED_MODELS == {
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
            "gemini-3.1-flash-tts-preview",
        }
        assert GeminiSpeechProvider.DEFAULT_MODEL == "gemini-3.8-flash-tts"

    def test_legacy_preview_flagged(self):
        provider, _, _ = _make_gemini_provider()
        assert provider.model_status("gemini-3.8-flash-tts") == "current"
        assert provider.model_status("gemini-3.1-flash-tts-preview") == "deprecated"


class TestGeminiClientConstruction:
    def test_client_built_with_api_key_and_ms_timeout(self):
        _, _, ctor = _make_gemini_provider(timeout=30.0)
        kwargs = ctor.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        # HttpOptions.timeout is milliseconds in google-genai.
        assert kwargs["http_options"].timeout == 30000
        # API-key auth, never the Vertex path.
        assert "vertexai" not in kwargs


class TestGeminiRequestAssembly:
    @pytest.mark.asyncio
    async def test_audio_modality_and_voice_config(self):
        provider, client, _ = _make_gemini_provider()

        await provider.synthesize("Hello there", voice="Puck")

        kwargs = client.aio.models.generate_content.call_args.kwargs
        assert kwargs["model"] == "gemini-3.8-flash-tts"
        assert kwargs["contents"] == "Hello there"
        config = kwargs["config"]
        assert config.response_modalities == ["AUDIO"]
        voice_cfg = config.speech_config.voice_config
        assert voice_cfg.prebuilt_voice_config.voice_name == "Puck"
        assert config.speech_config.language_code is None

    @pytest.mark.asyncio
    async def test_default_voice_and_language_code(self):
        provider, client, _ = _make_gemini_provider()

        await provider.synthesize(
            "Hello", model="gemini-3.8-flash-lite-tts", language_code="ja"
        )

        config = client.aio.models.generate_content.call_args.kwargs["config"]
        assert (
            config.speech_config.voice_config.prebuilt_voice_config.voice_name
            == GeminiSpeechProvider.DEFAULT_VOICE
        )
        assert config.speech_config.language_code == "ja"

    @pytest.mark.asyncio
    async def test_unsupported_model_rejected(self):
        provider, _, _ = _make_gemini_provider()
        with pytest.raises(ProviderError) as exc:
            await provider.synthesize("hi", model="gemini-3.8-pro-tts")
        assert exc.value.error_code == "UNSUPPORTED_MODEL"

    @pytest.mark.asyncio
    async def test_legacy_preview_warns_but_works(self, caplog):
        provider, _, _ = _make_gemini_provider()
        with caplog.at_level(logging.WARNING):
            result = await provider.synthesize(
                "hi", model="gemini-3.1-flash-tts-preview"
            )
        assert result.audio_data
        assert any("deprecated" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_empty_response_raises(self):
        provider, _, _ = _make_gemini_provider(
            response=SimpleNamespace(candidates=[])
        )
        with pytest.raises(ProviderError) as exc:
            await provider.synthesize("hi")
        assert exc.value.error_code == "TTS_FAILED"


class TestGeminiAudioFraming:
    def test_pcm_to_wav_defaults(self):
        wav_bytes = GeminiSpeechProvider._pcm_to_wav(PCM)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 24000
            assert wav.readframes(wav.getnframes()) == PCM

    def test_sample_rate_parsed_from_mime(self):
        parse = GeminiSpeechProvider._sample_rate_from_mime
        assert parse("audio/L16;codec=pcm;rate=16000") == 16000
        assert parse("audio/L16") is None
        assert parse(None) is None

    @pytest.mark.asyncio
    async def test_reported_rate_beats_the_constant(self):
        provider, _, _ = _make_gemini_provider(
            response=_gemini_response(mime_type="audio/L16;codec=pcm;rate=16000")
        )
        result = await provider.synthesize("hi")
        assert result.output_format == "wav"
        assert result.metadata["sample_rate"] == 16000
        with wave.open(io.BytesIO(result.audio_data), "rb") as wav:
            assert wav.getframerate() == 16000

    @pytest.mark.asyncio
    async def test_mp3_request_falls_back_to_wav_with_warning(self, caplog, monkeypatch):
        # without a system ffmpeg there is nothing to encode MP3 with (with one: test_lyria_and_mp3.py)
        monkeypatch.setattr("omniapi_mcp.capabilities.speech.ffmpeg_path", lambda: None)
        provider, _, _ = _make_gemini_provider()
        with caplog.at_level(logging.WARNING):
            result = await provider.synthesize("hi", output_format="mp3_44100_128")
        assert result.output_format == "wav"
        assert result.audio_data.startswith(b"RIFF")
        assert any("PCM" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_pcm_request_returns_raw_frames(self):
        provider, _, _ = _make_gemini_provider()
        result = await provider.synthesize("hi", output_format="pcm_24000")
        assert result.output_format == "pcm"
        assert result.audio_data == PCM


# --------------------------------------------------------------------------
# SpeechTool wiring
# --------------------------------------------------------------------------


def _tool_settings(gemini_key, *, elevenlabs_key=None):
    providers = SimpleNamespace(
        elevenlabs=(
            SimpleNamespace(
                enabled=True,
                api_key=elevenlabs_key,
                base_url="https://api.elevenlabs.io",
                timeout=120.0,
                max_retries=3,
            )
            if elevenlabs_key
            else None
        ),
        openai=None,
        gemini=SimpleNamespace(
            enabled=True, api_key=gemini_key, timeout=45.0, max_retries=2
        ),
    )
    return SimpleNamespace(
        providers=providers, storage=SimpleNamespace(base_path="/tmp/omniapi-test")
    )


class TestSpeechToolRegistration:
    def test_gemini_models_registered_with_api_key(self):
        from omniapi_mcp.tools.speech import SpeechTool

        with patch("google.genai.Client", return_value=MagicMock()):
            tool = SpeechTool(_tool_settings("AIza-fake-developer-key"))

        assert set(tool.available_models()) == GeminiSpeechProvider.SUPPORTED_MODELS
        assert tool._default_model() == "gemini-3.8-flash-tts"

    def test_service_account_path_is_not_treated_as_an_api_key(self):
        """The Gemini settings slot doubles as a Vertex service-account path
        for the image provider; a path-shaped value must not reach TTS."""
        from omniapi_mcp.tools.speech import SpeechTool

        for path in (
            r"C:\creds\service-account.json",
            "/etc/secrets/sa.json",
            "creds/key.json",
        ):
            with patch("google.genai.Client", return_value=MagicMock()) as ctor:
                tool = SpeechTool(_tool_settings(path))
            assert tool.available_models() == []
            assert not ctor.called

    def test_elevenlabs_still_wins_the_default_slot(self):
        from omniapi_mcp.tools.speech import SpeechTool

        with patch("google.genai.Client", return_value=MagicMock()):
            tool = SpeechTool(
                _tool_settings("AIza-fake", elevenlabs_key="el-fake-key")
            )

        assert tool._default_model() == "eleven_flash_v2_5"
        assert "gemini-3.8-flash-tts" in tool.available_models()
        assert "eleven_v3_conversational" in tool.available_models()
