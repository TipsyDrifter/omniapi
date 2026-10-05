"""Model → harness routing and the Claude-harness endpoint table.

Routing rule (D18): the catalog's ``harness`` field per provider decides —
openai → codex, google → gemini, everything Anthropic-compatible (anthropic,
deepseek, openrouter and any ``vendor/model`` id) → claude. A caller may force
``harness=`` and we only check that the combination is configured.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..catalog import catalog
from .events import RunSpec

logger = logging.getLogger(__name__)

# provider → (harness, claude-endpoint key)
_PROVIDER_ROUTE: dict[str, tuple[str, Optional[str]]] = {
    "anthropic": ("claude", "anthropic"),
    "deepseek": ("claude", "deepseek"),
    "openrouter": ("claude", "openrouter"),
    "openai": ("codex", None),
    "google": ("gemini", None),
}

# our provider key → the vendor prefix OpenRouter uses for the same model (openai/gpt-6-sol)
_OPENROUTER_VENDOR = {"openai": "openai", "google": "google"}

# DeepSeek's Anthropic endpoint maps claude aliases to its models (official):
# claude-haiku/sonnet → deepseek-flash, claude-opus → deepseek-v4-pro.
DEEPSEEK_CLI_ALIAS = {"deepseek-flash": "claude-sonnet-5", "deepseek-v4-pro": "claude-opus-5"}


class HarnessRegistry:
    def __init__(self, settings: Any, store: Any = None):
        self.settings = settings
        self.store = store  # only the replay harness needs it

    def _provider_configured(self, key: str) -> bool:
        if key == "anthropic":
            # subscription endpoint: the owner's Claude Code login is enough
            from pathlib import Path

            return (Path.home() / ".claude" / ".credentials.json").exists() or self._provider_configured("anthropic-api")
        if key == "anthropic-api":
            key = "anthropic"
        cfg = getattr(self.settings.providers, "gemini" if key == "google" else key, None)
        return bool(cfg and getattr(cfg, "enabled", False) and getattr(cfg, "api_key", ""))

    def claude_cli(self):
        """Where the Claude Code executable is (``harness.claude.find_claude_cli``); every
        Claude run needs one, whichever endpoint it talks to."""
        from .claude import find_claude_cli

        return find_claude_cli(self.settings)

    def claude_availability(self) -> dict[str, Any]:
        """The claude entry of ``/api/harnesses``: usable only when the CLI is found AND at
        least one endpoint has credentials. ``reason`` (Chinese, for the board) says which
        part is missing; ``cli_reason`` is the lookup's own English detail."""
        endpoints = {k: self._provider_configured(k) for k in ("anthropic", "anthropic-api", "deepseek", "openrouter")}
        cli = self.claude_cli()
        out: dict[str, Any] = {
            "name": "Claude Code",
            "available": cli.path is not None and any(endpoints.values()),
            "cli": cli.path is not None,
            "cli_path": cli.path,
            "cli_source": cli.source,
            "endpoints": endpoints,
            "resume": True,
        }
        if cli.path is None:
            out["reason"] = "沒有裝 Claude Code（找不到 claude.exe）"
            out["cli_reason"] = cli.reason
        elif not any(endpoints.values()):
            out["reason"] = "沒有登入 Claude Code，也沒有設定 Anthropic／DeepSeek／OpenRouter 的 key"
        return out

    def resolve(self, spec: RunSpec) -> RunSpec:
        """Fill resolved_model / provider / harness / endpoint on the spec."""
        if spec.harness == "replay" or (spec.model or "").startswith("replay"):
            # development-only: re-emit a stored run, no model call, no billing
            from .replay import replay_enabled

            if not replay_enabled():
                raise ValueError("The replay harness is for development only (start the daemon with OMNIAPI_DEV=1).")
            spec.resolved_model = spec.model if (spec.model or "").startswith("replay") else "replay"
            spec.provider = "replay"
            spec.harness = "replay"
            spec.endpoint = None
            return spec
        from ..devmode import offline, offline_message

        if offline():
            raise ValueError(offline_message(f"an agent run on '{spec.model}'"))
        model = catalog.resolve(spec.model) or spec.model
        provider = catalog.provider_of(model)
        if provider is None and "/" in model:
            provider = "openrouter"
        if provider is None:
            raise ValueError(
                f"Unknown model '{spec.model}'. Use a tier (cheap/standard/strong), a catalog id, "
                "or an OpenRouter 'vendor/model' id — see list_available_models(modality='text')."
            )
        harness, endpoint = _PROVIDER_ROUTE.get(provider, ("claude", "openrouter"))
        if spec.harness:
            if spec.harness not in ("claude", "codex", "gemini"):
                raise ValueError(f"Unknown harness '{spec.harness}' (claude | codex | gemini)")
            if spec.harness == "claude" and endpoint is None:
                # An OpenAI/Google model on Claude Code goes through OpenRouter, the only
                # Anthropic-compatible door to it. OpenRouter knows the model under a
                # vendor-prefixed id, and bills it at its own price with its own key — so
                # from here on this run *is* an OpenRouter run.
                endpoint = "openrouter"
                if not self._provider_configured("openrouter"):
                    raise ValueError(
                        f"Running '{model}' on the claude harness needs OpenRouter (Anthropic-compatible gateway); "
                        "set PROVIDERS__OPENROUTER__API_KEY."
                    )
                gateway_id = f"{_OPENROUTER_VENDOR[provider]}/{model}"
                listed = catalog.ids(provider="openrouter", modality="text", include_snapshots=True)
                if len(listed) > 1 and gateway_id not in listed:  # > 1: discovery has run (the static roster is one id)
                    raise ValueError(
                        f"OpenRouter does not list '{gateway_id}', so '{model}' cannot run on the claude harness. "
                        f"Use its own harness ({harness}) or pick an OpenRouter 'vendor/model' id."
                    )
                model, provider = gateway_id, "openrouter"
            elif spec.harness != harness:
                # Codex speaks only to OpenAI, Gemini CLI only to Google: no gateway for those.
                raise ValueError(
                    f"The {spec.harness} harness cannot run '{model}' (a {provider} model). "
                    f"Use '{harness}'" + (" or 'claude' (through OpenRouter)." if endpoint is None else ".")
                )
            harness = spec.harness
        if endpoint == "anthropic" and (spec.auth or "").lower() == "api":
            endpoint = "anthropic-api"
        if harness == "claude" and endpoint and not self._provider_configured(endpoint):
            raise ValueError(f"Provider '{endpoint}' is not configured (needed to run '{model}' on the claude harness).")
        if harness == "claude":
            cli = self.claude_cli()
            if cli.path is None:
                raise ValueError(f"Cannot run '{model}' on the claude harness: {cli.reason}")
        if harness == "codex" and not self._provider_configured("openai"):
            raise ValueError("Codex harness needs PROVIDERS__OPENAI__API_KEY.")
        if harness == "gemini" and not self._provider_configured("google"):
            raise ValueError("Gemini harness needs PROVIDERS__GEMINI__API_KEY.")
        spec.resolved_model = model
        spec.provider = provider
        spec.harness = harness
        spec.endpoint = endpoint
        return spec

    def adapter(self, harness: str):
        if harness == "claude":
            from .claude import ClaudeHarness

            return ClaudeHarness(self.settings)
        if harness == "codex":
            from .codex import CodexHarness

            return CodexHarness(self.settings)
        if harness == "gemini":
            from .gemini import GeminiHarness

            return GeminiHarness(self.settings)
        if harness == "replay":
            from .replay import ReplayHarness

            return ReplayHarness(self.settings, store=self.store)
        raise ValueError(f"Unknown harness '{harness}'")
