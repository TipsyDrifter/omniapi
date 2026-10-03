"""Echo text provider (development only): streams a canned markdown reply.

No vendor is called and nothing is billed. It exists so the chat path —
create → stream → cancel → switch model → export — can be exercised end to
end without spending the owner's money. Enabled only when ``OMNIAPI_DEV=1``.

Models: ``echo`` (normal pace), ``echo-fast`` (no delay, used by tests) and
``echo-blind`` (no delay, takes neither images nor tools — the sandbox's
stand-in for a plain text model, so the GUI's greyed-out attach button and
"this model cannot generate in a chat" can be tried).
The reply quotes the last user message and how many turns came before, so a
multi-turn conversation is visibly different from turn to turn; when that
message carries images it says how many (1.2-M1).

Proposals (1.2-M4): when the turn offers ``propose_generation`` and the last
user message asks for a picture (畫 / 生圖 / 圖片 …) or a voice (語音 / 唸 …),
the reply also calls it — the default model named in the tool description
(the sandbox's stand-in models) — and twice when the message says 兩張 / 兩段.
When the turn follows a tool result, the reply quotes it, so the next turn
visibly knows what became of a proposal.

Files (1.2-M5): the reply lists the files the last message carried and how
each came (the ``[附件 …]`` line the chat put in front of it, plus how many
went as a ``file`` / ``input_audio`` part), and quotes the opening of each
inlined text. ``echo`` takes PDFs and audio as they are, ``echo-fast`` PDFs
only (so an audio file makes the chat ask the owner about transcribing),
``echo-blind`` neither. When the turn offers ``read_file`` and the message
says 讀, the first round calls it on the first file listed in the tool's
description; the next round quotes what came back — the in-turn tool loop
end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any, AsyncIterator

from ..providers.base import ProviderConfig
from .text import TextProvider, TextResult

#: id → (name in the model list, takes images, streams with a delay, takes tools, takes PDFs, takes audio)
ECHO_MODELS: dict[str, tuple[str, bool, bool, bool, bool, bool]] = {
    "echo": ("回音（開發用）", True, True, True, True, True),
    "echo-fast": ("回音・不延遲（開發用）", True, False, True, True, False),
    "echo-blind": ("回音・不看圖不用工具（開發用）", False, False, False, False, False),
}
_NONE = ("", False, False, False, False, False)
_READ_WORDS = ("讀",)

_SPEECH_WORDS = ("語音", "唸", "念出", "朗讀", "配音", "說給我聽")
_IMAGE_WORDS = ("畫", "生圖", "圖片", "一張圖", "插圖")
_TWO_WORDS = ("兩張", "兩段", "兩個")


def echo_enabled() -> bool:
    return os.environ.get("OMNIAPI_DEV") == "1"


def is_echo_model(model: str | None) -> bool:
    return bool(model) and model in ECHO_MODELS


def echo_vision(model: str | None) -> bool:
    """Does this echo model take images (its ``vision`` flag)?"""
    return bool(model) and ECHO_MODELS.get(model, _NONE)[1]


def echo_tools(model: str | None) -> bool:
    """Does this echo model take tools (its ``tools`` flag)?"""
    return bool(model) and ECHO_MODELS.get(model, _NONE)[3]


def echo_files(model: str | None) -> bool:
    """Does this echo model take a PDF as it is (its ``pdf`` flag)?"""
    return bool(model) and ECHO_MODELS.get(model, _NONE)[4]


def echo_audio(model: str | None) -> bool:
    """Does this echo model take audio as it is (its ``audio`` flag)?"""
    return bool(model) and ECHO_MODELS.get(model, _NONE)[5]


def echo_catalog_entries() -> list[dict[str, Any]]:
    """The echo models as ``/api/models`` lists them (development daemon only)."""
    return [
        {"id": mid, "provider": "echo", "modality": "text", "name": name, "status": "current", "online": True, "harness": None,
         "pricing": {"input": 0, "output": 0, "note": "開發用：不呼叫供應商"},
         "capabilities": {"reasoning": True, "vision": vision, "tools": tools, "pdf": pdf, "audio": audio}}
        for mid, (name, vision, _slow, tools, pdf, audio) in ECHO_MODELS.items()
    ]


def _tool_fn(tools: Any, name: str) -> dict[str, Any] | None:
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name") == name:
            return fn
    return None


def _read_calls(messages: list[dict[str, Any]], tools: Any) -> list[dict[str, Any]]:
    """A ``read_file`` call on the first listed file, when the message asks to read."""
    fn = _tool_fn(tools, "read_file")
    if fn is None or not messages or messages[-1].get("role") != "user":
        return []
    if not any(w in _own_text(messages[-1].get("content")) for w in _READ_WORDS):  # what the owner typed, not the file notes
        return []
    ids = re.findall(r"id: ([0-9A-Za-z_-]+)", str(fn.get("description") or ""))
    if not ids:
        return []
    return [{"id": f"call_echo_{uuid.uuid4().hex[:10]}", "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"file": ids[0], "start": 0, "length": 400})}}]


def _files_seen(content: Any) -> list[str]:
    """What the last message's files looked like from here."""
    if isinstance(content, str):
        parts = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        parts = [p for p in content if isinstance(p, dict)]
    else:
        return []
    out = []
    for p in parts:
        if p.get("type") == "text":
            lines = str(p.get("text") or "").split("\n")
            for j, line in enumerate(lines):
                if line.startswith("[附件 ") and not line.endswith("結束]") and not line.endswith("為止]"):
                    out.append(f"- {line}")
                    body = [x for x in lines[j + 1:j + 12] if x.strip() and not x.startswith("--- ") and not x.startswith("[")][:3]
                    if body and ("全文如下" in line or "只放了開頭" in line or "看得到開頭" in line):
                        out.append(f"  - 內容開頭：「{' / '.join(x[:60] for x in body)}」")
        elif p.get("type") == "file":
            out.append(f"- （收到一個原樣的檔案 part：{(p.get('file') or {}).get('filename')}）")
        elif p.get("type") == "input_audio":
            out.append(f"- （收到一段原樣的錄音 part，格式 {(p.get('input_audio') or {}).get('format')}）")
    return out


def _proposals(messages: list[dict[str, Any]], tools: Any) -> list[dict[str, Any]]:
    """The ``propose_generation`` calls the echo model makes for this turn
    (only answering the owner's message, never inside a tool round)."""
    fn = _tool_fn(tools, "propose_generation")
    if fn is None or not messages or messages[-1].get("role") != "user":
        return []
    last = _own_text(messages[-1].get("content"))
    if any(w in last for w in _SPEECH_WORDS):
        kind = "speech"
    elif any(w in last for w in _IMAGE_WORDS):
        kind = "image"
    else:
        return []
    defaults = dict(re.findall(r"(image|speech)=([^；。\s]+)", str(fn.get("description") or "")))
    subject = last.strip()[:200] or "示範"
    n = 2 if any(w in last for w in _TWO_WORDS) else 1
    calls = []
    for i in range(n):
        args: dict[str, Any] = {"kind": kind, "prompt": subject if kind == "speech" else f"{subject}（第 {i + 1} 版）" if n > 1 else subject,
                                "note": "回音模型的固定提議"}
        if defaults.get(kind):
            args["model"] = defaults[kind]
        calls.append({"id": f"call_echo_{uuid.uuid4().hex[:10]}", "type": "function",
                      "function": {"name": "propose_generation", "arguments": json.dumps(args, ensure_ascii=False)}})
    return calls


def _text(content: Any) -> str:
    if isinstance(content, list):  # OpenAI-style parts (a message with images)
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def _own_text(content: Any) -> str:
    """What the owner typed: the text before the chat's ``[附件 …]`` blocks."""
    if isinstance(content, list):
        content = "".join(str(p.get("text", "")) for p in content
                              if isinstance(p, dict) and p.get("type") == "text" and not str(p.get("text", "")).startswith("[附件 "))
    s = str(content or "")
    if s.startswith("[附件 "):
        return ""
    cut = s.find("\n[附件 ")
    return (s[:cut] if cut >= 0 else s).strip()


def _images(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") in ("image_url", "image"))


def _tool_results(messages: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(tool results between the last two user messages — what became of the
    previous reply's proposals; tool results after the last user message —
    what this turn's own tool rounds read)."""
    u = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    now = [_text(m.get("content")) for m in messages[u + 1:] if m.get("role") == "tool"]
    before: list[str] = []
    for m in reversed(messages[:u] if u >= 0 else []):
        if m.get("role") == "user":
            break
        if m.get("role") == "tool":
            before.append(_text(m.get("content")))
    return list(reversed(before)), now


def _reply(messages: list[dict[str, Any]], model: str, n_proposals: int = 0) -> tuple[str, str]:
    users = [m for m in messages if m.get("role") == "user"]
    last = _own_text(users[-1].get("content") if users else "")
    n_images = _images(users[-1].get("content")) if users else 0
    files = _files_seen(users[-1].get("content")) if users else []
    system = next((str(m.get("content")) for m in messages if m.get("role") in ("system", "developer")), "")
    results, reads = _tool_results(messages)
    thought = f"（回音模型的思考）這是第 {len(users)} 輪；使用者問的是「{last[:40]}」。我只會照稿回覆。"
    text = (
        f"收到第 **{len(users)}** 輪：「{last}」\n\n"
        + (f"這一則附了 **{n_images}** 張圖。\n\n" if n_images else "")
        + (f"這一則附的檔案（{len(files)} 行）與它們怎麼送來的：\n\n" + "\n".join(files) + "\n\n" if files else "")
        + "".join(f"read_file 讀到：「{r[:160]}」\n\n" for r in reads)
        + "".join(f"上一輪的提議結果：{r}\n\n" for r in results)
        + (f"我提議生成 **{n_proposals}** 個作品，按「生成」才會真的做。\n\n" if n_proposals else "")
        + "這是**回音模型**的固定稿，用來檢查聊天畫面，不呼叫任何供應商、不計費。\n\n"
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
        prompt = sum(len(_text(m.get("content"))) for m in messages)  # text only: an image's base64 is not counted
        return {"prompt_tokens": prompt, "completion_tokens": len(text), "total_tokens": prompt + len(text)}

    @staticmethod
    def _answer(model: str, messages: list[dict[str, Any]], tools: Any) -> tuple[str, str, list[dict[str, Any]]]:
        if not echo_tools(model):
            return (*_reply(messages, model), [])
        reads = _read_calls(messages, tools)
        if reads:  # first round of a file read: say so briefly and call the tool
            return "我先用 read_file 讀一下檔案。", "（回音模型的思考）主人要我讀檔，先讀開頭。", reads
        calls = _proposals(messages, tools)
        return (*_reply(messages, model, len(calls)), calls)

    async def complete(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> TextResult:
        text, thought, calls = self._answer(model, messages, kwargs.get("tools"))
        return TextResult(text=text, model=model, reasoning=thought, finish_reason="tool_calls" if calls else "stop",
                          usage=self._usage(messages, text), tool_calls=calls or None, cost_usd=0.0, metadata={"provider": "echo"})

    async def stream(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        text, thought, calls = self._answer(model, messages, kwargs.get("tools"))
        slow = ECHO_MODELS.get(model, ("", False, True, False, False, False))[2]
        delay = float(os.environ.get("OMNIAPI_ECHO_DELAY", "0.04")) if slow else 0.0
        yield {"type": "reasoning", "delta": thought}
        step = 6
        for i in range(0, len(text), step):
            if delay:
                await asyncio.sleep(delay)
            yield {"type": "text", "delta": text[i : i + step]}
        yield {
            "type": "done",
            "result": TextResult(text=text, model=model, reasoning=thought, finish_reason="tool_calls" if calls else "stop",
                                 usage=self._usage(messages, text), tool_calls=calls or None, cost_usd=0.0, metadata={"provider": "echo"}),
        }