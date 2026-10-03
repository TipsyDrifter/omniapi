"""Stand-in generators for the offline sandbox (``OMNIAPI_DEV=1`` together
with ``OMNIAPI_OFFLINE=1``): a picture, a tone, a canned transcript — written
through the same storage paths and returned in the same shape as the real
tools, so the works index, the file endpoints and the GUI can be exercised
end to end without a vendor call. Same idea as the ``echo`` text model.

``OMNIAPI_FAKE_DELAY`` (seconds, default 0) makes each one take a while,
for trying out the waiting states.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import struct
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles

FAKEABLE = frozenset({"generate_image", "edit_image", "generate_speech", "generate_music", "transcribe_audio"})

_TRANSCRIPT = (
    "（示範逐字稿，離線沙盒不會呼叫轉錄模型）\n"
    "大家好，這是一段示範用的逐字稿。它的用途是讓作品庫、檔案端點與介面在不花錢的情況下走完整個流程。\n"
    "第二段：真正的轉錄會在這裡放進錄音的內容，長度可能有好幾千字。"
)


async def _delay() -> None:
    try:
        seconds = float(os.environ.get("OMNIAPI_FAKE_DELAY", "0") or 0)
    except ValueError:
        seconds = 0.0
    if seconds > 0:
        await asyncio.sleep(seconds)


def _png(prompt: str, label: str, size: tuple[int, int] = (1024, 1024)) -> bytes:
    from PIL import Image, ImageDraw

    h = hashlib.sha256(prompt.encode("utf-8")).digest()
    bg = (60 + h[0] % 140, 60 + h[1] % 140, 60 + h[2] % 140)
    fg = (255 - bg[0] // 2, 255 - bg[1] // 2, 255 - bg[2] // 2)
    im = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(im)
    w, hh = size
    for i in range(6):  # a few prompt-dependent shapes so two fakes are told apart at a glance
        x, y, r = h[3 + i] / 255 * w, h[9 + i] / 255 * hh, 40 + h[15 + i] % 160
        d.ellipse((x - r, y - r, x + r, y + r), outline=fg, width=6)
    d.rectangle((0, hh - 96, w, hh), fill=(20, 20, 20))
    d.text((24, hh - 72), f"{label} · offline sandbox · {prompt[:60]}".encode("ascii", "replace").decode(), fill=(240, 240, 240))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _wav(seconds: float, freqs: tuple[float, ...], rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        step = max(1, n // len(freqs))
        frames = bytearray()
        for i in range(n):
            f = freqs[min(i // step, len(freqs) - 1)]
            env = min(1.0, (i % step) / 800, (step - i % step) / 800)  # no clicks between notes
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * f * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


async def _write(base: Path, folder: str, prefix: str, ext: str, data: bytes) -> Path:
    now = datetime.now(timezone.utc)
    out_dir = base / folder / now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = (out_dir / f"{prefix}_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}").resolve()
    async with aiofiles.open(path, "wb") as f:
        await f.write(data)
    return path


async def fake_generate(tool: str, kwargs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Produce the result ``tool`` would have returned, without a vendor."""
    await _delay()
    base = Path(ctx.settings.storage.base_path)
    if tool in ("generate_image", "edit_image"):
        prompt = str(kwargs.get("prompt") or "")
        model = "echo-image"
        n = kwargs.get("n") if isinstance(kwargs.get("n"), int) and tool == "generate_image" else 1
        saved = []
        for i in range(max(1, min(n or 1, 4))):
            data = _png(f"{prompt}#{i}" if i else prompt, "edit" if tool == "edit_image" else "image")
            image_id, _ = await ctx.storage_manager.save_image(
                image_data=data,
                metadata={"prompt": prompt, "model": model, "provider": "echo", "parameters": {"size": "1024x1024"},
                          "cost_info": {"estimated_cost_usd": 0.0}, **({"operation": "edit"} if tool == "edit_image" else {})},
                file_format="png",
            )
            saved.append({"image_id": image_id, "image_url": f"/images/{image_id}", "resource_uri": f"generated-images://{image_id}", "file_size_bytes": len(data)})
        out: dict[str, Any] = {
            "task_id": f"fake_{uuid.uuid4().hex[:8]}",
            **{k: saved[0][k] for k in ("image_id", "image_url", "resource_uri")},
            "metadata": {"model": model, "provider": "echo", "size": "1024x1024", "output_format": "png", "prompt": prompt, "cost_estimate": 0.0},
        }
        if tool == "edit_image":
            out["operation"] = "edit"
        if len(saved) > 1:
            out["images"], out["count"] = saved, len(saved)
        return out
    if tool == "generate_speech":
        text = str(kwargs.get("text") or "")
        data = _wav(min(6.0, 1.0 + len(text) / 40), (440.0,))
        path = await _write(base, "audio", "speech", "wav", data)
        return {"audio_path": str(path), "audio_url": f"file://{path}", "model": "echo-speech", "provider": "echo",
                "voice_id": kwargs.get("voice") or "echo", "output_format": "wav", "bytes": len(data), "cost_usd": 0.0}
    if tool == "generate_music":
        data = _wav(8.0, (261.6, 329.6, 392.0, 523.3, 392.0, 329.6, 261.6, 196.0))
        path = await _write(base, "music", "music", "wav", data)
        return {"audio_path": str(path), "audio_url": f"file://{path}", "model": "echo-music", "provider": "echo", "operation": "generate",
                "title": kwargs.get("title") or "示範曲", "duration": 8.0, "output_format": "wav", "bytes": len(data), "cost_usd": 0.0}
    if tool == "transcribe_audio":
        name = Path(str(kwargs.get("audio_path") or "audio")).name
        return {"text": f"{_TRANSCRIPT}\n（來源檔：{name}）", "model": "echo-transcribe", "provider": "echo", "language": "zh", "duration": 12.0, "cost_usd": 0.0}
    raise ValueError(f"no stand-in for {tool}")
