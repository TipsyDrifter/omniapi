"""Chat attachments → the bytes a vision model is sent (1.2-M2).

A stored attachment is an id (決策記錄 1.2-M1-a); right before a reply the
manager resolves it to the file and asks ``prepare_image`` for a base64
payload every vendor accepts. The OpenAI-style ``image_url`` data URL built
from it is the one wire shape chat uses; Anthropic's provider turns it into
its own ``image`` block in its message conversion.

Limits (official docs, checked 2026-10-03, see
docs/research/2026-10-03-各家看圖格式與上限查證.md): Anthropic takes 10 MB
per image after base64, at most 8000x8000 px, and scales anything over
1568 px on the long side itself; Gemini's inline data caps the whole request
at 20 MB; OpenAI the whole request at 512 MB. The constants below are one
conservative set that fits all of them.
"""

from __future__ import annotations

import base64
import io
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

#: longest side sent, px (conservative for every vendor; see module docstring)
MAX_IMAGE_SIDE = 2000
#: one image after base64, bytes (Anthropic allows 10 MB; half of that)
MAX_IMAGE_B64_BYTES = 5 * 1024 * 1024
#: all images of one request after base64, bytes (Gemini allows 20 MB inline);
#: past it the older images are left out and counted as skipped
MAX_REQUEST_IMAGE_B64_BYTES = 15 * 1024 * 1024
#: formats every vendor reads as they are; anything else is re-encoded
PASSTHROUGH = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_MIN_SIDE = 64  # stop shrinking here: an image this small that is still too big is not an image we can send
_CACHE_BYTES = 40 * 1024 * 1024  # prepared payloads kept in memory (a few requests' worth)


class ImageUnreadable(ValueError):
    """The file is not an image Pillow can open (or cannot be made small enough)."""


@dataclass(frozen=True)
class PreparedImage:
    mime: str
    data: str  # base64

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.data}"

    def part(self) -> dict:
        """The OpenAI-style content part (the one shape chat sends)."""
        return {"type": "image_url", "image_url": {"url": self.data_url}}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


def _encode(img: Image.Image, alpha: bool, quality: int) -> tuple[str, bytes]:
    buf = io.BytesIO()
    if alpha:
        img.convert("RGBA").save(buf, format="PNG", optimize=True)
        return "image/png", buf.getvalue()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return "image/jpeg", buf.getvalue()


def _prepare(raw: bytes) -> PreparedImage:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # not an image, truncated, unknown format
        raise ImageUnreadable(str(e)) from e
    fmt = (img.format or "").upper()
    orientation = img.getexif().get(0x0112, 1) if hasattr(img, "getexif") else 1
    if (fmt in PASSTHROUGH and max(img.size) <= MAX_IMAGE_SIDE and orientation in (None, 1)
            and len(raw) * 4 // 3 + 4 <= MAX_IMAGE_B64_BYTES):
        return PreparedImage(PASSTHROUGH[fmt], _b64(raw))
    if fmt == "GIF":
        img.seek(0)  # first frame only
        frame = img.convert("RGBA")
        alpha = True  # a GIF frame goes out as PNG
    else:
        frame = ImageOps.exif_transpose(img)
        alpha = _has_alpha(frame)
    side = min(MAX_IMAGE_SIDE, max(frame.size))
    quality = 90
    while True:
        work = frame.copy()
        work.thumbnail((side, side), Image.LANCZOS)
        mime, out = _encode(work, alpha, quality)
        if len(out) * 4 // 3 + 4 <= MAX_IMAGE_B64_BYTES:
            return PreparedImage(mime, _b64(out))
        if not alpha and quality > 60:
            quality -= 15
            continue
        side = int(side * 0.75)
        if side < _MIN_SIDE:
            raise ImageUnreadable("image could not be made small enough to send")


_cache: OrderedDict[tuple[str, int, int], PreparedImage] = OrderedDict()
_lock = threading.Lock()


def prepare_image(path: str | Path) -> PreparedImage:
    """``path`` → a payload within the limits above (passed through as it is
    when it already fits, else resized / re-encoded). Raises
    ``FileNotFoundError`` or ``ImageUnreadable``. Blocking: call it from a
    worker thread. Results are cached by (path, mtime, size), so a long
    conversation does not re-encode its old images on every turn."""
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    prepared = _prepare(p.read_bytes())
    with _lock:
        _cache[key] = prepared
        while len(_cache) > 1 and sum(v.size for v in _cache.values()) > _CACHE_BYTES:
            _cache.popitem(last=False)
    return prepared
