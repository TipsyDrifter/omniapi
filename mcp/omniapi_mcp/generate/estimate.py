"""What a generation will roughly cost, before it is sent.

Flat-priced models (per image, per 1k characters, per minute, per song) are a
multiplication. Token-priced ones cannot be computed up front, so the estimate
is what the same model actually cost in the works library — and when there is
no history either, the answer is "unknown", not a guess.

Every estimate names its ``basis`` so the GUI can say where the number came
from:

==================  =========================================================
``per_image``       catalog price per image at this resolution × count
``per_1k_chars``    catalog price per 1,000 characters × text length
``per_minute``      catalog price per minute × length
``per_song``        catalog price per song
``history``         mean of past works with the same model *and* settings
``history_model``   mean of past works with the same model (settings differ)
``history_chars``   past cost per character of this model × text length
``credits``         billed in vendor credits; the per-song amount is unpublished
``needs_length``    per-minute price, but the length is not known yet
``unknown``         token-priced and no history, or no price in the catalog
``sandbox``         offline sandbox: nothing is called, nothing is charged
                    (an OpenRouter image model adds ``listed``: what the
                    request would cost for real)
``variant``         OpenRouter image model: the listed price of the matching
                    resolution / quality variant × count (+ reference images)
``range``           OpenRouter: several listed prices could apply (the variant
                    cannot be matched, or two providers differ): ``low``–``high``
``per_megapixel``   OpenRouter: priced per megapixel; approximated from the
                    resolution tier (``approx``)
``per_token``       OpenRouter: priced per token, nothing to multiply up front
``no_price``        OpenRouter lists no price for the model
``per_second``      video (OpenRouter): the listed price per second for this
                    resolution / sound / first-frame setting × seconds (+ the
                    frame images' fee): one amount, always approximate
``range``           (video too) several listed prices could apply
``per_token``       (video too: Seedance) priced per token, not computable;
                    a model with past videos is estimated from them instead
                    (``history_per_second``, a range)
``unknown_sku``     video: a pricing key this version does not read (logged)
==================  =========================================================

Every video estimate carries ``approx: true`` (OpenRouter has billed less than
the listed price before) — the page says "about".
"""

from __future__ import annotations

from typing import Any, Optional

from .. import modalities as M
from ..catalog import catalog
from ..devmode import offline

#: Gemini image resolutions as the tool takes them -> the catalog's price keys
_IMAGE_TIER = {"512": "0.5K", "0.5K": "0.5K", "1K": "1K", "2K": "2K", "4K": "4K"}
_DEFAULT_IMAGE_TIER = "2K"  # generate_image: "default 2K"

#: the image settings that move the price of a token-priced model
_PRICE_PARAMS = ("quality", "size")


def _result(basis: str, usd: Optional[float] = None, **detail: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"basis": basis, "usd": round(usd, 6) if usd is not None else None}
    out.update({k: v for k, v in detail.items() if v is not None})
    return out


def _same_settings(row: dict[str, Any], params: dict[str, Any]) -> bool:
    past = row.get("params") if isinstance(row.get("params"), dict) else {}
    for key in _PRICE_PARAMS:
        want = params.get(key) or "auto"
        if (past.get(key) or "auto") != want:
            return False
    return True


async def _from_history(store: Any, tool: str, model: str, params: dict[str, Any], n: int) -> Optional[dict[str, Any]]:
    rows = await store.artifact_costs(tool=tool, model=model)
    if not rows:
        return None
    same = [r for r in rows if _same_settings(r, params)]
    basis, pool = ("history", same) if same else ("history_model", rows)
    costs = [r["cost_usd"] for r in pool]
    mean = sum(costs) / len(costs)
    return _result(basis, mean * n, low=round(min(costs) * n, 6), high=round(max(costs) * n, 6), samples=len(costs), n=n)


async def estimate(store: Any, *, kind: str, tool: str, params: dict[str, Any], duration_s: Optional[float] = None,
                   refs: int = 0, first_frame: bool = False) -> dict[str, Any]:
    """``params`` are the tool's own arguments (``model`` resolved by the
    caller). ``duration_s`` is the length of the source audio for a
    transcription — the browser knows it, the upload row does not. ``refs``
    is how many source images go along (an OpenRouter model may charge per
    reference image; a video's frame images). ``first_frame``: a video starts
    from a given first frame (image-to-video prices apply)."""
    model = params.get("model")
    # an OpenRouter id can be a chat model and an image model at once: price the kind's own
    entry = (catalog.get(model, modality=M.CATALOG_OF.get(kind)) or catalog.get(model)) if model else None
    pricing = entry.pricing if entry and isinstance(entry.pricing, dict) else None
    unit = pricing.get("unit") if pricing else None
    if kind == "video":
        return await _video(store, entry, params, refs, first_frame)
    if offline():
        out = _result("sandbox", 0.0)
        if unit == "openrouter":
            # what the same request would cost for real, from the listed price (shown, never charged)
            listed = _openrouter(pricing, params, refs)
            out["listed"] = listed
        return out
    by_kind = _BY_KIND.get(kind)
    if by_kind is None:
        return _result("unknown")
    if unit == "openrouter":
        found = _openrouter(pricing, params, refs)
        if found.get("basis") in ("no_price", "per_token") and model:
            # nothing to multiply: what the same model actually cost before, if anything
            for t in dict.fromkeys((tool, "generate_image", "edit_image")):
                hist = await _from_history(store, t, model, params, int(params.get("n") or 1))
                if hist:
                    return hist
        return found
    return await by_kind(store, tool, model, pricing, unit, params, duration_s)


def _openrouter(pricing: dict[str, Any], params: dict[str, Any], refs: int) -> dict[str, Any]:
    """An OpenRouter image model: its listed pricing lines, matched to the
    resolution / quality asked for (``openrouter_images.estimate``)."""
    from ..catalog.openrouter_images import estimate as or_estimate

    n = int(params.get("n") or 1)
    est = or_estimate(pricing, resolution=params.get("image_size"), quality=params.get("quality"),
                      aspect_ratio=params.get("aspect_ratio"), n=n, refs=refs)
    basis = est.pop("basis")
    usd = est.pop("usd")
    for k in ("low", "high", "ref_usd"):
        if isinstance(est.get(k), float):
            est[k] = round(est[k], 6)
    return _result(basis, usd, **est)


async def _video(store: Any, entry: Any, params: dict[str, Any], frames: int, first_frame: bool) -> dict[str, Any]:
    """A video: its listed price (``openrouter_videos.estimate``). A model
    priced per token has no up-front amount; past videos of it give one
    (their cost per second × the seconds asked for, as a range)."""
    from ..catalog.openrouter_videos import default_duration, estimate as or_estimate

    vp = (entry.video_params if entry is not None else None) or {}
    try:
        duration = int(params["duration"]) if params.get("duration") not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    audio = params.get("generate_audio") if isinstance(params.get("generate_audio"), bool) else None
    found = or_estimate(entry.pricing if entry is not None else None, vp, duration=duration,
                        resolution=params.get("resolution") or None, audio=audio, first_frame=first_frame, frames=frames)
    found = {k: v for k, v in found.items() if v is not None}
    if found.get("usd") is None and found.get("low") is None and entry is not None and not offline():
        seconds = duration or default_duration(vp)
        rows = [r for r in await store.artifact_costs(tool="generate_video", model=entry.id)]
        per_s = []
        for r in rows:
            p = r.get("params") if isinstance(r.get("params"), dict) else {}
            if p.get("duration"):
                per_s.append(r["cost_usd"] / float(p["duration"]))
        if per_s and seconds:
            found = {**found, "basis": "history_per_second", "usd": None, "low": round(min(per_s) * seconds, 6),
                     "high": round(max(per_s) * seconds, 6), "samples": len(per_s), "seconds": seconds, "approx": True,
                     "listed_basis": found.get("basis")}
    if offline():
        return _result("sandbox", 0.0, listed={"usd": found.pop("usd", None), **found})  # usd is always there (null: not computable)
    basis = found.pop("basis", "unknown")
    usd = found.pop("usd", None)
    return _result(basis, usd, **found)


async def _image(store: Any, tool: str, model: Optional[str], pricing: Any, unit: Optional[str],
                 params: dict[str, Any], duration_s: Optional[float]) -> dict[str, Any]:
    n = int(params.get("n") or 1)
    if unit == "per_image":
        tier = _IMAGE_TIER.get(str(params.get("image_size") or _DEFAULT_IMAGE_TIER), _DEFAULT_IMAGE_TIER)
        price = pricing.get(tier)
        if isinstance(price, (int, float)):
            return _result("per_image", price * n, unit_price=price, n=n, tier=tier)
    if model:
        # an edit and a fresh image of one model are priced alike; look at both
        for t in dict.fromkeys((tool, "generate_image", "edit_image")):
            found = await _from_history(store, t, model, params, n)
            if found:
                return found
    return _result("unknown", unit=unit)


async def _speech(store: Any, tool: str, model: Optional[str], pricing: Any, unit: Optional[str],
                  params: dict[str, Any], duration_s: Optional[float]) -> dict[str, Any]:
    chars = len(str(params.get("text") or ""))
    per_1k = catalog.speech_price_per_1k_chars(model) if model else None  # per-1k and per-1M character prices alike
    if per_1k is not None:
        return _result("per_1k_chars", per_1k * chars / 1000, unit_price=per_1k, chars=chars)
    if model:
        rows = [r for r in await store.artifact_costs(tool=tool, model=model) if r.get("prompt_len")]
        if rows:
            per_char = sum(r["cost_usd"] / r["prompt_len"] for r in rows) / len(rows)
            return _result("history_chars", per_char * chars, samples=len(rows), chars=chars)
    return _result("unknown", unit=unit, chars=chars)


async def _music(store: Any, tool: str, model: Optional[str], pricing: Any, unit: Optional[str],
                 params: dict[str, Any], duration_s: Optional[float]) -> dict[str, Any]:
    if unit == "per_song" and isinstance(pricing.get("song"), (int, float)):
        return _result("per_song", pricing["song"], unit_price=pricing["song"])
    if unit == "per_minute" and isinstance(pricing.get("audio"), (int, float)):
        ms = params.get("music_length_ms")
        if not ms:
            return _result("needs_length", unit_price=pricing["audio"])
        return _result("per_minute", pricing["audio"] * ms / 60000, unit_price=pricing["audio"], seconds=round(ms / 1000, 1))
    if unit == "credits":
        return _result("credits", credit_usd=pricing.get("credit_usd"))
    return _result("unknown", unit=unit)


async def _transcript(store: Any, tool: str, model: Optional[str], pricing: Any, unit: Optional[str],
                      params: dict[str, Any], duration_s: Optional[float]) -> dict[str, Any]:
    if unit == "per_minute" and isinstance(pricing.get("audio"), (int, float)):
        if not duration_s:
            return _result("needs_length", unit_price=pricing["audio"])
        return _result("per_minute", pricing["audio"] * duration_s / 60, unit_price=pricing["audio"], seconds=round(duration_s, 1))
    return _result("unknown", unit=unit)


#: kind -> how a request of it is priced (a video needs its frames too: ``estimate`` sends it to ``_video`` first)
_BY_KIND = {"image": _image, "speech": _speech, "music": _music, "transcript": _transcript, "video": None}
M.require_keys(_BY_KIND, M.KINDS, "generate.estimate._BY_KIND")
