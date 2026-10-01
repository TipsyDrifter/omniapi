"""Path utilities for image storage and URL generation."""

from datetime import datetime
from pathlib import Path
from typing import Optional


def extract_date_from_image_id(image_id: str) -> Optional[datetime]:
    """
    Extract datetime from image_id.

    Expected format: img_YYYYMMDDHHMMSS_{random}
    Returns datetime object or None if parsing fails.
    """
    try:
        # Split by underscore and get the timestamp part
        parts = image_id.split("_")
        if len(parts) >= 2 and parts[0] == "img":
            timestamp_str = parts[1]
            if len(timestamp_str) == 14:  # YYYYMMDDHHMMSS
                return datetime.strptime(timestamp_str, "%Y%m%d%H%M%S")
    except (ValueError, IndexError):
        pass
    return None


def build_image_storage_path(
    base_path: Path, image_id: str, file_format: str = "png"
) -> Path:
    """
    Build the full storage path for an image including date structure.

    Args:
        base_path: Base storage directory
        image_id: Image identifier containing timestamp
        file_format: File extension (png, jpg, etc.)

    Returns:
        Full path including year/month/day structure
    """
    # Extract date from image_id
    img_date = extract_date_from_image_id(image_id)

    if img_date is None:
        img_date = datetime.now()

    # Single flat date folder: yyyy-mm-dd (was yyyy/mm/dd which buried files
    # 3 levels deep — easier to browse with one folder per day).
    date_folder = img_date.strftime("%Y-%m-%d")
    date_path = base_path / "images" / date_folder

    return date_path / f"{image_id}.{file_format.lower()}"


def build_image_url_path(image_id: str, file_format: str = "png") -> str:
    """
    Build the URL path for an image including date structure.

    Args:
        image_id: Image identifier containing timestamp
        file_format: File extension (png, jpg, etc.)

    Returns:
        URL path like "images/2024-01-15/img_20240115123456_abc123.png"
    """
    img_date = extract_date_from_image_id(image_id) or datetime.now()
    date_folder = img_date.strftime("%Y-%m-%d")
    return f"images/{date_folder}/{image_id}.{file_format.lower()}"


def get_transport_type(default: str = "stdio") -> str:
    """Detect the MCP transport from the process CLI args (``--transport X``).

    The FastMCP app object does not expose the active transport, so the CLI
    argument (set in server.main) is the practical source of truth. Defaults
    to stdio — the Claude Desktop case.
    """
    import sys

    argv = getattr(sys, "argv", [])
    for i, arg in enumerate(argv):
        if arg == "--transport" and i + 1 < len(argv):
            return argv[i + 1]
    return default


def build_image_access_url(
    image_id: str,
    file_format: str,
    *,
    base_host: Optional[str],
    server_host: str,
    server_port: int,
    storage_base_path: str,
) -> str:
    """Build the externally visible URL for a stored image.

    Priority: configured CDN/nginx ``base_host`` → MCP HTTP endpoint (http
    transports) → ``file://`` path (stdio — Claude Desktop reads local files).
    Shared by the generation and editing tools so the URL scheme cannot drift
    between them.
    """
    if base_host:
        url_path = build_image_url_path(image_id, file_format)
        return f"{base_host.rstrip('/')}/{url_path}"
    if get_transport_type() in ("streamable-http", "sse"):
        return f"http://{server_host}:{server_port}/images/{image_id}"
    image_path = build_image_storage_path(
        Path(storage_base_path), image_id, file_format
    )
    return f"file://{image_path.absolute()}"


def find_existing_image_path(base_path: Path, image_id: str) -> Optional[Path]:
    """
    Find an existing image file by trying different formats and using date
    from image_id.

    Args:
        base_path: Base storage directory
        image_id: Image identifier

    Returns:
        Path to existing image file or None if not found
    """
    # Try different file formats
    for ext in ["png", "jpg", "jpeg", "webp", "gif"]:
        image_path = build_image_storage_path(base_path, image_id, ext)
        if image_path.exists():
            return image_path

    return None
