"""OpenRouter video provider (1.4-M3).

Endpoints (OpenRouter docs read 2026-10-05; two real runs that day —
``prototypes/openrouter-media-probe/``):

* ``POST {base}/videos`` -> 202 ``{id, polling_url, status: "pending"}``.
  Body: ``model``, ``prompt``, ``duration``, ``resolution``, ``aspect_ratio``,
  ``generate_audio``, ``seed``, ``frame_images`` (each ``{"type":
  "image_url", "image_url": {"url": <data URL>}, "frame_type":
  "first_frame" | "last_frame"}`` — a base64 data URL works, measured).
  A value the model does not take is a 400 whose ``error.message`` names the
  values it does take (measured: nothing is billed).
* ``GET {base}/videos/{id}`` -> ``{status, error?, unsigned_urls?, usage?}``;
  status ``pending`` / ``in_progress`` / ``completed`` / ``failed`` (the API
  reference also lists ``cancelled`` / ``expired``). The docs suggest asking
  every 30 seconds.
* ``GET {base}/videos/{id}/content?index=0`` -> the file (the key is sent;
  the URLs are not presigned).

No cancel endpoint exists (measured: 404 for both ways of asking; the job
ran on and was billed).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from ..capabilities.video import RemoteState, Submitted, VideoProvider, VideoProviderError, VideoRequest

logger = logging.getLogger(__name__)

_CONTENT_WORDS = ("moderation", "flagged", "safety", "content policy", "content_policy", "prohibited", "blocked", "nsfw", "violat")


def _said(resp: httpx.Response) -> str:
    """The vendor's own sentence for a failed request."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        return resp.text[:600].strip() or f"HTTP {resp.status_code}"
    if isinstance(err, str):
        return err[:1000]
    message = str(err.get("message") or "").strip()
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    raw = str(meta.get("raw") or meta.get("reasons") or "")[:600]
    if raw and raw not in message:
        message = f"{message} — {raw}" if message else raw
    return message or f"HTTP {resp.status_code}"


def _error_text(value: Any) -> Optional[str]:
    if value in (None, "", {}):
        return None
    if isinstance(value, dict):
        return str(value.get("message") or value.get("code") or value)[:1000]
    return str(value)[:1000]


class OpenRouterVideoProvider(VideoProvider):
    name = "openrouter"

    def __init__(self, api_key: str, base_url: Optional[str] = None, *, timeout: float = 120.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._key = api_key
        self._base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0), transport=transport,
                                         follow_redirects=True)

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._key}", "X-Title": "OmniAPI"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ submit
    @staticmethod
    def body(req: VideoRequest) -> dict[str, Any]:
        out: dict[str, Any] = {"model": req.model, "prompt": req.prompt}
        for key in ("duration", "resolution", "aspect_ratio", "generate_audio", "seed"):
            value = getattr(req, key)
            if value is not None:
                out[key] = value
        frames = [{"type": "image_url", "image_url": {"url": url}, "frame_type": kind}
                  for kind, url in (("first_frame", req.first_frame), ("last_frame", req.last_frame)) if url]
        if frames:
            out["frame_images"] = frames
        return out

    async def submit(self, request: VideoRequest) -> Submitted:
        try:
            resp = await self._client.post(self._base + "/videos", json=self.body(request), headers=self._headers(True))
        except httpx.TimeoutException as e:
            raise VideoProviderError(
                f"OpenRouter did not answer in time while the video was being sent ({type(e).__name__}); "
                "it may or may not have been accepted — and billed. Check your OpenRouter activity before sending it again.",
                provider_name=self.name, error_code="TIMEOUT", error_kind="timeout", charged="unknown") from e
        except httpx.HTTPError as e:
            raise VideoProviderError(f"could not connect to OpenRouter: {type(e).__name__}: {e}", provider_name=self.name,
                                     error_code="CONNECTION", error_kind="unavailable", charged="no") from e
        if resp.status_code >= 400:
            raise self._error(request.model, resp)
        try:
            j = resp.json()
        except ValueError as e:
            raise VideoProviderError(f"OpenRouter answered {resp.status_code} with something that is not JSON",
                                     provider_name=self.name, error_code="BAD_RESPONSE", error_kind="other",
                                     charged="unknown") from e
        remote_id = j.get("id") if isinstance(j, dict) else None
        if not remote_id:
            raise VideoProviderError(f"OpenRouter accepted the video ({resp.status_code}) but named no job id",
                                     provider_name=self.name, error_code="NO_ID", error_kind="other", charged="unknown")
        return Submitted(remote_id=str(remote_id), status=str(j.get("status") or "pending"),
                         raw={k: v for k, v in j.items() if k != "polling_url"})

    def _error(self, model: str, resp: httpx.Response) -> VideoProviderError:
        status = resp.status_code
        said = _said(resp)
        content = any(w in said.lower() for w in _CONTENT_WORDS)
        if status == 402:
            kind, text = "quota", f"OpenRouter: not enough credits for this video (402): {said}"
        elif status == 401:
            kind, text = "auth", f"OpenRouter rejected the API key (401): {said}"
        elif status == 403:
            kind = "rejected" if content else "auth"
            text = f"OpenRouter refused the request (403): {said}"
        elif status == 429:
            kind, text = "quota", f"OpenRouter rate limit (429): {said}"
        elif status == 413:
            kind, text = "too_large", f"the request is too large for OpenRouter (413) — a frame image may be too big: {said}"
        elif status in (400, 404, 422):
            kind = "rejected" if content else "invalid"
            text = said  # written for people, naming the values the model takes: passed on as it is
        elif status in (408, 504, 524):
            kind, text = "timeout", f"OpenRouter timed out ({status}): {said}"
        else:
            kind, text = "unavailable", f"OpenRouter or the model's provider failed ({status}): {said}"
        # a 4xx is refused before anything runs; a 5xx / timeout may have started the job
        charged = "no" if 400 <= status < 500 and status not in (408,) else "unknown"
        logger.warning("OpenRouter video %s -> HTTP %s (%s)", model, status, kind)
        return VideoProviderError(text, provider_name=self.name, error_code=str(status), error_kind=kind, said=said,
                                  charged=charged, status=status)

    # ------------------------------------------------------------ status
    async def status(self, remote_id: str) -> RemoteState:
        try:
            resp = await self._client.get(f"{self._base}/videos/{remote_id}", headers=self._headers())
        except httpx.HTTPError as e:
            raise VideoProviderError(f"could not reach OpenRouter to ask about the video: {type(e).__name__}",
                                     provider_name=self.name, error_code="CONNECTION", error_kind="unavailable",
                                     transient=True) from e
        if resp.status_code >= 400:
            said = _said(resp)
            code = resp.status_code
            # 408 / 429 / 5xx pass; any other 4xx says the same next time (404: the job is gone)
            transient = code in (408, 429) or code >= 500
            kind = "unavailable" if transient else "lost" if code == 404 else "auth" if code in (401, 403) else "invalid"
            raise VideoProviderError(
                f"OpenRouter answered {code} about the video job: {said}", provider_name=self.name,
                error_code=str(code), error_kind=kind, said=said, transient=transient, status=code)
        try:
            j = resp.json()
        except ValueError as e:
            raise VideoProviderError("OpenRouter's answer about the video job is not JSON", provider_name=self.name,
                                     error_code="BAD_RESPONSE", error_kind="other", transient=True) from e
        usage = j.get("usage") if isinstance(j.get("usage"), dict) else {}
        cost = usage.get("cost")
        return RemoteState(
            status=str(j.get("status") or "unknown").lower(),
            error=_error_text(j.get("error")),
            cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            usage=usage,
            outputs=len(j.get("unsigned_urls") or []),
            raw={k: v for k, v in j.items() if k not in ("polling_url",)},
        )

    # ------------------------------------------------------------ download
    async def download(self, remote_id: str, dest: Path, *, index: int = 0) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            async with self._client.stream("GET", f"{self._base}/videos/{remote_id}/content", params={"index": index},
                                           headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise VideoProviderError(f"OpenRouter answered {resp.status_code} for the video file: {_said(resp)}",
                                             provider_name=self.name, error_code=str(resp.status_code),
                                             error_kind="unavailable",
                                             transient=resp.status_code in (408, 429) or resp.status_code >= 500,
                                             status=resp.status_code)
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                        size += len(chunk)
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise VideoProviderError(f"the video file could not be downloaded: {type(e).__name__}", provider_name=self.name,
                                     error_code="CONNECTION", error_kind="unavailable", transient=True) from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise VideoProviderError("OpenRouter sent an empty video file", provider_name=self.name, error_code="EMPTY",
                                     error_kind="unavailable", transient=True)
        tmp.replace(dest)
        return size
