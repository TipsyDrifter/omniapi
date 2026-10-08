"""OpenRouter's video models (1.4-M3): the live roster, which of them make a
video from a prompt (and a first / last frame), what a request costs, and
what a request may ask for.

One free, keyless endpoint (OpenRouter docs, read 2026-10-05:
``docs/guides/overview/multimodal/video-generation.md`` and the API reference
``list-all-video-generation-models.md``):

* ``GET /api/v1/videos/models`` — ``{"data": [...]}``, per model
  ``supported_durations`` (seconds, a list of the allowed values),
  ``supported_resolutions``, ``supported_aspect_ratios``, ``supported_sizes``,
  ``supported_frame_images`` (``first_frame`` / ``last_frame``),
  ``generate_audio`` (true / false / null = not stated), ``seed`` (likewise),
  ``pricing_skus`` (string amounts under keys of several spellings, see
  :func:`parse_skus`) and ``allowed_passthrough_parameters``.

The roster is listed only when an OpenRouter key is set (the catalog decides
that), never in an offline sandbox (which reads ``sandbox_videos.json``).

Everything here is pure (no settings, no catalog state): the catalog, the
estimate, the job runner and the sandbox read the same rules.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from .discovery import DISCOVERY_TIMEOUT, DiscoveredModel

logger = logging.getLogger(__name__)

#: the key the roster is cached and reported under (``discovery-openrouter-videos.json``)
DISCOVERY_KEY = "openrouter-videos"
DEFAULT_BASE = "https://openrouter.ai/api/v1"

#: the listing fields kept on a roster entry (the description is long prose: cut)
_KEEP = ("name", "canonical_slug", "created", "supported_durations", "supported_resolutions", "supported_aspect_ratios",
         "supported_sizes", "supported_frame_images", "generate_audio", "seed", "upscale_factor", "creativity",
         "pricing_skus", "allowed_passthrough_parameters")
_DESCRIPTION_CHARS = 600

# --------------------------------------------------------------------------
# What the vendor closed (the listing does not say)
# --------------------------------------------------------------------------

#: Vendor closures for video models the curated catalog has no row of its own
#: for (an image model copies its status from the vendor's curated row; no
#: vendor video model is curated). One place, each with its source. OpenRouter
#: still lists these ids on 2026-10-05.
VENDOR_STATUS: dict[str, dict[str, Any]] = {
    "openai/sora-2-pro": {
        "status": "retired", "shutdown": "2026-09-24",
        "note": "OpenAI shut the Sora 2 models and its Videos API down on 2026-09-24 (no replacement).",
        "source": "https://developers.openai.com/api/docs/guides/video-generation",
    },
    **{mid: {
        "status": "deprecated", "shutdown": "2026-10-22", "replacement": "gemini-omni-1.1-flash",
        "note": "Google lists Veo 3.1 for shutdown on 2026-10-22 at the earliest; its replacement is Gemini Omni.",
        "source": "https://ai.google.dev/gemini-api/docs/deprecations",
    } for mid in ("google/veo-3.1", "google/veo-3.1-fast", "google/veo-3.1-lite")},
}

# --------------------------------------------------------------------------
# Duplicates of a model we call directly (1.4-M5)
# --------------------------------------------------------------------------

#: OpenRouter id prefix (the vendor) -> (the catalog provider we call directly,
#: the bare-id prefixes that are that provider's video line). An OpenRouter
#: video model matching one is not listed while that provider's key is set and
#: switched on (``ModelCatalog.hidden_as_duplicate``): Gemini Omni goes direct,
#: and Veo 3.1 is the model Omni replaces. Someone without a Google key still
#: sees OpenRouter's rows. The image rule is ``openrouter_images.DIRECT_VENDORS``.
DIRECT_VIDEO_TWINS: dict[str, tuple[str, tuple[str, ...]]] = {"google": ("google", ("gemini-omni", "veo-"))}


def direct_video_twin(model_id: str) -> Optional[str]:
    """The catalog provider that makes ``model_id``'s video directly, if any."""
    rule = DIRECT_VIDEO_TWINS.get(vendor_of(model_id))
    if rule is None:
        return None
    provider, prefixes = rule
    bare = model_id.split("/", 1)[1] if "/" in model_id else model_id
    return provider if bare.startswith(prefixes) else None


# --------------------------------------------------------------------------
# Which listed models make a video (the rule lives here, once)
# --------------------------------------------------------------------------

#: why a listed model is left out of this version, by the rule below
NOT_GENERATION_NOTE = ("Edits, upscales or animates a given video or presenter (it has no length of its own to choose): "
                       "not a text- or image-to-video model, left out in this version.")


def makes_video(item: dict[str, Any]) -> bool:
    """Does a listed model make a new video from a prompt (and frames)?

    The rule: it lists ``supported_durations``. On the 2026-10-05 listing the
    26 text/image-to-video models all do; the four that do not — FLUX Video
    Edit and FLUX Video Upscale (their length is the input video's; the
    upscaler also lists ``upscale_factor`` / ``creativity``), Runway Aleph 2
    (video-to-video) and HeyGen Avatar IV (a talking presenter driven by a
    script) — are exactly the editors, upscalers and avatars this version
    leaves out (roadmap 1.4: no video edit / extend / upscale / lip-sync).
    A model that lists an upscale factor is an upscaler whatever else it says."""
    durations = item.get("supported_durations")
    if item.get("upscale_factor") not in (None, {}, []):
        return False
    return isinstance(durations, list) and any(isinstance(d, (int, float)) and d > 0 for d in durations)


# --------------------------------------------------------------------------
# Reading one listed model
# --------------------------------------------------------------------------

def vendor_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else ""


def split_name(name: Optional[str], model_id: str) -> tuple[str, str]:
    """``"Alibaba: Wan 3.0"`` -> (``"Alibaba"``, ``"Wan 3.0"``)."""
    if name and ": " in name:
        vendor, _, model = name.partition(": ")
        return vendor.strip(), model.strip()
    return vendor_of(model_id), (name or model_id.split("/", 1)[-1])


def _strs(v: Any) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def _tri(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def video_params(item: dict[str, Any]) -> dict[str, Any]:
    """The request shape a model takes, from its listing. ``audio`` and
    ``seed`` are ``True`` / ``False`` / ``None`` (the listing does not say)."""
    durations = sorted({int(d) for d in (item.get("supported_durations") or []) if isinstance(d, (int, float)) and d > 0})
    frames = [f for f in _strs(item.get("supported_frame_images")) if f in ("first_frame", "last_frame")]
    return {
        "durations": durations,
        "resolutions": _strs(item.get("supported_resolutions")),
        "aspect_ratios": _strs(item.get("supported_aspect_ratios")),
        "sizes": _strs(item.get("supported_sizes")),
        "frames": frames,
        "audio": _tri(item.get("generate_audio")),
        "seed": _tri(item.get("seed")),
        "passthrough": _strs(item.get("allowed_passthrough_parameters")),
    }


def default_duration(params: dict[str, Any]) -> Optional[int]:
    """The length sent when the caller names none: 5 seconds when the model
    takes it, else the allowed length closest to 5 (the shorter one on a tie).
    Always sent, so the estimate and the bill speak of the same length."""
    durations = [int(d) for d in params.get("durations") or []]
    if not durations:
        return None
    return min(durations, key=lambda d: (abs(d - 5), d))


def default_audio(params: dict[str, Any]) -> Optional[bool]:
    """What OpenRouter does when ``generate_audio`` is not sent: on for models
    that make sound (docs: "Defaults to true for models that support audio
    output"), off for those that do not; unknown when the listing is silent."""
    return params.get("audio")


# --------------------------------------------------------------------------
# Pricing: the SKU keys, normalised
# --------------------------------------------------------------------------

_RES = r"(\d{3,4}p|\d(?:\.\d)?k)"


@dataclass(frozen=True)
class Sku:
    """One pricing line. ``kind``: ``per_second`` (USD per output second),
    ``per_frame_image`` (USD per frame image sent), ``per_reference_image``
    (USD per reference image — whether a first / last frame counts as one is
    not documented), ``minimum`` (USD per generation), ``per_token`` and
    ``per_megapixel_second`` (not computable up front); ``flag`` is not a
    price (``resolution_lines_only``: a resolution without its own line is
    not computable). ``mode``: ``None``
    (any), ``text_to_video``, ``image_to_video``, ``reference`` (reference-
    to-video, never sent by us) or ``continuation`` (extends a given video,
    never sent by us)."""

    key: str
    kind: str
    usd: float
    mode: Optional[str] = None
    audio: Optional[bool] = None
    resolution: Optional[str] = None


_PATTERNS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    # duration_seconds[_with_audio|_without_audio][_<res>], optionally after text_to_video_ / image_to_video_ / reference_
    (re.compile(rf"^(?:(text_to_video|image_to_video|reference)_)?duration_seconds(?:_(with_audio|without_audio))?(?:_{_RES})?$"),
     "per_second", 1.0),
    (re.compile(rf"^cents_per_second_output(?:_{_RES})?$"), "per_second", 0.01),
    (re.compile(rf"^cents_per_video_output_second(?:_{_RES})?$"), "per_second", 0.01),
    (re.compile(rf"^cents_per_second_video_continuation(?:_{_RES})?$"), "continuation", 0.01),
    # the docs' own example (``per-video-second``, ``per-video-second-1080p``), in dollars
    (re.compile(rf"^per-video-second(?:-{_RES})?$"), "per_second", 1.0),
    (re.compile(r"^cents_per_image_input$"), "per_frame_image", 0.01),
    (re.compile(r"^reference_images$"), "per_reference_image", 1.0),
    (re.compile(r"^minimum_cents_per_generation$"), "minimum", 0.01),
    (re.compile(r"^video_tokens(?:_[a-z0-9_]+)?$"), "per_token", 1.0),
    (re.compile(r"^cents_per_megapixel_second(?:_[a-z0-9_]+)?$"), "per_megapixel_second", 0.01),
    # not a price: a curated row's marker (Gemini Omni direct) that only the resolutions it names have a
    # known per-second price — any other resolution is "not computable", never "every listed price"
    (re.compile(r"^resolution_lines_only$"), "flag", 1.0),
)


def norm_resolution(res: Optional[str]) -> Optional[str]:
    """``"4K"`` -> ``"4k"``, ``"720P"`` -> ``"720p"``."""
    return str(res).strip().lower() if res not in (None, "") else None


def parse_skus(skus: Any) -> tuple[list[Sku], list[str]]:
    """``pricing_skus`` -> (the lines we read, the keys we do not know). An
    unknown key is never guessed at: :func:`price` then says "not computable"."""
    rules: list[Sku] = []
    unknown: list[str] = []
    for key, raw in (skus or {}).items() if isinstance(skus, dict) else []:
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            unknown.append(str(key))
            continue
        k = str(key).strip().lower()
        for pattern, kind, scale in _PATTERNS:
            m = pattern.match(k)
            if not m:
                continue
            if kind == "per_second" and pattern.pattern.startswith("^(?:(text_to_video"):
                mode, audio, res = m.group(1), m.group(2), m.group(3)
                rules.append(Sku(str(key), "per_second", amount * scale, mode=mode,
                                 audio=None if audio is None else audio == "with_audio", resolution=norm_resolution(res)))
            elif kind in ("per_second", "continuation"):
                rules.append(Sku(str(key), kind, amount * scale, mode="continuation" if kind == "continuation" else None,
                                 resolution=norm_resolution(m.group(1) if m.groups() else None)))
            else:
                rules.append(Sku(str(key), kind, amount * scale))
            break
        else:
            unknown.append(str(key))
    return rules, unknown


def _spread(values: Iterable[float]) -> tuple[float, float]:
    vs = list(values)
    return min(vs), max(vs)


def price(skus: Any, *, seconds: Optional[float], resolution: Optional[str] = None, audio: Optional[bool] = None,
          first_frame: bool = False, frames: int = 0) -> dict[str, Any]:
    """What one video would cost, from the listed SKUs.

    Returns ``{"kind": ...}``:

    * ``exact``   — one amount: ``usd`` (= ``low`` = ``high``), ``per_second``
    * ``range``   — several listed prices could apply: ``low`` .. ``high``
    * ``unknown`` — not computable up front; ``reason``: ``per_token``
      (priced per token, and how many tokens a second takes is not listed),
      ``per_megapixel`` (an upscaler), ``unknown_sku`` (a key this version
      does not read: ``keys``), ``no_price`` (nothing listed), ``no_length``

    ``audio`` is the request's (``None`` = unknown: when the listing prices
    sound separately both prices count). ``first_frame``: a first frame is
    sent (image-to-video prices apply); ``frames``: how many frame images go
    along (first + last). ``notes`` say what was assumed."""
    rules, unknown = parse_skus(skus)
    notes: list[str] = []
    if unknown:
        logger.warning("OpenRouter video pricing: unknown SKU keys %s — the estimate says 'not computable'", unknown)
        return {"kind": "unknown", "reason": "unknown_sku", "keys": unknown}
    if not rules:
        return {"kind": "unknown", "reason": "no_price"}
    if not seconds:
        return {"kind": "unknown", "reason": "no_length"}
    want_mode = "image_to_video" if first_frame else "text_to_video"
    per_second = [r for r in rules if r.kind == "per_second" and r.mode in (None, want_mode)]
    if not per_second:
        if any(r.kind == "per_token" for r in rules):
            return {"kind": "unknown", "reason": "per_token", "keys": [r.key for r in rules if r.kind == "per_token"]}
        if any(r.kind == "per_megapixel_second" for r in rules):
            return {"kind": "unknown", "reason": "per_megapixel"}
        return {"kind": "unknown", "reason": "no_price"}

    # sound: a line that names it wins for that setting; otherwise the plain lines apply
    if any(r.audio is not None for r in per_second):
        if audio is None:
            notes.append("the listing prices sound separately and this model does not say whether it makes sound: both prices count")
        else:
            named = [r for r in per_second if r.audio is audio]
            per_second = named or [r for r in per_second if r.audio is None] or per_second
    # mode: a text-to-video / image-to-video line wins over a plain one
    moded = [r for r in per_second if r.mode == want_mode]
    if moded and not any(r.audio is not None for r in per_second):
        per_second = moded
    # resolution: the line for it, else the plain line, else every line (a range)
    res = norm_resolution(resolution)
    strict = any(r.kind == "flag" and r.key.strip().lower() == "resolution_lines_only" for r in rules)
    if res is not None:
        exact = [r for r in per_second if r.resolution == res]
        plain = [r for r in per_second if r.resolution is None]
        if exact:
            per_second = exact
        elif plain:
            per_second = plain
        elif strict:
            # priced per token and the vendor states the tokens per second for some resolutions only
            return {"kind": "unknown", "reason": "per_token"}
        else:
            notes.append(f"no price is listed for {resolution}: every listed resolution counts")
    elif len({r.usd for r in per_second}) > 1:
        notes.append("no resolution chosen (the model's default): every listed resolution counts")
    low_s, high_s = _spread(r.usd for r in per_second)
    low, high = low_s * seconds, high_s * seconds
    used = [r.key for r in per_second]

    for r in rules:
        if r.kind == "per_frame_image" and frames > 0:
            low += r.usd * frames
            high += r.usd * frames
            used.append(r.key)
            notes.append(f"{frames} frame image(s) at ${r.usd:g} each")
        elif r.kind == "per_reference_image" and frames > 0:
            high += r.usd * frames  # whether a frame counts as a reference image is not documented
            used.append(r.key)
            notes.append(f"a reference-image price (${r.usd:g}) may apply to the frame image(s)")
    minimum = [r.usd for r in rules if r.kind == "minimum"]
    if minimum:
        floor = max(minimum)
        if low < floor or high < floor:
            notes.append(f"a minimum of ${floor:g} per video applies")
        low, high = max(low, floor), max(high, floor)
        used += [r.key for r in rules if r.kind == "minimum"]
    out: dict[str, Any] = {"low": round(low, 6), "high": round(high, 6), "seconds": seconds, "keys": used}
    if abs(high - low) < 1e-9:
        out.update(kind="exact", usd=round(low, 6))
        if low_s == high_s:
            out["per_second"] = round(low_s, 6)
    else:
        out["kind"] = "range"
        if low_s == high_s:
            out["per_second"] = round(low_s, 6)
    if notes:
        out["notes"] = notes
    return out


def estimate(pricing: Optional[dict[str, Any]], params: Optional[dict[str, Any]], *, duration: Optional[int],
             resolution: Optional[str], audio: Optional[bool], first_frame: bool, frames: int) -> dict[str, Any]:
    """The estimate fields for ``generate.estimate`` (``basis`` and friends).

    ``basis``: ``per_second`` (one amount), ``range`` (``low``–``high``),
    ``per_token`` / ``per_megapixel`` / ``unknown_sku`` / ``no_price`` (not
    computable: ``usd`` is ``None``). Every amount is approximate: OpenRouter
    has billed less than the listed price before (``approx``)."""
    vp = params or {}
    seconds = duration or default_duration(vp)
    if audio is None:
        audio = default_audio(vp)
    found = price((pricing or {}).get("skus"), seconds=seconds, resolution=resolution, audio=audio,
                  first_frame=first_frame, frames=frames)
    detail = {k: found.get(k) for k in ("low", "high", "per_second", "seconds", "notes", "keys") if found.get(k) is not None}
    detail.update(approx=True, resolution=resolution, audio=audio, frames=frames or None,
                  stale=True if (pricing or {}).get("stale") else None)
    if found["kind"] == "exact":
        return {"basis": "per_second", "usd": found["usd"], **detail}
    if found["kind"] == "range":
        return {"basis": "range", "usd": None, **detail}
    reason = found.get("reason") or "no_price"
    basis = {"per_token": "per_token", "per_megapixel": "per_megapixel", "unknown_sku": "unknown_sku"}.get(reason, "no_price")
    return {"basis": basis, "usd": None, "reason": reason, **({"keys": found["keys"]} if found.get("keys") else {}),
            "seconds": seconds, "approx": True}


def price_class(est: dict[str, Any]) -> str:
    """``exact`` / ``range`` / ``unknown`` for an :func:`estimate` result."""
    if est.get("usd") is not None:
        return "exact"
    if est.get("low") is not None and est.get("high") is not None:
        return "range"
    return "unknown"


# --------------------------------------------------------------------------
# Checking a request before it costs anything
# --------------------------------------------------------------------------

def check_request(params: dict[str, Any], *, duration: Optional[int], resolution: Optional[str] = None,
                  aspect_ratio: Optional[str] = None, audio: Optional[bool] = None, first_frame: bool = False,
                  last_frame: bool = False) -> Optional[str]:
    """Why a request cannot go to this model, in words that name what it does
    take — or ``None``. Only what the listing states is checked; the vendor
    has the last word (a 400 from OpenRouter is passed on as it is)."""
    durations = params.get("durations") or []
    if duration is not None and durations and int(duration) not in durations:
        return f"Duration {duration}s is not supported for this model. Supported durations: {_list(durations)} (seconds)."
    resolutions = params.get("resolutions") or []
    if resolution and resolutions and norm_resolution(resolution) not in [norm_resolution(r) for r in resolutions]:
        return f"Resolution {resolution} is not supported for this model. Supported resolutions: {', '.join(resolutions)}."
    ratios = params.get("aspect_ratios") or []
    if aspect_ratio and ratios and aspect_ratio not in ratios:
        return f"Aspect ratio {aspect_ratio} is not supported for this model. Supported aspect ratios: {', '.join(ratios)}."
    frames = params.get("frames") or []
    if first_frame and "first_frame" not in frames:
        return "This model takes no first frame (text to video only): leave the first frame out or pick a model that takes one."
    if last_frame and "last_frame" not in frames:
        took = "a first frame only" if "first_frame" in frames else "no frame image"
        return f"This model takes {took}, not a last frame: leave the last frame out or pick a model that takes one."
    if last_frame and not first_frame and params.get("last_frame_needs_first"):
        return "This model takes a last frame only together with a first frame: add a first frame or leave the last frame out."
    if audio is True and params.get("audio") is False:
        return "This model makes video without sound: generate_audio=true is not supported (send false or leave it out)."
    if audio is False and params.get("audio_fixed"):
        return "This model always makes sound (it has no switch for it): generate_audio=false is not supported (leave it out)."
    return None


def _list(values: list[int]) -> str:
    """``[2, 3, …, 30]`` -> ``"2-30"``; anything with gaps is listed."""
    if len(values) > 3 and values == list(range(values[0], values[-1] + 1)):
        return f"{values[0]}-{values[-1]}"
    return ", ".join(str(v) for v in values)


# --------------------------------------------------------------------------
# Fetching and reading the listing
# --------------------------------------------------------------------------

def _discovered(item: dict[str, Any]) -> DiscoveredModel:
    extra = {k: item[k] for k in _KEEP if k in item}
    if isinstance(item.get("description"), str):
        extra["description"] = item["description"][:_DESCRIPTION_CHARS]
    return DiscoveredModel(id=item["id"], display_name=item.get("name"), extra=extra)


async def fetch_openrouter_videos(base_url: str = DEFAULT_BASE, *, timeout: float = DISCOVERY_TIMEOUT,
                                  transport: Optional[httpx.AsyncBaseTransport] = None) -> list[DiscoveredModel]:
    """The roster (one public request; no key is sent)."""
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        resp = await client.get(base_url.rstrip("/") + "/videos/models")
        resp.raise_for_status()
        items = [m for m in (resp.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
    return [_discovered(i) for i in items]


def from_listing(raw: dict[str, Any]) -> list[DiscoveredModel]:
    """The same result as :func:`fetch_openrouter_videos` from a saved
    response (``{"data": [...]}``): the test fixtures and the sandbox."""
    return [_discovered(i) for i in raw.get("data") or [] if isinstance(i, dict) and i.get("id")]


def sandbox_listing() -> list[DiscoveredModel]:
    """A small stand-in roster for the offline sandbox (``sandbox_videos.json``,
    copied from the 2026-10-05 listing): one that takes a first frame only,
    one with first and last frames, one without sound, one priced per token,
    one the vendor has announced for shutdown, one the vendor closed, and one
    that is not a generation model (left out by :func:`makes_video`)."""
    raw = json.loads(Path(__file__).with_name("sandbox_videos.json").read_text(encoding="utf-8"))
    return from_listing(raw)


# --------------------------------------------------------------------------
# Turning a listed model into a catalog row
# --------------------------------------------------------------------------

def entry_fields(dm: DiscoveredModel, curated_twin: Any = None) -> dict[str, Any]:
    """Fields of the ``ModelEntry`` for one listed model. The vendor's own
    status wins: a curated twin's retired / deprecated status, else
    :data:`VENDOR_STATUS`."""
    extra = dm.extra if isinstance(dm.extra, dict) else {}
    vendor_label, name = split_name(extra.get("name") or dm.display_name, dm.id)
    params = video_params(extra)
    skus = extra.get("pricing_skus") if isinstance(extra.get("pricing_skus"), dict) else {}
    pricing: dict[str, Any] = {"unit": "openrouter_video", "source": "openrouter", "skus": dict(skus)}
    rules, unknown = parse_skus(skus)
    if not rules and not unknown:
        pricing["unknown"] = True
    if unknown:
        pricing["unknown_keys"] = unknown
    status, shutdown, replacement, note = "current", None, None, None
    if curated_twin is not None and getattr(curated_twin, "status", None) in ("retired", "deprecated"):
        status, shutdown = curated_twin.status, getattr(curated_twin, "shutdown", None)
        replacement = getattr(curated_twin, "replacement", None)
    elif dm.id in VENDOR_STATUS:
        vs = VENDOR_STATUS[dm.id]
        status, shutdown, replacement, note = vs["status"], vs.get("shutdown"), vs.get("replacement"), vs.get("note")
    listed = makes_video(extra)
    return {
        "name": name,
        "status": status,
        "shutdown": shutdown,
        "replacement": replacement,
        "note": note if listed else NOT_GENERATION_NOTE,
        "pricing": pricing,
        "capabilities": {"first_frame": "first_frame" in params["frames"], "last_frame": "last_frame" in params["frames"],
                         "audio": params["audio"]},
        "implemented": listed and status != "retired",
        "vendor": vendor_of(dm.id),
        "vendor_label": vendor_label,
        "video_params": params,
        "unlisted": None if listed else "not_generation",
    }
