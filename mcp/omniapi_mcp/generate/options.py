"""What the generate page can offer right now: the models of each kind with
whether they can actually be called (and why not), the request shape each
image model takes, and the voices to pick from.

"Can be called" is asked of the tools themselves — the same routing table a
real call goes through — not inferred from which keys are set: a model the
catalog lists but no tool routes (Lyria today) is as unusable as one whose
key is missing.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .. import modalities as M
from ..catalog import catalog
from ..devmode import dev_enabled, offline

logger = logging.getLogger(__name__)

#: generation kind -> catalog modality
MODALITY = M.CATALOG_OF

#: catalog provider -> (settings slot, the env var a user sets)
_PROVIDER_KEY = {
    "openai": ("openai", "PROVIDERS__OPENAI__API_KEY"),
    "google": ("gemini", "PROVIDERS__GEMINI__API_KEY"),
    "elevenlabs": ("elevenlabs", "PROVIDERS__ELEVENLABS__API_KEY"),
    "kie": ("kie", "PROVIDERS__KIE__API_KEY"),
    "openrouter": ("openrouter", "PROVIDERS__OPENROUTER__API_KEY"),
}

_VOICE_TTL = 600.0
_voice_cache: dict[str, Any] = {"at": 0.0, "voices": None, "error": None}


def provider_configured(settings: Any, provider: str) -> bool:
    slot = _PROVIDER_KEY.get(provider, (provider, ""))[0]
    cfg = getattr(settings.providers, slot, None)
    return bool(cfg and getattr(cfg, "enabled", False) and getattr(cfg, "api_key", None))


async def _image_models(ctx: Any) -> set[str]:
    await ctx.image_generation_tool.ensure_providers_registered()
    return set(ctx.image_generation_tool.provider_registry.get_supported_models())


async def _speech_models(ctx: Any) -> set[str]:
    return set(ctx.speech_tool.available_models())


async def _music_models(ctx: Any) -> set[str]:
    return set(ctx.music_generation_tool.available_models())


async def _transcript_models(ctx: Any) -> set[str]:
    provider = getattr(ctx.transcription_tool, "_provider", None)
    return set(provider.get_supported_models()) if provider is not None else set()


async def _video_models(ctx: Any) -> set[str]:
    """1.4-M3: every video model on OpenRouter's roster that makes video and is
    not closed, once the OpenRouter key is set (the roster is only listed then);
    1.4-M5: Gemini Omni (a curated Google video row) once the Google key is set."""
    out: set[str] = set()
    for e in catalog.models(modality="video"):
        route = e.video_route
        if route and e.implemented and e.online is not False and provider_configured(ctx.settings, route):
            out.add(e.id)
    return out


#: kind -> the routing table of the tool behind it
_ROUTES = {"image": _image_models, "speech": _speech_models, "music": _music_models, "transcript": _transcript_models,
           "video": _video_models}
M.require_keys(_ROUTES, M.KINDS, "generate.options._ROUTES")

#: kind -> the tool's own default model, when it has one beyond the settings
_TOOL_DEFAULT = {
    "image": lambda ctx: None,
    "speech": lambda ctx: ctx.speech_tool._default_model(),
    "music": lambda ctx: ctx.music_generation_tool._default_model(),
    "transcript": lambda ctx: None,
    "video": lambda ctx: ctx.videos.default_model() if getattr(ctx, "videos", None) is not None else None,
}
M.require_keys(_TOOL_DEFAULT, M.KINDS, "generate.options._TOOL_DEFAULT")


async def callable_models(ctx: Any, kind: str) -> set[str]:
    """Model ids the tool behind ``kind`` routes to a configured provider."""
    route = _ROUTES.get(kind)
    return await route(ctx) if route is not None else set()


def _sandbox() -> bool:
    return offline() and dev_enabled()


async def models_for(ctx: Any, kind: str) -> list[dict[str, Any]]:
    """Catalog entries of one kind, each with ``available`` and, when it is
    not, ``unavailable`` = ``{reason, env?}``:

    * ``missing_key``     — the provider's key is not set (``env`` names it)
    * ``not_implemented`` — listed, but no generation path is wired yet
    * ``offline``         — an offline daemon that is not a development sandbox
    * ``retired``         — (video) the vendor closed it; listed so it is not a mystery where it went
    """
    can_call = await callable_models(ctx, kind)
    out = []
    # a closed video model stays on the list (struck out on the page) rather than vanishing
    for entry in catalog.models(modality=MODALITY[kind], include_retired=kind == "video"):
        row = entry.to_dict()
        reason: Optional[dict[str, Any]] = None
        if entry.via_openrouter_image and not entry.implemented:
            reason = {"reason": "not_implemented"}  # SVG-only, or nobody serves it: not even the sandbox offers it
        elif entry.via_openrouter_video and entry.status == "retired":
            reason = {"reason": "retired"}
        elif kind == "video" and (not entry.implemented or not entry.video_route):
            # video goes through OpenRouter, or directly to Google for Gemini Omni (1.4-M5); the sandbox's
            # stand-in answers for both. Anything else on the list is known, not callable.
            reason = {"reason": "not_implemented"}
        elif _sandbox():
            pass  # stand-in generators answer for every model
        elif offline():
            reason = {"reason": "offline"}
        elif entry.id not in can_call:
            if not entry.implemented or provider_configured(ctx.settings, entry.provider):
                reason = {"reason": "not_implemented"}
            else:
                reason = {"reason": "missing_key", "env": _PROVIDER_KEY.get(entry.provider, ("", None))[1]}
        row["available"] = reason is None
        if reason:
            row["unavailable"] = reason
        out.append(row)
    return out


async def image_capabilities(ctx: Any) -> dict[str, Any]:
    """Per image model: the sizes / qualities / formats its provider accepts."""
    await ctx.image_generation_tool.ensure_providers_registered()
    registry = ctx.image_generation_tool.provider_registry
    caps: dict[str, Any] = {}
    for model_id in registry.get_supported_models():
        info = registry.get_model_info(model_id)
        if not info or info["provider"] == "openrouter":
            continue  # OpenRouter's models carry their request shape on their own row (image_params)
        c = info["capabilities"]
        caps[model_id] = {
            # the image registry calls Google's provider "gemini"; the catalog (and the page) say "google"
            "provider": {"gemini": "google"}.get(info["provider"], info["provider"]),
            "sizes": c.supported_sizes,
            "qualities": c.supported_qualities,
            "formats": c.supported_formats,
            "max_images": c.max_images_per_request,
            "supports_background": c.supports_background,
            "features": c.custom_parameters,
        }
    return caps


async def voices(ctx: Any, *, refresh: bool = False) -> dict[str, Any]:
    """Voices per speech provider. OpenAI and Gemini ship a fixed set; an
    ElevenLabs account has its own, asked for once and kept for ten minutes."""
    from ..capabilities.speech import ElevenLabsProvider, GeminiSpeechProvider, OpenAITTSProvider

    out: dict[str, Any] = {
        "openai": {
            "default": OpenAITTSProvider.DEFAULT_VOICE,
            "voices": [{"id": v, "name": v, "only": ["gpt-4o-mini-tts"] if v in OpenAITTSProvider.NEW_MODEL_ONLY_VOICES else None}
                       for v in OpenAITTSProvider.VOICES],
        },
        "google": {
            "default": GeminiSpeechProvider.DEFAULT_VOICE,
            "voices": [{"id": v, "name": v, "note": note} for v, note in GeminiSpeechProvider.VOICES.items()],
        },
        "elevenlabs": {"default": ElevenLabsProvider.DEFAULT_VOICE, "voices": [], "live": True},
    }
    eleven = next((p for p in getattr(ctx.speech_tool, "_providers", []) if isinstance(p, ElevenLabsProvider)), None)
    if eleven is None or offline():
        out["elevenlabs"]["voices"] = [{"id": ElevenLabsProvider.DEFAULT_VOICE, "name": "Rachel", "note": "preset"}]
        out["elevenlabs"]["live"] = False
        return out
    now = time.time()
    if refresh or _voice_cache["voices"] is None or now - _voice_cache["at"] > _VOICE_TTL:
        try:
            _voice_cache.update(at=now, voices=await eleven.list_voices(), error=None)
        except Exception as e:  # the form still works with the preset voice
            logger.warning("ElevenLabs voice list failed: %s", e)
            _voice_cache.update(at=now, voices=[], error=str(e)[:300])
    listed = list(_voice_cache["voices"] or [])
    if not any(v["id"] == ElevenLabsProvider.DEFAULT_VOICE for v in listed):
        listed.append({"id": ElevenLabsProvider.DEFAULT_VOICE, "name": "Rachel", "note": "preset"})
    out["elevenlabs"]["voices"] = listed
    if _voice_cache["error"]:
        out["elevenlabs"]["error"] = _voice_cache["error"]
    return out


async def options(ctx: Any, *, refresh_voices: bool = False) -> dict[str, Any]:
    """Everything the generate page needs to draw its four forms."""
    kinds = {}
    for kind in MODALITY:
        models = await models_for(ctx, kind)
        kinds[kind] = {"models": models, "default_model": _default_model(ctx, kind, models)}
    kinds["image"]["capabilities"] = await image_capabilities(ctx)
    kinds["speech"]["voices"] = await voices(ctx, refresh=refresh_voices)
    kinds["video"].update(video_extras(ctx))
    return {"offline": offline(), "sandbox": _sandbox(), "kinds": kinds}


def video_extras(ctx: Any) -> dict[str, Any]:
    """What the video form needs beyond the models: the limits in effect and
    the listed models left out of this version (with why)."""
    from ..video.jobs import POLL_SCHEDULE, _keep_collecting, _max_wait, mcp_limit, poll_interval

    settings = ctx.settings
    unlisted = [{"id": e.id, "name": e.name, "reason": e.unlisted, "note": e.note}
                for e in catalog.models(modality="video", include_unlisted=True, include_retired=True) if e.unlisted]
    return {
        "limits": {
            "mcp_max_usd": mcp_limit(settings),
            "max_wait_s": _max_wait(settings),
            "keep_collecting": _keep_collecting(settings),
            "poll_s": [poll_interval(i) for i in range(len(POLL_SCHEDULE))],
        },
        "unlisted": unlisted,
    }


def _default_model(ctx: Any, kind: str, models: list[dict[str, Any]]) -> Optional[str]:
    """The configured default (settings.json / env), then the tool's own,
    when usable; else the first usable one."""
    from ..config.user_settings import default_model

    usable = [m["id"] for m in models if m["available"]]
    candidates = [default_model(ctx.settings, kind), _TOOL_DEFAULT[kind](ctx)]
    for preferred in candidates:
        if preferred and preferred in usable:
            return preferred
    current = [m["id"] for m in models if m["available"] and m.get("status") == "current"]
    return (current or usable or [None])[0]
