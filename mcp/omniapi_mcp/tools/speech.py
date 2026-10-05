"""Speech synthesis tool — orchestrates TTS providers.

Routes each request to the provider that owns the requested model, then saves
the audio under storage/audio/<date>/ and returns the file path. ElevenLabs is
the preferred TTS (needs its own key); OpenAI TTS reuses the existing OpenAI key
so speech works even before an ElevenLabs key is added; Gemini TTS is available
when the Gemini slot holds a Developer-API key (not a service-account file).
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiofiles

from ..capabilities.speech import (
    ElevenLabsProvider,
    GeminiSpeechProvider,
    OpenAITTSProvider,
    SpeechProvider,
)
from ..config.settings import Settings
from ..providers.base import ProviderConfig

logger = logging.getLogger(__name__)

# Leading token of an output_format -> file extension.
_FORMAT_EXT = {
    "mp3": "mp3",
    "wav": "wav",
    "pcm": "pcm",
    "opus": "opus",
    "ulaw": "ulaw",
    "aac": "aac",
    "flac": "flac",
}


class SpeechTool:
    """Synthesize speech from text via a configured TTS provider."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._providers: list[SpeechProvider] = []
        self._model_map: dict[str, SpeechProvider] = {}
        self._init_providers()

    def _register(self, provider: SpeechProvider) -> None:
        self._providers.append(provider)
        for model_id in provider.get_supported_models():
            self._model_map[model_id] = provider
        logger.info(
            "Speech provider '%s' registered with %d models",
            provider.name,
            len(provider.get_supported_models()),
        )

    def _init_providers(self) -> None:
        # ElevenLabs first (preferred TTS; needs its own key). Listed first so
        # it becomes the default model once a key is present.
        el = getattr(self.settings.providers, "elevenlabs", None)
        if el and el.enabled and el.api_key:
            try:
                self._register(
                    ElevenLabsProvider(
                        ProviderConfig(
                            api_key=el.api_key,
                            base_url=el.base_url,
                            timeout=el.timeout,
                            max_retries=el.max_retries,
                            enabled=el.enabled,
                        )
                    )
                )
            except Exception as e:
                logger.error("Failed to init ElevenLabs provider: %s", e)

        # OpenAI TTS (reuses the OpenAI key — usable without an ElevenLabs key).
        oa = getattr(self.settings.providers, "openai", None)
        if oa and oa.enabled and oa.api_key:
            try:
                self._register(
                    OpenAITTSProvider(
                        ProviderConfig(
                            api_key=oa.api_key,
                            organization=oa.organization,
                            base_url=oa.base_url,
                            timeout=oa.timeout,
                            max_retries=oa.max_retries,
                            enabled=oa.enabled,
                        )
                    )
                )
            except Exception as e:
                logger.error("Failed to init OpenAI TTS provider: %s", e)

        # Gemini TTS (Developer API, API-key auth). Registered last so it never
        # displaces the existing default model.
        self._init_gemini_provider()

    @staticmethod
    def _looks_like_service_account(value: str) -> bool:
        """True when the configured secret is a credentials *file*, not a key.

        The Gemini image path authenticates to Vertex AI with a service-account
        JSON file and reuses the same settings field. Gemini TTS needs a plain
        Developer-API key, so a path-shaped value means 'no TTS key here'
        rather than 'try it and fail with a confusing 401'.
        """
        v = (value or "").strip()
        return v.lower().endswith(".json") or "/" in v or "\\" in v

    def _init_gemini_provider(self) -> None:
        gemini_cfg = getattr(self.settings.providers, "gemini", None)
        if not gemini_cfg or not getattr(gemini_cfg, "enabled", False):
            return
        # Read defensively: the Gemini settings block is shared with the image
        # path and its shape may change independently of this tool.
        api_key = (getattr(gemini_cfg, "api_key", "") or "").strip()
        if not api_key:
            return
        if self._looks_like_service_account(api_key):
            logger.info(
                "Skipping Gemini TTS: the Gemini api_key setting looks like a "
                "service-account file path, not a Developer-API key."
            )
            return
        try:
            self._register(
                GeminiSpeechProvider(
                    ProviderConfig(
                        api_key=api_key,
                        timeout=getattr(gemini_cfg, "timeout", 300.0),
                        max_retries=getattr(gemini_cfg, "max_retries", 3),
                        enabled=True,
                    )
                )
            )
        except Exception as e:
            logger.error("Failed to init Gemini TTS provider: %s", e)

    def _default_model(self) -> Optional[str]:
        if not self._providers:
            return None
        from ..config.user_settings import default_model

        preferred = default_model(self.settings, "speech")
        if preferred and preferred in self._model_map:
            return preferred
        first = self._providers[0]
        return getattr(first, "DEFAULT_MODEL", None) or next(
            iter(first.get_supported_models()), None
        )

    def available_models(self) -> list[str]:
        return sorted(self._model_map)

    @staticmethod
    def _ext_for(output_format: str) -> str:
        head = (output_format or "mp3").split("_")[0].lower()
        return _FORMAT_EXT.get(head, "mp3")

    async def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        output_format: str = "mp3_44100_128",
        instructions: Optional[str] = None,
        speed: Optional[float] = None,
        voice_settings: Optional[dict[str, Any]] = None,
        language_code: Optional[str] = None,
        seed: Optional[int] = None,
        previous_text: Optional[str] = None,
        next_text: Optional[str] = None,
        apply_text_normalization: Optional[str] = None,
        enable_logging: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Synthesize speech and save it to local storage; return the path.

        Advanced params are forwarded only when set; each provider reads the
        ones it understands (OpenAI: instructions/speed; ElevenLabs:
        voice_settings/language_code/seed/previous_text/next_text/
        apply_text_normalization/enable_logging; Gemini: language_code) and
        ignores the rest.

        Gemini TTS only emits PCM, so it returns WAV (or raw PCM) regardless of
        the requested container.
        """
        if not self._providers:
            raise RuntimeError(
                "No speech provider is configured. Set PROVIDERS__ELEVENLABS__API_KEY, "
                "or use OpenAI TTS via PROVIDERS__OPENAI__API_KEY — with the matching "
                "__ENABLED=true."
            )

        target_model = model or self._default_model()
        provider = self._model_map.get(target_model)
        if not provider:
            raise RuntimeError(
                f"No speech provider for model '{target_model}'. "
                f"Available models: {self.available_models()}"
            )

        extra: dict[str, Any] = {}
        if instructions is not None:
            extra["instructions"] = instructions
        if speed is not None:
            extra["speed"] = speed
        if voice_settings is not None:
            extra["voice_settings"] = voice_settings
        if language_code is not None:
            extra["language_code"] = language_code
        if seed is not None:
            extra["seed"] = seed
        if previous_text is not None:
            extra["previous_text"] = previous_text
        if next_text is not None:
            extra["next_text"] = next_text
        if apply_text_normalization is not None:
            extra["apply_text_normalization"] = apply_text_normalization
        if enable_logging is not None:
            extra["enable_logging"] = enable_logging

        result = await provider.synthesize(
            text,
            voice=voice,
            model=target_model,
            output_format=output_format,
            **extra,
        )

        ext = self._ext_for(result.output_format or output_format)
        now = datetime.now(timezone.utc)
        out_dir = Path(self.settings.storage.base_path) / "audio" / now.strftime(
            "%Y-%m-%d"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"speech_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
        out_path = (out_dir / fname).resolve()
        # Async write so multi-MB audio never stalls the event loop.
        async with aiofiles.open(out_path, "wb") as f:
            await f.write(result.audio_data)

        used_model = result.metadata.get("model") or target_model
        return {
            "audio_path": str(out_path),
            "audio_url": f"file://{out_path}",
            "model": used_model,
            "model_status": provider.model_status(used_model),
            "model_shutdown": provider.MODEL_SHUTDOWN.get(used_model),
            "provider": result.metadata.get("provider"),
            "voice_id": result.metadata.get("voice_id"),
            "output_format": result.output_format,
            "bytes": len(result.audio_data),
        }

    async def close(self) -> None:
        for provider in self._providers:
            if hasattr(provider, "close"):
                await provider.close()
