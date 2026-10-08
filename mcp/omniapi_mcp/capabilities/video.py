"""Video generation: the provider interface (1.4-M3).

A video takes minutes, so the provider side is three separate calls instead
of one — the job runner (``video/jobs.py``) stores what ``submit`` answers
before it waits, which is what lets a restarted service pick the job up again:

* ``submit(request)``  -> the vendor's job id (the money is committed here)
* ``status(job id)``   -> where the job is (asked again and again, free)
* ``download(job id)`` -> the file, once the job is ``completed``

There is no ``cancel``: OpenRouter has no endpoint for it (both a ``DELETE``
and a ``POST …/cancel`` answered 404 on 2026-10-05 and the job ran on and was
billed). A provider that can cancel would add the method then.

Implementations: ``providers/openrouter_videos.py`` (OpenRouter),
``providers/gemini_videos.py`` (Gemini Omni with the Google key, 1.4-M5) and
the offline sandbox's stand-in (``video/sandbox.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..providers.base import ProviderError

#: remote states, as OpenRouter names them (docs: pending, in_progress,
#: completed, failed; the API reference also lists cancelled and expired)
PENDING = ("pending", "queued", "in_progress", "processing", "running")
FINISHED = ("completed", "failed", "cancelled", "expired")


@dataclass
class VideoRequest:
    """What one video is asked for. Frames are data URLs (``data:image/…``)."""

    model: str
    prompt: str
    duration: Optional[int] = None
    resolution: Optional[str] = None
    aspect_ratio: Optional[str] = None
    generate_audio: Optional[bool] = None
    seed: Optional[int] = None
    first_frame: Optional[str] = None
    last_frame: Optional[str] = None

    def summary(self) -> dict[str, Any]:
        """The request without its images (what the job row keeps)."""
        out = {k: v for k, v in (("model", self.model), ("duration", self.duration), ("resolution", self.resolution),
                                 ("aspect_ratio", self.aspect_ratio), ("generate_audio", self.generate_audio),
                                 ("seed", self.seed)) if v is not None}
        frames = [n for n, v in (("first_frame", self.first_frame), ("last_frame", self.last_frame)) if v]
        if frames:
            out["frames"] = frames
        return out


@dataclass
class Submitted:
    """The vendor took the job (HTTP 202)."""

    remote_id: str
    status: str = "pending"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RemoteState:
    """One answer to "where is the job?"."""

    status: str
    error: Optional[str] = None
    #: what the vendor says it cost (USD), once it says
    cost_usd: Optional[float] = None
    usage: dict[str, Any] = field(default_factory=dict)
    outputs: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in FINISHED


class VideoProviderError(ProviderError):
    """A video call that failed. ``said`` is the vendor's own sentence (an
    OpenRouter 400 names the values the model takes: it is shown as it is).
    ``charged``: ``"no"`` (refused before anything ran), ``"unknown"``, ``"yes"``.
    ``transient``: worth asking again later (network, 5xx, 429)."""

    def __init__(self, message: str, *, provider_name: str, error_code: Optional[str] = None,
                 error_kind: Optional[str] = None, said: Optional[str] = None, charged: str = "unknown",
                 transient: bool = False, status: Optional[int] = None):
        super().__init__(message, provider_name=provider_name, error_code=error_code, error_kind=error_kind)
        self.said = said or message
        self.charged = charged
        self.transient = transient
        self.status = status


class VideoProvider(ABC):
    """Submit, ask, download. One instance per key; ``close`` frees its client."""

    name: str = "video"
    #: the vendor bills by usage it may leave out of its answer: a video collected
    #: without a cost is then billed at its estimate (``charged: likely``), not "unknown"
    estimate_when_unreported: bool = False

    @abstractmethod
    async def submit(self, request: VideoRequest) -> Submitted:
        """Send the request. Raises :class:`VideoProviderError` when it is refused."""

    @abstractmethod
    async def status(self, remote_id: str) -> RemoteState:
        """Where the job is. Raises :class:`VideoProviderError` (``transient``
        when asking again later may work)."""

    @abstractmethod
    async def download(self, remote_id: str, dest: Path, *, index: int = 0) -> int:
        """Write output ``index`` of a completed job to ``dest``; the byte count."""

    async def close(self) -> None:  # pragma: no cover - nothing to free by default
        return None
