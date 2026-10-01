"""Echo text provider (development only): streams a canned markdown reply.

No vendor is called and nothing is billed. It exists so the chat path —
create → stream → cancel → switch model → export — can be exercised end to
end without spending the owner's money. Enabled only when ``OMNIAPI_DEV=1``.

Models: ``echo`` (normal pace) and ``echo-fast`` (no delay, used by tests).
The reply quotes the last user message and how many turns came before, so a
multi-turn conversation is visibly different from turn to turn.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

from ..providers.base import ProviderConfig
from .text import TextProvider, TextResult

ECHO_MODELS = {"echo", "echo-fast"}


def echo_enabled() -> bool:
    return os.environ.get("OMNIAPI_DEV") == "1"


def is_echo_model(model: str | None) -> bool:
    return bool(model) and model in ECHO_MODELS


def _reply(messages: list[dict[str, Any]], model: str) -> tuple[str, str]:
    users = [m for m in messages if m.get("role") == "user"]
    last = str(users[-1].get("content") if users else "").strip()
    system = next((str(m.get("content")) for m in messages if m.get("role") in ("system", "developer")), "")
    thought = f"（回音模型的思考）這是第 {len(users)} 輪；使用者問的是「{last[:40]}」。我只會照稿回覆。"
    text = (
        f"收到第 **{len(users)}** 輪：「{last}」\n\n"
        "這是**回音模型**的固定稿，用來檢查聊天畫面，不呼叫任何供應商、不計費。\n\n"
        "## 排版檢查\n\n"
        "1. 有序清單第一項\n"
        "2. 第二項帶 `inline code`\n\n"
        "- 無序清單\n"
        "- 一個[連結](https://example.com)\n\n"
        "```python\n"
        "def hello(name: str) -> str:\n"
        "    return f\"hello, {name}\"\n"
        "```\n\n"
        "| 欄位 | 值 |\n|---|---|\n"
        f"| 模型 | `{model}` |\n"
        f"| 歷史訊息數 | {len(messages)} |\n"
        f"| system prompt | {'有' if system else '無'} |\n\n"
        "> 引用區塊：回覆結束。"
    )
    return text, thought


class EchoTextProvider(TextProvider):
    PROVIDER_KEY = "echo"
    SUPPORTED_MODELS = set(ECHO_MODELS)
    DEFAULT_MODEL = "echo"

    def __init__(self, config: ProviderConfig | None = None):
        super().__init__(config or ProviderConfig(api_key="echo", enabled=True))

    def get_supported_models(self) -> set[str]:
        return set(ECHO_MODELS)

    @staticmethod
    def _usage(messages: list[dict[str, Any]], text: str) -> dict[str, Any]:
        prompt = sum(len(str(m.get("content") or "")) for m in messages)
        return {"prompt_tokens": prompt, "completion_tokens": len(text), "total_tokens": prompt + len(text)}

    async def complete(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> TextResult:
        text, thought = _reply(messages, model)
        return TextResult(text=text, model=model, reasoning=thought, finish_reason="stop", usage=self._usage(messages, text), cost_usd=0.0, metadata={"provider": "echo"})

    async def stream(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        text, thought = _reply(messages, model)
        delay = 0.0 if model == "echo-fast" else float(os.environ.get("OMNIAPI_ECHO_DELAY", "0.04"))
        yield {"type": "reasoning", "delta": thought}
        step = 6
        for i in range(0, len(text), step):
            if delay:
                await asyncio.sleep(delay)
            yield {"type": "text", "delta": text[i : i + step]}
        yield {
            "type": "done",
            "result": TextResult(text=text, model=model, reasoning=thought, finish_reason="stop", usage=self._usage(messages, text), cost_usd=0.0, metadata={"provider": "echo"}),
        }
