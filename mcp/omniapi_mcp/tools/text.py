"""Text completion tool — orchestrates the text providers (OpenAI, DeepSeek,
Anthropic, Gemini, OpenRouter).

Reads settings, initializes every configured provider, resolves tier aliases
(``cheap`` / ``standard`` / ``strong``) through the model catalog and routes
each request to the provider that owns the requested model. Routing is
dynamic: a model discovered at startup is callable without a code change.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..capabilities.text import (
    AnthropicTextProvider,
    DeepSeekTextProvider,
    GeminiTextProvider,
    OpenAITextProvider,
    OpenRouterTextProvider,
    TextProvider,
)
from ..catalog import catalog
from ..config.settings import Settings
from ..providers.base import ProviderConfig

logger = logging.getLogger(__name__)

# settings.providers.<key> → provider class
_PROVIDER_CLASSES: dict[str, type[TextProvider]] = {
    "openai": OpenAITextProvider,
    "deepseek": DeepSeekTextProvider,
    "anthropic": AnthropicTextProvider,
    "gemini": GeminiTextProvider,
    "openrouter": OpenRouterTextProvider,
}


class TextTool:
    """Complete text/chat prompts via a configured text provider."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._providers: list[TextProvider] = []
        self._by_key: dict[str, TextProvider] = {}
        self._init_providers()

    def _register(self, provider: TextProvider) -> None:
        self._providers.append(provider)
        self._by_key[provider.PROVIDER_KEY] = provider
        logger.info(
            "Text provider '%s' registered (%d static models)",
            provider.name,
            len(provider.SUPPORTED_MODELS),
        )

    def _init_providers(self) -> None:
        for key, cls in _PROVIDER_CLASSES.items():
            cfg = getattr(self.settings.providers, key, None)
            if not (cfg and cfg.enabled and cfg.api_key):
                continue
            try:
                self._register(
                    cls(
                        ProviderConfig(
                            api_key=cfg.api_key,
                            organization=getattr(cfg, "organization", None),
                            base_url=getattr(cfg, "base_url", None)
                            if key != "gemini"
                            else None,  # Gemini text uses the OpenAI-compat URL
                            timeout=cfg.timeout,
                            max_retries=cfg.max_retries,
                            enabled=cfg.enabled,
                        )
                    )
                )
            except Exception as e:
                logger.error("Failed to init %s text provider: %s", key, e)

    # ------------------------------------------------------------ routing
    def _provider_for(self, model: str) -> Optional[TextProvider]:
        # 1) catalog knows the provider (curated or discovered)
        key = catalog.provider_of(model)
        if key and key in self._by_key:
            return self._by_key[key]
        # 2) static rosters
        for p in self._providers:
            if model in p.SUPPORTED_MODELS:
                return p
        # 3) namespaced ids → OpenRouter (accepts unknown ids)
        if "/" in model and "openrouter" in self._by_key:
            return self._by_key["openrouter"]
        # 4) any provider that claims it dynamically
        for p in self._providers:
            if p.supports_model(model) and not p.ACCEPT_UNKNOWN_MODELS:
                return p
        return None

    def _default_model(self) -> Optional[str]:
        """``cheap`` tier when its provider is configured, else the first
        registered provider's default."""
        cheap = catalog.resolve("cheap")
        if cheap and self._provider_for(cheap):
            return cheap
        if not self._providers:
            return None
        return self._providers[0].DEFAULT_MODEL

    def available_models(self) -> list[str]:
        out: set[str] = set()
        for p in self._providers:
            out |= p.get_supported_models()
        return sorted(out)

    def available_providers(self) -> list[str]:
        return [p.PROVIDER_KEY for p in self._providers]

    # ------------------------------------------------------------ complete
    async def complete(
        self,
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        system: Optional[str] = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Complete a prompt (or a full messages list) with the chosen model.

        ``model`` may be a tier (``cheap``/``standard``/``strong``), an alias
        or a provider model id. Advanced params are forwarded only when set;
        each provider drops the ones it does not understand.
        """
        from ..capabilities.echo import is_echo_model
        from ..devmode import refuse_if_offline

        if is_echo_model(model):  # development stand-in: no vendor call
            echo_model, echo = self.route(model)
            if messages is None:
                messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt or ""}]
            r = await echo.complete(echo_model, messages)
            return {"text": r.text, "model": r.model, "requested_model": model, "provider": "echo", "reasoning": r.reasoning,
                    "finish_reason": r.finish_reason, "tool_calls": None, "usage": r.usage, "cost_usd": 0.0, "deprecated": None}
        refuse_if_offline(f"text model '{model or 'default'}'")

        if not self._providers:
            raise RuntimeError(
                "No text provider is configured. Set PROVIDERS__OPENAI__API_KEY, "
                "PROVIDERS__ANTHROPIC__API_KEY, PROVIDERS__DEEPSEEK__API_KEY, "
                "PROVIDERS__GEMINI__API_KEY or PROVIDERS__OPENROUTER__API_KEY "
                "(with the matching __ENABLED=true)."
            )

        requested = model
        target_model = catalog.resolve(model) or self._default_model()
        provider = self._provider_for(target_model) if target_model else None
        if not provider:
            raise RuntimeError(
                f"No text provider for model '{requested or target_model}'. "
                f"Configured providers: {self.available_providers()}. "
                "Use list_available_models to see what is online."
            )

        entry = catalog.get(target_model)
        if entry and entry.status == "deprecated":
            logger.warning(
                "Model %s is deprecated%s%s",
                target_model,
                f" (shutdown {entry.shutdown})" if entry.shutdown else "",
                f"; use {entry.replacement}" if entry.replacement else "",
            )

        if messages is None:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt or ""})

        clean = {k: v for k, v in params.items() if v is not None}
        result = await provider.complete(target_model, messages, **clean)

        cost = result.cost_usd
        if cost is None:
            cost = catalog.estimate_text_cost(target_model, result.usage)

        return {
            "text": result.text,
            "model": result.model,
            "requested_model": requested,
            "provider": result.metadata.get("provider"),
            "reasoning": result.reasoning,
            "finish_reason": result.finish_reason,
            "tool_calls": result.tool_calls,
            "usage": result.usage,
            "cost_usd": cost,
            "deprecated": bool(entry and entry.status == "deprecated") or None,
        }

    # ------------------------------------------------------------ stream (chat)
    def route(self, model: Optional[str]) -> tuple[str, TextProvider]:
        """Resolve ``model`` (tier / alias / id) to ``(model id, provider)``.

        Stricter than ``complete()``: a model the catalog does not know is
        passed on as typed (a provider may still claim it) instead of being
        quietly swapped for the default — in a chat the owner picked the
        model on purpose, answering with another one would be a lie.
        """
        from ..capabilities.echo import EchoTextProvider, echo_enabled, is_echo_model

        if is_echo_model(model):
            if not echo_enabled():
                raise RuntimeError("The echo model is for development only (start the daemon with OMNIAPI_DEV=1).")
            if "echo" not in self._by_key:
                self._by_key["echo"] = EchoTextProvider()
            return str(model), self._by_key["echo"]
        from ..devmode import refuse_if_offline

        refuse_if_offline(f"text model '{model or 'default'}'")
        if not self._providers:
            raise RuntimeError("No text provider is configured (set a PROVIDERS__<NAME>__API_KEY).")
        target = (catalog.resolve(model) or model) if model else self._default_model()
        provider = self._provider_for(target) if target else None
        if not provider:
            raise RuntimeError(
                f"No text provider for model '{model or target}'. "
                f"Configured providers: {self.available_providers()}."
            )
        return str(target), provider

    async def stream(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        **params: Any,
    ):
        """Async generator: ``{"type": "text"|"reasoning", "delta"}`` pieces,
        then one ``{"type": "done", ...}`` shaped like ``complete()``'s return.

        ``tools`` (OpenAI-shaped function tools) go to the provider when given;
        the tool calls of the reply — text and calls may come together — are
        on ``done`` as ``tool_calls`` (OpenAI-shaped, arguments a JSON string).
        ``replay`` is what the vendor needs back with them on a later request
        (Anthropic's signed thinking), private keys to merge into the stored
        assistant message's provider form; ``None`` when there is nothing."""
        target_model, provider = self.route(model)
        clean = {k: v for k, v in params.items() if v is not None}
        async for piece in provider.stream(target_model, messages, **clean):
            if piece.get("type") != "done":
                yield piece
                continue
            result = piece["result"]
            cost = result.cost_usd
            if cost is None:
                cost = catalog.estimate_text_cost(target_model, result.usage)
            yield {
                "type": "done",
                "text": result.text,
                "model": result.model or target_model,
                "requested_model": model,
                "provider": result.metadata.get("provider") or provider.PROVIDER_KEY,
                "reasoning": result.reasoning,
                "finish_reason": result.finish_reason,
                "usage": result.usage,
                "cost_usd": cost,
                "tool_calls": result.tool_calls,
                "replay": (result.metadata or {}).get("replay"),
            }

    async def close(self) -> None:
        for provider in self._providers:
            if hasattr(provider, "close"):
                await provider.close()
