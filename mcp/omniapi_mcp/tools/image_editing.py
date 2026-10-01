"""Image editing tool implementation."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..config.settings import Settings
from ..providers.base import ProviderConfig
from ..providers.gemini import GeminiProvider
from ..providers.openai import OpenAIProvider
from ..storage.manager import ImageStorageManager
from ..types import BackgroundType, ImageQuality, ImageSize, OutputFormat
from ..utils.cache import CacheManager
from ..utils.path_utils import build_image_access_url

logger = logging.getLogger(__name__)


class ImageEditingTool:
    """Tool for editing images using multiple LLM providers."""

    def __init__(
        self,
        storage_manager: ImageStorageManager,
        cache_manager: CacheManager,
        settings: Settings,
        openai_client=None,
    ):
        """
        Args:
            storage_manager: ImageStorageManager instance.
            cache_manager: CacheManager instance.
            settings: Settings instance (must have .providers, .images, etc.).
            openai_client: Optional OpenAI client manager.
        """
        self.settings = settings
        self.storage_manager = storage_manager
        self.cache_manager = cache_manager
        # Edits route by model: gpt-image-* → OpenAI, Nano Banana → Gemini.
        # No hard validation here: like the other capability tools, an
        # unconfigured provider degrades gracefully (stays None and edit()
        # raises a friendly error) instead of crashing server startup.
        self._provider = openai_client or self._build_openai_provider()
        self._gemini_provider = self._build_gemini_provider()
        if self._provider is None and self._gemini_provider is None:
            logger.warning(
                "No image provider configured — edit_image will be unavailable "
                "until PROVIDERS__OPENAI__API_KEY or PROVIDERS__GEMINI__API_KEY is set."
            )

    def _build_openai_provider(self) -> Optional[OpenAIProvider]:
        """Instantiate the OpenAI provider from settings, or None if unconfigured."""
        cfg = getattr(self.settings.providers, "openai", None)
        if not (cfg and cfg.enabled and cfg.api_key):
            return None
        try:
            return OpenAIProvider(
                ProviderConfig(
                    api_key=cfg.api_key,
                    organization=cfg.organization,
                    base_url=cfg.base_url,
                    timeout=cfg.timeout,
                    max_retries=cfg.max_retries,
                    enabled=cfg.enabled,
                )
            )
        except Exception as e:
            logger.error("Failed to initialize OpenAI provider for editing: %s", e)
            return None

    def _build_gemini_provider(self) -> Optional[GeminiProvider]:
        """Instantiate the Gemini (Nano Banana) provider, or None if unconfigured."""
        cfg = getattr(self.settings.providers, "gemini", None)
        if not (cfg and cfg.enabled and cfg.api_key):
            return None
        try:
            return GeminiProvider(
                ProviderConfig(
                    api_key=cfg.api_key,
                    base_url=cfg.base_url,
                    timeout=cfg.timeout,
                    max_retries=cfg.max_retries,
                    enabled=cfg.enabled,
                )
            )
        except Exception as e:
            logger.error("Failed to initialize Gemini provider for editing: %s", e)
            return None

    def _provider_for(self, model: str):
        """Pick the provider that owns ``model`` (None if unavailable)."""
        canonical = GeminiProvider.ALIASES.get(model, model)
        if canonical in GeminiProvider.SUPPORTED_MODELS:
            return self._gemini_provider
        return self._provider

    async def close(self) -> None:
        for p in (self._provider, self._gemini_provider):
            if p is not None and hasattr(p, "close"):
                try:
                    await p.close()
                except Exception:
                    pass

    def _validate_openai_settings(self):
        """Raise if OpenAI provider settings are missing.

        Kept for callers that want a hard check; the constructor no longer
        invokes it (missing config degrades gracefully instead).
        """
        self.validate_openai_provider_settings(self.settings)

    @staticmethod
    def validate_openai_provider_settings(settings):
        """Validate that the OpenAI provider settings are present and correct."""
        if not hasattr(settings, 'providers') or settings.providers is None:
            raise ValueError(
                "Settings must have a 'providers' attribute for image editing "
                "functionality."
            )
        if not hasattr(settings.providers, 'openai') or not settings.providers.openai:
            raise ValueError(
                "OpenAI provider settings are required for image editing "
                "functionality. Please configure the 'providers.openai' section "
                "in your settings with api_key, api_base, and other required "
                "parameters."
            )

    def _build_image_url(self, image_id: str, file_format: str = "png") -> str:
        """Build the externally visible URL for a stored image (shared logic
        in utils.path_utils — kept as a method so tests can mock per-tool)."""
        return build_image_access_url(
            image_id,
            file_format,
            base_host=self.settings.images.base_host,
            server_host=self.settings.server.host,
            server_port=self.settings.server.port,
            storage_base_path=self.settings.storage.base_path,
        )

    async def edit(
        self,
        image_data: str,
        prompt: str,
        mask_data: str | None = None,
        model: Optional[str] = None,
        size: str = "1536x1024",
        quality: str = "auto",
        output_format: str = "png",
        compression: int = 100,
        background: str = "auto",
        input_fidelity: Optional[str] = None,
        additional_images: Optional[list[str]] = None,
        user: Optional[str] = None,
    ) -> dict[str, Any]:
        """Edit an existing image with text instructions."""

        # Apply defaults from settings
        model = model or self.settings.images.default_model
        provider = self._provider_for(model)
        if provider is None:
            raise RuntimeError(
                f"No provider is configured for editing with '{model}'. Set "
                "PROVIDERS__OPENAI__API_KEY for gpt-image-* or "
                "PROVIDERS__GEMINI__API_KEY for Nano Banana models."
            )
        quality = quality or self.settings.images.default_quality
        size = size or self.settings.images.default_size
        output_format = output_format or self.settings.images.default_output_format
        compression = (
            compression
            if compression is not None
            else self.settings.images.default_compression
        )

        # Normalize enum inputs to their plain string values (mirrors
        # image_generation). The server layer's validators hand us enum
        # members; passing those through to the OpenAI SDK form-encodes as
        # e.g. 'ImageQuality.HIGH' and the API rejects the request.
        quality = quality.value if isinstance(quality, ImageQuality) else str(quality)
        size = size.value if isinstance(size, ImageSize) else str(size)
        output_format = (
            output_format.value
            if isinstance(output_format, OutputFormat)
            else str(output_format)
        )
        background = (
            background.value
            if isinstance(background, BackgroundType)
            else str(background)
        )

        # Generate task ID for tracking
        task_id = str(uuid.uuid4())

        # Normalize size against the (chosen) model's capabilities so the cache
        # key and persisted metadata reflect what will actually be sent to the
        # API (invalid custom sizes fall back to the supported default).
        capabilities = OpenAIProvider.SUPPORTED_MODELS.get(model)
        if capabilities is not None:
            size = OpenAIProvider._resolve_size(size, capabilities, model)

        # Check cache first (using hash of image data). `user` is excluded — it
        # must not change the cached edit.
        cache_params = {
            "image_data": image_data,
            "prompt": prompt,
            "mask_data": mask_data,
            "quality": quality,
            "size": size,
            "output_format": output_format,
            "compression": compression,
            "background": background,
            "model": model,
            "input_fidelity": input_fidelity,
            "additional_images": additional_images,
        }

        cached_result = await self.cache_manager.get_image_edit(**cache_params)
        if cached_result:
            logger.info(f"Returning cached edit result for prompt: {prompt[:50]}...")
            return cached_result

        try:
            logger.info(f"Editing image for task {task_id} with model {model}")
            response = await provider.edit_image(
                model=model,
                image_data=image_data,
                prompt=prompt,
                mask_data=mask_data,
                quality=quality,
                size=size,
                output_format=output_format,
                compression=compression,
                background=background,
                n=1,
                input_fidelity=input_fidelity,
                additional_images=additional_images,
                user=user,
            )

            # Provider returns a normalized ImageResponse (raw image bytes).
            image_bytes = response.image_data

            # Estimate cost (quality/size-aware for gpt-image-2)
            cost_info = provider.estimate_cost(
                model, prompt, 1, quality=quality, size=size
            )

            # Prepare metadata
            metadata = {
                "task_id": task_id,
                "operation": "edit",
                "prompt": prompt,
                "has_mask": mask_data is not None,
                "parameters": {
                    "model": model,
                    "quality": quality,
                    "size": size,
                    "output_format": output_format,
                    "compression": compression,
                    "background": background,
                    "input_fidelity": input_fidelity,
                },
                "cost_info": cost_info,
                "provider_metadata": response.metadata,
            }

            # Save to local storage
            image_id, image_path = await self.storage_manager.save_image(
                image_data=image_bytes, metadata=metadata, file_format=output_format
            )

            # Build image URL instead of base64 data
            image_url = self._build_image_url(image_id, output_format)

            # Prepare result
            result = {
                "task_id": task_id,
                "image_id": image_id,
                "image_url": image_url,  # URL instead of base64 data
                "resource_uri": f"generated-images://{image_id}",
                "operation": "edit",
                "metadata": {
                    "model": model,
                    "size": size,
                    "quality": quality,
                    "output_format": output_format,
                    "background": background,
                    "input_fidelity": input_fidelity,
                    "input_image_count": 1 + len(additional_images or []),
                    "prompt": prompt,
                    "has_mask": mask_data is not None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "cost_estimate": cost_info.get("estimated_cost_usd"),
                    "file_size_bytes": len(image_bytes),
                    "dimensions": size,
                    "format": output_format.upper(),
                    "local_path": str(image_path),
                },
            }

            # Cache the result with URL
            await self.cache_manager.set_image_edit(result, **cache_params)

            logger.info(f"Successfully edited image {image_id} for task {task_id}")
            return result

        except Exception as e:
            logger.error(f"Error editing image for task {task_id}: {e}")
            raise RuntimeError(f"Image editing failed: {str(e)}") from e
