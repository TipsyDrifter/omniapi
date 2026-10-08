"""The offline sandbox's stand-in video vendor (``OMNIAPI_DEV=1`` +
``OMNIAPI_OFFLINE=1``): same three calls as OpenRouter, nothing leaves.

A "job" is a small JSON file under ``<data home>/sandbox/videos/`` — so it
outlives the process, and a sandbox restarted in the middle of a video picks
it up again exactly like the real thing. Where the job is follows the clock:

* ``pending`` for the first third of ``OMNIAPI_FAKE_DELAY`` seconds,
  ``in_progress`` for the rest, then ``completed`` (with ``usage.cost`` 0)
* a prompt containing ``[fail]`` ends ``failed`` (a content-policy message);
  ``[never]`` stays ``in_progress`` for good (to try "waited too long");
  ``[gone]`` is forgotten by the "vendor" when it would be done (a 404);
  ``OMNIAPI_FAKE_VIDEO=fail|never`` does the same for every job

The file it hands back is a real, playable MP4 bundled with the package
(256×144, 2 s, H.264 + AAC; a 144×256 one without sound for a request
without sound) — no ffmpeg needed.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..capabilities.video import RemoteState, Submitted, VideoProvider, VideoProviderError, VideoRequest
from ..catalog.paths import data_home

SAMPLE_WITH_SOUND = Path(__file__).with_name("sandbox") / "sample_av.mp4"
SAMPLE_SILENT = Path(__file__).with_name("sandbox") / "sample_v.mp4"


def _delay() -> float:
    try:
        return max(0.0, float(os.environ.get("OMNIAPI_FAKE_DELAY", "0") or 0))
    except ValueError:
        return 0.0


class SandboxVideoProvider(VideoProvider):
    name = "sandbox"

    def __init__(self, root: Path | None = None):
        self.root = root or (data_home() / "sandbox" / "videos")

    def _path(self, remote_id: str) -> Path:
        safe = "".join(ch for ch in remote_id if ch.isalnum() or ch in "-_")
        return self.root / f"{safe}.json"

    def _read(self, remote_id: str) -> dict[str, Any]:
        p = self._path(remote_id)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise VideoProviderError(f"Video job {remote_id} not found", provider_name=self.name, error_code="404",
                                     error_kind="lost", said=f"Video job {remote_id} not found", transient=False,
                                     status=404) from None

    async def submit(self, request: VideoRequest) -> Submitted:
        mode = os.environ.get("OMNIAPI_FAKE_VIDEO", "").strip().lower()
        prompt = request.prompt or ""
        for marker in ("fail", "never", "gone"):
            if f"[{marker}]" in prompt:
                mode = marker
        remote_id = f"sbx-{uuid.uuid4().hex[:12]}"
        self.root.mkdir(parents=True, exist_ok=True)
        job = {"id": remote_id, "created": time.time(), "delay": _delay(), "mode": mode, **request.summary(),
               "silent": request.generate_audio is False}
        self._path(remote_id).write_text(json.dumps(job), encoding="utf-8")
        return Submitted(remote_id=remote_id, status="pending", raw={"id": remote_id, "status": "pending", "sandbox": True})

    async def status(self, remote_id: str) -> RemoteState:
        job = self._read(remote_id)
        elapsed = time.time() - float(job.get("created") or 0)
        delay = float(job.get("delay") or 0)
        if job.get("mode") == "never" or elapsed < delay:
            state = "pending" if elapsed < delay / 3 else "in_progress"
            return RemoteState(status=state, raw={"id": remote_id, "status": state})
        if job.get("mode") == "fail":
            return RemoteState(status="failed", error="Content policy violation (sandbox stand-in)",
                               raw={"id": remote_id, "status": "failed"})
        if job.get("mode") == "gone":
            self._path(remote_id).unlink(missing_ok=True)
            raise VideoProviderError(f"Video job {remote_id} not found", provider_name=self.name, error_code="404",
                                     error_kind="lost", said=f"Video job {remote_id} not found", transient=False, status=404)
        usage = {"cost": 0.0, "is_byok": False, "sandbox": True}
        return RemoteState(status="completed", cost_usd=0.0, usage=usage, outputs=1,
                           raw={"id": remote_id, "status": "completed", "usage": usage,
                                "unsigned_urls": [f"sandbox://{remote_id}/content?index=0"]})

    async def download(self, remote_id: str, dest: Path, *, index: int = 0) -> int:
        job = self._read(remote_id)
        src = SAMPLE_SILENT if job.get("silent") else SAMPLE_WITH_SOUND
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest.stat().st_size
