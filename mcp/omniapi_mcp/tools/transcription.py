"""Transcription tool — high-level orchestration for audio->text.

Mirrors `ImageGenerationTool`: reads settings, initializes the provider, and
exposes a single high-level `transcribe()` coroutine the MCP tool calls. Only
OpenAI Whisper is wired up today; additional transcription providers (e.g. a
Groq-hosted Whisper) can be added behind the same interface later.
"""

import base64
import logging
from pathlib import Path
from typing import Any, Optional

from ..capabilities.transcription import OpenAIWhisperProvider, TranscriptionProvider
from ..config.settings import Settings
from ..modalities import TRANSCRIBE_INPUT_EXTS
from ..providers.base import ProviderConfig

logger = logging.getLogger(__name__)

# Container formats the OpenAI Audio API accepts (an .mp4 included: its audio
# track is transcribed); used only to warn on a likely-wrong extension, never
# to hard-block (the API is the source of truth).
_KNOWN_AUDIO_EXTS = TRANSCRIBE_INPUT_EXTS


class TranscriptionTool:
    """Transcribe audio to text using a configured transcription provider."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._provider: Optional[TranscriptionProvider] = None
        self._init_provider()

    def _init_provider(self) -> None:
        """Initialize the OpenAI Whisper provider from settings (reuses the
        same OpenAI credentials as image generation)."""
        openai_cfg = getattr(self.settings.providers, "openai", None)
        if openai_cfg and openai_cfg.enabled and openai_cfg.api_key:
            try:
                config = ProviderConfig(
                    api_key=openai_cfg.api_key,
                    organization=openai_cfg.organization,
                    base_url=openai_cfg.base_url,
                    timeout=openai_cfg.timeout,
                    max_retries=openai_cfg.max_retries,
                    enabled=openai_cfg.enabled,
                )
                self._provider = OpenAIWhisperProvider(config)
                logger.info("OpenAI Whisper transcription provider initialized")
            except Exception as e:
                logger.error(f"Failed to initialize transcription provider: {e}")

    def _default_model(self) -> str:
        """The configured transcription default when the provider takes it,
        else the provider's own (gpt-transcribe)."""
        from ..config.user_settings import default_model

        own = getattr(self._provider, "DEFAULT_MODEL", "gpt-transcribe")
        preferred = default_model(getattr(self, "settings", None), "transcript")
        try:
            supported = self._provider.get_supported_models() if self._provider is not None else set()
        except Exception:
            supported = set()
        return preferred if preferred and preferred in supported else own

    @staticmethod
    def _load_audio(
        audio_path: Optional[str], audio_data: Optional[str]
    ) -> tuple[bytes, str]:
        """Resolve audio input into (bytes, filename).

        Accepts a local file path (preferred — keeps the real extension so the
        API detects the format) or base64 / data-URL bytes.
        """
        if audio_path:
            p = Path(audio_path)
            if not p.is_file():
                raise RuntimeError(f"Audio file not found: {audio_path}")
            if p.suffix.lower() not in _KNOWN_AUDIO_EXTS:
                logger.warning(
                    "Audio extension %r may not be supported by the API", p.suffix
                )
            return p.read_bytes(), p.name
        if audio_data:
            b64 = audio_data
            if b64.startswith("data:") and "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                return base64.b64decode(b64), "audio.mp3"
            except Exception as e:
                raise RuntimeError(f"Invalid base64 audio data: {e}")
        raise RuntimeError("Provide either audio_path or audio_data")

    async def transcribe(
        self,
        audio_path: Optional[str] = None,
        audio_data: Optional[str] = None,
        model: Optional[str] = None,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
        response_format: str = "text",
        temperature: Optional[float] = None,
        timestamp_granularities: Optional[list[str]] = None,
        chunking_strategy: Optional[Any] = None,
        include: Optional[list[str]] = None,
        known_speaker_names: Optional[list[str]] = None,
        known_speaker_references: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Transcribe audio to text. Returns text + metadata (+ segments/words
        when response_format='verbose_json').

        Defaults to `gpt-transcribe` (OpenAI's current file-transcription
        model). The legacy models are still selectable but deprecated and shut
        down on 2027-02-26; picking one logs a warning and sets
        `model_status='deprecated'` in the result. Word-level timestamps
        (timestamp_granularities=['word'] + verbose_json) and srt/vtt are
        documented for whisper-1 only; speaker labels need
        model='gpt-4o-transcribe-diarize' with response_format='diarized_json'.
        """
        if not self._provider:
            raise RuntimeError(
                "No transcription provider is configured. Set "
                "PROVIDERS__OPENAI__API_KEY (and PROVIDERS__OPENAI__ENABLED=true)."
            )

        audio_bytes, filename = self._load_audio(audio_path, audio_data)
        target_model = model or self._default_model()

        result = await self._provider.transcribe(
            target_model,
            audio_bytes,
            filename,
            language=language,
            prompt=prompt,
            response_format=response_format,
            temperature=temperature,
            timestamp_granularities=timestamp_granularities,
            chunking_strategy=chunking_strategy,
            include=include,
            known_speaker_names=known_speaker_names,
            known_speaker_references=known_speaker_references,
        )

        status = "current"
        shutdown = None
        if hasattr(self._provider, "model_status"):
            status = self._provider.model_status(target_model)
            shutdown = getattr(self._provider, "MODEL_SHUTDOWN", {}).get(target_model)

        return {
            "text": result.text,
            "model": target_model,
            "model_status": status,
            "model_shutdown": shutdown,
            "provider": result.metadata.get("provider"),
            "language": result.metadata.get("language")
            or result.metadata.get("requested_language"),
            "duration": result.metadata.get("duration"),
            "segments": result.segments,
            "words": result.words,
            "logprobs": result.logprobs,
            "source_filename": filename,
            "metadata": result.metadata,
        }

    async def close(self) -> None:
        """Close the provider's underlying client on shutdown."""
        if self._provider and hasattr(self._provider, "close"):
            await self._provider.close()
