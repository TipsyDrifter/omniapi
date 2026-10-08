"""Music generation tool — orchestrates music providers (ElevenLabs Music, Suno).

`generate` routes by model to either backend (ElevenLabs Music, synchronous; or
Suno via kie.ai, asynchronous). The remaining operations (extend / cover / add
vocals / separate stems / lyrics / wav / mp4) are Suno-only and route straight to
the SunoProvider. All downloadable outputs are saved under
``storage/music/<date>/``; lyrics/timestamp outputs are returned inline.

The MCP surface bundles those operations into five tools rather than thirteen,
so each per-operation method also has an ``action``-dispatching router near the
bottom of the class: ``edit`` (extend / cover / upload_extend /
add_instrumental / add_vocals / separate_vocals), ``lyrics``, ``utility`` and
``compose``. The routers are synchronous and return the coroutine to await —
see the comment above them for why.
"""

import logging
import uuid
from collections.abc import Coroutine, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiofiles

from ..capabilities.music import (
    LyriaProvider,
    ElevenLabsMusicProvider,
    MusicProvider,
    MusicResult,
    SunoProvider,
)
from ..config.settings import Settings
from ..providers.base import ProviderConfig

logger = logging.getLogger(__name__)

# Leading token of an output_format -> file extension.
_FORMAT_EXT = {
    "mp3": "mp3",
    "wav": "wav",
    "mp4": "mp4",
    "pcm": "pcm",
    "opus": "opus",
    "flac": "flac",
    "aac": "aac",
}

# ---- action -> required parameters, for the consolidated MCP tools ---------
#
# The five-tool MCP surface takes the union of every action's parameters and
# makes them all optional, so these tables are the only place that says what an
# action actually needs. Keep them in sync with the tool descriptions in
# server.py — an MCP client only reads the description to decide how to call.

_EDIT_REQUIRED: dict[str, tuple[str, ...]] = {
    "extend": ("audio_id",),
    "cover": ("upload_url", "prompt"),
    "upload_extend": ("upload_url",),
    "add_instrumental": ("upload_url", "title", "tags", "negative_tags"),
    "add_vocals": ("upload_url", "prompt", "title", "style", "negative_tags"),
    "separate_vocals": ("task_id", "audio_id"),
}
# Extra requirements once custom_mode=True (Suno calls this defaultParamFlag
# on the extend endpoints and customMode on cover).
_EDIT_CUSTOM_REQUIRED: dict[str, tuple[str, ...]] = {
    "extend": ("prompt", "style", "title", "continue_at"),
    "upload_extend": ("prompt", "style", "title", "continue_at"),
    "cover": ("style", "title"),
}
_LYRICS_REQUIRED: dict[str, tuple[str, ...]] = {
    "generate": ("prompt",),
    "timestamped": ("task_id", "audio_id"),
}
_UTILITY_REQUIRED: dict[str, tuple[str, ...]] = {
    "convert_to_wav": ("task_id", "audio_id"),
    "create_music_video": ("task_id", "audio_id"),
}
_COMPOSE_REQUIRED: dict[str, tuple[str, ...]] = {
    "create_plan": ("prompt",),
    "compose": (),  # validated separately: exactly one of prompt/composition_plan
}


def _kie_credit_usd(model: Optional[str]) -> Optional[float]:
    """USD per kie.ai credit, from the catalogue's ``pricing.credit_usd`` on a
    kie model (every kie model carries the same figure). ``None`` lets the
    provider fall back to its own default."""
    try:
        from ..catalog import catalog

        for model_id in (model, "V6", "V6_MINI", "V6_WILD"):
            entry = catalog.get(model_id, "kie") if model_id else None
            value = (entry.pricing or {}).get("credit_usd") if entry else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
    except Exception as e:  # noqa: BLE001 - the catalogue is optional here
        logger.debug("No kie credit price from the catalogue: %s", e)
    return None


def suno_all_tracks(settings: Any) -> bool:
    """settings.json `music.suno_all_tracks` (env `MUSIC__SUNO_ALL_TRACKS`):
    keep every song a Suno job returns (default) or only the first. Reads a
    test double without the field as the default."""
    music = getattr(settings, "music", None)
    value = getattr(music, "suno_all_tracks", None) if music is not None else None
    return True if value is None else bool(value)


def _check_action(tool: str, action: str, table: dict[str, tuple[str, ...]]) -> None:
    """Reject an unknown action, naming the ones this tool accepts."""
    if action not in table:
        raise ValueError(
            f"{tool}: unknown action {action!r}. "
            f"Valid actions: {', '.join(sorted(table))}."
        )


def _check_required(
    tool: str,
    action: str,
    values: dict[str, Any],
    names: Sequence[str],
    *,
    note: str = "",
) -> None:
    """Raise a client-readable error naming every required parameter left out."""
    missing = [
        name
        for name in names
        if values.get(name) is None
        or (isinstance(values.get(name), str) and not values[name].strip())
    ]
    if missing:
        raise ValueError(
            f"{tool}(action={action!r}) is missing required parameter(s): "
            f"{', '.join(missing)}.{note}"
        )


class MusicGenerationTool:
    """Generate/transform music via a configured provider (ElevenLabs / Suno)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._providers: list[MusicProvider] = []
        self._model_map: dict[str, MusicProvider] = {}
        self._init_providers()

    def _register(self, provider: MusicProvider) -> None:
        self._providers.append(provider)
        for model_id in provider.get_supported_models():
            self._model_map[model_id] = provider
        logger.info(
            "Music provider '%s' registered with %d models",
            provider.name,
            len(provider.get_supported_models()),
        )

    def _init_providers(self) -> None:
        # ElevenLabs Music first (synchronous; reuses the ElevenLabs TTS key).
        el = getattr(self.settings.providers, "elevenlabs", None)
        if el and el.enabled and el.api_key:
            try:
                self._register(
                    ElevenLabsMusicProvider(
                        ProviderConfig(
                            api_key=el.api_key,
                            base_url=el.base_url,
                            timeout=el.timeout,
                            max_retries=el.max_retries,
                            enabled=el.enabled,
                        )
                    )
                )
            except Exception as e:
                logger.error("Failed to init ElevenLabs Music provider: %s", e)

        # Suno via kie.ai (asynchronous; needs its own kie.ai key).
        kie = getattr(self.settings.providers, "kie", None)
        if kie and kie.enabled and kie.api_key:
            try:
                legacy_timeout = getattr(kie, "timeout", None)
                request_timeout = getattr(kie, "request_timeout", None) or legacy_timeout or 60.0
                poll_timeout = getattr(kie, "poll_timeout", None) or legacy_timeout or 900.0
                self._register(
                    SunoProvider(
                        ProviderConfig(
                            api_key=kie.api_key,
                            base_url=kie.base_url,
                            timeout=request_timeout,
                            max_retries=kie.max_retries,
                            enabled=kie.enabled,
                        ),
                        poll_timeout=poll_timeout,
                        routes=getattr(kie, "suno_routes", None),
                        credit_usd=_kie_credit_usd(getattr(kie, "default_model", None)),
                        all_tracks=suno_all_tracks(self.settings),
                    )
                )
            except Exception as e:
                logger.error("Failed to init Suno provider: %s", e)

        # Google Lyria (Gemini Developer API key; the paid tier only). Registered last so it
        # never displaces the default model.
        gem = getattr(self.settings.providers, "gemini", None)
        gem_key = (getattr(gem, "api_key", "") or "").strip() if gem else ""
        looks_like_file = gem_key.lower().endswith(".json") or "/" in gem_key or "\\" in gem_key  # a service-account path is not an API key
        if gem and getattr(gem, "enabled", False) and gem_key and not looks_like_file:
            try:
                self._register(
                    LyriaProvider(
                        ProviderConfig(
                            api_key=gem_key,
                            timeout=getattr(gem, "timeout", 300.0),
                            max_retries=getattr(gem, "max_retries", 3),
                            enabled=True,
                        )
                    )
                )
            except Exception as e:
                logger.error("Failed to init Lyria provider: %s", e)

    def _default_model(self) -> Optional[str]:
        if not self._providers:
            return None
        from ..config.user_settings import default_model

        preferred = default_model(getattr(self, "settings", None), "music")
        if preferred and preferred in self._model_map:
            return preferred
        first = self._providers[0]
        return getattr(first, "DEFAULT_MODEL", None) or next(
            iter(first.get_supported_models()), None
        )

    def available_models(self) -> list[str]:
        return sorted(self._model_map)

    def _suno(self) -> SunoProvider:
        """Return the Suno provider, or raise if kie.ai isn't configured."""
        for p in self._providers:
            if isinstance(p, SunoProvider):
                return p
        raise RuntimeError(
            "Suno (kie.ai) is not configured. Set PROVIDERS__KIE__API_KEY with "
            "PROVIDERS__KIE__ENABLED=true. (These operations are Suno-only.)"
        )

    # ---- storage helpers -------------------------------------------------

    @staticmethod
    def _ext_for(output_format: str) -> str:
        head = (output_format or "mp3").split("_")[0].lower()
        return _FORMAT_EXT.get(head, "mp3")

    async def _save(self, data: bytes, ext: str, prefix: str = "music", *, name: Optional[str] = None) -> Path:
        """Write one output file. ``name`` (without extension) replaces the
        generated ``<prefix>_<UTC stamp>_<random>`` stem — used to file a
        job's second song next to its first as ``<first stem>_2``."""
        now = datetime.now(timezone.utc)
        out_dir = Path(self.settings.storage.base_path) / "music" / now.strftime(
            "%Y-%m-%d"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = name or f"{prefix}_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        out_path = (out_dir / f"{stem}.{ext}").resolve()
        # Async write — music/video payloads reach tens of MB; a synchronous
        # write_bytes would stall the event loop and every in-flight job.
        async with aiofiles.open(out_path, "wb") as f:
            await f.write(data)
        return out_path

    async def _audio_result(self, r: MusicResult) -> dict[str, Any]:
        """Save an audio/wav/mp4 result and return its path + metadata.

        The top-level fields describe the first (or only) song. A job that
        returned more than one song (Suno gives two) also gets ``tracks``: every
        song in order, the first one repeated, each with its own file."""
        ext = self._ext_for(r.output_format)
        path = await self._save(r.audio_data or b"", ext, prefix="music")
        out = self._first_track_fields(r, path)
        if r.extra_tracks:
            tracks = [{k: out[k] for k in ("audio_path", "audio_id", "title", "duration", "bytes")}]
            for n, extra in enumerate(r.extra_tracks, start=2):
                entry: dict[str, Any] = {k: extra.get(k) for k in ("audio_id", "title", "duration")}
                if extra.get("audio_data"):
                    p = await self._save(extra["audio_data"], ext, name=f"{path.stem}_{n}")
                    entry = {"audio_path": str(p), **entry, "bytes": len(extra["audio_data"])}
                else:
                    entry["error"] = extra.get("error") or "not downloaded"
                tracks.append(entry)
            out["tracks"] = tracks
            out["track_count"] = len(tracks)
        return out

    def _first_track_fields(self, r: MusicResult, path: Path) -> dict[str, Any]:
        return {
            "audio_path": str(path),
            "audio_url": f"file://{path}",
            "provider": r.metadata.get("provider"),
            "operation": r.metadata.get("operation"),
            "task_id": r.metadata.get("task_id"),
            "audio_id": r.metadata.get("audio_id"),
            "title": r.metadata.get("title"),
            "duration": r.metadata.get("duration"),
            "output_format": r.output_format,
            "bytes": len(r.audio_data or b""),
            # only set when the cost is known: Lyria's flat price, or kie's
            # creditsConsumed x credit_usd on the jobs route; otherwise absent
            **{k: r.metadata[k] for k in ("model", "cost_usd") if r.metadata.get(k) is not None},
            **({"lyrics": r.text} if r.text else {}),
        }

    # ---- generate (both backends, routed by model) ----------------------

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        music_length_ms: Optional[int] = None,
        style: Optional[str] = None,
        title: Optional[str] = None,
        custom_mode: bool = False,
        vocal_gender: Optional[str] = None,
        negative_tags: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate music from a text description; save it and return the path."""
        if not self._providers:
            raise RuntimeError(
                "No music provider is configured. Set PROVIDERS__ELEVENLABS__API_KEY "
                "(ElevenLabs Music) or PROVIDERS__KIE__API_KEY (Suno via kie.ai) — "
                "with the matching __ENABLED=true."
            )

        target_model = model or self._default_model()
        provider = self._model_map.get(target_model)
        if not provider:
            raise RuntimeError(
                f"No music provider for model '{target_model}'. "
                f"Available models: {self.available_models()}"
            )

        extra: dict[str, Any] = dict(kwargs)
        if music_length_ms is not None:
            extra["music_length_ms"] = music_length_ms
        if style is not None:
            extra["style"] = style
        if title is not None:
            extra["title"] = title
        if custom_mode:
            extra["customMode"] = True
        if vocal_gender is not None:
            extra["vocalGender"] = vocal_gender
        if negative_tags is not None:
            extra["negativeTags"] = negative_tags

        result = await provider.generate(
            prompt,
            model=target_model,
            instrumental=instrumental,
            output_format=output_format,
            **extra,
        )
        return await self._audio_result(result)

    # ---- Suno-only operations -------------------------------------------

    async def extend(
        self,
        *,
        audio_id: str,
        model: Optional[str] = None,
        default_param_flag: bool = False,
        prompt: Optional[str] = None,
        style: Optional[str] = None,
        title: Optional[str] = None,
        continue_at: Optional[float] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().extend(
            audio_id=audio_id,
            model=model,
            default_param_flag=default_param_flag,
            prompt=prompt,
            style=style,
            title=title,
            continue_at=continue_at,
            **kwargs,
        )
        return await self._audio_result(r)

    async def cover(
        self,
        *,
        upload_url: str,
        prompt: str,
        model: Optional[str] = None,
        custom_mode: bool = False,
        instrumental: bool = False,
        style: Optional[str] = None,
        title: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().cover(
            upload_url=upload_url,
            prompt=prompt,
            model=model,
            custom_mode=custom_mode,
            instrumental=instrumental,
            style=style,
            title=title,
            **kwargs,
        )
        return await self._audio_result(r)

    async def upload_extend(
        self,
        *,
        upload_url: str,
        model: Optional[str] = None,
        default_param_flag: bool = False,
        instrumental: bool = False,
        prompt: Optional[str] = None,
        style: Optional[str] = None,
        title: Optional[str] = None,
        continue_at: Optional[float] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().upload_extend(
            upload_url=upload_url,
            model=model,
            default_param_flag=default_param_flag,
            instrumental=instrumental,
            prompt=prompt,
            style=style,
            title=title,
            continue_at=continue_at,
            **kwargs,
        )
        return await self._audio_result(r)

    async def add_instrumental(
        self,
        *,
        upload_url: str,
        title: str,
        tags: str,
        negative_tags: str,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().add_instrumental(
            upload_url=upload_url,
            title=title,
            tags=tags,
            negative_tags=negative_tags,
            model=model,
            **kwargs,
        )
        return await self._audio_result(r)

    async def add_vocals(
        self,
        *,
        upload_url: str,
        prompt: str,
        title: str,
        style: str,
        negative_tags: str,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().add_vocals(
            upload_url=upload_url,
            prompt=prompt,
            title=title,
            style=style,
            negative_tags=negative_tags,
            model=model,
            **kwargs,
        )
        return await self._audio_result(r)

    async def separate_vocals(
        self,
        *,
        task_id: str,
        audio_id: str,
        separation_type: str = "separate_vocal",
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().separate_vocals(
            task_id=task_id,
            audio_id=audio_id,
            separation_type=separation_type,
            **kwargs,
        )
        saved: dict[str, str] = {}
        for name, data in (r.files or {}).items():
            saved[name] = str(
                await self._save(data, "mp3", prefix=f"stem_{name}")
            )
        return {
            "stems": saved,
            "count": len(saved),
            "provider": r.metadata.get("provider"),
            "operation": r.metadata.get("operation"),
            "separation_type": r.metadata.get("separation_type"),
            "task_id": r.metadata.get("task_id"),
            "source_task_id": r.metadata.get("source_task_id"),
        }

    async def generate_lyrics(self, *, prompt: str, **kwargs: Any) -> dict[str, Any]:
        r = await self._suno().generate_lyrics(prompt=prompt, **kwargs)
        lyrics_path = None
        if r.text:
            lyrics_path = str(
                await self._save(r.text.encode("utf-8"), "txt", prefix="lyrics")
            )
        return {
            "text": r.text,
            "title": r.metadata.get("title"),
            "lyrics_path": lyrics_path,
            "variants": r.metadata.get("variants"),
            "provider": r.metadata.get("provider"),
            "operation": r.metadata.get("operation"),
            "task_id": r.metadata.get("task_id"),
            # only when kie's task detail reported creditsConsumed (jobs route)
            **({"cost_usd": r.metadata["cost_usd"]} if r.metadata.get("cost_usd") is not None else {}),
        }

    async def get_timestamped_lyrics(
        self, *, task_id: str, audio_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        r = await self._suno().get_timestamped_lyrics(
            task_id=task_id, audio_id=audio_id, **kwargs
        )
        data = r.data or {}
        return {
            "aligned_words": data.get("alignedWords"),
            "waveform_data": data.get("waveformData"),
            "provider": r.metadata.get("provider"),
            "operation": r.metadata.get("operation"),
            "source_task_id": r.metadata.get("source_task_id"),
        }

    async def to_wav(
        self, *, task_id: str, audio_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        r = await self._suno().to_wav(task_id=task_id, audio_id=audio_id, **kwargs)
        return await self._audio_result(r)

    async def to_mp4(
        self,
        *,
        task_id: str,
        audio_id: str,
        author: Optional[str] = None,
        domain_name: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        r = await self._suno().to_mp4(
            task_id=task_id,
            audio_id=audio_id,
            author=author,
            domain_name=domain_name,
            **kwargs,
        )
        return await self._audio_result(r)

    # ---- ElevenLabs composition-plan workflow ---------------------------

    def _elevenlabs(self) -> ElevenLabsMusicProvider:
        for p in self._providers:
            if isinstance(p, ElevenLabsMusicProvider):
                return p
        raise RuntimeError(
            "ElevenLabs Music is not configured. Set PROVIDERS__ELEVENLABS__API_KEY "
            "with PROVIDERS__ELEVENLABS__ENABLED=true. (Composition plans are "
            "ElevenLabs-only.)"
        )

    async def create_composition_plan(
        self,
        *,
        prompt: str,
        model: Optional[str] = None,
        music_length_ms: Optional[int] = None,
        source_composition_plan: Any = None,
    ) -> dict[str, Any]:
        r = await self._elevenlabs().create_plan(
            prompt=prompt,
            model=model,
            music_length_ms=music_length_ms,
            source_composition_plan=source_composition_plan,
        )
        return {
            "composition_plan": r.data,
            "provider": r.metadata.get("provider"),
            "operation": r.metadata.get("operation"),
            "model": r.metadata.get("model"),
        }

    async def compose_detailed(
        self,
        *,
        prompt: Optional[str] = None,
        composition_plan: Any = None,
        model: Optional[str] = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        music_length_ms: Optional[int] = None,
        with_timestamps: bool = False,
    ) -> dict[str, Any]:
        r = await self._elevenlabs().compose(
            prompt=prompt,
            composition_plan=composition_plan,
            model=model,
            instrumental=instrumental,
            output_format=output_format,
            music_length_ms=music_length_ms,
            detailed=True,
            with_timestamps=with_timestamps,
        )
        d = await self._audio_result(r)
        d["composition_plan"] = r.metadata.get("composition_plan")
        d["song_metadata"] = r.metadata.get("song_metadata")
        return d

    # ---- consolidated action routers -------------------------------------
    #
    # The MCP surface exposes five music tools instead of thirteen. Each of the
    # four multi-action tools takes an ``action`` plus the union of the
    # parameters its actions need, and routes here.
    #
    # Routing and required-parameter checks live in this layer (rather than in
    # server.py) for two reasons: they are unit-testable without an MCP
    # context, and they are SYNCHRONOUS. server.py hands the returned coroutine
    # to JobManager.run(), so a malformed call has to fail *before* a
    # background job exists — otherwise a plain typo would come back as a job
    # ticket and only surface as an error on a later fetch.

    def edit(
        self,
        *,
        action: str,
        audio_id: Optional[str] = None,
        task_id: Optional[str] = None,
        upload_url: Optional[str] = None,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        custom_mode: bool = False,
        instrumental: bool = False,
        style: Optional[str] = None,
        title: Optional[str] = None,
        tags: Optional[str] = None,
        negative_tags: Optional[str] = None,
        continue_at: Optional[float] = None,
        separation_type: str = "separate_vocal",
        **kwargs: Any,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        """Route an ``edit_music`` action to its Suno operation.

        Returns the coroutine to await. Raises ValueError (synchronously) for an
        unknown action or a missing required parameter.
        """
        values = locals()
        _check_action("edit_music", action, _EDIT_REQUIRED)
        _check_required("edit_music", action, values, _EDIT_REQUIRED[action])
        if custom_mode and action in _EDIT_CUSTOM_REQUIRED:
            _check_required(
                "edit_music",
                action,
                values,
                _EDIT_CUSTOM_REQUIRED[action],
                note=(
                    " custom_mode=True needs "
                    f"{', '.join(_EDIT_CUSTOM_REQUIRED[action])}; "
                    "pass custom_mode=False to inherit the source track's settings."
                ),
            )

        if action == "extend":
            return self.extend(
                audio_id=audio_id,
                model=model,
                default_param_flag=custom_mode,
                prompt=prompt,
                style=style,
                title=title,
                continue_at=continue_at,
                **kwargs,
            )
        if action == "cover":
            return self.cover(
                upload_url=upload_url,
                prompt=prompt,
                model=model,
                custom_mode=custom_mode,
                instrumental=instrumental,
                style=style,
                title=title,
                **kwargs,
            )
        if action == "upload_extend":
            return self.upload_extend(
                upload_url=upload_url,
                model=model,
                default_param_flag=custom_mode,
                instrumental=instrumental,
                prompt=prompt,
                style=style,
                title=title,
                continue_at=continue_at,
                **kwargs,
            )
        if action == "add_instrumental":
            return self.add_instrumental(
                upload_url=upload_url,
                title=title,
                tags=tags,
                negative_tags=negative_tags,
                model=model,
                **kwargs,
            )
        if action == "add_vocals":
            return self.add_vocals(
                upload_url=upload_url,
                prompt=prompt,
                title=title,
                style=style,
                negative_tags=negative_tags,
                model=model,
                **kwargs,
            )
        # separate_vocals
        if separation_type not in ("separate_vocal", "split_stem"):
            raise ValueError(
                "edit_music(action='separate_vocals') needs separation_type to be "
                "'separate_vocal' (vocals + accompaniment) or 'split_stem' "
                f"(per-instrument stems); got {separation_type!r}."
            )
        return self.separate_vocals(
            task_id=task_id,
            audio_id=audio_id,
            separation_type=separation_type,
            **kwargs,
        )

    def lyrics(
        self,
        *,
        action: str,
        prompt: Optional[str] = None,
        task_id: Optional[str] = None,
        audio_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        """Route a ``music_lyrics`` action (generate / timestamped)."""
        values = locals()
        _check_action("music_lyrics", action, _LYRICS_REQUIRED)
        _check_required("music_lyrics", action, values, _LYRICS_REQUIRED[action])

        if action == "generate":
            return self.generate_lyrics(prompt=prompt, **kwargs)
        return self.get_timestamped_lyrics(
            task_id=task_id, audio_id=audio_id, **kwargs
        )

    def utility(
        self,
        *,
        action: str,
        task_id: Optional[str] = None,
        audio_id: Optional[str] = None,
        author: Optional[str] = None,
        domain_name: Optional[str] = None,
        **kwargs: Any,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        """Route a ``music_utility`` action (convert_to_wav / create_music_video)."""
        values = locals()
        _check_action("music_utility", action, _UTILITY_REQUIRED)
        _check_required("music_utility", action, values, _UTILITY_REQUIRED[action])

        if action == "convert_to_wav":
            return self.to_wav(task_id=task_id, audio_id=audio_id, **kwargs)
        return self.to_mp4(
            task_id=task_id,
            audio_id=audio_id,
            author=author,
            domain_name=domain_name,
            **kwargs,
        )

    def compose(
        self,
        *,
        action: str,
        prompt: Optional[str] = None,
        composition_plan: Any = None,
        source_composition_plan: Any = None,
        model: Optional[str] = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        music_length_ms: Optional[int] = None,
        with_timestamps: bool = False,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        """Route a ``compose_music`` action (create_plan / compose)."""
        values = locals()
        _check_action("compose_music", action, _COMPOSE_REQUIRED)
        _check_required("compose_music", action, values, _COMPOSE_REQUIRED[action])

        if action == "create_plan":
            return self.create_composition_plan(
                prompt=prompt,
                model=model,
                music_length_ms=music_length_ms,
                source_composition_plan=source_composition_plan,
            )
        # compose: exactly one of prompt / composition_plan
        if bool(prompt) == bool(composition_plan):
            raise ValueError(
                "compose_music(action='compose') needs exactly one of 'prompt' or "
                "'composition_plan' — pass a prompt for a one-shot compose, or a "
                "plan from action='create_plan' for section-level control."
            )
        return self.compose_detailed(
            prompt=prompt,
            composition_plan=composition_plan,
            model=model,
            instrumental=instrumental,
            output_format=output_format,
            music_length_ms=music_length_ms,
            with_timestamps=with_timestamps,
        )

    async def close(self) -> None:
        for provider in self._providers:
            if hasattr(provider, "close"):
                await provider.close()
