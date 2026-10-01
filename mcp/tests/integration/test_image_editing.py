"""Smoke test for image editing (1 API call).

Verifies the complete editing pipeline works end-to-end.
Requires a valid API key, existing images in storage/,
and will incur a small cost (~$0.05).

Run with: pytest tests/integration/test_image_editing.py -v -s
"""

import base64
from pathlib import Path

import pytest

from omniapi_mcp.config.settings import Settings, StorageSettings
from omniapi_mcp.storage.manager import ImageStorageManager
from omniapi_mcp.tools.image_editing import ImageEditingTool
from omniapi_mcp.utils.cache import CacheManager
from omniapi_mcp.utils.openai_client import OpenAIClientManager


@pytest.mark.integration
@pytest.mark.slow
class TestImageEditing:
    """Single smoke test for image editing."""

    @pytest.fixture
    async def editing_tool(self, tmp_path):
        """Create image editing tool with OpenAI client."""
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
        openai_client = OpenAIClientManager(settings.providers.openai)

        tool = ImageEditingTool(
            storage_manager=storage_manager,
            cache_manager=cache_manager,
            settings=settings,
            openai_client=openai_client,
        )

        yield tool

        if hasattr(storage_manager, 'close'):
            await storage_manager.close()
        if hasattr(cache_manager, 'close'):
            await cache_manager.close()

    async def test_edit_image(self, editing_tool):
        """Smoke test: edit a single image to verify the pipeline works."""
        project_storage = Path(__file__).parent.parent.parent / "storage"
        image_files = list((project_storage / "images").rglob("*.png"))
        if not image_files:
            pytest.skip("No existing images found to edit")

        with open(image_files[0], "rb") as f:
            image_bytes = f.read()

        image_b64 = base64.b64encode(image_bytes).decode()
        image_data = f"data:image/png;base64,{image_b64}"

        result = await editing_tool.edit(
            image_data=image_data,
            prompt="Add a small star in the corner",
            size="1024x1024",
            quality="low",
        )

        assert result['image_id']
        assert result['operation'] == 'edit'


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
