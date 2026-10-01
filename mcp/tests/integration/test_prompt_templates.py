"""Prompt template tests (pure logic, no API calls).

These tests verify template listing, rendering, and metadata
without making any real API calls.

Run with: pytest tests/integration/test_prompt_templates.py -v -s
"""

import pytest

from omniapi_mcp.prompts.template_manager import template_manager


class TestPromptTemplates:
    """Test prompt template functionality (no API calls)."""

    def test_list_all_templates(self):
        """Test listing all available templates."""
        templates = template_manager.list_templates()

        assert len(templates) > 0
        assert all('id' in t for t in templates)

    def test_list_templates_by_category(self):
        """Test listing templates organized by category."""
        categories = template_manager.list_templates_by_category()

        assert len(categories) > 0

    def test_get_template_details(self):
        """Test getting detailed information about a template."""
        template_id = "creative_image"
        details = template_manager.get_template_details(template_id)

        assert details is not None
        assert 'parameters' in details

    def test_render_template(self):
        """Test rendering a template with parameters."""
        template_id = "creative_image"
        params = {
            "subject": "a majestic dragon",
            "style": "fantasy digital art",
            "mood": "epic and mysterious",
            "color_palette": "deep blues and purples with gold accents"
        }

        rendered, metadata = template_manager.render_template(template_id, **params)

        assert rendered is not None
        assert len(rendered) > 0
        assert "dragon" in rendered.lower()


if __name__ == "__main__":
    """Allow running tests directly with python."""
    pytest.main([__file__, "-v", "-s"])
