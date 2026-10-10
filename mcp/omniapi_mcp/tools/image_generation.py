"""Image generation tool implementation."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..config.settings import Settings
from ..providers.base import ProviderConfig, ProviderError
from ..providers.gemini import GeminiProvider
from ..providers.openai import OpenAIProvider
from ..providers.openrouter_images import OpenRouterImageProvider
from ..providers.registry import ProviderRegistry
from ..storage.manager import ImageStorageManager
from ..types.enums import (
    BackgroundType,
    ImageQuality,
    ImageSize,
    ImageStyle,
    ModerationLevel,
    OutputFormat,
)
from ..utils.cache import CacheManager
from ..utils.path_utils import build_image_access_url

logger = logging.getLogger(__name__)


class ImageGenerationTool:
    """Tool for generating images using multiple LLM providers."""

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
            openai_client: Optional OpenAI client.
        """
        self.settings = settings
        self.storage_manager = storage_manager
        self.cache_manager = cache_manager
        self.provider_registry = ProviderRegistry()
        self.openai_client = openai_client
        # Providers constructed here, registered lazily on first use (register
        # is async; __init__ is not).
        self._pending_providers: list = []
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Initialize and register all available providers."""
        # Initialize OpenAI provider
        openai_provider = getattr(self.settings.providers, "openai", None)
        if openai_provider and openai_provider.enabled and openai_provider.api_key:
            try:
                openai_config = ProviderConfig(
                    api_key=self.settings.providers.openai.api_key,
                    organization=self.settings.providers.openai.organization,
                    base_url=self.settings.providers.openai.base_url,
                    timeout=self.settings.providers.openai.timeout,
                    max_retries=self.settings.providers.openai.max_retries,
                    enabled=self.settings.providers.openai.enabled,
                )
                openai_provider = OpenAIProvider(openai_config)
                # Register provider asynchronously - we'll handle this later
                self._register_provider_async(openai_provider)
                logger.info("OpenAI provider initialized successfully")

            except Exception as e:
                logger.error(f"Failed to initialize OpenAI provider: {e}")

        # Initialize Gemini provider
        gemini_provider = getattr(self.settings.providers, "gemini", None)
        if gemini_provider and gemini_provider.enabled and gemini_provider.api_key:
            try:
                gemini_config = ProviderConfig(
                    api_key=self.settings.providers.gemini.api_key,
                    base_url=self.settings.providers.gemini.base_url,
                    timeout=self.settings.providers.gemini.timeout,
                    max_retries=self.settings.providers.gemini.max_retries,
                    enabled=self.settings.providers.gemini.enabled,
                )
                gemini_provider = GeminiProvider(gemini_config)

                # Register provider asynchronously - we'll handle this later
                self._register_provider_async(gemini_provider)
                logger.info("Gemini provider initialized successfully")

            except Exception as e:
                logger.error(f"Failed to initialize Gemini provider: {e}")

        # OpenRouter's image models (1.4-M2): the same key as its chat models
        openrouter = getattr(self.settings.providers, "openrouter", None)
        if openrouter and openrouter.enabled and openrouter.api_key:
            try:
                self._register_provider_async(
                    OpenRouterImageProvider(
                        ProviderConfig(
                            api_key=openrouter.api_key,
                            base_url=openrouter.base_url,
                            timeout=openrouter.timeout,
                            max_retries=openrouter.max_retries,
                            enabled=openrouter.enabled,
                        )
                    )
                )
                logger.info("OpenRouter image provider initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize OpenRouter image provider: {e}")

    def _register_provider_async(self, provider) -> None:
        """Store provider for async registration later."""
        self._pending_providers.append(provider)

    async def ensure_providers_registered(self) -> None:
        """Register any pending providers with the routing registry.

        Public because the server (health_check / list_available_models) needs
        to force registration before inspecting the registry.
        """
        for provider in self._pending_providers:
            try:
                await self.provider_registry.register_provider(provider)
            except Exception as e:
                logger.error(f"Failed to register provider {provider.name}: {e}")
        # Clear pending providers after registration
        self._pending_providers = []

    # Backwards-compatible private alias (older callers/tests).
    _ensure_providers_registered = ensure_providers_registered

    def get_openai_provider(self) -> Optional[OpenAIProvider]:
        """Return the OpenAI provider instance if configured (registered or
        pending). Lets ImageEditingTool share one provider/HTTP client instead
        of building a duplicate."""
        for provider in self._pending_providers:
            if isinstance(provider, OpenAIProvider):
                return provider
        provider = self.provider_registry.get_provider("openai")
        return provider if isinstance(provider, OpenAIProvider) else None

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

    def _get_default_model(self) -> str:
        """Get the default model based on configuration and available providers."""
        # First try the configured default model
        configured_default = self.settings.images.default_model

        # Check if the configured default is available
        if self.provider_registry.is_model_supported(configured_default):
            return configured_default

        # If configured default is not available, try to find any available model
        available_models = self.provider_registry.get_supported_models()
        if available_models:
            # Return the first available model
            return next(iter(available_models))

        # If no models are available, return the configured default anyway
        # This will cause a proper error message later
        return configured_default

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        quality: ImageQuality | str = ImageQuality.AUTO,
        size: ImageSize | str = ImageSize.LANDSCAPE,
        style: ImageStyle | str = ImageStyle.VIVID,
        moderation: ModerationLevel | str = ModerationLevel.AUTO,
        output_format: OutputFormat | str = OutputFormat.PNG,
        compression: int = 100,
        background: BackgroundType | str = BackgroundType.AUTO,
        n: int = 1,
        user: Optional[str] = None,
        image_size: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        person_generation: Optional[str] = None,
        seed: Optional[int] = None,
        safety_filter_level: Optional[str] = None,
        enhance_prompt: Optional[bool] = None,
        guidance_scale: Optional[float] = None,
    ) -> dict[str, Any]:
        """Generate an image from a text prompt using the specified or default model.

        Provider-specific extras (``user`` for OpenAI; ``image_size``,
        ``aspect_ratio``, ``person_generation``, ``seed``, ``safety_filter_level``,
        ``enhance_prompt``, ``guidance_scale`` for Gemini) are forwarded only when
        set; each provider ignores the ones it does not support.
        """

        # Ensure providers are registered
        await self.ensure_providers_registered()

        # Convert enums to string values for API calls
        quality_str = (
            quality.value if isinstance(quality, ImageQuality) else str(quality)
        )
        size_str = size.value if isinstance(size, ImageSize) else str(size)
        style_str = style.value if isinstance(style, ImageStyle) else str(style)
        moderation_str = (
            moderation.value
            if isinstance(moderation, ModerationLevel)
            else str(moderation)
        )
        output_format_str = (
            output_format.value
            if isinstance(output_format, OutputFormat)
            else str(output_format)
        )
        background_str = (
            background.value
            if isinstance(background, BackgroundType)
            else str(background)
        )

        # Determine which model to use
        target_model = model or self._get_default_model()

        # Get the provider for this model
        provider = self.provider_registry.get_provider_for_model(target_model)
        if not provider:
            available_models = list(self.provider_registry.get_supported_models())
            if not available_models:
                raise RuntimeError(
                    "No providers are available. Please ensure you have "
                    "configured at least one provider with a valid API key. "
                    "Set PROVIDERS__OPENAI__API_KEY for OpenAI, "
                    "PROVIDERS__GEMINI__API_KEY for Gemini or "
                    "PROVIDERS__OPENROUTER__API_KEY for OpenRouter's image models."
                )
            else:
                raise RuntimeError(
                    f"No provider found for model '{target_model}'. "
                    f"Available models: {available_models}. "
                    "Use list_available_models() to see detailed information."
                )

        if not provider.is_available():
            raise RuntimeError(
                f"Provider '{provider.name}' for model '{target_model}' is not "
                "available or misconfigured"
            )

        # Generate task ID for tracking
        task_id = str(uuid.uuid4())

        # Provider-specific extras forwarded only when set. `user` is OpenAI-only
        # and excluded from the cache key (it must not change the cached image);
        # the rest affect the output so they DO belong in the key.
        provider_kwargs: dict[str, Any] = {}
        if user:
            provider_kwargs["user"] = user
        if image_size:
            provider_kwargs["image_size"] = image_size
        if aspect_ratio:
            provider_kwargs["aspect_ratio"] = aspect_ratio
        if person_generation:
            provider_kwargs["person_generation"] = person_generation
        if seed is not None:
            provider_kwargs["seed"] = seed
        if safety_filter_level:
            provider_kwargs["safety_filter_level"] = safety_filter_level
        if enhance_prompt is not None:
            provider_kwargs["enhance_prompt"] = enhance_prompt
        if guidance_scale is not None:
            provider_kwargs["guidance_scale"] = guidance_scale

        # Build parameters for caching and validation
        params = {
            "prompt": prompt,
            "quality": quality_str,
            "size": size_str,
            "style": style_str,
            "moderation": moderation_str,
            "output_format": output_format_str,
            "compression": compression,
            "background": background_str,
            "model": target_model,
            "n": n,
            **{k: v for k, v in provider_kwargs.items() if k != "user"},
        }

        # Check cache first
        cached_result = await self.cache_manager.get_image_generation(**params)
        if cached_result:
            logger.info(f"Returning cached result for prompt: {prompt[:50]}...")
            return cached_result

        try:
            # Validate parameters for the specific model
            validated_params = self.provider_registry.validate_model_request(
                target_model, params
            )

            # Generate image using the provider
            logger.info(
                f"Generating image for task {task_id} using model {target_model} "
                f"via {provider.name}"
            )

            effective_n = max(1, int(validated_params.get("n", n)))
            provider_response = await provider.generate_image(
                model=target_model,
                prompt=prompt,
                quality=validated_params.get("quality", quality_str),
                size=validated_params.get("size", size_str),
                style=validated_params.get("style", style_str),
                moderation=validated_params.get("moderation", moderation_str),
                output_format=validated_params.get("output_format", output_format_str),
                compression=validated_params.get("compression", compression),
                background=validated_params.get("background", background_str),
                n=effective_n,
                **provider_kwargs,
            )

            # All generated images (one entry unless the provider returned a
            # real list of >1). Falls back to the single primary image.
            imgs = provider_response.images
            image_bytes_list = (
                imgs if isinstance(imgs, list) and imgs
                else [provider_response.image_data]
            )

            # What it cost: the provider's own report of this call when it gives one
            # (OpenRouter's usage.cost), else the estimate (quality/size-aware for gpt-image-2)
            actual = (provider_response.metadata or {}).get("cost_usd")
            if isinstance(actual, (int, float)):
                cost_info = {"provider": provider.name, "model": target_model, "estimated_cost_usd": float(actual),
                             "currency": "USD", "actual": True}
            else:
                cost_info = provider.estimate_cost(
                    target_model,
                    prompt,
                    len(image_bytes_list),
                    quality=validated_params.get("quality", quality_str),
                    size=validated_params.get("size", size_str),
                )

            # Prepare metadata
            metadata = {
                "task_id": task_id,
                "prompt": prompt,
                "model": target_model,
                "provider": provider.name,
                "parameters": validated_params,
                "cost_info": cost_info,
                "provider_metadata": provider_response.metadata,
            }

            # Save every generated image to local storage (in the format it came
            # back in, when the provider says: OpenRouter's models pick their own)
            file_format = (provider_response.metadata or {}).get("file_format") or validated_params.get("output_format", output_format_str)
            saved = []
            for img_bytes in image_bytes_list:
                image_id, _ = await self.storage_manager.save_image(
                    image_data=img_bytes,
                    metadata=metadata,
                    file_format=file_format,
                )
                saved.append(
                    {
                        "image_id": image_id,
                        "image_url": self._build_image_url(image_id, file_format),
                        "resource_uri": f"generated-images://{image_id}",
                        "file_size_bytes": len(img_bytes),
                    }
                )

            primary = saved[0]
            # Prepare result — single-image shape stays unchanged for back-compat;
            # an `images` list (+ count) is added only when n>1.
            result = {
                "task_id": task_id,
                "image_id": primary["image_id"],
                "image_url": primary["image_url"],
                "resource_uri": primary["resource_uri"],
                "metadata": {
                    "model": target_model,
                    "provider": provider.name,
                    "size": validated_params.get("size", size_str),
                    "quality": validated_params.get("quality", quality_str),
                    "style": validated_params.get("style", style_str),
                    "moderation": validated_params.get("moderation", moderation_str),
                    "output_format": file_format,
                    "background": validated_params.get("background", background_str),
                    "prompt": prompt,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "cost_estimate": cost_info.get("estimated_cost_usd"),
                    # true unless the provider reported what this call cost
                    "cost_estimated": not cost_info.get("actual"),
                    "file_size_bytes": primary["file_size_bytes"],
                    "dimensions": validated_params.get("size", size_str),
                    "format": file_format.upper(),
                    "image_count": len(saved),
                },
            }
            if len(saved) > 1:
                result["images"] = saved
                result["count"] = len(saved)

            # Cache the result
            await self.cache_manager.set_image_generation(result, **params)

            logger.info(
                f"Successfully generated {len(saved)} image(s) "
                f"({primary['image_id']}) for task {task_id} using {provider.name}"
            )
            return result

        except ProviderError as e:
            logger.error(f"Provider error for task {task_id}: {e}")
            raise RuntimeError(f"Image generation failed: {str(e)}") from e
        except Exception as e:
            logger.error(f"Error generating image for task {task_id}: {e}")
            raise RuntimeError(f"Image generation failed: {str(e)}") from e

    def get_supported_models(self) -> dict[str, Any]:
        """Get information about all supported models."""
        return self.provider_registry.get_registry_stats()

    def get_available_providers(self) -> list[str]:
        """Get list of available provider names."""
        return [
            provider.name
            for provider in self.provider_registry.get_available_providers()
        ]
