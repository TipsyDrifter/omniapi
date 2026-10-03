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
==================  =========================================================
"""

from __future__ import annotations

from typing import Any, Optional

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


async def estimate(store: Any, *, kind: str, tool: str, params: dict[str, Any], duration_s: Optional[float] = None) -> dict[str, Any]:
    """``params`` are the tool's own arguments (``model`` resolved by the
    caller). ``duration_s`` is the length of the source audio for a
    transcription — the browser knows it, the upload row does not."""
    if offline():
        return _result("sandbox", 0.0)
    model = params.get("model")
    entry = catalog.get(model) if model else None
    pricing = entry.pricing if entry and isinstance(entry.pricing, dict) else None
    unit = pricing.get("unit") if pricing else None

    if kind == "image":
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

    if kind == "speech":
        chars = len(str(params.get("text") or ""))
        if unit == "per_1k_chars" and isinstance(pricing.get("text"), (int, float)):
            return _result("per_1k_chars", pricing["text"] * chars / 1000, unit_price=pricing["text"], chars=chars)
        if model:
            rows = [r for r in await store.artifact_costs(tool=tool, model=model) if r.get("prompt_len")]
            if rows:
                per_char = sum(r["cost_usd"] / r["prompt_len"] for r in rows) / len(rows)
                return _result("history_chars", per_char * chars, samples=len(rows), chars=chars)
        return _result("unknown", unit=unit, chars=chars)

    if kind == "music":
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

    if kind == "transcript":
        if unit == "per_minute" and isinstance(pricing.get("audio"), (int, float)):
            if not duration_s:
                return _result("needs_length", unit_price=pricing["audio"])
            return _result("per_minute", pricing["audio"] * duration_s / 60, unit_price=pricing["audio"], seconds=round(duration_s, 1))
        return _result("unknown", unit=unit)

    return _result("unknown")
