"""Smoke test for real image generation (1 API call).

Verifies the complete generation pipeline works end-to-end.
Requires a valid API key and will incur a small cost (~$0.05).

Run with: pytest tests/integration/test_real_image_generation.py -v -s
"""

from pathlib import Path

import pytest

from omniapi_mcp.config.settings import Settings, StorageSettings
from omniapi_mcp.storage.manager import ImageStorageManager
from omniapi_mcp.tools.image_generation import ImageGenerationTool
from omniapi_mcp.types.enums import ImageQuality, ImageSize, ImageStyle
from omniapi_mcp.utils.cache import CacheManager


@pytest.mark.integration
@pytest.mark.slow
class TestRealImageGeneration:
    """Single smoke test for real image generation."""

    @pytest.fixture
    async def real_tool(self, tmp_path):
        """Create a real image generation tool with actual settings."""
        settings = Settings()

        project_storage = Path(__file__).parent.parent.parent / "storage"
        storage_settings = StorageSettings(
            base_path=str(project_storage),
            retention_days=30,
            max_size_gb=10.0,
            cleanup_interval_hours=24,
        )

        storage_manager = ImageStorageManager(settings=storage_settings)
        cache_manager = CacheManager(settings=settings.cache)

        tool = ImageGenerationTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings,
        )

        yield tool

        if hasattr(storage_manager, 'close'):
            await storage_manager.close()
        if hasattr(cache_manager, 'close'):
            await cache_manager.close()

    async def test_generate_image(self, real_tool):
        """Smoke test: generate a single image to verify the pipeline works."""
        result = await real_tool.generate(
            prompt="A simple red circle on a white background",
            style=ImageStyle.NATURAL,
            quality=ImageQuality.LOW,
            size=ImageSize.SQUARE,
        )

        assert result['image_id']
        assert result['task_id']
        assert result['image_url']
        assert result['metadata']['prompt']


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
