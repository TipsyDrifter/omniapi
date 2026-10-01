import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omniapi_mcp.config.settings import ProvidersSettings, Settings
from omniapi_mcp.providers.base import ImageResponse, ProviderError
from omniapi_mcp.tools.image_editing import ImageEditingTool
from omniapi_mcp.tools.image_generation import ImageGenerationTool


def _edit_response(b64: str) -> ImageResponse:
    """Build a normalized provider ImageResponse from a base64 string."""
    return ImageResponse(
        image_data=base64.b64decode(b64), metadata={"provider": "openai"}
    )


@pytest.fixture
def mock_generation_tool(storage_manager, cache_manager, mock_settings):
    """Fixture for creating an ImageGenerationTool with mocked dependencies."""
    tool = ImageGenerationTool(
        storage_manager=storage_manager,
        cache_manager=cache_manager,
        settings=mock_settings,
    )
    # Mock the provider registry method
    tool.provider_registry.get_provider_for_model = MagicMock()
    return tool


@pytest.fixture
def mock_editing_tool(storage_manager, cache_manager, mock_settings):
    """Fixture for creating an ImageEditingTool with mocked dependencies."""
    tool = ImageEditingTool(
        storage_manager=storage_manager,
        cache_manager=cache_manager,
        settings=mock_settings,
    )
    # Replace the real OpenAI provider with a mock. edit_image returns a
    # normalized ImageResponse (raw bytes); estimate_cost is sync.
    tool._provider = MagicMock()
    tool._provider.edit_image = AsyncMock()
    tool._provider.estimate_cost = MagicMock(
        return_value={"estimated_cost_usd": 0.01}
    )
    return tool


class TestImageGenerationTool:
    """Unit tests for the ImageGenerationTool."""

    @pytest.mark.asyncio
    async def test_generate_image_basic(self, mock_generation_tool):
        """Test basic image generation."""
        # Use high-level mocking by patching the cache manager to return None
        # (cache miss) and the storage manager to save the image
        mock_generation_tool.cache_manager.get_image_generation = AsyncMock(
            return_value=None
        )
        mock_generation_tool.cache_manager.set_image_generation = AsyncMock()
        mock_generation_tool.storage_manager.save_image = AsyncMock(
            return_value=("test_id", "/path/to/image")
        )

        # Mock the _build_image_url method
        mock_generation_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/test_id.png"
        )

        # Mock provider registry to skip complex provider logic
        mock_generation_tool.provider_registry.get_supported_models = MagicMock(
            return_value=["gpt-image-2"]
        )
        mock_generation_tool._get_default_model = MagicMock(
            return_value="gpt-image-2"
        )

        # Use MagicMock for the provider object to mock non-async methods and
        # attributes. Use AsyncMock for async methods like generate_image (see below).
        mock_provider = MagicMock()
        mock_provider.name = "test-provider"
        mock_provider.is_available.return_value = True
        mock_provider.generate_image = AsyncMock(
            return_value=MagicMock(
                image_data=b"test_image_data", metadata={"width": 1536, "height": 1024}
            )
        )
        mock_provider.estimate_cost.return_value = {"estimated_cost_usd": 0.01}

        mock_generation_tool.provider_registry.get_provider_for_model = MagicMock(
            return_value=mock_provider
        )
        mock_generation_tool.provider_registry.validate_model_request = MagicMock(
            return_value={
                "quality": "auto",
                "size": "1536x1024",
                "style": "vivid",
                "moderation": "auto",
                "output_format": "png",
                "compression": 100,
                "background": "auto",
            }
        )

        result = await mock_generation_tool.generate(prompt="test image")

        assert "image_id" in result
        assert result["image_id"] == "test_id"
        assert "image_url" in result
        assert result["image_url"] == "http://localhost:3001/images/test_id.png"
        mock_provider.generate_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_image_with_defaults(self, mock_generation_tool):
        """Test image generation using default settings."""
        # Mock dependencies
        mock_generation_tool.cache_manager.get_image_generation = AsyncMock(
            return_value=None
        )
        mock_generation_tool.cache_manager.set_image_generation = AsyncMock()
        mock_generation_tool.storage_manager.save_image = AsyncMock(
            return_value=("test_id", "/path/to/image")
        )
        mock_generation_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/test_id.png"
        )
        mock_generation_tool.provider_registry.get_supported_models = MagicMock(
            return_value=["gpt-image-2"]
        )
        mock_generation_tool._get_default_model = MagicMock(
            return_value="gpt-image-2"
        )

        # Mock provider
        mock_provider = MagicMock()
        mock_provider.name = "test-provider"
        mock_provider.is_available.return_value = True
        mock_provider.generate_image = AsyncMock(
            return_value=MagicMock(
                image_data=b"test_image_data", metadata={"width": 1024, "height": 1024}
            )
        )
        mock_provider.estimate_cost.return_value = {"estimated_cost_usd": 0.01}

        mock_generation_tool.provider_registry.get_provider_for_model = MagicMock(
            return_value=mock_provider
        )
        mock_generation_tool.provider_registry.validate_model_request = MagicMock(
            return_value={
                "quality": "auto",
                "size": "1024x1024",
                "style": "vivid",
                "moderation": "auto",
                "output_format": "png",
                "compression": 100,
                "background": "auto",
            }
        )

        result = await mock_generation_tool.generate(prompt="test image")

        assert result["metadata"]["quality"] == "auto"
        assert result["metadata"]["size"] == "1024x1024"

    @pytest.mark.asyncio
    async def test_generate_image_with_cache_hit(
        self, mock_generation_tool, cache_manager
    ):
        """Test that a cached result is returned on a cache hit."""
        cache_manager.get_image_generation = AsyncMock(
            return_value={"image_id": "cached_id"}
        )

        result = await mock_generation_tool.generate(prompt="cached image")

        assert result["image_id"] == "cached_id"
        cache_manager.get_image_generation.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_image_cache_miss_and_store(
        self, mock_generation_tool, cache_manager
    ):
        """Test that a result is cached on a cache miss."""
        cache_manager.get_image_generation = AsyncMock(return_value=None)
        cache_manager.set_image_generation = AsyncMock()

        # Mock other dependencies
        mock_generation_tool.storage_manager.save_image = AsyncMock(
            return_value=("test_id", "/path/to/image")
        )
        mock_generation_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/test_id.png"
        )
        mock_generation_tool.provider_registry.get_supported_models = MagicMock(
            return_value=["gpt-image-2"]
        )
        mock_generation_tool._get_default_model = MagicMock(
            return_value="gpt-image-2"
        )

        # Mock provider
        mock_provider = MagicMock()
        mock_provider.name = "test-provider"
        mock_provider.is_available.return_value = True
        mock_provider.generate_image = AsyncMock(
            return_value=MagicMock(
                image_data=b"test_image_data", metadata={"width": 1536, "height": 1024}
            )
        )
        mock_provider.estimate_cost.return_value = {"estimated_cost_usd": 0.01}

        mock_generation_tool.provider_registry.get_provider_for_model = MagicMock(
            return_value=mock_provider
        )
        mock_generation_tool.provider_registry.validate_model_request = MagicMock(
            return_value={
                "quality": "auto",
                "size": "1536x1024",
                "style": "vivid",
                "moderation": "auto",
                "output_format": "png",
                "compression": 100,
                "background": "auto",
            }
        )

        await mock_generation_tool.generate(prompt="new image")

        cache_manager.set_image_generation.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_image_with_format_conversion(self, mock_generation_tool):
        """Test image generation with a non-default output format."""
        # Mock dependencies
        mock_generation_tool.cache_manager.get_image_generation = AsyncMock(
            return_value=None
        )
        mock_generation_tool.cache_manager.set_image_generation = AsyncMock()
        mock_generation_tool.storage_manager.save_image = AsyncMock(
            return_value=("test_id", "/path/to/image")
        )
        mock_generation_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/test_id.jpeg"
        )
        mock_generation_tool.provider_registry.get_supported_models = MagicMock(
            return_value=["gpt-image-2"]
        )
        mock_generation_tool._get_default_model = MagicMock(
            return_value="gpt-image-2"
        )

        # Mock provider
        mock_provider = MagicMock()
        mock_provider.name = "test-provider"
        mock_provider.is_available.return_value = True
        mock_provider.generate_image = AsyncMock(
            return_value=MagicMock(
                image_data=b"test_image_data", metadata={"width": 1536, "height": 1024}
            )
        )
        mock_provider.estimate_cost.return_value = {"estimated_cost_usd": 0.01}

        mock_generation_tool.provider_registry.get_provider_for_model = MagicMock(
            return_value=mock_provider
        )
        mock_generation_tool.provider_registry.validate_model_request = MagicMock(
            return_value={
                "quality": "auto",
                "size": "1536x1024",
                "style": "vivid",
                "moderation": "auto",
                "output_format": "jpeg",
                "compression": 100,
                "background": "auto",
            }
        )

        result = await mock_generation_tool.generate(
            prompt="jpeg image", output_format="jpeg"
        )

        assert result["metadata"]["output_format"] == "jpeg"

    @pytest.mark.asyncio
    async def test_generate_image_error_handling(self, mock_generation_tool):
        """Test that provider errors are handled correctly."""
        # Mock dependencies
        mock_generation_tool.cache_manager.get_image_generation = AsyncMock(
            return_value=None
        )
        mock_generation_tool.provider_registry.get_supported_models = MagicMock(
            return_value=["gpt-image-2"]
        )
        mock_generation_tool._get_default_model = MagicMock(
            return_value="gpt-image-2"
        )

        # Mock provider with error
        mock_provider = MagicMock()
        mock_provider.name = "test-provider"
        mock_provider.is_available.return_value = True
        mock_provider.generate_image = AsyncMock(
            side_effect=ProviderError("API Error", "test-provider")
        )

        mock_generation_tool.provider_registry.get_provider_for_model = MagicMock(
            return_value=mock_provider
        )
        mock_generation_tool.provider_registry.validate_model_request = MagicMock(
            return_value={
                "quality": "auto",
                "size": "1536x1024",
                "style": "vivid",
                "moderation": "auto",
                "output_format": "png",
                "compression": 100,
                "background": "auto",
            }
        )

        with pytest.raises(
            RuntimeError, match="Image generation failed: \\[test-provider\\] API Error"
        ):
            await mock_generation_tool.generate(prompt="error test")

    @pytest.mark.asyncio
    async def test_generate_image_parameter_validation(self, mock_generation_tool):
        """Test that invalid parameters are caught."""
        with pytest.raises(RuntimeError):
            await mock_generation_tool.generate(prompt="test", model="invalid-model")

    @pytest.mark.asyncio
    async def test_generate_image_storage_integration(
        self, mock_generation_tool, storage_manager
    ):
        """Test that the generated image is stored correctly."""
        storage_manager.save_image = AsyncMock(
            return_value=("stored_id", "/path/to/image")
        )

        # Mock other dependencies
        mock_generation_tool.cache_manager.get_image_generation = AsyncMock(
            return_value=None
        )
        mock_generation_tool.cache_manager.set_image_generation = AsyncMock()
        mock_generation_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/stored_id.png"
        )
        mock_generation_tool.provider_registry.get_supported_models = MagicMock(
            return_value=["gpt-image-2"]
        )
        mock_generation_tool._get_default_model = MagicMock(
            return_value="gpt-image-2"
        )

        # Mock provider
        mock_provider = MagicMock()
        mock_provider.name = "test-provider"
        mock_provider.is_available.return_value = True
        mock_provider.generate_image = AsyncMock(
            return_value=MagicMock(
                image_data=b"test_image_data", metadata={"width": 1536, "height": 1024}
            )
        )
        mock_provider.estimate_cost.return_value = {"estimated_cost_usd": 0.01}

        mock_generation_tool.provider_registry.get_provider_for_model = MagicMock(
            return_value=mock_provider
        )
        mock_generation_tool.provider_registry.validate_model_request = MagicMock(
            return_value={
                "quality": "auto",
                "size": "1536x1024",
                "style": "vivid",
                "moderation": "auto",
                "output_format": "png",
                "compression": 100,
                "background": "auto",
            }
        )

        result = await mock_generation_tool.generate(prompt="storage test")

        assert result["image_id"] == "stored_id"
        storage_manager.save_image.assert_called_once()

    def test_works_with_disabled_providers(
        self, storage_manager, cache_manager, mock_openai_settings
    ):
        """Test that ImageGenerationTool works with some providers disabled."""
        # Create settings with only OpenAI provider enabled
        settings = Settings()
        settings.providers = ProvidersSettings()
        settings.providers.openai = mock_openai_settings
        # Gemini provider is None/missing - this should be fine

        # This should NOT raise an error - tool should work with just OpenAI
        tool = ImageGenerationTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings
        )
        assert tool is not None

    def test_missing_all_providers_configuration(
        self, storage_manager, cache_manager
    ):
        """Test that missing all provider configurations works.

        When no providers are enabled, tool creation should succeed.
        """
        # Create settings with no enabled providers
        settings = Settings()
        settings.providers = ProvidersSettings()
        # Both providers are None or disabled

        # This should NOT raise an error during initialization
        # The error should only occur when trying to generate images
        tool = ImageGenerationTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings
        )
        assert tool is not None  # Tool creation should succeed


class TestImageEditingTool:
    """Unit tests for the ImageEditingTool."""

    @pytest.mark.asyncio
    async def test_edit_image_basic(self, mock_editing_tool, sample_image_data):
        """Test basic image editing."""
        # Mock dependencies for editing tool
        mock_editing_tool.cache_manager.get_image_edit = AsyncMock(return_value=None)
        mock_editing_tool.cache_manager.set_image_edit = AsyncMock()
        mock_editing_tool.storage_manager.save_image = AsyncMock(
            return_value=("test_id", "/path/to/image")
        )
        mock_editing_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/test_id.png"
        )

        # Mock response from edit_image
        mock_response = _edit_response(sample_image_data.split(",", 1)[1])

        mock_editing_tool._provider.edit_image.return_value = mock_response
        mock_editing_tool._provider.estimate_cost.return_value = {
            "estimated_cost_usd": 0.01
        }

        result = await mock_editing_tool.edit(
            image_data=sample_image_data, prompt="edit test"
        )

        assert "image_id" in result
        assert result["image_id"] == "test_id"
        assert "image_url" in result
        mock_editing_tool._provider.edit_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_edit_image_normalizes_enum_params(
        self, mock_editing_tool, sample_image_data
    ):
        """Enum members from the server validators must reach the provider as
        plain strings — form-encoding an Enum sends 'ImageQuality.HIGH' and the
        OpenAI API rejects it (regression test)."""
        from omniapi_mcp.types import (
            BackgroundType,
            ImageQuality,
            ImageSize,
            OutputFormat,
        )

        mock_response = _edit_response(sample_image_data.split(",", 1)[1])
        mock_editing_tool._provider.edit_image.return_value = mock_response
        mock_editing_tool._provider.estimate_cost.return_value = {
            "estimated_cost_usd": 0.01
        }

        await mock_editing_tool.edit(
            image_data=sample_image_data,
            prompt="enum normalization test",
            quality=ImageQuality.HIGH,
            size=ImageSize.SQUARE,
            output_format=OutputFormat.PNG,
            background=BackgroundType.TRANSPARENT,
        )

        call_kwargs = mock_editing_tool._provider.edit_image.call_args.kwargs
        assert call_kwargs["quality"] == "high"
        assert call_kwargs["size"] == "1024x1024"
        assert call_kwargs["output_format"] == "png"
        assert call_kwargs["background"] == "transparent"

    @pytest.mark.asyncio
    async def test_edit_image_with_mask(self, mock_editing_tool, sample_image_data):
        """Test image editing with a mask."""
        # Configure all mocks with proper values
        sample_b64 = sample_image_data.split(",", 1)[1]

        # Create mock response object
        mock_response = _edit_response(sample_b64)

        mock_editing_tool._provider.edit_image.return_value = mock_response
        mock_editing_tool._provider.estimate_cost.return_value = {
            "estimated_cost_usd": 0.05,
            "tokens_used": 100,
        }

        result = await mock_editing_tool.edit(
            image_data=sample_image_data,
            prompt="edit test",
            mask_data=sample_image_data,
        )

        assert "image_id" in result
        mock_editing_tool._provider.edit_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_edit_image_without_mask(self, mock_editing_tool, sample_image_data):
        """Test image editing without a mask."""
        # Configure all mocks with proper values
        sample_b64 = sample_image_data.split(",", 1)[1]

        # Create mock response object
        mock_response = _edit_response(sample_b64)

        mock_editing_tool._provider.edit_image.return_value = mock_response
        mock_editing_tool._provider.estimate_cost.return_value = {
            "estimated_cost_usd": 0.05,
            "tokens_used": 100,
        }

        result = await mock_editing_tool.edit(
            image_data=sample_image_data, prompt="unmasked edit"
        )

        assert result["metadata"]["has_mask"] is False

    @pytest.mark.asyncio
    async def test_edit_image_format_conversion(
        self, mock_editing_tool, sample_image_data
    ):
        """Test image editing with a non-default output format."""
        # Configure all mocks with proper values
        sample_b64 = sample_image_data.split(",", 1)[1]

        # Create mock response object
        mock_response = _edit_response(sample_b64)

        mock_editing_tool._provider.edit_image.return_value = mock_response
        mock_editing_tool._provider.estimate_cost.return_value = {
            "estimated_cost_usd": 0.05,
            "tokens_used": 100,
        }

        result = await mock_editing_tool.edit(
            image_data=sample_image_data, prompt="jpeg edit", output_format="jpeg"
        )

        assert result["metadata"]["output_format"] == "jpeg"

    @pytest.mark.asyncio
    async def test_edit_image_parameter_validation(self, mock_editing_tool):
        """Provider-level rejection of bad input surfaces as a RuntimeError.

        Image-content validation now lives in the provider (prepare_image_upload)
        and the server tool (validate_base64_image), so at the tool layer a bad
        input manifests as a provider error — which edit() must wrap in a
        RuntimeError.
        """
        mock_editing_tool._provider.edit_image.side_effect = ProviderError(
            "Invalid image data", provider_name="openai"
        )

        with pytest.raises(RuntimeError, match="Image editing failed"):
            await mock_editing_tool.edit(image_data="", prompt="test")

    @pytest.mark.asyncio
    async def test_edit_image_data_url_processing(
        self, mock_editing_tool, sample_image_data
    ):
        """Test that data URLs are processed correctly."""
        # Configure all mocks with proper values
        sample_b64 = sample_image_data.split(",", 1)[1]

        # Create mock response object
        mock_response = _edit_response(sample_b64)

        mock_editing_tool._provider.edit_image.return_value = mock_response
        mock_editing_tool._provider.estimate_cost.return_value = {
            "estimated_cost_usd": 0.05,
            "tokens_used": 100,
        }

        await mock_editing_tool.edit(image_data=sample_image_data, prompt="test1")
        mock_editing_tool._provider.edit_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_edit_image_error_handling(
        self, mock_editing_tool, sample_image_data
    ):
        """Test that errors from the OpenAI client are handled correctly."""
        mock_editing_tool._provider.edit_image.side_effect = Exception("API Error")

        with pytest.raises(RuntimeError, match="Image editing failed: API Error"):
            await mock_editing_tool.edit(
                image_data=sample_image_data, prompt="error test"
            )

    @pytest.mark.asyncio
    async def test_edit_image_storage_integration(
        self, mock_editing_tool, storage_manager, sample_image_data
    ):
        """Test that the edited image is stored correctly."""
        storage_manager.save_image = AsyncMock(
            return_value=("edited_id", "/path/to/edited_image")
        )
        mock_editing_tool._provider.edit_image.return_value = _edit_response(
            sample_image_data.split(",", 1)[1]
        )

        result = await mock_editing_tool.edit(
            image_data=sample_image_data, prompt="storage test"
        )

        assert result["image_id"] == "edited_id"
        storage_manager.save_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_edit_invalid_custom_size_normalized_for_cache_and_metadata(
        self, mock_editing_tool, sample_image_data
    ):
        """Invalid custom sizes should be normalized before cache lookup and
        before building the returned metadata, matching what is actually sent
        to the OpenAI edits API."""
        # Prepare mocks
        sample_b64 = sample_image_data.split(",", 1)[1]
        captured_cache_params: dict[str, Any] = {}

        async def capture_get(**kwargs):
            captured_cache_params.update(kwargs)
            return None

        mock_editing_tool.cache_manager.get_image_edit = AsyncMock(
            side_effect=capture_get
        )
        mock_editing_tool.cache_manager.set_image_edit = AsyncMock()
        mock_editing_tool.storage_manager.save_image = AsyncMock(
            return_value=("edited_id", "/path/to/image")
        )
        mock_editing_tool._build_image_url = MagicMock(
            return_value="http://localhost:3001/images/edited_id.png"
        )

        # spec_set deliberately omits .size so getattr(resp, "size", size)
        # falls back to the normalized local variable.
        mock_response = _edit_response(sample_b64)
        mock_editing_tool._provider.edit_image.return_value = mock_response

        result = await mock_editing_tool.edit(
            image_data=sample_image_data,
            prompt="oversize test",
            size="9999x9999",
        )

        # Cache key reflects the normalized size ("auto"), not "9999x9999"
        assert captured_cache_params["size"] == "auto"

        # Returned metadata uses the normalized size consistently
        assert result["metadata"]["size"] == "auto"
        assert result["metadata"]["dimensions"] == "auto"

        # The call to the underlying client also used the normalized size
        call_kwargs = mock_editing_tool._provider.edit_image.call_args.kwargs
        assert call_kwargs["size"] == "auto"

    async def test_missing_openai_provider_configuration(
        self, storage_manager, cache_manager
    ):
        """Missing OpenAI config degrades gracefully instead of crashing startup.

        Since v0.4.5 the constructor no longer raises — a Gemini-only or
        music-only server must still boot with edit_image simply unavailable.
        The misconfiguration surfaces as a friendly error on edit() instead.
        """
        # Create settings with missing OpenAI provider
        settings = Settings()
        settings.providers = ProvidersSettings()  # Empty providers (no openai)

        tool = ImageEditingTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings,
        )
        assert tool._provider is None

        with pytest.raises(RuntimeError, match="No provider is configured for editing"):
            await tool.edit(image_data="aGVsbG8=", prompt="make it blue")

    def test_validate_openai_settings_helper_method(
        self, storage_manager, cache_manager, mock_openai_settings
    ):
        """Test the OpenAI settings validation helper method."""
        # First test with valid settings
        settings = Settings()
        settings.providers = ProvidersSettings()
        settings.providers.openai = mock_openai_settings

        tool = ImageEditingTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings
        )

        # Should not raise an exception when validation passes
        tool._validate_openai_settings()  # Should complete without error

        # Test with invalid settings - providers without openai
        invalid_settings = Settings()
        invalid_settings.providers = ProvidersSettings()  # Empty providers (no openai)

        # Directly test the static method with invalid settings
        with pytest.raises(ValueError, match="OpenAI provider settings are required"):
            ImageEditingTool.validate_openai_provider_settings(invalid_settings)

    def test_validate_openai_provider_settings_static_method(
        self, mock_openai_settings
    ):
        """Test the static OpenAI provider settings validation method."""
        # Test with valid settings
        valid_settings = Settings()
        valid_settings.providers = ProvidersSettings()
        valid_settings.providers.openai = mock_openai_settings

        # Should not raise exception
        ImageEditingTool.validate_openai_provider_settings(valid_settings)

        # Test with missing providers attribute
        class SettingsWithoutProviders:
            pass

        settings_no_providers = SettingsWithoutProviders()
        with pytest.raises(
            ValueError, match="Settings must have a 'providers' attribute"
        ):
            ImageEditingTool.validate_openai_provider_settings(settings_no_providers)

        # Test with None providers
        settings_none_providers = Settings()
        settings_none_providers.providers = None
        with pytest.raises(
            ValueError, match="Settings must have a 'providers' attribute"
        ):
            ImageEditingTool.validate_openai_provider_settings(settings_none_providers)

        # Test with missing openai in providers
        settings_no_openai = Settings()
        settings_no_openai.providers = ProvidersSettings()
        with pytest.raises(ValueError, match="OpenAI provider settings are required"):
            ImageEditingTool.validate_openai_provider_settings(settings_no_openai)
