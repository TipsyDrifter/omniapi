"""OpenRouter's image models: the live roster, how to read it, what a request
costs, and which of them we leave out because we call the vendor directly.

Two free, keyless endpoints (OpenRouter docs, read 2026-10-05):

* ``GET /api/v1/images/models`` — every image model with its
  ``supported_parameters`` (typed descriptors: ``enum`` / ``range`` /
  ``boolean``; an absent key means unsupported) and the URL of its endpoints.
* ``GET /api/v1/images/models/{id}/endpoints`` — per serving provider the
  ``pricing`` lines: ``{billable, unit, cost_usd, variant?}`` with ``billable``
  ``output_image`` / ``input_image`` / ``input_reference`` …, ``unit``
  ``image`` / ``megapixel`` / ``token`` / ``request``, and ``variant`` a tier
  such as ``1k``, ``low_1k``, ``768``.

The roster is one listing plus one request per model (57 on 2026-10-05):
the endpoints are fetched in parallel (a few at a time), and a model whose
endpoints cannot be read keeps the pricing a previous listing had, or is
marked "price unknown" — it never fails the whole roster.

Everything here is pure (no settings, no catalog state) so the catalog, the
estimate, the provider and the sandbox all read the same rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from .discovery import DISCOVERY_TIMEOUT, DiscoveredModel

logger = logging.getLogger(__name__)

#: the key the roster is cached and reported under (``discovery-openrouter-images.json``)
DISCOVERY_KEY = "openrouter-images"
DEFAULT_BASE = "https://openrouter.ai/api/v1"
#: endpoints fetched at once (57 models: a handful of round trips, not 57 at the same instant)
ENDPOINT_CONCURRENCY = 8
ENDPOINT_TIMEOUT = 8.0

#: OpenRouter id prefix (the vendor) -> the catalog provider we call directly for
#: that vendor's image models. The single place that decides "this one is a
#: duplicate": an OpenRouter image model is hidden when its vendor's direct key
#: is set and switched on (see ``ModelCatalog.set_direct_providers``).
DIRECT_VENDORS: dict[str, str] = {"openai": "openai", "google": "google"}

#: resolution tiers as the API names them, smallest first
RESOLUTIONS = ("512", "768", "1K", "1.5K", "2K", "4K")
#: roughly how many megapixels a square image of each tier has (only for
#: megapixel-priced models, and only as an approximation)
_TIER_MP = {"512": 0.26, "768": 0.59, "1k": 1.05, "1.5k": 2.36, "2k": 4.19, "4k": 16.78}


# --------------------------------------------------------------------------
# Reading the listing
# --------------------------------------------------------------------------

def vendor_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else ""


def direct_twin(model_id: str) -> Optional[str]:
    """The catalog provider that serves ``model_id``'s vendor directly, if any."""
    return DIRECT_VENDORS.get(vendor_of(model_id))


def bare_id(model_id: str) -> str:
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def split_name(name: Optional[str], model_id: str) -> tuple[str, str]:
    """``"xAI: Grok Imagine Image 2.0"`` -> (``"xAI"``, ``"Grok Imagine Image 2.0"``)."""
    if name and ": " in name:
        vendor, _, model = name.partition(": ")
        return vendor.strip(), model.strip()
    return vendor_of(model_id), (name or bare_id(model_id))


def _enum(params: dict[str, Any], key: str) -> list[str]:
    d = params.get(key)
    if isinstance(d, dict) and d.get("type") == "enum" and isinstance(d.get("values"), list):
        return [str(v) for v in d["values"]]
    return []


def _range(params: dict[str, Any], key: str) -> Optional[tuple[int, int]]:
    d = params.get(key)
    if isinstance(d, dict) and d.get("type") == "range":
        try:
            return int(d.get("min", 0)), int(d.get("max", 0))
        except (TypeError, ValueError):
            return None
    return None


def image_params(supported: Any) -> dict[str, Any]:
    """The request shape a model takes, from its ``supported_parameters``.

    ``max_references`` 0 means it takes no reference image at all (absent
    ``input_references`` = unsupported); ``min_references`` > 0 means it
    cannot run without one (Recraft's style models, Ming's layer model)."""
    p = supported if isinstance(supported, dict) else {}
    refs = _range(p, "input_references")
    n = _range(p, "n")
    out: dict[str, Any] = {
        "resolutions": _enum(p, "resolution"),
        "aspect_ratios": _enum(p, "aspect_ratio"),
        "qualities": _enum(p, "quality"),
        "output_formats": _enum(p, "output_format"),
        "backgrounds": _enum(p, "background"),
        "min_references": refs[0] if refs else 0,
        "max_references": refs[1] if refs else 0,
        "max_n": max(1, n[1]) if n else 1,
        "seed": isinstance(p.get("seed"), dict),
    }
    return out


def vector_only(params: dict[str, Any]) -> bool:
    """Only answers with SVG (Recraft's vector models): the works wall and its
    thumbnails are raster-only in this version."""
    fmts = params.get("output_formats") or []
    return bool(fmts) and all(f == "svg" for f in fmts)


def pricing_lines(endpoints: Any) -> list[dict[str, Any]]:
    """Every pricing line of every endpoint (a model served by two providers
    may price differently: the estimate then shows the range)."""
    out: list[dict[str, Any]] = []
    for ep in endpoints if isinstance(endpoints, list) else []:
        for line in (ep or {}).get("pricing") or []:
            try:
                cost = float(line.get("cost_usd"))
            except (TypeError, ValueError):
                continue
            row = {"billable": str(line.get("billable") or ""), "unit": str(line.get("unit") or ""), "cost_usd": cost}
            if line.get("variant") not in (None, ""):
                row["variant"] = str(line["variant"])
            if row not in out:
                out.append(row)
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

async def fetch_openrouter_images(
    base_url: str = DEFAULT_BASE,
    *,
    previous: Optional[dict[str, dict[str, Any]]] = None,
    timeout: float = DISCOVERY_TIMEOUT,
    concurrency: int = ENDPOINT_CONCURRENCY,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> list[DiscoveredModel]:
    """The roster with every model's endpoints. No key is sent (both endpoints
    are public). ``previous`` maps id -> the ``extra`` an earlier listing kept:
    a model whose endpoints fail this time keeps those (``pricing_stale``)."""
    base = base_url.rstrip("/")
    root = base[: -len("/api/v1")] if base.endswith("/api/v1") else base
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        resp = await client.get(base + "/images/models")
        resp.raise_for_status()
        items = [m for m in (resp.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def endpoints(item: dict[str, Any]) -> tuple[Optional[list[Any]], Optional[str]]:
            path = item.get("endpoints") or f"/api/v1/images/models/{item['id']}/endpoints"
            url = path if path.startswith("http") else root + path
            async with sem:
                try:
                    r = await client.get(url, timeout=ENDPOINT_TIMEOUT)
                    r.raise_for_status()
                    eps = r.json().get("endpoints")
                    return (eps if isinstance(eps, list) else []), None
                except Exception as e:  # one model's price never fails the roster
                    return None, f"{type(e).__name__}: {e}"[:200]

        got = await asyncio.gather(*(endpoints(i) for i in items))
    out: list[DiscoveredModel] = []
    for item, (eps, err) in zip(items, got):
        extra: dict[str, Any] = {
            k: item[k] for k in ("name", "description", "created", "architecture", "supported_parameters") if k in item
        }
        if eps is not None:
            extra["endpoints"] = [
                {k: ep.get(k) for k in ("provider_name", "provider_slug", "supported_parameters", "pricing") if isinstance(ep, dict)}
                for ep in eps
            ]
        else:
            old = (previous or {}).get(item["id"]) or {}
            if "endpoints" in old:
                extra["endpoints"] = old["endpoints"]
                extra["pricing_stale"] = True
            extra["endpoints_error"] = err
        out.append(DiscoveredModel(id=item["id"], display_name=item.get("name"), extra=extra))
    failed = sum(1 for _e, err in got if err)
    if failed:
        logger.warning("OpenRouter image roster: endpoints of %d of %d models could not be read", failed, len(items))
    return out


def from_listing(models: dict[str, Any], endpoints: dict[str, Any]) -> list[DiscoveredModel]:
    """The same result as :func:`fetch_openrouter_images` from saved
    responses (``{"data": [...]}`` and ``{id: {"endpoints": [...]}}``): the
    test fixtures and the offline sandbox."""
    out = []
    for item in models.get("data") or []:
        extra = {k: item[k] for k in ("name", "description", "created", "architecture", "supported_parameters") if k in item}
        body = endpoints.get(item["id"])
        if isinstance(body, dict) and isinstance(body.get("endpoints"), list):
            extra["endpoints"] = body["endpoints"]
        else:
            extra["endpoints_error"] = "not in the saved listing"
        out.append(DiscoveredModel(id=item["id"], display_name=item.get("name"), extra=extra))
    return out


def sandbox_listing() -> list[DiscoveredModel]:
    """A small stand-in roster for the offline sandbox (``sandbox_images.json``):
    a few representative models — one that takes no reference image, one with
    a quality knob, one priced by resolution, one with no published price."""
    raw = json.loads(Path(__file__).with_name("sandbox_images.json").read_text(encoding="utf-8"))
    return from_listing(raw["models"], raw["endpoints"])


# --------------------------------------------------------------------------
# Turning a listed model into a catalog row
# --------------------------------------------------------------------------

def entry_fields(dm: DiscoveredModel, curated_twin: Any = None) -> dict[str, Any]:
    """Fields of the ``ModelEntry`` for one listed model.

    ``curated_twin`` is the catalog's own row for the same model at the
    direct vendor (``google/gemini-2.5-flash-image`` -> Google's
    ``gemini-2.5-flash-image``), when there is one: its retired / deprecated
    status and shutdown date carry over — the vendor closing a model closes
    it everywhere."""
    extra = dm.extra if isinstance(dm.extra, dict) else {}
    vendor_label, name = split_name(extra.get("name") or dm.display_name, dm.id)
    params = image_params(extra.get("supported_parameters"))
    eps = extra.get("endpoints")
    lines = pricing_lines(eps)
    pricing: dict[str, Any] = {"unit": "openrouter", "source": "openrouter", "lines": lines}
    if not lines:
        pricing["unknown"] = True
    if extra.get("pricing_stale"):
        pricing["stale"] = True
    implemented = True
    note = None
    if isinstance(eps, list) and not eps:
        implemented, note = False, "OpenRouter lists this model but no provider serves it right now."
    elif vector_only(params):
        implemented, note = False, "Answers with SVG vector images only; the works wall takes raster images in this version."
    status, shutdown = "current", None
    if curated_twin is not None and getattr(curated_twin, "status", None) in ("retired", "deprecated"):
        status, shutdown = curated_twin.status, getattr(curated_twin, "shutdown", None)
    caps = {"edit": params["max_references"] > 0, "multi_reference": params["max_references"] > 1}
    return {
        "name": name,
        "status": status,
        "shutdown": shutdown,
        "pricing": pricing,
        "capabilities": caps,
        "implemented": implemented,
        "note": note,
        "vendor": vendor_of(dm.id),
        "vendor_label": vendor_label,
        "image_params": params,
        # an old alias of a model OpenRouter now lists under its final name
        "snapshot": dm.id.endswith("-preview"),
    }


# --------------------------------------------------------------------------
# What a request costs
# --------------------------------------------------------------------------

def _res_token(resolution: Optional[str]) -> Optional[str]:
    return str(resolution).strip().lower() if resolution else None


def _variant_known(variant: str) -> bool:
    """Is a pricing variant a resolution tier or a quality_resolution pair?
    (Anything else — ``high_resolution`` — we cannot map to a request.)"""
    v = variant.lower()
    tiers = {t.lower() for t in RESOLUTIONS}
    if v in tiers:
        return True
    q, _, r = v.partition("_")
    return bool(r) and r in tiers and q in ("auto", "low", "medium", "high", "xhigh", "max")


def _spread(costs: Iterable[float]) -> tuple[float, float]:
    cs = list(costs)
    return min(cs), max(cs)


def price_output(lines: list[dict[str, Any]], *, resolution: Optional[str], quality: Optional[str],
                 aspect_ratio: Optional[str] = None) -> dict[str, Any]:
    """Price of ONE output image: ``{kind, low, high, variant?, approx?}``.

    ``kind``: ``exact`` (one price), ``range`` (several prices could apply:
    the variant could not be matched, or two providers price it apart),
    ``megapixel`` (an approximation from the tier's pixel count), ``token``
    (priced per token: not computable up front), ``none`` (no price published).
    """
    out_lines = [ln for ln in lines if ln.get("billable") == "output_image"]
    if not out_lines:
        return {"kind": "none"}
    per_image = [ln for ln in out_lines if ln.get("unit") == "image"]
    if per_image:
        res = _res_token(resolution)
        q = (quality or "").lower() or None
        wanted = [w for w in ((f"{q}_{res}" if q and res else None), res, q) if w]
        for w in wanted:
            hit = [ln["cost_usd"] for ln in per_image if str(ln.get("variant", "")).lower() == w]
            if hit:
                low, high = _spread(hit)
                return {"kind": "exact" if low == high else "range", "low": low, "high": high, "variant": w}
        # half a match narrows the range: 2K with no quality said is one of the *_2k lines
        partial = [ln["cost_usd"] for ln in per_image if "variant" in ln and (
            (res and str(ln["variant"]).lower().endswith("_" + res)) or (q and not res and str(ln["variant"]).lower().startswith(q + "_")))]
        if partial:
            low, high = _spread(partial)
            return {"kind": "exact" if low == high else "range", "low": low, "high": high}
        base = [ln["cost_usd"] for ln in per_image if "variant" not in ln]
        variants = [str(ln["variant"]) for ln in per_image if "variant" in ln]
        # no variant matched: the variant-less line is the price only when every
        # variant is a tier/quality we can name and the request is none of them
        if base and (not variants or (res and all(_variant_known(v) for v in variants))):
            low, high = _spread(base)
            return {"kind": "exact" if low == high else "range", "low": low, "high": high}
        low, high = _spread(ln["cost_usd"] for ln in per_image)
        return {"kind": "exact" if low == high else "range", "low": low, "high": high}
    per_mp = [ln for ln in out_lines if ln.get("unit") == "megapixel"]
    if per_mp:
        # a tier names the long edge class; a non-square image of it has about as
        # many pixels as the square one, so the ratio is not used (an approximation either way)
        mp = _TIER_MP.get(_res_token(resolution) or "1k", 1.05)
        low, high = _spread(ln["cost_usd"] for ln in per_mp)
        return {"kind": "megapixel", "low": low * mp, "high": high * mp, "unit_price": low, "megapixels": mp, "approx": True}
    tok = [ln for ln in out_lines if ln.get("unit") == "token"]
    if tok:
        return {"kind": "token", "unit_price": min(ln["cost_usd"] for ln in tok)}
    return {"kind": "none"}


def price_references(lines: list[dict[str, Any]], refs: int) -> dict[str, Any]:
    """What ``refs`` reference / source images add: ``{low, high, unknown?}``.
    Per-image lines multiply, per-request lines count once; token and
    megapixel lines cannot be computed (``unknown``)."""
    if refs <= 0:
        return {"low": 0.0, "high": 0.0}
    low = high = 0.0
    unknown = False
    for billable in ("input_image", "input_reference"):
        ls = [ln for ln in lines if ln.get("billable") == billable]
        if not ls:
            continue
        for unit, times in (("image", refs), ("request", 1)):
            us = [ln["cost_usd"] for ln in ls if ln.get("unit") == unit]
            if us:
                a, b = _spread(us)
                low += a * times
                high += b * times
        if any(ln.get("unit") in ("token", "megapixel") and ln["cost_usd"] > 0 for ln in ls):
            unknown = True
    out: dict[str, Any] = {"low": low, "high": high}
    if unknown:
        out["unknown"] = True
    return out


def estimate(pricing: Optional[dict[str, Any]], *, resolution: Optional[str], quality: Optional[str],
             aspect_ratio: Optional[str], n: int, refs: int) -> dict[str, Any]:
    """The estimate fields for ``generate.estimate`` (``basis`` and friends)."""
    lines = (pricing or {}).get("lines") or []
    one = price_output(lines, resolution=resolution, quality=quality, aspect_ratio=aspect_ratio)
    kind = one["kind"]
    if kind in ("none", "token"):
        basis = "no_price" if kind == "none" else "per_token"
        return {"basis": basis, "usd": None, "unit_price": one.get("unit_price"), "n": n}
    ref = price_references(lines, refs)
    low = one["low"] * n + ref["low"]
    high = one["high"] * n + ref["high"]
    detail: dict[str, Any] = {
        "n": n, "unit_price": one["low"] if one["low"] == one["high"] else None,
        "variant": one.get("variant"), "refs": refs or None,
        "ref_usd": round(ref["low"], 6) if refs and ref["low"] == ref["high"] else None,
        "refs_unpriced": True if ref.get("unknown") else None,
        "stale": True if (pricing or {}).get("stale") else None,
    }
    if kind == "megapixel":
        return {"basis": "per_megapixel", "usd": low, "low": low, "high": high, "megapixels": one["megapixels"],
                "mp_price": one["unit_price"], "approx": True, **detail}
    if low == high:
        return {"basis": "variant", "usd": low, **detail}
    return {"basis": "range", "usd": None, "low": low, "high": high, **detail}


# --------------------------------------------------------------------------
# Checking a request before it costs anything
# --------------------------------------------------------------------------

def check_request(params: dict[str, Any], *, refs: int, resolution: Optional[str] = None,
                  aspect_ratio: Optional[str] = None, quality: Optional[str] = None, n: int = 1) -> Optional[str]:
    """Why a request cannot go to this model, in words — or ``None``. Only
    what the listing states is checked; the vendor has the last word."""
    max_refs = int(params.get("max_references") or 0)
    min_refs = int(params.get("min_references") or 0)
    if refs > 0 and max_refs == 0:
        return "This model takes no reference image, so it cannot edit an image: pick a model that edits, or remove the source image."
    if refs > max_refs:
        return f"This model takes at most {max_refs} reference image(s); {refs} were given."
    if refs < min_refs:
        return f"This model needs at least {min_refs} reference image(s) (it restyles a given image); add a source image."
    for key, value, allowed in (("resolution", resolution, params.get("resolutions")),
                                ("aspect_ratio", aspect_ratio, params.get("aspect_ratios")),
                                ("quality", quality, params.get("qualities"))):
        if value and allowed and value not in allowed:
            return f"invalid {key} {value!r} for this model; supported: {', '.join(allowed)}"
    if n > int(params.get("max_n") or 1):
        return f"invalid n={n}: this model makes at most {params.get('max_n') or 1} image(s) per request"
    return None


def check_tool_args(entry: Any, tool: str, args: dict[str, Any]) -> Optional[str]:
    """:func:`check_request` for a ``generate_image`` / ``edit_image`` call's
    arguments (source images as ``image_path`` / ``image_data`` +
    ``additional_image(_path)s``) against a catalog row — the GUI's job
    manager and the sandbox ask this before anything is sent."""
    if entry is None or not getattr(entry, "via_openrouter_image", False):
        return None
    if not entry.implemented:
        return f"{entry.id} cannot be used in this version: {entry.note or 'not supported'}"
    refs = 0
    if tool == "edit_image":
        refs = (1 if (args.get("image_path") or args.get("image_data")) else 0) + len(args.get("additional_image_paths") or []) \
            + len(args.get("additional_images") or [])
    params = entry.image_params or {}
    quality = args.get("quality")
    if quality and (not params.get("qualities") or (quality == "auto" and "auto" not in params["qualities"])):
        quality = None  # not sent for this model (see the provider): nothing to check
    return check_request(params, refs=refs,
                         resolution=args.get("image_size") if params.get("resolutions") else None,
                         aspect_ratio=args.get("aspect_ratio") if params.get("aspect_ratios") else None,
                         quality=quality, n=int(args.get("n") or 1) if tool == "generate_image" else 1)


def ratio_of_size(size: Optional[str]) -> Optional[str]:
    """``"1536x1024"`` -> ``"3:2"`` (for callers that only gave a pixel size)."""
    m = re.match(r"^(\d+)x(\d+)$", str(size or ""))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    g = math.gcd(w, h) or 1
    return f"{w // g}:{h // g}"
