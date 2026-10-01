"""Gemini provider — Google native image generation (Nano Banana family)
via the Gemini Developer API and the google-genai SDK.

v1.0 change log (2026-09-25):
* Authentication is a plain AI Studio **API key** (``genai.Client(api_key=…)``).
  The v0.x Vertex service-account path is gone.
* Imagen is gone: Google shut Imagen 4 down and marked the Vertex Imagen
  endpoints deprecated; ``gemini-3.1-flash-image`` (Nano Banana 2) and
  ``gemini-3-pro-image`` (Nano Banana Pro) are the current image models.
* ``edit_image`` is implemented: Nano Banana edits by passing the source
  image(s) as inline parts alongside the prompt to ``generate_content``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from google import genai
from google.genai import types

from .base import (
    ImageResponse,
    LLMProvider,
    ModelCapability,
    ProviderConfig,
    ProviderError,
)

logger = logging.getLogger(__name__)

_MIME_BY_FORMAT = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def _to_bytes(data: str | bytes) -> tuple[bytes, str]:
    """Accept raw bytes, base64 or a data URL; return (bytes, mime)."""
    if isinstance(data, bytes):
        return data, "image/png"
    s = data.strip()
    mime = "image/png"
    if s.startswith("data:"):
        header, _, payload = s.partition(",")
        mime = header[5:].split(";")[0] or mime
        s = payload
    return base64.b64decode(s), mime


class GeminiProvider(LLMProvider):
    """Gemini native image generation/editing (Nano Banana) via generate_content."""

    _NANO_BANANA_CAPABILITY = dict(
        supported_sizes=["auto", "1024x1024", "1536x1024", "1024x1536"],
        supported_qualities=["auto", "high", "medium", "low"],
        supported_formats=["png", "jpeg", "webp"],
        max_images_per_request=1,
        supports_style=False,
        supports_background=False,
        supports_compression=False,
        custom_parameters={
            "aspect_ratio": [
                "1:1", "2:3", "3:2", "3:4", "4:3",
                "4:5", "5:4", "9:16", "16:9", "21:9",
            ],
            "image_size": ["512", "1K", "2K", "4K"],
        },
    )

    SUPPORTED_MODELS = {
        "gemini-3.1-flash-image": ModelCapability(
            model_id="gemini-3.1-flash-image", **_NANO_BANANA_CAPABILITY
        ),
        "gemini-3-pro-image": ModelCapability(
            model_id="gemini-3-pro-image", **_NANO_BANANA_CAPABILITY
        ),
        "gemini-3.1-flash-lite-image": ModelCapability(
            model_id="gemini-3.1-flash-lite-image", **_NANO_BANANA_CAPABILITY
        ),
        # Access limited to prior users (Google, 2026-09); kept for those.
        "gemini-2.5-flash-image": ModelCapability(
            model_id="gemini-2.5-flash-image", **_NANO_BANANA_CAPABILITY
        ),
    }
    # Friendly aliases → model ids
    ALIASES = {
        "nano-banana-2": "gemini-3.1-flash-image",
        "nano-banana-pro": "gemini-3-pro-image",
        "nano-banana-2-lite": "gemini-3.1-flash-lite-image",
        "nano-banana": "gemini-2.5-flash-image",
    }
    DEFAULT_MODEL = "gemini-3.1-flash-image"

    # Nano Banana supports exact 3:2 / 2:3 so preset mapping is precise.
    SIZE_TO_ASPECT_RATIO = {
        "1024x1024": "1:1",
        "1536x1024": "3:2",
        "1024x1536": "2:3",
        "auto": "1:1",
    }
    # Per-image USD (Gemini API pricing page, 2026-09-25).
    PRICING_PER_IMAGE = {
        "gemini-3.1-flash-image": {"512": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151},
        "gemini-3-pro-image": {"1K": 0.134, "2K": 0.134, "4K": 0.24},
        "gemini-3.1-flash-lite-image": {"1K": 0.045, "2K": 0.067},  # unverified
        "gemini-2.5-flash-image": {"1K": 0.039},
    }

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        if not config.api_key or "/" in config.api_key or "\\" in config.api_key:
            raise ValueError(
                "PROVIDERS__GEMINI__API_KEY must be a Gemini (AI Studio) API key. "
                "Service-account file paths are no longer supported (Imagen/Vertex retired)."
            )
        self._httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(config.timeout))
        self.client = genai.Client(
            api_key=config.api_key,
            http_options=types.HttpOptions(
                api_version="v1beta",
                httpx_async_client=self._httpx_client,
            ),
        )

    async def close(self) -> None:
        try:
            if not self._httpx_client.is_closed:
                await self._httpx_client.aclose()
        except Exception as e:  # pragma: no cover
            self._logger.debug("Error closing httpx client: %s", e)

    # ------------------------------------------------------------ registry
    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS.keys())

    def get_model_capabilities(self, model_id: str) -> ModelCapability | None:
        return self.SUPPORTED_MODELS.get(self.ALIASES.get(model_id, model_id))

    def _canonical(self, model: str) -> str:
        model = self.ALIASES.get(model, model)
        if model not in self.SUPPORTED_MODELS:
            raise ProviderError(
                f"Model '{model}' is not supported by Gemini provider",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        return model

    # ------------------------------------------------------------ helpers
    def _resolve_aspect_ratio(self, size: str, kwargs: dict) -> str:
        explicit = kwargs.get("aspect_ratio")
        if explicit:
            return explicit
        return self.SIZE_TO_ASPECT_RATIO.get(size or "auto", "1:1")

    @staticmethod
    def _normalize_image_size(value: str | None) -> str | None:
        """Gemini requires an UPPERCASE K (e.g. '4K'); lowercase is rejected."""
        if not value:
            return None
        return str(value).strip().upper()

    def _image_config(self, size: str, output_format: str, kwargs: dict) -> types.ImageConfig:
        # NOTE: output_mime_type is Vertex-only — the Developer API rejects it
        # ("output_mime_type parameter is not supported in Gemini API",
        # verified live 2026-09-25). Format conversion happens client-side
        # in _convert_format().
        cfg: dict[str, Any] = {
            "aspect_ratio": self._resolve_aspect_ratio(size, kwargs),
            "image_size": self._normalize_image_size(kwargs.get("image_size")) or "2K",
        }
        if kwargs.get("person_generation"):
            cfg["person_generation"] = kwargs["person_generation"]
        return types.ImageConfig(**cfg)

    @staticmethod
    def _convert_format(
        image_bytes: bytes, mime: str | None, output_format: str, compression: int = 100
    ) -> tuple[bytes, str]:
        """Re-encode to the requested format when Gemini's native output
        (PNG) differs. Returns (bytes, mime)."""
        target = _MIME_BY_FORMAT.get((output_format or "png").lower(), "image/png")
        if mime == target or target == "image/png" and (mime in (None, "image/png")):
            return image_bytes, mime or "image/png"
        try:
            import io as _io

            from PIL import Image

            im = Image.open(_io.BytesIO(image_bytes))
            buf = _io.BytesIO()
            fmt = {"image/jpeg": "JPEG", "image/webp": "WEBP", "image/png": "PNG"}[target]
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            save_kwargs: dict[str, Any] = {}
            if fmt in ("JPEG", "WEBP"):
                save_kwargs["quality"] = max(1, min(100, int(compression or 100)))
            im.save(buf, format=fmt, **save_kwargs)
            return buf.getvalue(), target
        except Exception as e:  # pragma: no cover - conversion is best effort
            logger.warning("Gemini image format conversion to %s failed: %s", target, e)
            return image_bytes, mime or "image/png"

    @staticmethod
    def _first_image(response: Any) -> tuple[bytes | None, str | None]:
        """Raw bytes of the first inline image part (already bytes — do NOT
        base64-decode again)."""
        for cand in getattr(response, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and getattr(inline, "data", None):
                    return inline.data, getattr(inline, "mime_type", None)
        return None, None

    async def _generate_content(
        self, model_id: str, contents: list[Any], config: types.ImageConfig
    ) -> Any:
        return await self.client.aio.models.generate_content(
            model=model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"], image_config=config
            ),
        )

    # ------------------------------------------------------------ generate
    async def generate_image(
        self,
        model: str,
        prompt: str,
        quality: str = "auto",
        size: str = "auto",
        style: str = "vivid",
        moderation: str = "auto",
        output_format: str = "png",
        compression: int = 100,
        background: str = "auto",
        n: int = 1,
        **kwargs,
    ) -> ImageResponse:
        model = self._canonical(model)
        config = self._image_config(size, output_format, kwargs)
        try:
            self._logger.info("Generating image with Gemini model %s", model)
            response = await self._generate_content(model, [prompt], config)
            image_bytes, mime = self._first_image(response)
            if not image_bytes:
                raise ProviderError(
                    "No image data in Gemini response (possibly blocked by safety filters)",
                    provider_name=self.name,
                    error_code="INVALID_RESPONSE",
                )
            image_bytes, mime = self._convert_format(image_bytes, mime, output_format, compression)
            return ImageResponse(
                image_data=image_bytes,
                metadata={
                    "model": model,
                    "prompt": prompt,
                    "size": size,
                    "aspect_ratio": config.aspect_ratio,
                    "image_size": config.image_size,
                    "mime_type": mime,
                    "provider": self.name,
                    "created_at": None,
                },
                provider_response={},
            )
        except ProviderError:
            raise
        except Exception as e:
            msg = str(e) or type(e).__name__
            self._logger.error("Error generating Gemini image: %s", msg)
            raise ProviderError(
                f"Gemini image generation failed: {msg}",
                provider_name=self.name,
                error_code="GENERATION_FAILED",
            )

    # ------------------------------------------------------------ edit
    async def edit_image(
        self,
        model: str,
        image_data: str | bytes,
        prompt: str,
        mask_data: str | bytes | None = None,
        quality: str = "auto",
        size: str = "auto",
        output_format: str = "png",
        compression: int = 100,
        background: str = "auto",
        n: int = 1,
        additional_images: list[str | bytes] | None = None,
        **kwargs,
    ) -> ImageResponse:
        """Edit / compose with Nano Banana: source image(s) + instruction.

        Masks are not a Gemini concept; when ``mask_data`` is given it is
        passed as an extra reference image with a hint in the prompt (best
        effort, documented as unverified).
        """
        model = self._canonical(model)
        config = self._image_config(size, output_format, kwargs)
        contents: list[Any] = []
        src, src_mime = _to_bytes(image_data)
        contents.append(types.Part.from_bytes(data=src, mime_type=src_mime))
        for extra in additional_images or []:
            b, m = _to_bytes(extra)
            contents.append(types.Part.from_bytes(data=b, mime_type=m))
        instruction = prompt
        if mask_data:
            mb, mm = _to_bytes(mask_data)
            contents.append(types.Part.from_bytes(data=mb, mime_type=mm))
            instruction = (
                f"{prompt}\n\nThe last image is a mask: white areas mark the "
                "region to change; keep everything else unchanged."
            )
        contents.append(instruction)
        try:
            self._logger.info("Editing image with Gemini model %s", model)
            response = await self._generate_content(model, contents, config)
            image_bytes, mime = self._first_image(response)
            if not image_bytes:
                raise ProviderError(
                    "No image data in Gemini edit response",
                    provider_name=self.name,
                    error_code="INVALID_RESPONSE",
                )
            image_bytes, mime = self._convert_format(image_bytes, mime, output_format, compression)
            return ImageResponse(
                image_data=image_bytes,
                metadata={
                    "model": model,
                    "prompt": prompt,
                    "aspect_ratio": config.aspect_ratio,
                    "image_size": config.image_size,
                    "mime_type": mime,
                    "provider": self.name,
                    "reference_images": 1 + len(additional_images or []),
                },
                provider_response={},
            )
        except ProviderError:
            raise
        except Exception as e:
            msg = str(e) or type(e).__name__
            self._logger.error("Error editing Gemini image: %s", msg)
            raise ProviderError(
                f"Gemini image edit failed: {msg}",
                provider_name=self.name,
                error_code="EDIT_FAILED",
            )

    # ------------------------------------------------------------ misc
    async def check_health(self) -> dict[str, Any]:
        """Free call: list models on the Developer API and check ours are there."""
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            async with httpx.AsyncClient(timeout=10) as client:
                # key 走 header：放 query 會被 httpx 的 INFO log 連同 URL 寫進 daemon.log
                resp = await client.get(url, params={"pageSize": 200}, headers={"x-goog-api-key": self.config.api_key})
                if resp.status_code != 200:
                    try:
                        msg = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
                    except Exception:
                        msg = f"HTTP {resp.status_code}"
                    return {"status": "unhealthy", "error": msg}
                names = {
                    m.get("name", "").split("/")[-1] for m in resp.json().get("models", [])
                }
            available = sorted(m for m in self.SUPPORTED_MODELS if m in names)
            missing = sorted(m for m in self.SUPPORTED_MODELS if m not in names)
            return {
                "status": "healthy" if available else "unhealthy",
                "available_models": available,
                "missing_models": missing,
            }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    def validate_model_params(self, model: str, params: dict[str, Any]) -> dict[str, Any]:
        model = self.ALIASES.get(model, model)
        params = super().validate_model_params(model, params)
        if params.get("n", 1) > 1:
            self._logger.warning("Gemini image models return one image per call; n forced to 1")
            params["n"] = 1
        return params

    def estimate_cost(self, model: str, prompt: str, image_count: int = 1, **kwargs) -> dict[str, Any]:
        model = self.ALIASES.get(model, model)
        table = self.PRICING_PER_IMAGE.get(model)
        if not table:
            return super().estimate_cost(model, prompt, image_count)
        image_size = self._normalize_image_size(kwargs.get("image_size")) or "2K"
        per_image = table.get(image_size) or next(iter(table.values()))
        total = per_image * image_count
        return {
            "provider": self.name,
            "model": model,
            "estimated_cost_usd": round(total, 4),
            "currency": "USD",
            "breakdown": {
                "per_image": per_image,
                "image_size": image_size,
                "total_images": image_count,
                "base_cost": total,
            },
        }
