"""Text-to-speech capability — speech synthesis providers.

Modality: text -> audio. Three backends:

* ElevenLabs — its own REST API, called directly with httpx (no extra
  dependency; httpx is already required).
* OpenAI — the Audio API via the `openai` SDK.
* Gemini — `generate_content` with response_modalities=['AUDIO'] via the
  `google-genai` SDK; returns raw PCM which we wrap into a WAV container.

All three reuse `ProviderConfig` / `ProviderError` from `providers.base`.
Model rosters follow docs/research/2026-09-25-多模態模型榜單查證.md §2.
"""

import io
import logging
import re
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..providers.base import ProviderConfig, ProviderError
from ..utils.audio import ffmpeg_path, pcm_to_mp3

logger = logging.getLogger(__name__)


@dataclass
class SpeechResult:
    """Standardized text-to-speech response."""

    audio_data: bytes
    output_format: str
    metadata: dict[str, Any] = field(default_factory=dict)


class SpeechProvider(ABC):
    """Abstract base for text-to-speech providers."""

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
        ...

    def model_status(self, model_id: str) -> str:
        """Lifecycle status of a model: 'current' or 'deprecated'."""
        return self.MODEL_STATUS.get(model_id, "current")

    def deprecated_models(self) -> set[str]:
        return {m for m, s in self.MODEL_STATUS.items() if s == "deprecated"}

    def _warn_if_deprecated(self, model_id: str) -> None:
        """Log (never raise) when a deprecated model is used.

        Deprecated models stay selectable on purpose — pinned workflows must
        keep working until the provider actually shuts the model off.
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
    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        output_format: str = "mp3_44100_128",
        **kwargs: Any,
    ) -> SpeechResult:
        ...

    def is_available(self) -> bool:
        return self.config.enabled and bool(self.config.api_key)


class ElevenLabsProvider(SpeechProvider):
    """ElevenLabs text-to-speech via the REST API.

    Endpoint: POST {base}/v1/text-to-speech/{voice_id}
    Auth header: xi-api-key. model_id goes in the JSON body; output_format is a
    query param. Returns raw audio bytes.

    Roster as of 2026-09-25 (docs/research/2026-09-25-多模態模型榜單查證.md
    §2.1). `eleven_turbo_v2_5` is marked deprecated on the official models page
    ("outclassed by Flash models") with `eleven_flash_v2_5` as the named
    replacement — no shutdown date has been announced, so it stays callable.

    2026-10-05: Eleven v4 and v4 Turbo (released 2026-09-28) go through the
    same endpoint with their own ``model_id`` (docs/research/
    2026-10-05-kie的API形狀與現有key的目錄缺口查證.md §2).
    """

    SUPPORTED_MODELS = {
        "eleven_v4",                # newest, most expressive (2026-09-28)
        "eleven_v4_turbo",          # v4 at half the price
        "eleven_v3",                # most advanced / expressive
        "eleven_v3_conversational",  # most expressive realtime (~280ms)
        "eleven_multilingual_v2",   # most lifelike, rich emotion
        "eleven_flash_v2_5",        # ultra-fast, ~75ms (default here)
        "eleven_flash_v2",          # ultra-fast, English-focused
        "eleven_turbo_v2_5",        # deprecated -> eleven_flash_v2_5
    }
    DEFAULT_MODEL = "eleven_flash_v2_5"

    MODEL_STATUS = {
        "eleven_v4": "current",
        "eleven_v4_turbo": "current",
        "eleven_v3": "current",
        "eleven_v3_conversational": "current",
        "eleven_multilingual_v2": "current",
        "eleven_flash_v2_5": "current",
        "eleven_flash_v2": "current",
        "eleven_turbo_v2_5": "deprecated",
    }
    # ElevenLabs has announced no shutdown date for eleven_turbo_v2_5, so
    # MODEL_SHUTDOWN stays empty here on purpose.
    MODEL_REPLACEMENT = {"eleven_turbo_v2_5": "eleven_flash_v2_5"}
    # Rachel — a long-standing ElevenLabs preset voice. Override per call, or
    # look up the account's voices via GET /v1/voices.
    DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"
    DEFAULT_BASE = "https://api.elevenlabs.io"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.base_url = (config.base_url or self.DEFAULT_BASE).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    def _http(self) -> httpx.AsyncClient:
        """Long-lived shared HTTP client (lazy) — keeps the TLS pool warm
        across calls instead of re-handshaking per synthesis."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self._client

    async def close(self) -> None:
        """Release the shared HTTP client (called on server shutdown)."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def list_voices(self, *, max_pages: int = 5) -> list[dict[str, Any]]:
        """The account's voices (``GET /v2/voices``, 100 per page — the v1
        list stops working past 500 voices; docs/research/
        2026-10-01-語音聲音清單與上傳上限查證.md §3)."""
        voices: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"page_size": 100}
            if token:
                params["next_page_token"] = token
            resp = await self._http().get(
                f"{self.base_url}/v2/voices", headers={"xi-api-key": self.config.api_key}, params=params
            )
            if resp.status_code != 200:
                raise ProviderError(
                    f"ElevenLabs voices returned HTTP {resp.status_code}: {resp.text[:300]}",
                    provider_name=self.name,
                    error_code="VOICES_FAILED",
                )
            data = resp.json()
            for v in data.get("voices") or []:
                labels = v.get("labels") if isinstance(v.get("labels"), dict) else {}
                voices.append({
                    "id": v.get("voice_id"),
                    "name": v.get("name"),
                    "note": " · ".join(str(x) for x in (labels.get("gender"), labels.get("accent"), v.get("category")) if x) or None,
                    "preview_url": v.get("preview_url"),
                })
            token = data.get("next_page_token")
            if not data.get("has_more") or not token:
                break
        return [v for v in voices if v["id"]]

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        output_format: str = "mp3_44100_128",
        **kwargs: Any,
    ) -> SpeechResult:
        voice_id = voice or self.DEFAULT_VOICE
        model_id = model or self.DEFAULT_MODEL
        if model_id not in self.SUPPORTED_MODELS:
            raise ProviderError(
                f"Model '{model_id}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        self._warn_if_deprecated(model_id)

        url = f"{self.base_url}/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self.config.api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        body: dict[str, Any] = {"text": text, "model_id": model_id}
        if kwargs.get("voice_settings"):
            body["voice_settings"] = kwargs["voice_settings"]
        if kwargs.get("language_code"):
            body["language_code"] = kwargs["language_code"]
        # Optional body fields, forwarded only when set (seed=0 is valid).
        for key in ("seed", "previous_text", "next_text", "apply_text_normalization"):
            if kwargs.get(key) is not None:
                body[key] = kwargs[key]

        params: dict[str, Any] = {"output_format": output_format}
        # enable_logging=false requests zero-retention mode (privacy). Query param.
        if kwargs.get("enable_logging") is not None:
            params["enable_logging"] = kwargs["enable_logging"]

        try:
            self._logger.info(
                "Synthesizing %d chars with %s model %s (voice %s)",
                len(text),
                self.name,
                model_id,
                voice_id,
            )
            resp = await self._http().post(
                url, headers=headers, json=body, params=params
            )
            if resp.status_code != 200:
                detail = resp.text[:300]
                raise ProviderError(
                    f"ElevenLabs TTS returned HTTP {resp.status_code}: {detail}",
                    provider_name=self.name,
                    error_code="TTS_FAILED",
                )
            audio = resp.content

            return SpeechResult(
                audio_data=audio,
                output_format=output_format,
                metadata={
                    "provider": self.name,
                    "model": model_id,
                    "voice_id": voice_id,
                },
            )
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in ElevenLabs TTS: %s", e)
            raise ProviderError(
                f"ElevenLabs TTS failed: {str(e)}",
                provider_name=self.name,
                error_code="TTS_FAILED",
            )


class OpenAITTSProvider(SpeechProvider):
    """OpenAI text-to-speech via the Audio API (gpt-4o-mini-tts / tts-1).

    Reuses the same OpenAI key as the other OpenAI capabilities, so TTS can be
    tested end-to-end without an ElevenLabs key.

    Roster unchanged as of 2026-09-25 (docs/research/
    2026-09-25-多模態模型榜單查證.md §2.2): `gpt-4o-mini-tts` remains OpenAI's
    newest TTS model. The GPT-Live-1 family is realtime conversation, not a
    TTS API model, so it is deliberately not listed here.

    2026-10-05 (docs/research/2026-10-05-kie的API形狀與現有key的目錄缺口查證.md
    §2): OpenAI's deprecations page announced on 2026-10-01 that `tts-1` and
    `tts-1-hd` shut down on 2027-01-06.
    """

    SUPPORTED_MODELS = {"gpt-4o-mini-tts", "tts-1", "tts-1-hd"}
    DEFAULT_MODEL = "gpt-4o-mini-tts"
    MODEL_STATUS = {
        "gpt-4o-mini-tts": "current",
        "tts-1": "deprecated",
        "tts-1-hd": "deprecated",
    }
    # No replacement named here: the notice points at a realtime model this
    # TTS path does not call; gpt-4o-mini-tts (the default) is the one to use.
    MODEL_SHUTDOWN = {"tts-1": "2027-01-06", "tts-1-hd": "2027-01-06"}
    DEFAULT_VOICE = "alloy"
    # Built-in voices (docs/research/2026-10-01-語音聲音清單與上傳上限查證.md §1).
    # The last four exist on gpt-4o-mini-tts only; OpenAI recommends marin / cedar.
    VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar")
    NEW_MODEL_ONLY_VOICES = frozenset({"ballad", "verse", "marin", "cedar"})
    # Natural-language tone/emotion control is only honoured by gpt-4o-mini-tts;
    # tts-1 / tts-1-hd ignore `instructions` (and instead honour `speed`).
    INSTRUCTIONS_MODELS = {"gpt-4o-mini-tts"}

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=config.api_key,
            organization=config.organization,
            base_url=config.base_url or "https://api.openai.com/v1",
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        output_format: str = "mp3_44100_128",
        **kwargs: Any,
    ) -> SpeechResult:
        voice_id = voice or self.DEFAULT_VOICE
        model_id = model or self.DEFAULT_MODEL
        if model_id not in self.SUPPORTED_MODELS:
            raise ProviderError(
                f"Model '{model_id}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        self._warn_if_deprecated(model_id)
        # OpenAI uses simple format tokens (mp3/opus/aac/flac/wav/pcm); accept
        # ElevenLabs-style "mp3_44100_128" too and keep the leading token.
        fmt = (output_format or "mp3").split("_")[0].lower()
        if fmt not in {"mp3", "opus", "aac", "flac", "wav", "pcm"}:
            fmt = "mp3"

        # Optional, forwarded only when set. `instructions` (tone/emotion) is
        # gpt-4o-mini-tts-only; `speed` (0.25-4.0) is honoured by tts-1/hd.
        extra: dict[str, Any] = {}
        instructions = kwargs.get("instructions")
        if instructions:
            if model_id in self.INSTRUCTIONS_MODELS:
                extra["instructions"] = instructions
            else:
                self._logger.warning(
                    "Ignoring instructions: model %s does not support it "
                    "(only %s do).",
                    model_id,
                    sorted(self.INSTRUCTIONS_MODELS),
                )
        speed = kwargs.get("speed")
        if speed is not None:
            extra["speed"] = speed

        try:
            self._logger.info(
                "Synthesizing %d chars with %s model %s (voice %s)",
                len(text),
                self.name,
                model_id,
                voice_id,
            )
            resp = await self.client.audio.speech.create(
                model=model_id,
                voice=voice_id,
                input=text,
                response_format=fmt,
                **extra,
            )
            audio = resp.content if hasattr(resp, "content") else resp.read()
            return SpeechResult(
                audio_data=audio,
                output_format=fmt,
                metadata={
                    "provider": self.name,
                    "model": model_id,
                    "voice_id": voice_id,
                },
            )
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in OpenAI TTS: %s", e)
            raise ProviderError(
                f"OpenAI TTS failed: {str(e)}",
                provider_name=self.name,
                error_code="TTS_FAILED",
            )

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            pass


class GeminiSpeechProvider(SpeechProvider):
    """Gemini text-to-speech via the google-genai SDK.

    Unlike the image path (which runs on Vertex AI with service-account
    credentials), TTS here talks to the Gemini Developer API with a plain
    **API key**: ``genai.Client(api_key=...)``.

    Call shape: ``models.generate_content`` with
    ``response_modalities=['AUDIO']`` and a ``SpeechConfig`` naming a prebuilt
    voice. The audio arrives as raw PCM in
    ``candidates[0].content.parts[*].inline_data.data`` (already bytes — never
    base64-decode it again), with a mime type such as
    ``audio/L16;codec=pcm;rate=24000``.

    Roster as of 2026-09-25 (docs/research/2026-09-25-多模態模型榜單查證.md
    §2.3).
    """

    SUPPORTED_MODELS = {
        "gemini-3.8-flash-tts",       # stable, most expressive (default here)
        "gemini-3.8-flash-lite-tts",  # stable, cheaper
        "gemini-3.1-flash-tts-preview",
    }
    DEFAULT_MODEL = "gemini-3.8-flash-tts"
    # A prebuilt Gemini TTS voice. Override per call; the full voice list is on
    # the speech-generation docs page.
    DEFAULT_VOICE = "Kore"
    # Prebuilt voices with Google's one-word description (docs/research/
    # 2026-10-01-語音聲音清單與上傳上限查證.md §2).
    VOICES = {
        "Zephyr": "Bright", "Puck": "Upbeat", "Charon": "Informative", "Kore": "Firm", "Fenrir": "Excitable",
        "Leda": "Youthful", "Orus": "Firm", "Aoede": "Breezy", "Callirrhoe": "Easy-going", "Autonoe": "Bright",
        "Enceladus": "Breathy", "Iapetus": "Clear", "Umbriel": "Easy-going", "Algieba": "Smooth", "Despina": "Smooth",
        "Erinome": "Clear", "Algenib": "Gravelly", "Rasalgethi": "Informative", "Laomedeia": "Upbeat", "Achernar": "Soft",
        "Alnilam": "Firm", "Schedar": "Even", "Gacrux": "Mature", "Pulcherrima": "Forward", "Achird": "Friendly",
        "Zubenelgenubi": "Casual", "Vindemiatrix": "Gentle", "Sadachbia": "Lively", "Sadaltager": "Knowledgeable",
        "Sulafat": "Warm",
    }

    # NOTE (judgement call): Google's models page labels
    # `gemini-3.1-flash-tts-preview` a "Legacy text-to-speech preview model"
    # but has published no formal deprecation notice or shutdown date, so it is
    # classified deprecated here with no entry in MODEL_SHUTDOWN.
    MODEL_STATUS = {
        "gemini-3.8-flash-tts": "current",
        "gemini-3.8-flash-lite-tts": "current",
        "gemini-3.1-flash-tts-preview": "deprecated",
    }
    MODEL_REPLACEMENT = {"gemini-3.1-flash-tts-preview": "gemini-3.8-flash-tts"}

    # PCM framing for Gemini TTS output. Per the official speech-generation
    # docs the models emit 24 kHz 16-bit mono PCM. NOT MEASURED against a live
    # response — when the reply carries a mime type like
    # "audio/L16;codec=pcm;rate=24000" we prefer the rate it reports and only
    # fall back to these constants.
    PCM_SAMPLE_RATE_HZ = 24000
    PCM_SAMPLE_WIDTH_BYTES = 2  # 16-bit
    PCM_CHANNELS = 1

    _RATE_RE = re.compile(r"rate=(\d+)")

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        from google import genai
        from google.genai import types as genai_types

        self._genai_types = genai_types
        http_options: Any = None
        if config.timeout:
            # HttpOptions.timeout is in milliseconds.
            http_options = genai_types.HttpOptions(
                timeout=int(config.timeout * 1000)
            )
        self.client = genai.Client(
            api_key=config.api_key,
            **({"http_options": http_options} if http_options else {}),
        )

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    @classmethod
    def _pcm_to_wav(
        cls,
        pcm: bytes,
        sample_rate: int | None = None,
        *,
        channels: int | None = None,
        sample_width: int | None = None,
    ) -> bytes:
        """Wrap raw PCM frames in a WAV (RIFF) container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(channels or cls.PCM_CHANNELS)
            wav.setsampwidth(sample_width or cls.PCM_SAMPLE_WIDTH_BYTES)
            wav.setframerate(sample_rate or cls.PCM_SAMPLE_RATE_HZ)
            wav.writeframes(pcm)
        return buf.getvalue()

    @classmethod
    def _sample_rate_from_mime(cls, mime_type: str | None) -> int | None:
        """Read `rate=NNNNN` out of e.g. 'audio/L16;codec=pcm;rate=24000'."""
        if not mime_type:
            return None
        match = cls._RATE_RE.search(mime_type)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_audio(response: Any) -> tuple[bytes | None, str | None]:
        """Pull the first inline audio blob out of a generate_content reply."""
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline is not None else None
                if data:
                    return data, getattr(inline, "mime_type", None)
        return None, None

    async def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        output_format: str = "wav",
        **kwargs: Any,
    ) -> SpeechResult:
        types = self._genai_types
        voice_id = voice or self.DEFAULT_VOICE
        model_id = model or self.DEFAULT_MODEL
        if model_id not in self.SUPPORTED_MODELS:
            raise ProviderError(
                f"Model '{model_id}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        self._warn_if_deprecated(model_id)

        # Gemini only ever returns PCM. Hand back raw PCM when asked for it, MP3
        # when asked for it and a system ffmpeg can encode it, otherwise a WAV
        # container.
        head = (output_format or "wav").split("_")[0].lower()
        if head == "mp3" and ffmpeg_path() is None:
            self._logger.warning("Gemini TTS emits PCM and no ffmpeg is on PATH to encode MP3; returning WAV.")
            head = "wav"
        if head not in {"wav", "pcm", "mp3"}:
            self._logger.warning(
                "Gemini TTS only emits PCM; returning WAV instead of "
                "requested output_format=%r.",
                output_format,
            )
            head = "wav"

        speech_kwargs: dict[str, Any] = {
            "voice_config": types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_id
                )
            )
        }
        if kwargs.get("language_code"):
            speech_kwargs["language_code"] = kwargs["language_code"]

        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(**speech_kwargs),
        )

        try:
            self._logger.info(
                "Synthesizing %d chars with %s model %s (voice %s)",
                len(text),
                self.name,
                model_id,
                voice_id,
            )
            response = await self.client.aio.models.generate_content(
                model=model_id,
                contents=text,
                config=config,
            )

            pcm, mime_type = self._extract_audio(response)
            if not pcm:
                raise ProviderError(
                    "Gemini TTS returned no audio data",
                    provider_name=self.name,
                    error_code="TTS_FAILED",
                )

            sample_rate = self._sample_rate_from_mime(mime_type)
            audio = pcm
            if head == "mp3":
                mp3 = await pcm_to_mp3(pcm, sample_rate=sample_rate or self.PCM_SAMPLE_RATE_HZ, channels=self.PCM_CHANNELS)
                if mp3 is None:  # the reason is already logged; a WAV still plays everywhere
                    head = "wav"
                else:
                    audio = mp3
            if head == "wav":
                audio = self._pcm_to_wav(pcm, sample_rate)

            return SpeechResult(
                audio_data=audio,
                output_format=head,
                metadata={
                    "provider": self.name,
                    "model": model_id,
                    "voice_id": voice_id,
                    "source_mime_type": mime_type,
                    "sample_rate": sample_rate or self.PCM_SAMPLE_RATE_HZ,
                },
            )
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in Gemini TTS: %s", e)
            raise ProviderError(
                f"Gemini TTS failed: {str(e)}",
                provider_name=self.name,
                error_code="TTS_FAILED",
            )

    async def close(self) -> None:
        """google-genai owns its transport; nothing to release explicitly."""
        return None
