"""Text completion capability — chat/text providers.

Modality: text -> text (chat completions).

Provider families:

* ``OpenAITextProvider`` and its dialect subclasses (DeepSeek, Gemini's
  OpenAI-compatible endpoint, OpenRouter) share the OpenAI wire format; each
  subclass only flips dialect flags and the base URL.
* ``AnthropicTextProvider`` speaks the Messages API natively and converts
  the OpenAI-shaped messages/tools OmniAPI uses everywhere into Anthropic
  blocks and back.

Model rosters are *dynamic*: ``get_supported_models()`` unions the static
fallback set with whatever the model catalog knows for the provider (curated
+ live discovery), so a new model a vendor ships is callable the moment the
provider lists it.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from ..catalog import catalog
from ..providers.base import ProviderConfig, ProviderError

logger = logging.getLogger(__name__)


@dataclass
class TextResult:
    """Standardized text-completion response across providers."""

    text: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)
    reasoning: str | None = None  # chain-of-thought when the provider exposes it
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] | None = None  # OpenAI-shaped
    cost_usd: float | None = None  # provider-reported cost (OpenRouter) if any
    provider_response: dict[str, Any] | None = None


class TextProvider(ABC):
    """Abstract base for text/chat-completion providers."""

    #: key into the model catalog (``catalog.json`` → ``providers``)
    PROVIDER_KEY: str = ""
    #: static fallback roster, used when discovery has not run / is unavailable
    SUPPORTED_MODELS: set[str] = set()
    DEFAULT_MODEL: str = ""
    #: accept ids the roster does not know (gateways whose roster is huge)
    ACCEPT_UNKNOWN_MODELS = False

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.name = self.PROVIDER_KEY or self.__class__.__name__.replace(
            "Provider", ""
        ).lower()
        self._logger = logging.getLogger(f"{__name__}.{self.name}")

    def get_supported_models(self) -> set[str]:
        """Static roster ∪ catalog roster (curated + discovered) for this provider."""
        models = set(self.SUPPORTED_MODELS)
        if self.PROVIDER_KEY:
            models |= catalog.ids(
                provider=self.PROVIDER_KEY, modality="text", include_snapshots=True
            )
        return models

    def supports_model(self, model: str) -> bool:
        return self.ACCEPT_UNKNOWN_MODELS or model in self.get_supported_models()

    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> TextResult:
        """Run a chat completion."""
        ...

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a chat completion, yielding pieces as they arrive.

        Yields ``{"type": "text" | "reasoning", "delta": str}`` and finally
        exactly one ``{"type": "done", "result": TextResult}``.

        Default: no real streaming — one ``complete()`` call whose whole text
        comes out as a single delta, so every provider can be chatted with.
        """
        result = await self.complete(model, messages, **kwargs)
        if result.reasoning:
            yield {"type": "reasoning", "delta": result.reasoning}
        if result.text:
            yield {"type": "text", "delta": result.text}
        yield {"type": "done", "result": result}

    def is_available(self) -> bool:
        return self.config.enabled and bool(self.config.api_key)

    async def close(self) -> None:  # pragma: no cover - trivial
        pass


# ============================================================================
# OpenAI wire format family
# ============================================================================


class OpenAITextProvider(TextProvider):
    """OpenAI chat completions (GPT-6 / GPT-5.x families)."""

    PROVIDER_KEY = "openai"
    SUPPORTED_MODELS = {
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.4",
        "gpt-5.4-pro",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
    }
    DEFAULT_MODEL = "gpt-5.4-mini"

    # --- provider dialect (subclasses override these) ----------------------
    # GPT-5.x/6 reasoning models reject sampling params (temperature/top_p/seed)
    # with HTTP 400, so we DROP them for reasoning models rather than forward.
    DROP_SAMPLING_FOR_REASONING = True
    # With o1+/GPT-5.x the `system` role is replaced by `developer`.
    REMAP_SYSTEM_TO_DEVELOPER = True
    # OpenAI uses `max_completion_tokens`; most compatible APIs use `max_tokens`.
    MAX_OUTPUT_PARAM = "max_completion_tokens"
    # `verbosity` is an OpenAI-only knob.
    SUPPORTS_VERBOSITY = True
    # `thinking` (sent via extra_body) is a DeepSeek-only toggle.
    SUPPORTS_THINKING = False
    # `parallel_tool_calls` + the metadata/routing extras are OpenAI-platform only.
    SUPPORTS_OPENAI_EXTRAS = True
    # Substrings marking a NON-reasoning "Instant"/chat model that still
    # accepts sampling params. Everything else in the family is treated as a
    # reasoning model.
    NON_REASONING_MARKERS = ("chat-latest", "gpt-4o", "gpt-4.1", "gpt-4-", "gpt-3.5")
    # Extra default headers (OpenRouter attribution etc.)
    DEFAULT_HEADERS: dict[str, str] = {}
    # extra_body merged into every request (OpenRouter usage accounting etc.)
    EXTRA_BODY: dict[str, Any] = {}

    _REASONING_REJECTED_PARAMS = ("temperature", "top_p", "seed", "stop")
    _PASSTHROUGH_PARAMS = ("tools", "tool_choice", "response_format")
    _OPENAI_ONLY_PARAMS = (
        "parallel_tool_calls",
        "store",
        "metadata",
        "service_tier",
        "prompt_cache_key",
        "safety_identifier",
    )

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            organization=config.organization,
            base_url=config.base_url or self.default_base_url(),
            timeout=config.timeout,
            max_retries=config.max_retries,
            default_headers=self.DEFAULT_HEADERS or None,
        )

    @classmethod
    def default_base_url(cls) -> str:
        return "https://api.openai.com/v1"

    def _is_reasoning_model(self, model: str) -> bool:
        m = model.lower()
        return not any(marker in m for marker in self.NON_REASONING_MARKERS)

    def _prepare_messages(
        self, model: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not (self.REMAP_SYSTEM_TO_DEVELOPER and self._is_reasoning_model(model)):
            return messages
        return [
            {**msg, "role": "developer"} if msg.get("role") == "system" else msg
            for msg in messages
        ]

    def _build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Assemble chat.completions.create kwargs for this provider's dialect.
        Pure/sync so it can be unit-tested without a network call."""
        request: dict[str, Any] = {
            "model": model,
            "messages": self._prepare_messages(model, messages),
        }
        is_reasoning = self._is_reasoning_model(model)

        for name in self._REASONING_REJECTED_PARAMS:
            val = params.get(name)
            if val is None:
                continue
            if is_reasoning and self.DROP_SAMPLING_FOR_REASONING:
                self._logger.warning(
                    "Dropping %s=%s for reasoning model %s (rejected with HTTP 400). "
                    "Use reasoning_effort / verbosity instead.",
                    name,
                    val,
                    model,
                )
            else:
                request[name] = val

        for name in self._PASSTHROUGH_PARAMS:
            if params.get(name) is not None:
                request[name] = params[name]

        if self.SUPPORTS_OPENAI_EXTRAS:
            for name in self._OPENAI_ONLY_PARAMS:
                if params.get(name) is None:
                    continue
                if name == "parallel_tool_calls" and params.get("tools") is None:
                    continue
                request[name] = params[name]

        if params.get("reasoning_effort") is not None:
            request["reasoning_effort"] = params["reasoning_effort"]
        if params.get("verbosity") is not None and self.SUPPORTS_VERBOSITY:
            request["verbosity"] = params["verbosity"]
        if params.get("max_completion_tokens") is not None:
            request[self.MAX_OUTPUT_PARAM] = params["max_completion_tokens"]

        extra_body: dict[str, Any] = dict(self.EXTRA_BODY)
        if params.get("thinking") is not None and self.SUPPORTS_THINKING:
            extra_body["thinking"] = params["thinking"]
        if extra_body:
            request["extra_body"] = extra_body

        return request

    @staticmethod
    def _extract_tool_calls(msg: Any) -> list[dict[str, Any]] | None:
        raw = getattr(msg, "tool_calls", None)
        if not raw:
            return None
        out = [tc.model_dump() if hasattr(tc, "model_dump") else tc for tc in raw]
        return out or None

    @staticmethod
    def _extract_usage(resp: Any) -> dict[str, Any] | None:
        u = getattr(resp, "usage", None)
        if not u:
            return None
        usage: dict[str, Any] = {
            "prompt_tokens": getattr(u, "prompt_tokens", None),
            "completion_tokens": getattr(u, "completion_tokens", None),
            "total_tokens": getattr(u, "total_tokens", None),
        }
        details = getattr(u, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None) if details else None
        if reasoning_tokens is not None:
            usage["reasoning_tokens"] = reasoning_tokens
        pdetails = getattr(u, "prompt_tokens_details", None)
        cached = getattr(pdetails, "cached_tokens", None) if pdetails else None
        if cached is not None:
            usage["cached_tokens"] = cached
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cost"):
            val = getattr(u, key, None)
            if val is None and hasattr(u, "model_extra"):
                val = (u.model_extra or {}).get(key)
            if val is not None:
                usage[key] = val
        return usage

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
        max_completion_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
        top_p: float | None = None,
        seed: int | None = None,
        stop: Any | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        parallel_tool_calls: bool | None = None,
        store: bool | None = None,
        metadata: dict[str, Any] | None = None,
        service_tier: str | None = None,
        prompt_cache_key: str | None = None,
        safety_identifier: str | None = None,
        **kwargs: Any,
    ) -> TextResult:
        if not self.supports_model(model):
            raise ProviderError(
                f"Model '{model}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )

        params = {
            "temperature": temperature,
            "top_p": top_p,
            "seed": seed,
            "stop": stop,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": parallel_tool_calls,
            "response_format": response_format,
            "reasoning_effort": reasoning_effort,
            "verbosity": verbosity,
            "max_completion_tokens": max_completion_tokens,
            "thinking": thinking,
            "store": store,
            "metadata": metadata,
            "service_tier": service_tier,
            "prompt_cache_key": prompt_cache_key,
            "safety_identifier": safety_identifier,
        }
        request = self._build_request(model, messages, params)

        try:
            self._logger.info("Text completion with %s model %s", self.name, model)
            resp = await self.client.chat.completions.create(**request)
            choice = resp.choices[0]
            msg = choice.message
            usage = self._extract_usage(resp)
            cost = usage.pop("cost", None) if usage else None
            return TextResult(
                text=msg.content or "",
                model=getattr(resp, "model", model),
                reasoning=getattr(msg, "reasoning_content", None)
                or getattr(msg, "reasoning", None),
                finish_reason=choice.finish_reason,
                usage=usage,
                tool_calls=self._extract_tool_calls(msg),
                cost_usd=float(cost) if cost is not None else None,
                metadata={"provider": self.name},
            )
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in %s text completion: %s", self.name, e)
            raise ProviderError(
                f"{self.name} text completion failed: {str(e)}",
                provider_name=self.name,
                error_code="COMPLETION_FAILED",
            )

    #: ask for the usage chunk at the end of a stream (OpenAI ``stream_options``)
    STREAM_INCLUDE_USAGE = True

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streamed chat completion (``stream=True``), same dialect rules as
        ``complete()``. Tool calls are not assembled here — chat does not use
        them; a run that needs tools goes through a harness."""
        if not self.supports_model(model):
            raise ProviderError(
                f"Model '{model}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        request = self._build_request(model, messages, dict(kwargs))
        request["stream"] = True
        if self.STREAM_INCLUDE_USAGE:
            request["stream_options"] = {"include_usage": True}

        texts: list[str] = []
        thoughts: list[str] = []
        finish: str | None = None
        usage: dict[str, Any] | None = None
        served_model = model
        try:
            self._logger.info("Text stream with %s model %s", self.name, model)
            stream = await self.client.chat.completions.create(**request)
            async for chunk in stream:
                served_model = getattr(chunk, "model", None) or served_model
                if getattr(chunk, "usage", None):
                    usage = self._extract_usage(chunk)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                extra = getattr(delta, "model_extra", None) or {}
                thought = (
                    getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                    or extra.get("reasoning_content")
                    or extra.get("reasoning")
                )
                if isinstance(thought, str) and thought:
                    thoughts.append(thought)
                    yield {"type": "reasoning", "delta": thought}
                piece = getattr(delta, "content", None)
                if isinstance(piece, str) and piece:
                    texts.append(piece)
                    yield {"type": "text", "delta": piece}
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in %s text stream: %s", self.name, e)
            raise ProviderError(
                f"{self.name} text stream failed: {str(e)}",
                provider_name=self.name,
                error_code="COMPLETION_FAILED",
            )
        cost = usage.pop("cost", None) if usage else None
        yield {
            "type": "done",
            "result": TextResult(
                text="".join(texts),
                model=served_model,
                reasoning="".join(thoughts) or None,
                finish_reason=finish,
                usage=usage,
                cost_usd=float(cost) if cost is not None else None,
                metadata={"provider": self.name},
            ),
        }

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            pass


class DeepSeekTextProvider(OpenAITextProvider):
    """DeepSeek text / reasoning (OpenAI-compatible, ``system`` role kept,
    ``max_tokens``, ``thinking`` toggle via extra_body)."""

    PROVIDER_KEY = "deepseek"
    SUPPORTED_MODELS = {"deepseek-flash", "deepseek-v4-pro"}
    DEFAULT_MODEL = "deepseek-flash"

    DROP_SAMPLING_FOR_REASONING = False
    REMAP_SYSTEM_TO_DEVELOPER = False
    MAX_OUTPUT_PARAM = "max_tokens"
    SUPPORTS_VERBOSITY = False
    SUPPORTS_THINKING = True
    SUPPORTS_OPENAI_EXTRAS = False

    @classmethod
    def default_base_url(cls) -> str:
        return "https://api.deepseek.com"


class GeminiTextProvider(OpenAITextProvider):
    """Google Gemini via its OpenAI-compatible endpoint.

    Function calling, JSON-schema structured output and ``reasoning_effort``
    all pass straight through; sampling params are honoured (Gemini never
    400s on temperature), so nothing is dropped.
    """

    PROVIDER_KEY = "google"
    SUPPORTED_MODELS = {"gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-pro-preview"}
    DEFAULT_MODEL = "gemini-3.8-flash"

    DROP_SAMPLING_FOR_REASONING = False
    REMAP_SYSTEM_TO_DEVELOPER = False
    MAX_OUTPUT_PARAM = "max_tokens"
    SUPPORTS_VERBOSITY = False
    SUPPORTS_THINKING = False
    SUPPORTS_OPENAI_EXTRAS = False

    @classmethod
    def default_base_url(cls) -> str:
        return "https://generativelanguage.googleapis.com/v1beta/openai/"

    @staticmethod
    def _extract_usage(resp: Any) -> dict[str, Any] | None:
        return fold_unreported_output(OpenAITextProvider._extract_usage(resp))


def fold_unreported_output(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Gemini leaves thinking tokens out of ``completion_tokens``: they only
    show up as ``total - prompt - completion``. Google bills them as output, and
    every other provider here already counts reasoning inside completion, so
    fold the gap in (and name it ``reasoning_tokens``) — otherwise the cost is
    understated by however much the model thought."""
    if not usage:
        return usage
    prompt, completion, total = (usage.get(k) or 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    gap = total - prompt - completion
    if gap > 0:
        usage["completion_tokens"] = completion + gap
        usage["reasoning_tokens"] = usage.get("reasoning_tokens") or gap
    return usage


class OpenRouterTextProvider(OpenAITextProvider):
    """OpenRouter — one key for the long tail (Kimi, GLM, MiniMax, Qwen, Muse
    Spark, Mistral, Llama, …). Ids are namespaced ``vendor/model``; the roster
    is whatever discovery returned, and unknown ids are still forwarded
    because the live list changes daily.
    """

    PROVIDER_KEY = "openrouter"
    SUPPORTED_MODELS = {"openrouter/auto"}
    DEFAULT_MODEL = "openrouter/auto"
    ACCEPT_UNKNOWN_MODELS = True

    DROP_SAMPLING_FOR_REASONING = False
    REMAP_SYSTEM_TO_DEVELOPER = False
    MAX_OUTPUT_PARAM = "max_tokens"
    SUPPORTS_VERBOSITY = False
    SUPPORTS_THINKING = False
    SUPPORTS_OPENAI_EXTRAS = False
    DEFAULT_HEADERS = {
        "HTTP-Referer": "https://github.com/TipsyDrifter/omniapi",
        "X-Title": "OmniAPI",
    }
    # Ask OpenRouter to report the real cost with the usage object.
    EXTRA_BODY = {"usage": {"include": True}}

    @classmethod
    def default_base_url(cls) -> str:
        return "https://openrouter.ai/api/v1"


# ============================================================================
# Anthropic (Messages API)
# ============================================================================


class AnthropicTextProvider(TextProvider):
    """Anthropic Claude via the native Messages API.

    Input/output stay OpenAI-shaped at the OmniAPI boundary (messages with
    ``system``/``user``/``assistant``/``tool`` roles, ``tools`` as
    ``{"type":"function","function":{...}}``), converted here.
    """

    PROVIDER_KEY = "anthropic"
    SUPPORTED_MODELS = {
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    }
    DEFAULT_MODEL = "claude-sonnet-5"
    DEFAULT_MAX_TOKENS = 4096
    # reasoning_effort → extended-thinking budget (tokens)
    THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 16000, "xhigh": 32000}

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.base_url or "https://api.anthropic.com",
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    # ------------------------------------------------------------ conversion
    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        out = []
        for t in tools:
            fn = t.get("function", t) if isinstance(t, dict) else {}
            if not fn.get("name"):
                continue
            out.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        return out or None

    @staticmethod
    def _convert_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
        if tool_choice is None:
            return None
        if isinstance(tool_choice, str):
            return {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}.get(
                tool_choice
            )
        if isinstance(tool_choice, dict):
            name = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
            if name:
                return {"type": "tool", "name": name}
        return None

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """OpenAI-shaped history → (system_text, anthropic_messages)."""
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role in ("system", "developer"):
                if isinstance(content, str) and content:
                    system_parts.append(content)
                continue
            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue
            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                if isinstance(content, str) and content:
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    blocks.extend(content)
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": fn.get("arguments")}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": args,
                        }
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
                continue
            # user (string or content blocks)
            out.append({"role": "user", "content": content if content is not None else ""})
        # Anthropic requires alternating roles starting with user; merge
        # consecutive same-role messages defensively.
        merged: list[dict[str, Any]] = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"]:
                a, b = merged[-1]["content"], m["content"]
                a = a if isinstance(a, list) else [{"type": "text", "text": a}]
                b = b if isinstance(b, list) else [{"type": "text", "text": b}]
                merged[-1]["content"] = a + b
            else:
                merged.append(dict(m))
        return ("\n\n".join(system_parts) or None, merged)

    def _build_request(
        self, model: str, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> dict[str, Any]:
        system, converted = self._convert_messages(messages)
        max_tokens = params.get("max_completion_tokens") or self.DEFAULT_MAX_TOKENS
        req: dict[str, Any] = {"model": model, "messages": converted, "max_tokens": max_tokens}
        if system:
            req["system"] = system
        for k in ("temperature", "top_p"):
            if params.get(k) is not None:
                req[k] = params[k]
        if params.get("stop") is not None:
            stop = params["stop"]
            req["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
        tools = self._convert_tools(params.get("tools"))
        if tools:
            req["tools"] = tools
            tc = self._convert_tool_choice(params.get("tool_choice"))
            if tc:
                req["tool_choice"] = tc
        thinking = params.get("thinking")
        effort = params.get("reasoning_effort")
        if isinstance(thinking, dict) and thinking.get("type"):
            req["thinking"] = thinking
        elif effort and effort != "none":
            budget = self.THINKING_BUDGETS.get(effort, self.THINKING_BUDGETS["medium"])
            req["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if req["max_tokens"] <= budget:
                req["max_tokens"] = budget + self.DEFAULT_MAX_TOKENS
            # extended thinking forbids temperature/top_p tweaks
            req.pop("temperature", None)
            req.pop("top_p", None)
        rf = params.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_schema":
            schema = (rf.get("json_schema") or {}).get("schema") or rf.get("schema")
            if schema:
                # Structured outputs: output_config.format (platform docs 2026-09).
                # Sent via extra_body so an older SDK does not choke on it.
                req["extra_body"] = {
                    "output_config": {"format": {"type": "json_schema", "schema": schema}}
                }
        return req

    @staticmethod
    def _parse_response(resp: Any, model: str, provider: str) -> TextResult:
        texts: list[str] = []
        thoughts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(getattr(block, "text", ""))
            elif btype == "thinking":
                thoughts.append(getattr(block, "thinking", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": getattr(block, "id", ""),
                        "type": "function",
                        "function": {
                            "name": getattr(block, "name", ""),
                            "arguments": json.dumps(getattr(block, "input", {}) or {}),
                        },
                    }
                )
        u = getattr(resp, "usage", None)
        usage = None
        if u:
            usage = {
                "prompt_tokens": getattr(u, "input_tokens", None),
                "completion_tokens": getattr(u, "output_tokens", None),
            }
            cached = getattr(u, "cache_read_input_tokens", None)
            if cached is not None:
                usage["cached_tokens"] = cached
            if usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None:
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        stop = getattr(resp, "stop_reason", None)
        finish = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls",
                  "stop_sequence": "stop"}.get(stop, stop)
        return TextResult(
            text="".join(texts),
            model=getattr(resp, "model", model),
            reasoning="\n".join(thoughts) or None,
            finish_reason=finish,
            usage=usage,
            tool_calls=tool_calls or None,
            metadata={"provider": provider},
        )

    async def complete(
        self, model: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> TextResult:
        if not self.supports_model(model):
            raise ProviderError(
                f"Model '{model}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        request = self._build_request(model, messages, kwargs)
        try:
            self._logger.info("Text completion with %s model %s", self.name, model)
            resp = await self.client.messages.create(**request)
            return self._parse_response(resp, model, self.name)
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in %s text completion: %s", self.name, e)
            raise ProviderError(
                f"{self.name} text completion failed: {str(e)}",
                provider_name=self.name,
                error_code="COMPLETION_FAILED",
            )

    _STOP_MAP = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls", "stop_sequence": "stop"}

    async def stream(
        self, model: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Streamed Messages API call, read from the raw server-sent events
        (``message_start`` → ``content_block_delta``… → ``message_delta``)."""
        if not self.supports_model(model):
            raise ProviderError(
                f"Model '{model}' is not supported by {self.name} provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        request = self._build_request(model, messages, kwargs)
        request["stream"] = True
        texts: list[str] = []
        thoughts: list[str] = []
        usage: dict[str, Any] = {}
        stop: str | None = None
        served_model = model
        try:
            self._logger.info("Text stream with %s model %s", self.name, model)
            stream = await self.client.messages.create(**request)
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    served_model = getattr(msg, "model", None) or served_model
                    u = getattr(msg, "usage", None)
                    if u is not None:
                        usage["prompt_tokens"] = getattr(u, "input_tokens", None)
                        cached = getattr(u, "cache_read_input_tokens", None)
                        if cached is not None:
                            usage["cached_tokens"] = cached
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        piece = getattr(delta, "text", "") or ""
                        if piece:
                            texts.append(piece)
                            yield {"type": "text", "delta": piece}
                    elif dtype == "thinking_delta":
                        piece = getattr(delta, "thinking", "") or ""
                        if piece:
                            thoughts.append(piece)
                            yield {"type": "reasoning", "delta": piece}
                elif etype == "message_delta":
                    delta = getattr(event, "delta", None)
                    stop = getattr(delta, "stop_reason", None) or stop
                    u = getattr(event, "usage", None)
                    if u is not None and getattr(u, "output_tokens", None) is not None:
                        usage["completion_tokens"] = u.output_tokens
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in %s text stream: %s", self.name, e)
            raise ProviderError(
                f"{self.name} text stream failed: {str(e)}",
                provider_name=self.name,
                error_code="COMPLETION_FAILED",
            )
        if usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        yield {
            "type": "done",
            "result": TextResult(
                text="".join(texts),
                model=served_model,
                reasoning="".join(thoughts) or None,
                finish_reason=self._STOP_MAP.get(stop or "", stop),
                usage=usage or None,
                metadata={"provider": self.name},
            ),
        }

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            pass
