"""Gemini Omni, called directly with the Google key (1.4-M5).

The shape (Google's docs read 2026-10-06; copied into
``docs/research/2026-10-06-gemini-omni-interactions-api形狀.md``):

* ``POST {base}/interactions`` with ``model``, ``input`` (the prompt, or a
  list of ``{"type": "image", "data": <base64>, "mime_type"}`` blocks and a
  ``{"type": "text"}`` one), ``response_format`` (``{"type": "video",
  "aspect_ratio": "16:9" | "9:16", "resolution": "360p" | "720p" | "1080p" |
  "4k", "duration": "<n>s", "delivery": "inline" | "uri"}``) and
  ``generation_config.video_config.task`` (``image_to_video``: the first
  image is the starting frame, an optional second one the ending frame).
  The key goes in the ``x-goog-api-key`` header (never in the URL).
* ``background: true`` (with ``store: true``, which it needs) answers at once
  with the interaction's ``id`` and ``status: "in_progress"``; the work goes on
  at Google. Without it the call holds until the video is made (the docs'
  examples do that).
* ``GET {base}/interactions/{id}`` -> ``{id, status, steps, usage, errors?}``;
  status ``in_progress`` / ``requires_action`` / ``completed`` / ``failed`` /
  ``cancelled``. The video is a ``{"type": "video", "mime_type": "video/mp4",
  "data": <base64>}`` block in the ``model_output`` step's ``content`` (the docs:
  a GET answers inline base64 even for an interaction made with ``delivery:
  "uri"``; a ``uri`` block is downloaded with the key once its file is ``ACTIVE``
  — ``GET {base}/files/{id}`` -> ``state`` PROCESSING / ACTIVE / FAILED).
* ``usage``: ``total_input_tokens``, ``total_output_tokens``,
  ``total_thought_tokens``, ``output_tokens_by_modality`` (``[{modality,
  tokens}]``). Billing is per output token ($17.50 per 1M video tokens,
  5,792 per second of 720p) — the cost is worked out from these.
* Stored interactions are kept 55 days on the paid tier. There is a cancel
  endpoint (``POST …/{id}/cancel``, background interactions only); the job
  runner has no use for it yet (its "stop waiting" only ends our waiting).

MEASURED 2026-10-06 with the owner's key (three runs):

* the waiting way (no ``background``) works: the POST held 30.6 s and answered
  ``completed`` with the video inline (3 s asked, 3.008 s 1280x720 with sound
  came back: ``response_format.duration`` is honoured); ``usage`` carried
  ``output_tokens_by_modality`` (video 17,376), ``total_thought_tokens`` and its
  own ``cost`` (0.309401 — :func:`cost_from_usage` gave the same).
* ``background: true`` is accepted (the POST answers ``in_progress`` with an
  id), but every ``GET /interactions/{id}`` of that interaction then answers
  400 "Multiple authentication credentials received" — with the key in the
  header alone, or in the query alone. A plain text interaction stored the
  same way GETs fine with either. So Google cannot be asked about a background
  Omni interaction (a misleading error on its side, not a second credential
  of ours) and **the waiting way is the default**; the background way stays
  behind ``OMNIAPI_GEMINI_VIDEO_BACKGROUND=1`` for when Google fixes it.

What waiting means for a restart: the interaction id only comes back with the
finished video, so a service that dies while it waits has nothing to ask
about — the job runner marks it lost ("sent, probably billed, cannot be
collected"). Once the answer is in, the video sits in the local cache and a
restart collects it from there.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from ..capabilities.video import RemoteState, Submitted, VideoProvider, VideoProviderError, VideoRequest

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
#: how long a waiting create may hold when the caller names nothing (the job runner passes the
#: owner's "longest wait", ``video.max_wait_minutes``): a video took 30 s for 3 s of 720p on 2026-10-06
SYNC_TIMEOUT = 1200.0
#: ``1`` = send in the background and ask by id (off: Google answers every GET of a background Omni
#: interaction with a 400, measured 2026-10-06; see the module docstring)
BACKGROUND_ENV = "OMNIAPI_GEMINI_VIDEO_BACKGROUND"


def background_wanted() -> bool:
    return os.environ.get(BACKGROUND_ENV, "").strip().lower() in ("1", "true", "yes", "on")
#: an uploaded-file URI is polled this often / this long before its bytes are fetched
FILE_POLL_S = 3.0
FILE_POLL_MAX_S = 120.0

_CONTENT_WORDS = ("safety", "blocked", "prohibited", "policy", "violat", "sensitive", "harm", "recitation")
#: words of Google's answer when the project has no paid tier (the free tier has no Omni quota)
_PAID_WORDS = ("paid tier", "billing", "free tier", "free_tier", "limit: 0", "paid plan", "prepay", "prepaid",
               "upgrade your plan", "requires a paid", "not available on the free")
PAID_TIER_MESSAGE = ("Gemini 影片要開付費層：這把 Google key 所在的專案還沒開帳單（或付費層還沒生效），Omni 沒有免費額度。"
                     "到 Google AI Studio 為這個專案開啟帳單後再送。")

#: query parameters that are a credential of their own (a signed or keyed URL): a redirect to such a URL
#: is followed without our key — sending both is what Google answers with "Multiple authentication credentials"
_CRED_PARAMS = ("key", "access_token", "token", "sig", "signature", "x-goog-signature", "x-goog-credential",
                "x-amz-signature", "googleaccessid")
_REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5
#: Google's words when a request carried two credentials (the key and something else)
_MULTI_CRED = "multiple authentication credentials"

STATUS_MAP = {"in_progress": "in_progress", "requires_action": "failed", "completed": "completed",
              "failed": "failed", "cancelled": "cancelled"}


def api_base(base_url: Optional[str]) -> str:
    """The ``…/v1beta`` root from the Gemini slot's base URL (which may carry a
    trailing slash, or be the OpenAI-compatible ``…/v1beta/openai/``)."""
    base = (base_url or DEFAULT_BASE).strip().rstrip("/")
    m = re.match(r"^(https?://[^/]+/v1(?:beta|alpha)?)(?:/.*)?$", base)
    return m.group(1) if m else base


def _said(resp: httpx.Response) -> tuple[str, str]:
    """(Google's own sentence, its ``status`` word such as RESOURCE_EXHAUSTED)."""
    try:
        j = resp.json()
    except ValueError:
        return (resp.text[:600].strip() or f"HTTP {resp.status_code}"), ""
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, str):
        return err[:1000], ""
    if isinstance(err, dict):
        return (str(err.get("message") or "").strip()[:1000] or f"HTTP {resp.status_code}"), str(err.get("status") or "")
    return f"HTTP {resp.status_code}", ""


def mask_url(url: Any) -> str:
    """A URL with the value of any credential-like query parameter hidden."""
    try:
        u = httpx.URL(str(url))
    except Exception:  # noqa: BLE001 — only for logs
        return "<unreadable url>"
    if not u.params:
        return str(u)
    params = [(k, "***" if k.lower() in _CRED_PARAMS else v) for k, v in u.params.multi_items()]
    return str(u.copy_with(params=params))


def transient_status(code: int) -> bool:
    """Worth asking again later: 408, 429 and 5xx. Any other 4xx says the same next time."""
    return code in (408, 429) or code >= 500


def _data_url(url: str) -> tuple[str, str]:
    """``data:image/png;base64,AAAA`` -> (``image/png``, ``AAAA``)."""
    m = re.match(r"^data:([^;,]+)(;base64)?,(.*)$", url, re.S)
    if not m or not m.group(2):
        raise VideoProviderError("a frame image is not a base64 data URL", provider_name="google", error_code="BAD_FRAME",
                                 error_kind="invalid", charged="no")
    return m.group(1), m.group(3)


def video_block(interaction: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The last video content block of the model's output (``steps``; the
    older ``outputs`` list too, and the SDK's ``output_video`` if present)."""
    found = None
    direct = interaction.get("output_video")
    if isinstance(direct, dict) and (direct.get("data") or direct.get("uri")):
        found = direct
    for holder in ("steps", "outputs"):
        for step in interaction.get(holder) or []:
            if not isinstance(step, dict) or step.get("type") in ("user_input", "thought"):
                continue
            parts = step.get("content") if isinstance(step.get("content"), list) else [step]
            for part in parts:
                if isinstance(part, dict) and (part.get("type") == "video" or str(part.get("mime_type") or "").startswith("video/")) \
                        and (part.get("data") or part.get("uri")):
                    found = part
    return found


def output_text(interaction: dict[str, Any]) -> str:
    """What the model said in words (a refusal comes back as text, not as an error)."""
    out: list[str] = []
    for step in interaction.get("steps") or interaction.get("outputs") or []:
        if not isinstance(step, dict) or step.get("type") in ("user_input", "thought"):
            continue
        for part in step.get("content") if isinstance(step.get("content"), list) else [step]:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                out.append(str(part["text"]))
    return " ".join(out).strip()[:1000]


def _errors(interaction: dict[str, Any]) -> Optional[str]:
    errs = interaction.get("errors")
    if isinstance(errs, list) and errs:
        return "; ".join(str((e or {}).get("message") or (e or {}).get("code") or e) for e in errs if e)[:1000] or None
    err = interaction.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)[:1000]
    return str(err)[:1000] if err else None


def _int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def usage_cost(usage: Any, pricing: Optional[dict[str, Any]]) -> Optional[float]:
    """What the video cost: the ``cost`` Google puts in the usage (seen 2026-10-06),
    else :func:`cost_from_usage` from the token counts."""
    if isinstance(usage, dict):
        c = usage.get("cost")
        if isinstance(c, (int, float)) and not isinstance(c, bool) and c >= 0:
            return round(float(c), 6)
    return cost_from_usage(usage, pricing)


def cost_from_usage(usage: Any, pricing: Optional[dict[str, Any]]) -> Optional[float]:
    """USD from an interaction's ``usage`` and the catalog's per-1M rates
    (``video_output``, ``text_output`` — thinking counts as text output, per
    the pricing page — and ``media_input``). ``None`` when the usage names no
    output tokens. Without a per-modality breakdown every output token is
    taken as video (the dearer rate: a guess, on the safe side)."""
    if not isinstance(usage, dict) or not isinstance(pricing, dict):
        return None
    video_rate = pricing.get("video_output")
    if not isinstance(video_rate, (int, float)):
        return None
    text_rate = float(pricing.get("text_output") or video_rate)
    input_rate = float(pricing.get("media_input") or 0.0)
    by_modality = usage.get("output_tokens_by_modality") if isinstance(usage.get("output_tokens_by_modality"), list) else []
    video = sum(_int(m.get("tokens")) for m in by_modality if isinstance(m, dict) and str(m.get("modality") or "").lower() == "video")
    other = sum(_int(m.get("tokens")) for m in by_modality if isinstance(m, dict) and str(m.get("modality") or "").lower() != "video")
    total_out = _int(usage.get("total_output_tokens"))
    if not by_modality:
        video, other = total_out, 0
    elif total_out > video + other:
        other = total_out - video
    thought = _int(usage.get("total_thought_tokens"))
    if video + other + thought == 0:
        return None
    usd = (video * float(video_rate) + (other + thought) * text_rate + _int(usage.get("total_input_tokens")) * input_rate) / 1_000_000
    return round(usd, 6)


def _slim(interaction: dict[str, Any]) -> dict[str, Any]:
    """The interaction without its base64 (what a job row may keep)."""
    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: (f"<{len(x)} base64 chars>" if k == "data" and isinstance(x, str) else strip(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return strip({k: v for k, v in interaction.items() if k not in ("input",)})


class GeminiVideoProvider(VideoProvider):
    """Submit, ask, download — Gemini Omni over the Interactions API (httpx:
    the installed ``google-genai`` 1.66 knows no video response format)."""

    name = "google"
    #: Google reports usage (tokens), not a price; a completed answer without usage is billed at the estimate
    estimate_when_unreported = True

    def __init__(self, api_key: str, base_url: Optional[str] = None, *, timeout: float = 120.0,
                 sync_timeout: float = SYNC_TIMEOUT, transport: Optional[httpx.AsyncBaseTransport] = None,
                 cache_dir: Optional[Path] = None, pricing: Optional[Any] = None, file_poll_s: float = FILE_POLL_S,
                 background: Optional[bool] = None):
        self._key = api_key
        #: the background way (an id at once, then asking): off by default, see the module docstring
        self.background = background_wanted() if background is None else bool(background)
        self._base = api_base(base_url)
        self._timeout = timeout
        self._sync_timeout = max(sync_timeout, timeout)
        # redirects are followed by hand (``_send``): httpx would carry the key header to the next hop,
        # and a hop that has its own credential then holds two
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0), transport=transport,
                                         follow_redirects=False)
        self._cache_dir = cache_dir
        #: model id -> the catalog pricing (a callable, so a test can hand in a dict)
        self._pricing = pricing
        self._file_poll_s = file_poll_s
        #: called with one dict per HTTP hop (method, masked URL, header names, status, where it
        #: redirected to) — the probe writes them out; never the key itself
        self.trace: Optional[Any] = None

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {"x-goog-api-key": self._key}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _trace_hop(self, req: httpx.Request, resp: httpx.Response, hop: int) -> None:
        if self.trace is None:
            return
        try:
            sent = {k: (f"<set, {len(v)} chars>" if k.lower() in ("x-goog-api-key", "authorization", "cookie",
                                                                    "proxy-authorization") else v)
                    for k, v in req.headers.items()}
            got = {k: (mask_url(v) if k.lower() == "location" else v) for k, v in resp.headers.items()
                   if k.lower() in ("location", "content-type", "www-authenticate", "x-goog-request-id", "server",
                                    "content-length", "via", "alt-svc")}
            self.trace({"hop": hop, "method": req.method, "url": mask_url(req.url), "sent": sent, "status": resp.status_code,
                        "got": got})
        except Exception as e:  # noqa: BLE001 — tracing never breaks a call
            logger.debug("trace failed: %s", e)

    async def _send(self, method: str, url: str, *, json_body: Any = None, stream: bool = False,
                    timeout: Optional[httpx.Timeout] = None) -> httpx.Response:
        """One request, its redirects followed by hand. The key goes to Google's own
        API host only; a hop to another host, or to a URL that carries its own
        credential, goes without it. ``stream``: the caller reads and closes."""
        headers = self._headers(json_body is not None)
        api_host = httpx.URL(self._base).host
        for hop in range(MAX_REDIRECTS + 1):
            req = self._client.build_request(method, url, headers=headers, json=json_body,
                                             **({"timeout": timeout} if timeout is not None else {}))
            resp = await self._client.send(req, stream=stream)
            self._trace_hop(req, resp, hop)
            location = resp.headers.get("location")
            if resp.status_code not in _REDIRECTS or not location or hop == MAX_REDIRECTS:
                return resp
            if stream:
                await resp.aclose()
            nxt = req.url.join(location)
            own_credential = any(k.lower() in _CRED_PARAMS for k in nxt.params.keys())
            if nxt.host != api_host or own_credential:
                headers = {k: v for k, v in headers.items() if k.lower() != "x-goog-api-key"}
            if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                method, json_body = "GET", None
                headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            url = str(nxt)
        return resp  # pragma: no cover - the loop returns

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ helpers
    def _cache(self) -> Path:
        if self._cache_dir is None:
            from ..catalog.paths import data_home

            self._cache_dir = data_home() / "cache" / "gemini-videos"
        return self._cache_dir

    def _cached(self, remote_id: str) -> Path:
        safe = "".join(ch for ch in remote_id if ch.isalnum() or ch in "-_.") or uuid.uuid4().hex
        return self._cache() / f"{safe}.mp4"

    def _keep(self, remote_id: str, block: dict[str, Any]) -> bool:
        """Write an inline video to the local cache (so ``download`` needs no
        second request). False when the block has no inline data."""
        data = block.get("data")
        if not isinstance(data, str) or not data:
            return False
        try:
            raw = base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError):
            return False
        if not raw:
            return False
        path = self._cached(remote_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(raw)
        tmp.replace(path)
        return True

    def _note(self, remote_id: str, usage: dict[str, Any], cost: Optional[float], uri: Optional[str] = None) -> None:
        """What Google reported next to a cached video (its usage and cost; ``uri``
        when the video came as a file to fetch), for an answer read from the
        cache later (after a restart, or the waiting way's)."""
        try:
            path = self._cached(remote_id).with_suffix(".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"usage": usage, "cost": cost, **({"uri": uri} if uri else {})}), encoding="utf-8")
        except OSError as e:  # pragma: no cover - the cost then falls back to the estimate
            logger.debug("could not note the usage of %s: %s", remote_id, e)

    def _noted(self, remote_id: str) -> tuple[dict[str, Any], Optional[float]]:
        j = self._sidecar(remote_id)
        cost = j.get("cost")
        return (j.get("usage") if isinstance(j.get("usage"), dict) else {}), (float(cost) if isinstance(cost, (int, float)) else None)

    def _sidecar(self, remote_id: str) -> dict[str, Any]:
        try:
            j = json.loads(self._cached(remote_id).with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return j if isinstance(j, dict) else {}

    def _pricing_of(self, model: Optional[str]) -> Optional[dict[str, Any]]:
        if callable(self._pricing):
            return self._pricing(model)
        if isinstance(self._pricing, dict):
            return self._pricing
        from ..catalog import catalog

        entry = catalog.get(model or "gemini-omni-1.1-flash", modality="video")
        return entry.pricing if entry is not None and isinstance(entry.pricing, dict) else None

    # ------------------------------------------------------------ submit
    @staticmethod
    def body(req: VideoRequest, *, background: bool = True) -> dict[str, Any]:
        fmt: dict[str, Any] = {"type": "video"}
        if req.aspect_ratio:
            fmt["aspect_ratio"] = req.aspect_ratio
        if req.resolution:
            fmt["resolution"] = str(req.resolution).lower()
        if req.duration:
            fmt["duration"] = f"{int(req.duration)}s"  # google-duration ("5s"): the API reference's schema
        frames = [url for url in (req.first_frame, req.last_frame) if url]
        if frames:
            blocks: list[dict[str, Any]] = []
            for url in frames:
                mime, data = _data_url(url)
                blocks.append({"type": "image", "data": data, "mime_type": mime})
            blocks.append({"type": "text", "text": req.prompt})
            input_: Any = blocks
        else:
            input_ = req.prompt
        out: dict[str, Any] = {"model": req.model, "input": input_, "response_format": fmt, "store": True}
        if frames:
            # "Generates video from one or two source images. The first image defines the starting
            # frame, and the optional second image defines the ending frame." (API reference)
            out["generation_config"] = {"video_config": {"task": "image_to_video"}}
        if background:
            out["background"] = True
        elif str(req.resolution or "").lower() in ("1080p", "4k"):
            fmt["delivery"] = "uri"  # the docs: past 4 MB, ask for a URI (a waiting create answers with the video)
        return out

    @property
    def waits_in_submit(self) -> bool:
        """``submit`` holds until the video is made (the default): the job runner
        sends it in a task of its own instead of making its caller wait."""
        return not self.background

    async def submit(self, request: VideoRequest) -> Submitted:
        """The default: send the request and wait for the video in the same call
        (:meth:`_submit_waiting`). With ``background`` on: send it in the
        background (an id comes back at once).

        Fallback (a guess about how Google would say no): a 400 that names
        ``background`` means Omni only answers in one go — the same request is
        then sent without it and this call waits until the video is made
        (minutes). Its file is kept in the local cache and :meth:`status`
        answers "completed" from there. Because the interaction is stored
        (``store: true``), a service restarted after that still finds it by
        id; only a service that dies *while* it waits loses the job (the
        runner marks it ``lost``: sent, maybe billed, nothing to ask about)."""
        if not self.background:
            return await self._submit_waiting(request)
        try:
            resp = await self._send("POST", self._base + "/interactions", json_body=self.body(request))
        except httpx.TimeoutException as e:
            raise self._timeout_error(e) from e
        except httpx.HTTPError as e:
            raise VideoProviderError(f"could not connect to Google: {type(e).__name__}: {e}", provider_name=self.name,
                                     error_code="CONNECTION", error_kind="unavailable", charged="no") from e
        if resp.status_code == 400 and "background" in _said(resp)[0].lower():
            logger.warning("Gemini Omni refused background=true (%s): waiting for the video inside the request", _said(resp)[0][:200])
            return await self._submit_waiting(request)
        if resp.status_code >= 400:
            raise self._error(request.model, resp)
        j = self._json(resp, "accepted the video")
        return self._submitted(j, resp.status_code)

    async def _submit_waiting(self, request: VideoRequest) -> Submitted:
        try:
            resp = await self._send("POST", self._base + "/interactions", json_body=self.body(request, background=False),
                                    timeout=httpx.Timeout(self._sync_timeout, connect=15.0))
        except httpx.TimeoutException as e:
            raise self._timeout_error(e) from e
        except httpx.HTTPError as e:
            raise VideoProviderError(f"lost the connection to Google while the video was being made: {type(e).__name__}",
                                     provider_name=self.name, error_code="CONNECTION", error_kind="unavailable",
                                     charged="unknown") from e
        if resp.status_code >= 400:
            raise self._error(request.model, resp)
        j = self._json(resp, "answered")
        sub = self._submitted(j, resp.status_code)
        sub.raw["waited_in_request"] = True
        return sub

    def _submitted(self, j: dict[str, Any], code: int) -> Submitted:
        remote_id = j.get("id")
        if not remote_id:
            raise VideoProviderError(f"Google accepted the video ({code}) but named no interaction id", provider_name=self.name,
                                     error_code="NO_ID", error_kind="other", charged="unknown")
        status = STATUS_MAP.get(str(j.get("status") or "in_progress").lower(), "in_progress")
        block = video_block(j)
        if block is not None:
            usage = j.get("usage") if isinstance(j.get("usage"), dict) else {}
            cost = usage_cost(usage, self._pricing_of(j.get("model")))
            if self._keep(str(remote_id), block):
                self._note(str(remote_id), usage, cost)
            elif block.get("uri"):
                # a file to fetch: the URI is only in this answer (a GET answers inline, the docs say)
                self._note(str(remote_id), usage, cost, uri=str(block["uri"]))
        return Submitted(remote_id=str(remote_id), status="pending" if status == "in_progress" else status, raw=_slim(j))

    def _json(self, resp: httpx.Response, what: str) -> dict[str, Any]:
        try:
            j = resp.json()
        except ValueError as e:
            raise VideoProviderError(f"Google {what} ({resp.status_code}) with something that is not JSON", provider_name=self.name,
                                     error_code="BAD_RESPONSE", error_kind="other", charged="unknown") from e
        if not isinstance(j, dict):
            raise VideoProviderError(f"Google {what} ({resp.status_code}) with something that is not an object",
                                     provider_name=self.name, error_code="BAD_RESPONSE", error_kind="other", charged="unknown")
        return j

    def _timeout_error(self, e: Exception) -> VideoProviderError:
        return VideoProviderError(
            f"Google did not answer in time while the video was being sent ({type(e).__name__}); it may or may not have "
            "been accepted — and billed. Check the project's usage in Google AI Studio before sending it again.",
            provider_name=self.name, error_code="TIMEOUT", error_kind="timeout", charged="unknown")

    def _error(self, model: str, resp: httpx.Response) -> VideoProviderError:
        status = resp.status_code
        said, word = _said(resp)
        low = said.lower()
        content = any(w in low for w in _CONTENT_WORDS)
        paid = any(w in low for w in _PAID_WORDS) or (word == "FAILED_PRECONDITION" and "location" not in low)
        if paid and status in (400, 403, 429):
            kind, text = "quota", f"{PAID_TIER_MESSAGE} Google 原話：{said}"
        elif status == 429:
            kind, text = "quota", f"Google rate limit or quota (429): {said}"
        elif _MULTI_CRED in low:
            kind, text = "auth", (f"Google received more than one credential ({status}): {said} — OmniAPI sends only the "
                                  "x-goog-api-key header; something on the way (a redirect, a proxy) added another")
        elif status == 401 or (status in (400, 403) and ("api key" in low or "api_key" in low)):
            kind, text = "auth", f"Google rejected the API key ({status}): {said}"
        elif status == 403:
            kind = "rejected" if content else "auth"
            text = f"Google refused the request (403): {said}"
        elif status == 413:
            kind, text = "too_large", f"the request is too large for Google (413) — a frame image may be too big: {said}"
        elif status in (400, 404, 422):
            kind = "rejected" if content else "invalid"
            text = f"Google refused the request ({status}): {said}"
        elif status in (408, 504):
            kind, text = "timeout", f"Google timed out ({status}): {said}"
        else:
            kind, text = "unavailable", f"Google failed ({status}): {said}"
        charged = "no" if 400 <= status < 500 and status != 408 else "unknown"
        logger.warning("Gemini video %s -> HTTP %s (%s)", model, status, kind)
        return VideoProviderError(text, provider_name=self.name, error_code=str(status), error_kind=kind, said=text,
                                  charged=charged, status=status)

    # ------------------------------------------------------------ status
    async def _get(self, remote_id: str) -> dict[str, Any]:
        try:
            resp = await self._send("GET", f"{self._base}/interactions/{remote_id}")
        except httpx.HTTPError as e:
            raise VideoProviderError(f"could not reach Google to ask about the video: {type(e).__name__}",
                                     provider_name=self.name, error_code="CONNECTION", error_kind="unavailable",
                                     transient=True) from e
        if resp.status_code >= 400:
            # 404: Google no longer knows the job. Any other 4xx but 408 / 429 says the same thing next time
            # (asking again for 20 minutes only hides it): not transient, the runner stops and keeps the id
            said, _ = _said(resp)
            code = resp.status_code
            transient = transient_status(code)
            text = f"Google answered {code} about the video: {said}"
            if code == 404:
                kind = "lost"
            elif transient:
                kind = "unavailable"
            else:
                refused = self._error("(asking)", resp)  # the same reading as a refused submit (key, paid tier, two credentials)
                kind, text = refused.error_kind or "other", f"asking about the video: {refused.said}"
            raise VideoProviderError(text, provider_name=self.name, error_code=str(code), error_kind=kind, said=text,
                                     transient=transient, status=code)
        try:
            j = resp.json()
        except ValueError as e:
            raise VideoProviderError("Google's answer about the video is not JSON", provider_name=self.name,
                                     error_code="BAD_RESPONSE", error_kind="other", transient=True) from e
        return j if isinstance(j, dict) else {}

    async def status(self, remote_id: str) -> RemoteState:
        cached = self._cached(remote_id)
        if (cached.is_file() and cached.stat().st_size > 0) or self._sidecar(remote_id).get("uri"):
            # made inside the request (the waiting way), or read by an earlier ask: no need to ask again
            usage, cost = self._noted(remote_id)
            return RemoteState(status="completed", outputs=1, cost_usd=cost, usage={**usage, **({"cost_computed": cost} if cost is not None else {})},
                               raw={"id": remote_id, "status": "completed", "cached": True})
        j = await self._get(remote_id)
        status = STATUS_MAP.get(str(j.get("status") or "").lower(), str(j.get("status") or "unknown").lower())
        usage = j.get("usage") if isinstance(j.get("usage"), dict) else {}
        cost = usage_cost(usage, self._pricing_of(j.get("model")))
        error = _errors(j)
        outputs = 0
        if status == "completed":
            block = video_block(j)
            if block is None:
                said = output_text(j)
                status = "failed"
                error = error or ("the model answered without a video" + (f": {said}" if said else ""))
            else:
                outputs = 1
                if self._keep(remote_id, block):
                    self._note(remote_id, usage, cost)
        elif str(j.get("status") or "").lower() == "requires_action":
            error = error or "the interaction asks for an action a video request cannot give"
        state = RemoteState(status=status, error=error, cost_usd=cost if status == "completed" else None,
                            usage={**usage, **({"cost_computed": cost} if cost is not None else {})}, outputs=outputs, raw=_slim(j))
        return state

    # ------------------------------------------------------------ download
    async def download(self, remote_id: str, dest: Path, *, index: int = 0) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cached = self._cached(remote_id)
        uri = self._sidecar(remote_id).get("uri")
        if uri and not (cached.is_file() and cached.stat().st_size > 0):
            n = await self._download_uri(str(uri), dest)
            cached.with_suffix(".json").unlink(missing_ok=True)
            return n
        if not (cached.is_file() and cached.stat().st_size > 0):
            j = await self._get(remote_id)
            block = video_block(j)
            if block is None:
                raise VideoProviderError("Google's interaction holds no video", provider_name=self.name, error_code="EMPTY",
                                         error_kind="unavailable", transient=False)
            if not self._keep(remote_id, block):
                if not block.get("uri"):
                    raise VideoProviderError("Google's video block has neither data nor a URI", provider_name=self.name,
                                             error_code="EMPTY", error_kind="unavailable", transient=True)
                return await self._download_uri(str(block["uri"]), dest)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(cached.read_bytes())
        tmp.replace(dest)
        cached.unlink(missing_ok=True)
        cached.with_suffix(".json").unlink(missing_ok=True)
        return dest.stat().st_size

    async def _download_uri(self, uri: str, dest: Path) -> int:
        """A ``files/<id>`` video: wait until it is ``ACTIVE``, then fetch it with the key."""
        m = re.search(r"files/([^/:?]+)", uri)
        if m:
            waited = 0.0
            while True:
                try:
                    resp = await self._send("GET", f"{self._base}/files/{m.group(1)}")
                except httpx.HTTPError as e:
                    raise VideoProviderError(f"could not reach Google about the video file: {type(e).__name__}",
                                             provider_name=self.name, error_code="CONNECTION", error_kind="unavailable",
                                             transient=True) from e
                state = ""
                if resp.status_code < 400:
                    try:
                        state = str((resp.json() or {}).get("state") or "")
                    except ValueError:
                        state = ""
                if state == "ACTIVE" or resp.status_code >= 400:
                    break  # a file we cannot look up is tried directly
                if state == "FAILED":
                    raise VideoProviderError("Google could not prepare the video file (FAILED)", provider_name=self.name,
                                             error_code="FILE_FAILED", error_kind="unavailable", transient=False)
                if waited >= FILE_POLL_MAX_S:
                    raise VideoProviderError("Google's video file is still being prepared", provider_name=self.name,
                                             error_code="FILE_PENDING", error_kind="unavailable", transient=True)
                await asyncio.sleep(self._file_poll_s)
                waited += self._file_poll_s
        url = uri if uri.startswith("http") else f"{self._base}/{uri.lstrip('/')}"
        if ":download" not in url and "alt=media" not in url and m:
            url = f"{self._base}/files/{m.group(1)}:download?alt=media"
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            resp = await self._send("GET", url, stream=True)
            try:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise VideoProviderError(f"Google answered {resp.status_code} for the video file: {_said(resp)[0]}",
                                             provider_name=self.name, error_code=str(resp.status_code),
                                             error_kind="unavailable", transient=transient_status(resp.status_code),
                                             status=resp.status_code)
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                        size += len(chunk)
            finally:
                await resp.aclose()
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise VideoProviderError(f"the video file could not be downloaded: {type(e).__name__}", provider_name=self.name,
                                     error_code="CONNECTION", error_kind="unavailable", transient=True) from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise VideoProviderError("Google sent an empty video file", provider_name=self.name, error_code="EMPTY",
                                     error_kind="unavailable", transient=True)
        tmp.replace(dest)
        return size
