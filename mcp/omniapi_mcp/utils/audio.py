"""Audio transcoding through a system ``ffmpeg`` (optional).

Some vendors only emit raw PCM (Gemini TTS). With ffmpeg on PATH that can be
handed back as MP3; without it the caller keeps its WAV fallback — ffmpeg is
never a hard requirement.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Optional

logger = logging.getLogger(__name__)


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


async def pcm_to_mp3(pcm: bytes, *, sample_rate: int, channels: int = 1, bitrate: str = "128k", timeout: float = 60.0) -> Optional[bytes]:
    """Encode signed 16-bit little-endian PCM as MP3. ``None`` when ffmpeg is
    missing or fails (the reason is logged) — the caller falls back to WAV."""
    exe = ffmpeg_path()
    if exe is None:
        return None
    args = [exe, "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels),
            "-i", "pipe:0", "-codec:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3", "pipe:1"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(pcm), timeout=timeout)
    except Exception as e:  # noqa: BLE001 — any failure means "no mp3", never a failed synthesis
        logger.warning("ffmpeg could not be run for the MP3 transcode: %s", e)
        return None
    if proc.returncode != 0 or not out:
        logger.warning("ffmpeg MP3 transcode failed (exit %s): %s", proc.returncode, err.decode("utf-8", "replace")[:300])
        return None
    return out
