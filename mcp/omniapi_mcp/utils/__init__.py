"""Utility functions and helpers."""

from .cache import CacheManager
from .openai_client import OpenAIClientManager

__all__ = [
    "CacheManager",
    "OpenAIClientManager",
    "detect_image_mime",
    "prepare_image_upload",
]


def detect_image_mime(data_url: str | None, image_bytes: bytes) -> tuple[str, str]:
    """Return (filename, mime_type) from a data-URL prefix or byte magic.

    Falls back to PNG when the format cannot be determined.
    """
    if data_url and data_url.startswith("data:"):
        # e.g. "data:image/webp;base64,..."
        mime = data_url.split(";", 1)[0].removeprefix("data:")
        ext = mime.split("/", 1)[1] if "/" in mime else "png"
        return f"image.{ext}", mime

    # Magic-byte sniffing
    if image_bytes[:4] == b"\x89PNG":
        return "image.png", "image/png"
    if image_bytes[:2] == b"\xff\xd8":
        return "image.jpeg", "image/jpeg"
    is_webp = (
        len(image_bytes) >= 12
        and image_bytes[:4] == b"RIFF"
        and image_bytes[8:12] == b"WEBP"
    )
    if is_webp:
        return "image.webp", "image/webp"

    return "image.png", "image/png"


def prepare_image_upload(
    image_data: str | bytes,
) -> tuple[str | None, bytes, tuple[str, bytes, str]]:
    """Decode image input and build an SDK upload tuple.

    Accepts a base64 string, a data-URL string, or raw bytes.

    Returns:
        (data_url_or_none, raw_bytes, upload_tuple)
        where upload_tuple is (filename, raw_bytes, mime_type).
    """
    import base64

    data_url = (
        image_data
        if isinstance(image_data, str) and image_data.startswith("data:")
        else None
    )
    if isinstance(image_data, str):
        if image_data.startswith("data:"):
            image_data = image_data.split(",", 1)[1]
        image_bytes = base64.b64decode(image_data)
    else:
        image_bytes = image_data

    name, mime = detect_image_mime(data_url, image_bytes)
    return data_url, image_bytes, (name, image_bytes, mime)
