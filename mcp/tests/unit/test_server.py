"""Unit tests for server endpoints (health check, server info)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestHealthCheck:
    """Test the health_check tool function."""

    def _make_provider(self, name, healthy=True):
        """Create a mock provider with a check_health coroutine."""
        provider = MagicMock()
        provider.name = name
        if healthy:
            provider.check_health = AsyncMock(return_value={"status": "healthy"})
        else:
            provider.check_health = AsyncMock(
                return_value={"status": "unhealthy", "error": "auth failed"}
            )
        return provider

    def _build_server_context(
        self,
        *,
        providers=None,
        storage_path_exists=True,
        cache_enabled=True,
        cache_has_backend=True,
    ):
        """Build a mock ServerContext for health check tests."""
        ctx = MagicMock()

        # Provider registry
        registry = MagicMock()
        if providers is None:
            providers = [self._make_provider("openai")]
        registry.get_all_providers.return_value = providers
        ctx.image_generation_tool.provider_registry = registry
        ctx.image_generation_tool.ensure_providers_registered = AsyncMock()

        # Storage
        ctx.storage_manager.base_path = MagicMock(spec=Path)
        ctx.storage_manager.base_path.exists.return_value = storage_path_exists

        # Cache
        ctx.cache_manager.enabled = cache_enabled
        ctx.cache_manager.cache = MagicMock() if cache_has_backend else None

        return ctx

    async def _run_health_check(self, server_ctx):
        """Run health_check with a mock MCP context wrapping server_ctx."""
        from omniapi_mcp.server import health_check, mcp

        mock_ctx = MagicMock()
        mock_ctx.request_context.lifespan_context = server_ctx

        with patch.object(mcp, "get_context", return_value=mock_ctx):
            return await health_check()

    @pytest.fixture(autouse=True)
    def _patch_server_settings(self):
        """Patch the module-level settings used by health_check."""
        mock_settings = MagicMock()
        mock_settings.server.version = "1.0.0-test"
        with patch("omniapi_mcp.server.settings", mock_settings):
            yield

    async def test_healthy_status(self):
        """Health check returns healthy when all services are up."""
        server_ctx = self._build_server_context()
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "healthy"
        assert result["services"]["providers"] == "healthy"
        assert result["services"]["storage"] == "healthy"
        assert result["services"]["cache"] == "healthy"
        assert result["provider_details"]["openai"]["status"] == "healthy"
        assert "timestamp" in result
        assert result["version"] == "1.0.0-test"

    async def test_unhealthy_when_no_providers(self):
        """Health check returns unhealthy when no providers are registered."""
        server_ctx = self._build_server_context(providers=[])
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "unhealthy"
        assert result["services"]["providers"] == "unhealthy"

    async def test_unhealthy_when_api_auth_fails(self):
        """Health check returns unhealthy when API ping fails."""
        bad_provider = self._make_provider("openai", healthy=False)
        server_ctx = self._build_server_context(providers=[bad_provider])
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "unhealthy"
        assert result["services"]["providers"] == "unhealthy"
        assert result["provider_details"]["openai"]["status"] == "unhealthy"
        assert "error" in result["provider_details"]["openai"]

    async def test_degraded_when_one_provider_down(self):
        """Health check returns degraded when one of multiple providers is down."""
        providers = [
            self._make_provider("openai", healthy=True),
            self._make_provider("gemini", healthy=False),
        ]
        server_ctx = self._build_server_context(providers=providers)
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "degraded"
        assert result["services"]["providers"] == "degraded"
        assert result["provider_details"]["openai"]["status"] == "healthy"
        assert result["provider_details"]["gemini"]["status"] == "unhealthy"

    async def test_degraded_when_storage_missing(self):
        """Health check returns degraded when storage path doesn't exist."""
        server_ctx = self._build_server_context(storage_path_exists=False)
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "degraded"
        assert result["services"]["storage"] == "unhealthy"

    async def test_cache_disabled_is_ok(self):
        """Health check still returns healthy when cache is disabled."""
        server_ctx = self._build_server_context(
            cache_enabled=False, cache_has_backend=False
        )
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "healthy"
        assert result["services"]["cache"] == "disabled"

    async def test_degraded_when_cache_backend_missing(self):
        """Health check returns degraded when cache is enabled but has no backend."""
        server_ctx = self._build_server_context(
            cache_enabled=True, cache_has_backend=False
        )
        result = await self._run_health_check(server_ctx)

        assert result["status"] == "degraded"
        assert result["services"]["cache"] == "unhealthy"

    async def test_provider_timeout_returns_unhealthy(self):
        """Health check marks provider unhealthy if it times out."""
        slow_provider = MagicMock()
        slow_provider.name = "openai"
        slow_provider.check_health = AsyncMock(side_effect=asyncio.TimeoutError)

        server_ctx = self._build_server_context(providers=[slow_provider])
        result = await self._run_health_check(server_ctx)

        assert result["provider_details"]["openai"]["status"] == "unhealthy"
        assert result["status"] == "unhealthy"

    async def test_provider_exception_returns_unhealthy(self):
        """Health check marks providers unhealthy on unexpected exception."""
        server_ctx = self._build_server_context()
        registry = server_ctx.image_generation_tool.provider_registry
        registry.get_all_providers.side_effect = RuntimeError("broken")

        result = await self._run_health_check(server_ctx)

        assert result["status"] == "unhealthy"
        assert result["services"]["providers"] == "unhealthy"
