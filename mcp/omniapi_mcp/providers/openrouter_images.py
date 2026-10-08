"""OpenRouter image provider (1.4-M2): every image model on OpenRouter's
live roster, through one request shape.

``POST {base}/images`` (synchronous; OpenRouter docs read 2026-10-05, one
real call on 2026-10-05 — ``prototypes/openrouter-media-probe/``):

* request: ``model``, ``prompt``, and only what the model's listing says it
  takes — ``resolution``, ``aspect_ratio``, ``quality``, ``n``, ``seed``,
  ``output_format``, ``background``, ``input_references`` (each
  ``{"type": "image_url", "image_url": {"url": <data URL>}}``: a local image
  goes inline, no public address needed).
* response: ``data[].b64_json`` + ``media_type``; ``usage.cost`` is what the
  call actually cost (USD) — that, not the listed price, goes on the ledger.
* billing is all-or-nothing: a failed generation is not billed.

The roster is the catalog's (``catalog/openrouter_images.py``); this class
asks it per call, so a model OpenRouter adds shows up without a restart.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

import httpx

from ..catalog.openrouter_images import check_request, ratio_of_size
from .base import ImageResponse, LLMProvider, ModelCapability, ProviderConfig, ProviderError

logger = logging.getLogger(__name__)

_FORMAT_OF_MEDIA = {"image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/webp": "webp", "image/svg+xml": "svg"}
_CONTENT_WORDS = ("moderation", "flagged", "safety", "content policy", "content_policy", "prohibited", "blocked", "nsfw", "violat")


def _magic_format(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"


def _data_url(image: str | bytes) -> str:
    """Raw bytes, plain base64 or a data URL -> a data URL."""
    if isinstance(image, bytes):
        fmt = _magic_format(image)
        return f"data:image/{fmt};base64,{base64.b64encode(image).decode()}"
    s = image.strip()
    if s.startswith("data:"):
        return s
    try:
        head = base64.b64decode(s[:64] + "=" * (-len(s[:64]) % 4))
    except Exception:  # noqa: BLE001 — not decodable: let the vendor say so
        head = b""
    return f"data:image/{_magic_format(head)};base64,{s}"


class OpenRouterImageProvider(LLMProvider):
    """Image generation and editing for OpenRouter's image models."""

    #: the registry asks this provider per model instead of a fixed set
    dynamic_models = True

    def __init__(self, config: ProviderConfig, catalog: Any = None):
        super().__init__(config)
        self.name = "openrouter"
        if catalog is None:
            from ..catalog import catalog as default_catalog

            catalog = default_catalog
        self._catalog = catalog
        self._base = (config.base_url or "https://openrouter.ai/api/v1").rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(config.timeout, connect=15.0))

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ roster
    def _entry(self, model: str):
        return self._catalog.openrouter_image(model)

    def supports_model(self, model: str) -> bool:
        e = self._entry(model)
        return bool(e and e.implemented and e.online is not False)

    def get_supported_models(self) -> set[str]:
        return {e.id for e in self._catalog.models(modality="image", provider="openrouter", include_snapshots=True,
                                                   include_duplicates=True)
                if e.via_openrouter_image and e.implemented and e.online is not False}

    def get_model_capabilities(self, model_id: str) -> Optional[ModelCapability]:
        e = self._entry(model_id)
        if not e:
            return None
        p = e.image_params or {}
        return ModelCapability(
            model_id=model_id,
            supported_sizes=[],
            supported_qualities=list(p.get("qualities") or []),
            supported_formats=list(p.get("output_formats") or []),
            max_images_per_request=int(p.get("max_n") or 1),
            supports_background=bool(p.get("backgrounds")),
            custom_parameters={"openrouter": p},
        )

    def validate_model_params(self, model: str, params: dict[str, Any]) -> dict[str, Any]:
        """The listing decides what is sent (``_body``); nothing is rewritten here."""
        if not self.supports_model(model):
            raise ProviderError(f"'{model}' is not on OpenRouter's image roster (or cannot be called now)",
                                provider_name=self.name, error_code="UNSUPPORTED_MODEL", error_kind="invalid")
        return params

    # ------------------------------------------------------------ requests
    def _params(self, model: str) -> dict[str, Any]:
        e = self._entry(model)
        if not e:
            raise ProviderError(f"'{model}' is not on OpenRouter's image roster; list the models again "
                                "(list_available_models with refresh=true)",
                                provider_name=self.name, error_code="UNSUPPORTED_MODEL", error_kind="invalid")
        if not e.implemented:
            raise ProviderError(f"'{model}' cannot be used in this version: {e.note or 'not supported'}",
                                provider_name=self.name, error_code="UNSUPPORTED_MODEL", error_kind="invalid")
        return e.image_params or {}

    def _body(self, model: str, prompt: str, *, refs: list[str], quality: Optional[str], size: Optional[str],
              n: int, image_size: Optional[str], aspect_ratio: Optional[str], seed: Optional[int],
              output_format: Optional[str], background: Optional[str]) -> dict[str, Any]:
        p = self._params(model)
        ratio = aspect_ratio or (ratio_of_size(size) if (ratio_of_size(size) in (p.get("aspect_ratios") or [])) else None)
        resolution = image_size if p.get("resolutions") else None
        q = quality if (p.get("qualities") and quality and (quality != "auto" or "auto" in p["qualities"])) else None
        problem = check_request(p, refs=len(refs), resolution=resolution,
                                aspect_ratio=ratio if p.get("aspect_ratios") else None, quality=q, n=n)
        if problem:
            raise ProviderError(problem, provider_name=self.name, error_code="INVALID_REQUEST", error_kind="invalid")
        body: dict[str, Any] = {"model": model, "prompt": prompt}
        if resolution:
            body["resolution"] = resolution
        if ratio and p.get("aspect_ratios"):
            body["aspect_ratio"] = ratio
        if q:
            body["quality"] = q
        if n > 1:
            body["n"] = n
        if seed is not None and p.get("seed"):
            body["seed"] = int(seed)
        if output_format and output_format in (p.get("output_formats") or []):
            body["output_format"] = output_format
        if background and background != "auto" and background in (p.get("backgrounds") or []):
            body["background"] = background
        if refs:
            body["input_references"] = [{"type": "image_url", "image_url": {"url": _data_url(r)}} for r in refs]
        return body

    async def _post(self, model: str, body: dict[str, Any]) -> ImageResponse:
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json",
                   "X-Title": "OmniAPI"}
        try:
            resp = await self._client.post(self._base + "/images", json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise ProviderError(f"OpenRouter timed out generating with {model}: {e}", provider_name=self.name,
                                error_code="TIMEOUT", error_kind="timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"could not connect to OpenRouter: {type(e).__name__}: {e}", provider_name=self.name,
                                error_code="CONNECTION", error_kind="unavailable") from e
        if resp.status_code >= 400:
            raise self._error(model, body, resp)
        try:
            j = resp.json()
        except ValueError as e:
            raise ProviderError(f"OpenRouter answered {resp.status_code} with something that is not JSON",
                                provider_name=self.name, error_code="BAD_RESPONSE", error_kind="other") from e
        images: list[bytes] = []
        formats: list[str] = []
        for item in j.get("data") or []:
            b64 = (item or {}).get("b64_json")
            if not b64:
                continue
            raw = base64.b64decode(b64)
            images.append(raw)
            formats.append(_FORMAT_OF_MEDIA.get(str(item.get("media_type") or "").lower()) or _magic_format(raw))
        if not images:
            raise ProviderError(f"OpenRouter returned no image for {model}", provider_name=self.name,
                                error_code="NO_IMAGE", error_kind="other")
        usage = j.get("usage") if isinstance(j.get("usage"), dict) else {}
        cost = usage.get("cost")
        meta = {
            "model": model,
            "provider": "openrouter",
            "vendor": model.split("/", 1)[0],
            "file_format": formats[0],
            "media_type": (j.get("data") or [{}])[0].get("media_type"),
            "usage": usage,
            "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
            "request": {k: v for k, v in body.items() if k not in ("prompt", "input_references")},
            "references": len(body.get("input_references") or []),
        }
        return ImageResponse(image_data=images[0], images=images if len(images) > 1 else None, metadata=meta)

    def _error(self, model: str, body: dict[str, Any], resp: httpx.Response) -> ProviderError:
        """An HTTP failure in our failure kinds. A 400 names what the model does
        take (the listing's values), so the caller sees what to change."""
        status = resp.status_code
        message, raw = "", ""
        try:
            err = (resp.json() or {}).get("error") or {}
            message = str(err.get("message") or "")
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            raw = str(meta.get("raw") or meta.get("reasons") or "")[:600]
        except ValueError:
            message = resp.text[:600]
        said = f"{message}{' — ' + raw if raw and raw not in message else ''}".strip() or f"HTTP {status}"
        content = any(w in said.lower() for w in _CONTENT_WORDS)
        if status == 402:
            kind, text = "quota", f"OpenRouter: not enough credits for this image (402): {said}"
        elif status == 401:
            kind, text = "auth", f"OpenRouter rejected the API key (401): {said}"
        elif status == 403:
            kind = "rejected" if content else "auth"
            text = (f"OpenRouter blocked the request by content policy (403): {said}" if content
                    else f"OpenRouter refused the request (403): {said}")
        elif status == 429:
            kind, text = "quota", f"OpenRouter rate limit (429): {said}"
        elif status == 413:
            kind, text = "too_large", f"the request is too large for OpenRouter (413): {said}"
        elif status in (408, 524):
            kind, text = "timeout", f"OpenRouter timed out ({status}): {said}"
        elif status in (400, 422):
            if content:
                kind, text = "rejected", f"the content was refused by {model} ({status}): {said}"
            else:
                kind, text = "invalid", f"invalid request for {model} ({status}): {said}{self._supported_hint(model, body)}"
        elif content:
            kind, text = "rejected", f"the content was refused by {model} ({status}): {said}"
        else:
            kind, text = "unavailable", f"OpenRouter or the model's provider failed ({status}): {said}"
        logger.warning("OpenRouter image %s -> HTTP %s (%s)", model, status, kind)
        return ProviderError(text, provider_name=self.name, error_code=str(status), error_kind=kind)

    def _supported_hint(self, model: str, body: dict[str, Any]) -> str:
        e = self._entry(model)
        p = (e.image_params if e else None) or {}
        parts = []
        for key, listed in (("resolution", "resolutions"), ("aspect_ratio", "aspect_ratios"), ("quality", "qualities")):
            if key in body and p.get(listed):
                parts.append(f"{key} {', '.join(p[listed])}")
        if body.get("input_references"):
            parts.append(f"reference images {p.get('min_references', 0)}-{p.get('max_references', 0)}")
        return f". The listing says this model takes: {'; '.join(parts)}" if parts else ""

    # ------------------------------------------------------------ the two calls
    async def generate_image(self, model: str, prompt: str, quality: str = "auto", size: str = "auto",
                             style: str = "vivid", moderation: str = "auto", output_format: str = "png",
                             compression: int = 100, background: str = "auto", n: int = 1, **kwargs: Any) -> ImageResponse:
        body = self._body(model, prompt, refs=[], quality=quality, size=size, n=max(1, int(n or 1)),
                          image_size=kwargs.get("image_size"), aspect_ratio=kwargs.get("aspect_ratio"),
                          seed=kwargs.get("seed"), output_format=output_format, background=background)
        return await self._post(model, body)

    async def edit_image(self, model: str, image_data: str | bytes, prompt: str, mask_data: str | bytes | None = None,
                         quality: str = "auto", size: str = "auto", output_format: str = "png", compression: int = 100,
                         background: str = "auto", n: int = 1, **kwargs: Any) -> ImageResponse:
        if mask_data:
            raise ProviderError("OpenRouter image models take no mask; describe the change in the prompt instead",
                                provider_name=self.name, error_code="INVALID_REQUEST", error_kind="invalid")
        refs = [image_data, *(kwargs.get("additional_images") or [])]
        body = self._body(model, prompt, refs=refs, quality=quality, size=None, n=1,
                          image_size=kwargs.get("image_size"), aspect_ratio=kwargs.get("aspect_ratio"),
                          seed=kwargs.get("seed"), output_format=output_format, background=background)
        return await self._post(model, body)

    def estimate_cost(self, model: str, prompt: str, image_count: int = 1, **kwargs: Any) -> dict[str, Any]:
        """The listed price (before a call; the tools replace it with ``usage.cost`` after)."""
        from ..catalog.openrouter_images import estimate

        e = self._entry(model)
        est = estimate(e.pricing if e else None, resolution=kwargs.get("image_size"), quality=kwargs.get("quality"),
                       aspect_ratio=kwargs.get("aspect_ratio"), n=image_count, refs=int(kwargs.get("refs") or 0))
        return {"provider": self.name, "model": model, "estimated_cost_usd": est.get("usd"), "currency": "USD", "basis": est}
