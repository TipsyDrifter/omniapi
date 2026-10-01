"""Audio transcription capability — provider abstraction + implementations.

Modality: audio -> text. Deliberately does NOT inherit `LLMProvider` (which
mandates `generate_image`/`edit_image`); a transcription provider only needs to
implement `transcribe()`. Reuses `ProviderConfig` / `ProviderError` from
`providers.base` so config loading and error handling stay uniform across the
whole server.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from ..providers.base import ProviderConfig, ProviderError

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    """Standardized transcription response across providers."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    segments: list[dict[str, Any]] | None = None
    words: list[dict[str, Any]] | None = None
    logprobs: list[dict[str, Any]] | None = None
    provider_response: dict[str, Any] | None = None


class TranscriptionProvider(ABC):
    """Abstract base for audio->text transcription providers.

    Mirrors the shape of `LLMProvider` (name derived from class, availability
    check, supported-models set) but for the transcription modality only.
    """

    #: model_id -> "current" | "deprecated". Read by the catalog layer; a model
    #: marked deprecated stays callable (we only log a warning).
    MODEL_STATUS: dict[str, str] = {}
    #: model_id -> ISO shutdown date, for deprecated models that have one.
    MODEL_SHUTDOWN: dict[str, str] = {}
    #: model_id -> officially recommended replacement model id.
    MODEL_REPLACEMENT: dict[str, str] = {}

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.name = self.__class__.__name__.replace("Provider", "").lower()
        self._logger = logging.getLogger(f"{__name__}.{self.name}")

    @abstractmethod
    def get_supported_models(self) -> set[str]:
        """Return the set of model IDs this provider can transcribe with."""
        ...

    def model_status(self, model_id: str) -> str:
        """Lifecycle status of a model: 'current' or 'deprecated'."""
        return self.MODEL_STATUS.get(model_id, "current")

    def deprecated_models(self) -> set[str]:
        return {m for m, s in self.MODEL_STATUS.items() if s == "deprecated"}

    def _warn_if_deprecated(self, model_id: str) -> None:
        """Log (never raise) when a deprecated model is used.

        Deprecated models remain selectable on purpose — callers with pinned
        workflows must keep working until the provider actually shuts them off.
        """
        if self.model_status(model_id) != "deprecated":
            return
        shutdown = self.MODEL_SHUTDOWN.get(model_id)
        replacement = self.MODEL_REPLACEMENT.get(model_id)
        self._logger.warning(
            "Model %r is deprecated%s%s.",
            model_id,
            f" and shuts down on {shutdown}" if shutdown else "",
            f"; migrate to {replacement!r}" if replacement else "",
        )

    @abstractmethod
    async def transcribe(
        self,
        model: str,
        audio_bytes: bytes,
        filename: str = "audio.mp3",
        *,
        language: str | None = None,
        prompt: str | None = None,
        response_format: str = "text",
        temperature: float | None = None,
        **kwargs: Any,
    ) -> TranscriptionResult:
        """Transcribe audio bytes to text."""
        ...

    def is_available(self) -> bool:
        """Provider is usable when enabled and holding an API key."""
        return self.config.enabled and bool(self.config.api_key)


class OpenAIWhisperProvider(TranscriptionProvider):
    """OpenAI transcription via the Audio API (gpt-transcribe + legacy models).

    Uses the same `AsyncOpenAI` client setup as `OpenAIProvider`, so the
    existing `PROVIDERS__OPENAI__API_KEY` serves transcription with zero extra
    configuration.

    Roster as of 2026-09-25 (see docs/research/2026-09-25-多模態模型榜單查證.md
    §3.1). `gpt-transcribe` is OpenAI's recommended file-transcription model
    ("Start with gpt-transcribe"). The four older models were deprecated on
    2026-08-26 and shut down on 2027-02-26; they stay callable here and only
    log a warning.

    Streaming-only models (`gpt-live-transcribe`, `gpt-realtime-whisper`,
    `gpt-realtime-translate`) are deliberately absent — they live on the
    Realtime API, not `/v1/audio/transcriptions`.
    """

    # whisper-1 supports every response_format (incl. verbose_json/srt/vtt).
    # The gpt-4o transcribe models only support json/text. The diarize model
    # adds speaker labels via response_format='diarized_json'.
    SUPPORTED_MODELS = {
        "gpt-transcribe",
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe-diarize",
    }
    DEFAULT_MODEL = "gpt-transcribe"

    #: OpenAI deprecation notice of 2026-08-26 — shutdown 2027-02-26.
    _SHUTDOWN_DATE = "2027-02-26"
    MODEL_STATUS = {
        "gpt-transcribe": "current",
        "whisper-1": "deprecated",
        "gpt-4o-transcribe": "deprecated",
        "gpt-4o-mini-transcribe": "deprecated",
        "gpt-4o-transcribe-diarize": "deprecated",
    }
    MODEL_SHUTDOWN = {
        "whisper-1": _SHUTDOWN_DATE,
        "gpt-4o-transcribe": _SHUTDOWN_DATE,
        "gpt-4o-mini-transcribe": _SHUTDOWN_DATE,
        "gpt-4o-transcribe-diarize": _SHUTDOWN_DATE,
    }
    MODEL_REPLACEMENT = {
        "whisper-1": "gpt-transcribe",
        "gpt-4o-transcribe": "gpt-transcribe",
        "gpt-4o-mini-transcribe": "gpt-transcribe",
        "gpt-4o-transcribe-diarize": "gpt-transcribe",
    }

    # The speaker-diarization model: rejects prompt + include, needs
    # chunking_strategy for >30s audio, and supports diarized_json output.
    DIARIZE_MODEL = "gpt-4o-transcribe-diarize"
    # include=['logprobs'] only works on these models with response_format=json.
    LOGPROBS_MODELS = {"gpt-4o-transcribe", "gpt-4o-mini-transcribe"}

    # Response formats each model is believed to accept. Used only to warn —
    # the API stays the source of truth and nothing is blocked here.
    #
    # UNVERIFIED: `gpt-transcribe`'s response_format range is not stated in the
    # 2026-09-25 docs sweep. Assumed conservatively to be json/text only (the
    # same as the gpt-4o transcribe family); verbose_json/srt/vtt/word-level
    # timestamps remain documented for whisper-1 only. Widen this set once the
    # official reference is checked.
    MODEL_RESPONSE_FORMATS: dict[str, set[str]] = {
        "gpt-transcribe": {"json", "text"},
        "whisper-1": {"json", "text", "verbose_json", "srt", "vtt"},
        "gpt-4o-transcribe": {"json", "text"},
        "gpt-4o-mini-transcribe": {"json", "text"},
        "gpt-4o-transcribe-diarize": {"json", "text", "diarized_json"},
    }
    # Only whisper-1 documents verbose_json + timestamp_granularities.
    TIMESTAMP_MODELS = {"whisper-1"}

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            organization=config.organization,
            base_url=config.base_url or "https://api.openai.com/v1",
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    async def transcribe(
        self,
        model: str,
        audio_bytes: bytes,
        filename: str = "audio.mp3",
        *,
        language: str | None = None,
        prompt: str | None = None,
        response_format: str = "text",
        temperature: float | None = None,
        timestamp_granularities: list[str] | None = None,
        chunking_strategy: Any | None = None,
        include: list[str] | None = None,
        known_speaker_names: list[str] | None = None,
        known_speaker_references: list[str] | None = None,
        **kwargs: Any,
    ) -> TranscriptionResult:
        if model not in self.SUPPORTED_MODELS:
            raise ProviderError(
                f"Model '{model}' is not supported by OpenAI Whisper provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )

        self._warn_if_deprecated(model)

        # Warn (never block) when the format looks outside the model's range.
        known_formats = self.MODEL_RESPONSE_FORMATS.get(model)
        if known_formats and response_format not in known_formats:
            self._logger.warning(
                "response_format=%r is not among the formats documented for "
                "%s (%s); the API may reject it.",
                response_format,
                model,
                sorted(known_formats),
            )

        is_diarize = model == self.DIARIZE_MODEL

        # The OpenAI SDK accepts a (filename, bytes) tuple for the file arg, so
        # we never need to touch the real filesystem here.
        request: dict[str, Any] = {
            "model": model,
            "file": (filename, audio_bytes),
            "response_format": response_format,
        }
        if language:
            request["language"] = language
        # The diarize model rejects `prompt` — drop it there.
        if prompt:
            if is_diarize:
                self._logger.warning(
                    "Dropping prompt: %s does not support it.", model
                )
            else:
                request["prompt"] = prompt
        if temperature is not None:
            request["temperature"] = temperature
        if timestamp_granularities:
            # The API only returns timestamps with verbose_json (whisper-1).
            if response_format != "verbose_json" or model not in self.TIMESTAMP_MODELS:
                self._logger.warning(
                    "timestamp_granularities=%s needs response_format="
                    "'verbose_json' on %s; got model=%s, format=%r — the API "
                    "will likely reject it.",
                    timestamp_granularities,
                    sorted(self.TIMESTAMP_MODELS),
                    model,
                    response_format,
                )
            request["timestamp_granularities"] = timestamp_granularities

        # include=['logprobs'] only works on gpt-4o-transcribe(-mini) + json.
        if include:
            if model in self.LOGPROBS_MODELS and response_format == "json":
                request["include"] = include
            else:
                self._logger.warning(
                    "Dropping include=%s: only %s with response_format='json' "
                    "support it (got model=%s, format=%s).",
                    include,
                    sorted(self.LOGPROBS_MODELS),
                    model,
                    response_format,
                )

        # Auto-chunking / VAD (server-side). Required for diarize on >30s audio,
        # so default it to 'auto' there when the caller did not set it.
        if chunking_strategy is not None:
            request["chunking_strategy"] = chunking_strategy
        elif is_diarize:
            request["chunking_strategy"] = "auto"

        # Speaker references are only valid for the diarize model.
        if known_speaker_names or known_speaker_references:
            if is_diarize:
                if known_speaker_names:
                    request["known_speaker_names"] = known_speaker_names
                if known_speaker_references:
                    request["known_speaker_references"] = known_speaker_references
            else:
                self._logger.warning(
                    "Dropping known_speaker_* : only %s supports speaker "
                    "references.",
                    self.DIARIZE_MODEL,
                )

        try:
            self._logger.info(
                "Transcribing %d bytes with OpenAI model %s (format=%s)",
                len(audio_bytes),
                model,
                response_format,
            )
            resp = await self.client.audio.transcriptions.create(**request)

            # text/srt/vtt formats return a plain string; json/verbose_json
            # return an object with .text (+ .segments for verbose_json).
            if isinstance(resp, str):
                text = resp
                segments = None
                words = None
                logprobs = None
                raw = None
                meta_extra: dict[str, Any] = {}
            else:
                text = getattr(resp, "text", "") or ""
                segments = getattr(resp, "segments", None)
                if segments and hasattr(segments[0], "model_dump"):
                    segments = [s.model_dump() for s in segments]
                words = getattr(resp, "words", None)
                if words and hasattr(words[0], "model_dump"):
                    words = [w.model_dump() for w in words]
                logprobs = getattr(resp, "logprobs", None)
                if logprobs and hasattr(logprobs[0], "model_dump"):
                    logprobs = [lp.model_dump() for lp in logprobs]
                raw = resp.model_dump() if hasattr(resp, "model_dump") else None
                meta_extra = {
                    "language": getattr(resp, "language", None),
                    "duration": getattr(resp, "duration", None),
                }

            metadata = {
                "model": model,
                "provider": self.name,
                "response_format": response_format,
                "requested_language": language,
                **meta_extra,
            }

            return TranscriptionResult(
                text=text,
                metadata=metadata,
                segments=segments,
                words=words,
                logprobs=logprobs,
                provider_response=raw,
            )

        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error transcribing audio with OpenAI: %s", e)
            raise ProviderError(
                f"OpenAI transcription failed: {str(e)}",
                provider_name=self.name,
                error_code="TRANSCRIPTION_FAILED",
            )

    async def close(self) -> None:
        """Release the underlying httpx client (called on server shutdown)."""
        try:
            await self.client.close()
        except Exception:
            pass
