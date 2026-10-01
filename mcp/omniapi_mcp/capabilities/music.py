"""Music generation capability — providers for ElevenLabs Music and Suno (kie.ai).

Modality: text/audio -> music. Two very different provider styles:

- **ElevenLabs Music** is SYNCHRONOUS: ``POST /v1/music`` returns audio bytes
  directly (same shape as the TTS provider, reuses the ElevenLabs key).
- **Suno via kie.ai** is ASYNCHRONOUS: a job endpoint returns a ``taskId``; we
  poll a record-info endpoint until the status field reads ``SUCCESS``, then
  download the result URL. Wrapped so callers just await once.

Suno exposes a whole family of operations (generate / extend / cover / add
vocals / separate stems / lyrics / wav / mp4). They differ in three axes the
engine below parametrises:
  1. **result endpoint** — the audio family shares ``/generate/record-info``;
     lyrics, wav, mp4 and vocal-removal each have a dedicated one.
  2. **status field** — ``data.status`` (audio family + lyrics) vs
     ``data.successFlag`` (wav / mp4 / vocal-removal).
  3. **output** — a downloadable file (audio/wav/mp4), multiple stem files,
     lyrics text, or timestamped-word JSON.

Every Suno endpoint requires ``callBackUrl`` even though we only poll — a
placeholder satisfies the check (verified live: it 422s without one).

Reuses ``ProviderConfig`` / ``ProviderError`` from ``providers.base``; uses
``httpx`` (already a dependency).
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..providers.base import ProviderConfig, ProviderError

logger = logging.getLogger(__name__)


@dataclass
class MusicResult:
    """Standardized music-generation response.

    Only the field(s) relevant to the operation are populated:
    - ``audio_data`` — a single downloadable file (audio / wav / mp4 bytes)
    - ``files`` — named extra files, e.g. separated stems {name: bytes}
    - ``text`` — lyrics text
    - ``data`` — structured JSON (e.g. timestamped words)
    """

    output_format: str = "mp3"
    metadata: dict[str, Any] = field(default_factory=dict)
    audio_data: bytes | None = None
    files: dict[str, bytes] | None = None
    text: str | None = None
    data: Any = None


class MusicProvider(ABC):
    """Abstract base for music-generation providers (text -> music)."""

    #: model id -> "current" | "deprecated". Read by the catalogue layer so it
    #: can show which ids the upstream vendor still advertises. Deprecated ids
    #: stay callable (the upstream API may still honour them for a while) but
    #: every call logs a warning.
    MODEL_STATUS: dict[str, str] = {}

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.name = self.__class__.__name__.replace("Provider", "").lower()
        self._logger = logging.getLogger(f"{__name__}.{self.name}")
        self._client: httpx.AsyncClient | None = None

    @abstractmethod
    def get_supported_models(self) -> set[str]:
        ...

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        **kwargs: Any,
    ) -> MusicResult:
        ...

    def is_available(self) -> bool:
        return self.config.enabled and bool(self.config.api_key)

    def _validate_model(
        self, model_id: str, allowed: set[str], *, operation: str = ""
    ) -> str:
        """Reject unknown model ids; warn (but allow) deprecated ones.

        ``allowed`` is the set accepted by this particular operation — some
        endpoints take a narrower set than the provider as a whole.
        """
        if model_id not in allowed:
            where = f" for {operation}" if operation else ""
            raise ProviderError(
                f"Model '{model_id}' is not supported by {self.name}{where} "
                f"(use one of {sorted(allowed)})",
                provider_name=self.name,
                error_code="UNSUPPORTED_MODEL",
            )
        if self.MODEL_STATUS.get(model_id) == "deprecated":
            self._logger.warning(
                "Music model '%s' is discontinued upstream; it may stop working "
                "without notice. Prefer a current model (%s).",
                model_id,
                ", ".join(
                    sorted(
                        m
                        for m, s in self.MODEL_STATUS.items()
                        if s == "current" and m in allowed
                    )
                )
                or "none listed",
            )
        return model_id

    def _http(self) -> httpx.AsyncClient:
        """Long-lived shared HTTP client (created lazily).

        Reusing one client keeps the TLS connection pool warm across tool
        calls instead of paying a fresh handshake per request. Closed via
        close() on server shutdown.
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self._client

    async def close(self) -> None:
        """Release the shared HTTP client (called on server shutdown)."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


class ElevenLabsMusicProvider(MusicProvider):
    """ElevenLabs Music via the REST API — synchronous.

    Endpoint: ``POST {base}/v1/music``. Auth header ``xi-api-key``; the request
    body carries the prompt / model_id, ``output_format`` is a query param.
    Returns raw audio bytes. Shares the ElevenLabs account/key with the TTS
    provider. ``music_length_ms`` and ``force_instrumental`` apply only with a
    text prompt.
    """

    # Allowed values per the ElevenLabs compose reference (checked 2026-09-25):
    # "Defaults to music_v1 ... Allowed values: music_v1 music_v2 music_v2_5".
    # All three are current; music_v2_5 is the highest-quality one.
    SUPPORTED_MODELS = {"music_v1", "music_v2", "music_v2_5"}
    MODEL_STATUS: dict[str, str] = {
        "music_v1": "current",
        "music_v2": "current",
        "music_v2_5": "current",
    }
    DEFAULT_MODEL = "music_v1"
    DEFAULT_BASE = "https://api.elevenlabs.io"
    DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.base_url = (config.base_url or self.DEFAULT_BASE).rstrip("/")

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        **kwargs: Any,
    ) -> MusicResult:
        """Prompt-based compose via /v1/music (delegates to compose())."""
        return await self.compose(
            prompt=prompt,
            model=model,
            instrumental=instrumental,
            output_format=output_format,
            **kwargs,
        )

    async def compose(
        self,
        *,
        prompt: str | None = None,
        composition_plan: Any = None,
        model: str | None = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        music_length_ms: int | None = None,
        seed: int | None = None,
        detailed: bool = False,
        with_timestamps: bool = False,
        **kwargs: Any,
    ) -> MusicResult:
        """Compose via /v1/music, or /v1/music/detailed when detailed=True.

        Provide exactly one of ``prompt`` or ``composition_plan``. With
        ``detailed=True`` the API returns a multipart response bundling the
        audio with the plan used and song metadata, parsed into metadata here.
        """
        model_id = self._validate_model(
            model or self.DEFAULT_MODEL, self.SUPPORTED_MODELS, operation="compose"
        )
        if bool(prompt) == bool(composition_plan):
            raise ProviderError(
                "Provide exactly one of 'prompt' or 'composition_plan'.",
                provider_name=self.name,
                error_code="MISSING_PARAM",
            )

        endpoint = "/v1/music/detailed" if detailed else "/v1/music"
        headers = {
            "xi-api-key": self.config.api_key,
            "content-type": "application/json",
        }
        body: dict[str, Any] = {"model_id": model_id}
        if prompt:
            body["prompt"] = prompt
            if music_length_ms is not None:
                body["music_length_ms"] = music_length_ms
            if instrumental:
                body["force_instrumental"] = True
        else:
            body["composition_plan"] = composition_plan
            if seed is not None:
                body["seed"] = seed
        if detailed and with_timestamps:
            body["with_timestamps"] = True
        for key in (
            "respect_sections_durations",
            "store_for_inpainting",
            "sign_with_c2pa",
        ):
            if kwargs.get(key) is not None:
                body[key] = kwargs[key]

        fmt = output_format or self.DEFAULT_OUTPUT_FORMAT
        if fmt == "mp3":
            fmt = self.DEFAULT_OUTPUT_FORMAT
        params = {"output_format": fmt}

        try:
            self._logger.info(
                "ElevenLabs compose via %s (model %s, detailed=%s)",
                endpoint,
                model_id,
                detailed,
            )
            resp = await self._http().post(
                f"{self.base_url}{endpoint}",
                headers=headers,
                json=body,
                params=params,
            )
            if resp.status_code != 200:
                detail = resp.text[:300]
                raise ProviderError(
                    f"ElevenLabs Music returned HTTP {resp.status_code}: {detail}",
                    provider_name=self.name,
                    error_code="GENERATION_FAILED",
                )
            meta: dict[str, Any] = {"provider": self.name, "model": model_id}
            ctype = resp.headers.get("content-type", "")
            if detailed and "multipart" in ctype:
                audio, extra = self._parse_multipart(resp.content, ctype)
                meta.update(extra)
            else:
                audio = resp.content

            return MusicResult(
                audio_data=audio, output_format=fmt, metadata=meta
            )
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in ElevenLabs Music: %s", e)
            raise ProviderError(
                f"ElevenLabs Music failed: {str(e)}",
                provider_name=self.name,
                error_code="GENERATION_FAILED",
            )

    async def create_plan(
        self,
        *,
        prompt: str,
        model: str | None = None,
        music_length_ms: int | None = None,
        source_composition_plan: Any = None,
    ) -> MusicResult:
        """Create a composition plan from a prompt via /v1/music/plan (JSON)."""
        model_id = self._validate_model(
            model or self.DEFAULT_MODEL, self.SUPPORTED_MODELS, operation="create_plan"
        )
        headers = {
            "xi-api-key": self.config.api_key,
            "content-type": "application/json",
        }
        body: dict[str, Any] = {"prompt": prompt, "model_id": model_id}
        if music_length_ms is not None:
            body["music_length_ms"] = music_length_ms
        if source_composition_plan is not None:
            body["source_composition_plan"] = source_composition_plan
        try:
            resp = await self._http().post(
                f"{self.base_url}/v1/music/plan", headers=headers, json=body
            )
            if resp.status_code != 200:
                raise ProviderError(
                    f"ElevenLabs plan returned HTTP {resp.status_code}: "
                    f"{resp.text[:300]}",
                    provider_name=self.name,
                    error_code="GENERATION_FAILED",
                )
            plan = resp.json()
            return MusicResult(
                output_format="json",
                data=plan,
                metadata={
                    "provider": self.name,
                    "model": model_id,
                    "operation": "create_plan",
                },
            )
        except ProviderError:
            raise
        except Exception as e:
            self._logger.error("Error in ElevenLabs plan: %s", e)
            raise ProviderError(
                f"ElevenLabs plan failed: {str(e)}",
                provider_name=self.name,
                error_code="GENERATION_FAILED",
            )

    @staticmethod
    def _parse_multipart(content: bytes, content_type: str) -> tuple[bytes, dict[str, Any]]:
        """Parse a multipart/mixed response into (audio_bytes, metadata_dict).

        Uses the stdlib email parser. NOTE: not yet exercised against the live
        API (needs a paid ElevenLabs plan) — verify the part layout on first use.
        """
        import json
        from email.parser import BytesParser
        from email.policy import default

        parsed = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content
        )
        audio = b""
        meta: dict[str, Any] = {}
        for part in parsed.iter_parts():
            ptype = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            if ptype == "application/json":
                try:
                    meta = json.loads(payload)
                except Exception:
                    pass
            elif ptype.startswith("audio/") or ptype == "application/octet-stream":
                audio = payload
        return audio, meta


class SunoProvider(MusicProvider):
    """Suno via the kie.ai aggregator — asynchronous, wrapped to look synchronous.

    Every job endpoint returns ``data.taskId``; we poll the matching record-info
    endpoint until the status field is ``SUCCESS``, then download the output.
    ``callBackUrl`` is required by kie.ai even for polling — a placeholder
    satisfies it (we never receive the callback; results come from polling).

    Model ids follow kie.ai's spelling. As of 2026-09-25 the kie.ai docs list
    V6 / V6_MINI / V6_WILD as current and mark V4, V4_5, V4_5PLUS, V4_5ALL, V5
    and V5_5 "Discontinued". The discontinued ids stay callable here (old task
    ids and saved presets still reference them) but every use logs a warning.
    """

    CURRENT_MODELS = {"V6", "V6_MINI", "V6_WILD"}
    DEPRECATED_MODELS = {"V4", "V4_5", "V4_5PLUS", "V4_5ALL", "V5", "V5_5"}
    SUPPORTED_MODELS = {
        "V6",
        "V6_MINI",
        "V6_WILD",
        "V4",
        "V4_5",
        "V4_5PLUS",
        "V4_5ALL",
        "V5",
        "V5_5",
    }
    MODEL_STATUS: dict[str, str] = {
        "V6": "current",
        "V6_MINI": "current",
        "V6_WILD": "current",
        "V4": "deprecated",
        "V4_5": "deprecated",
        "V4_5PLUS": "deprecated",
        "V4_5ALL": "deprecated",
        "V5": "deprecated",
        "V5_5": "deprecated",
    }
    # add-instrumental / add-vocals accept a narrower set. kie.ai does not
    # publish a per-model matrix for the stem-add endpoints; the nearest stated
    # fact is that personas are "Only available for V5 (Discontinued), V5.5
    # (Discontinued), V6, V6_MINI, and V6_WILD". We therefore allow the current
    # V6 family, and keep the previously-documented V4_5PLUS/V5/V5_5 as a
    # deprecated tail so existing callers do not break.
    STEM_ADD_MODELS = {"V6", "V6_MINI", "V6_WILD"}
    STEM_ADD_DEPRECATED = {"V4_5PLUS", "V5", "V5_5"}
    DEFAULT_MODEL = "V6"
    DEFAULT_ADD_MODEL = "V6"
    DEFAULT_BASE = "https://api.kie.ai"

    # kie.ai rejects requests without callBackUrl even though we only poll.
    _CALLBACK_PLACEHOLDER = "https://example.com/omniapi-mcp/suno-callback"
    # Non-terminal statuses seen on both data.status and data.successFlag.
    _PENDING = {"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"}
    # All the stem URL fields vocal-removal can return (camelCase, record-info).
    _STEM_FIELDS = (
        "vocalUrl",
        "instrumentalUrl",
        "backingVocalsUrl",
        "drumsUrl",
        "bassUrl",
        "guitarUrl",
        "pianoUrl",
        "keyboardUrl",
        "percussionUrl",
        "stringsUrl",
        "synthUrl",
        "fxUrl",
        "brassUrl",
        "woodwindsUrl",
    )

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.base_url = (config.base_url or self.DEFAULT_BASE).rstrip("/")
        self.poll_interval = 5.0  # seconds; ceiling is config.timeout

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    # ---- shared engine ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _cb(self, kwargs: dict[str, Any]) -> str:
        return kwargs.get("callBackUrl") or self._CALLBACK_PLACEHOLDER

    def _parse_json(self, resp: httpx.Response, label: str) -> dict[str, Any]:
        try:
            return resp.json() or {}
        except Exception:
            raise ProviderError(
                f"Suno {label} returned non-JSON (HTTP {resp.status_code}): "
                f"{resp.text[:200]}",
                provider_name=self.name,
                error_code="BAD_RESPONSE",
            )

    async def _submit(
        self, client: httpx.AsyncClient, endpoint: str, body: dict[str, Any]
    ) -> str:
        """POST a job endpoint and return its taskId."""
        try:
            resp = await client.post(
                f"{self.base_url}{endpoint}", headers=self._headers(), json=body
            )
        except Exception as e:
            raise ProviderError(
                f"Suno request to {endpoint} failed: {e}",
                provider_name=self.name,
                error_code="REQUEST_FAILED",
            )
        data = self._parse_json(resp, endpoint)
        task_id = (data.get("data") or {}).get("taskId")
        if resp.status_code != 200 or not task_id:
            raise ProviderError(
                f"Suno {endpoint} failed (HTTP {resp.status_code}): "
                f"{resp.text[:300]}",
                provider_name=self.name,
                error_code="GENERATION_FAILED",
            )
        return task_id

    async def _poll(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        task_id: str,
        status_field: str = "status",
    ) -> dict[str, Any]:
        """Poll a record-info endpoint until SUCCESS; return the ``data`` dict.

        ``status_field`` is ``status`` for the audio family + lyrics, or
        ``successFlag`` for wav / mp4 / vocal-removal. Raises on error/timeout.
        """
        url = f"{self.base_url}{endpoint}"
        deadline = self.config.timeout
        waited = 0.0
        while waited < deadline:
            try:
                resp = await client.get(
                    url, headers=self._headers(), params={"taskId": task_id}
                )
            except Exception as e:
                raise ProviderError(
                    f"Suno poll {endpoint} failed: {e}",
                    provider_name=self.name,
                    error_code="REQUEST_FAILED",
                )
            data = self._parse_json(resp, endpoint).get("data") or {}
            status = data.get(status_field)
            if status == "SUCCESS":
                return data
            if status and status not in self._PENDING:
                msg = data.get("errorMessage") or status
                raise ProviderError(
                    f"Suno task failed: {msg}",
                    provider_name=self.name,
                    error_code=str(data.get("errorCode") or "GENERATION_FAILED"),
                )
            await asyncio.sleep(self.poll_interval)
            waited += self.poll_interval

        raise ProviderError(
            f"Suno task timed out after {int(deadline)}s (task {task_id}).",
            provider_name=self.name,
            error_code="TIMEOUT",
        )

    async def _download(self, client: httpx.AsyncClient, url: str) -> bytes:
        try:
            dl = await client.get(url)
        except Exception as e:
            raise ProviderError(
                f"Failed to download Suno output: {e}",
                provider_name=self.name,
                error_code="DOWNLOAD_FAILED",
            )
        if dl.status_code != 200:
            raise ProviderError(
                f"Failed to download Suno output (HTTP {dl.status_code}).",
                provider_name=self.name,
                error_code="DOWNLOAD_FAILED",
            )
        return dl.content

    async def _run_audio(
        self, endpoint: str, body: dict[str, Any], op: str
    ) -> MusicResult:
        """Submit an audio-family job, poll /generate/record-info, download track."""
        self._logger.info("Suno %s -> %s", op, endpoint)
        client = self._http()
        task_id = await self._submit(client, endpoint, body)
        data = await self._poll(
            client, "/api/v1/generate/record-info", task_id, "status"
        )
        suno_data = ((data.get("response") or {}).get("sunoData")) or []
        if not suno_data:
            raise ProviderError(
                "Suno reported SUCCESS but returned no sunoData.",
                provider_name=self.name,
                error_code="NO_AUDIO",
            )
        track = suno_data[0]
        audio_url = track.get("audioUrl") or track.get("audio_url")
        if not audio_url:
            raise ProviderError(
                "Suno finished but returned no audio URL.",
                provider_name=self.name,
                error_code="NO_AUDIO",
            )
        audio = await self._download(client, audio_url)

        return MusicResult(
            audio_data=audio,
            output_format="mp3",
            metadata={
                "provider": self.name,
                "operation": op,
                "task_id": task_id,
                "audio_id": track.get("id"),
                "audio_ids": [t.get("id") for t in suno_data],
                "title": track.get("title"),
                "duration": track.get("duration"),
                "tags": track.get("tags"),
                "all_tracks": [
                    t.get("audioUrl") or t.get("audio_url") for t in suno_data
                ],
            },
        )

    # ---- text -> music (generate) ---------------------------------------

    def _apply_common(self, body: dict[str, Any], kwargs: dict[str, Any]) -> None:
        """Attach the optional style/weight/persona params shared by most jobs."""
        vocal_gender = kwargs.get("vocalGender") or kwargs.get("vocal_gender")
        if vocal_gender:
            body["vocalGender"] = vocal_gender
        if kwargs.get("negativeTags"):
            body["negativeTags"] = kwargs["negativeTags"]
        for key in ("styleWeight", "weirdnessConstraint", "audioWeight"):
            if kwargs.get(key) is not None:
                body[key] = kwargs[key]
        for key in ("personaId", "personaModel"):
            if kwargs.get(key):
                body[key] = kwargs[key]

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(
            model or self.DEFAULT_MODEL, self.SUPPORTED_MODELS, operation="generate"
        )

        custom_mode = bool(kwargs.get("customMode", kwargs.get("custom_mode", False)))
        style = kwargs.get("style")
        title = kwargs.get("title")
        if custom_mode and (not style or not title):
            raise ProviderError(
                "Suno custom mode requires both 'style' and 'title'.",
                provider_name=self.name,
                error_code="MISSING_PARAM",
            )

        body: dict[str, Any] = {
            "prompt": prompt,
            "model": model_id,
            "customMode": custom_mode,
            "instrumental": bool(instrumental),
            "callBackUrl": self._cb(kwargs),
        }
        if style:
            body["style"] = style
        if title:
            body["title"] = title
        self._apply_common(body, kwargs)
        return await self._run_audio("/api/v1/generate", body, "generate")

    # ---- extend / cover / upload-extend ---------------------------------

    async def extend(
        self,
        *,
        audio_id: str,
        model: str | None = None,
        default_param_flag: bool = False,
        prompt: str | None = None,
        style: str | None = None,
        title: str | None = None,
        continue_at: float | None = None,
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(
            model or self.DEFAULT_MODEL, self.SUPPORTED_MODELS, operation="extend"
        )
        body: dict[str, Any] = {
            "audioId": audio_id,
            "model": model_id,
            "defaultParamFlag": bool(default_param_flag),
            "callBackUrl": self._cb(kwargs),
        }
        if default_param_flag:
            if not (prompt and style and title and continue_at is not None):
                raise ProviderError(
                    "extend custom mode needs prompt, style, title and continue_at.",
                    provider_name=self.name,
                    error_code="MISSING_PARAM",
                )
            body.update(
                prompt=prompt, style=style, title=title, continueAt=continue_at
            )
        self._apply_common(body, kwargs)
        return await self._run_audio("/api/v1/generate/extend", body, "extend")

    async def cover(
        self,
        *,
        upload_url: str,
        prompt: str,
        model: str | None = None,
        custom_mode: bool = False,
        instrumental: bool = False,
        style: str | None = None,
        title: str | None = None,
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(
            model or self.DEFAULT_MODEL, self.SUPPORTED_MODELS, operation="cover"
        )
        if custom_mode and (not style or not title):
            raise ProviderError(
                "cover custom mode requires both 'style' and 'title'.",
                provider_name=self.name,
                error_code="MISSING_PARAM",
            )
        body: dict[str, Any] = {
            "uploadUrl": upload_url,
            "prompt": prompt,
            "customMode": bool(custom_mode),
            "instrumental": bool(instrumental),
            "model": model_id,
            "callBackUrl": self._cb(kwargs),
        }
        if style:
            body["style"] = style
        if title:
            body["title"] = title
        self._apply_common(body, kwargs)
        return await self._run_audio(
            "/api/v1/generate/upload-cover", body, "cover"
        )

    async def upload_extend(
        self,
        *,
        upload_url: str,
        model: str | None = None,
        default_param_flag: bool = False,
        instrumental: bool = False,
        prompt: str | None = None,
        style: str | None = None,
        title: str | None = None,
        continue_at: float | None = None,
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(
            model or self.DEFAULT_MODEL,
            self.SUPPORTED_MODELS,
            operation="upload_extend",
        )
        body: dict[str, Any] = {
            "uploadUrl": upload_url,
            "defaultParamFlag": bool(default_param_flag),
            "instrumental": bool(instrumental),
            "model": model_id,
            "callBackUrl": self._cb(kwargs),
        }
        if default_param_flag:
            if not (prompt and style and title and continue_at is not None):
                raise ProviderError(
                    "upload_extend custom mode needs prompt, style, title, continue_at.",
                    provider_name=self.name,
                    error_code="MISSING_PARAM",
                )
            body.update(
                prompt=prompt, style=style, title=title, continueAt=continue_at
            )
        self._apply_common(body, kwargs)
        return await self._run_audio(
            "/api/v1/generate/upload-extend", body, "upload_extend"
        )

    # ---- add instrumental / vocals (narrower model set) -----------------

    async def add_instrumental(
        self,
        *,
        upload_url: str,
        title: str,
        tags: str,
        negative_tags: str,
        model: str | None = None,
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(
            model or self.DEFAULT_ADD_MODEL,
            self.STEM_ADD_MODELS | self.STEM_ADD_DEPRECATED,
            operation="add_instrumental",
        )
        body: dict[str, Any] = {
            "uploadUrl": upload_url,
            "title": title,
            "tags": tags,
            "negativeTags": negative_tags,
            "model": model_id,
            "callBackUrl": self._cb(kwargs),
        }
        self._apply_common(body, kwargs)
        return await self._run_audio(
            "/api/v1/generate/add-instrumental", body, "add_instrumental"
        )

    async def add_vocals(
        self,
        *,
        upload_url: str,
        prompt: str,
        title: str,
        style: str,
        negative_tags: str,
        model: str | None = None,
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(
            model or self.DEFAULT_ADD_MODEL,
            self.STEM_ADD_MODELS | self.STEM_ADD_DEPRECATED,
            operation="add_vocals",
        )
        body: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "negativeTags": negative_tags,
            "style": style,
            "uploadUrl": upload_url,
            "model": model_id,
            "callBackUrl": self._cb(kwargs),
        }
        self._apply_common(body, kwargs)
        return await self._run_audio(
            "/api/v1/generate/add-vocals", body, "add_vocals"
        )

    # ---- separate vocals / stems (dedicated record-info, successFlag) ----

    async def separate_vocals(
        self,
        *,
        task_id: str,
        audio_id: str,
        separation_type: str = "separate_vocal",
        **kwargs: Any,
    ) -> MusicResult:
        if separation_type not in ("separate_vocal", "split_stem"):
            raise ProviderError(
                "separation_type must be 'separate_vocal' or 'split_stem'.",
                provider_name=self.name,
                error_code="MISSING_PARAM",
            )
        body = {
            "taskId": task_id,
            "audioId": audio_id,
            "type": separation_type,
            "callBackUrl": self._cb(kwargs),
        }
        self._logger.info("Suno separate_vocals (%s)", separation_type)
        client = self._http()
        new_task = await self._submit(
            client, "/api/v1/vocal-removal/generate", body
        )
        data = await self._poll(
            client,
            "/api/v1/vocal-removal/record-info",
            new_task,
            "successFlag",
        )
        resp = data.get("response") or {}
        files: dict[str, bytes] = {}
        for key in self._STEM_FIELDS:
            url = resp.get(key)
            if url:
                # e.g. "vocalUrl" -> "vocal"
                name = key[:-3] if key.endswith("Url") else key
                files[name] = await self._download(client, url)
        if not files:
            raise ProviderError(
                "Vocal separation returned no stem URLs.",
                provider_name=self.name,
                error_code="NO_AUDIO",
            )
        return MusicResult(
            output_format="mp3",
            files=files,
            metadata={
                "provider": self.name,
                "operation": "separate_vocals",
                "separation_type": separation_type,
                "task_id": new_task,
                "source_task_id": task_id,
                "stems": sorted(files),
            },
        )

    # ---- lyrics (dedicated record-info, status) -------------------------

    async def generate_lyrics(self, *, prompt: str, **kwargs: Any) -> MusicResult:
        body = {"prompt": prompt, "callBackUrl": self._cb(kwargs)}
        self._logger.info("Suno generate_lyrics")
        client = self._http()
        task_id = await self._submit(client, "/api/v1/lyrics", body)
        data = await self._poll(
            client, "/api/v1/lyrics/record-info", task_id, "status"
        )
        items = ((data.get("response") or {}).get("data")) or []
        if not items:
            raise ProviderError(
                "Lyrics job succeeded but returned no text.",
                provider_name=self.name,
                error_code="NO_LYRICS",
            )
        first = items[0]
        return MusicResult(
            output_format="txt",
            text=first.get("text"),
            metadata={
                "provider": self.name,
                "operation": "generate_lyrics",
                "task_id": task_id,
                "title": first.get("title"),
                "variants": [i.get("text") for i in items],
            },
        )

    # ---- timestamped lyrics (SYNCHRONOUS, no callback/poll) --------------

    async def get_timestamped_lyrics(
        self, *, task_id: str, audio_id: str, **kwargs: Any
    ) -> MusicResult:
        body = {"taskId": task_id, "audioId": audio_id}
        self._logger.info("Suno get_timestamped_lyrics (sync)")
        try:
            resp = await self._http().post(
                f"{self.base_url}/api/v1/generate/get-timestamped-lyrics",
                headers=self._headers(),
                json=body,
            )
        except Exception as e:
            raise ProviderError(
                f"Suno get-timestamped-lyrics failed: {e}",
                provider_name=self.name,
                error_code="REQUEST_FAILED",
            )
        payload = self._parse_json(resp, "get-timestamped-lyrics")
        if resp.status_code != 200:
            raise ProviderError(
                f"Suno get-timestamped-lyrics HTTP {resp.status_code}: "
                f"{resp.text[:200]}",
                provider_name=self.name,
                error_code="GENERATION_FAILED",
            )
        data = payload.get("data") or {}
        return MusicResult(
            output_format="json",
            data={
                "alignedWords": data.get("alignedWords") or [],
                "waveformData": data.get("waveformData"),
            },
            metadata={
                "provider": self.name,
                "operation": "get_timestamped_lyrics",
                "source_task_id": task_id,
            },
        )

    # ---- format conversion: wav / mp4 (dedicated record-info, successFlag)

    async def to_wav(
        self, *, task_id: str, audio_id: str, **kwargs: Any
    ) -> MusicResult:
        body = {
            "taskId": task_id,
            "audioId": audio_id,
            "callBackUrl": self._cb(kwargs),
        }
        self._logger.info("Suno to_wav")
        client = self._http()
        new_task = await self._submit(client, "/api/v1/wav/generate", body)
        data = await self._poll(
            client, "/api/v1/wav/record-info", new_task, "successFlag"
        )
        wav_url = (data.get("response") or {}).get("audioWavUrl")
        if not wav_url:
            raise ProviderError(
                "WAV job succeeded but returned no URL.",
                provider_name=self.name,
                error_code="NO_AUDIO",
            )
        audio = await self._download(client, wav_url)
        return MusicResult(
            audio_data=audio,
            output_format="wav",
            metadata={
                "provider": self.name,
                "operation": "to_wav",
                "task_id": new_task,
                "source_task_id": task_id,
            },
        )

    async def to_mp4(
        self,
        *,
        task_id: str,
        audio_id: str,
        author: str | None = None,
        domain_name: str | None = None,
        **kwargs: Any,
    ) -> MusicResult:
        body: dict[str, Any] = {
            "taskId": task_id,
            "audioId": audio_id,
            "callBackUrl": self._cb(kwargs),
        }
        if author:
            body["author"] = author
        if domain_name:
            body["domainName"] = domain_name
        self._logger.info("Suno to_mp4")
        client = self._http()
        new_task = await self._submit(client, "/api/v1/mp4/generate", body)
        data = await self._poll(
            client, "/api/v1/mp4/record-info", new_task, "successFlag"
        )
        video_url = (data.get("response") or {}).get("videoUrl")
        if not video_url:
            raise ProviderError(
                "MP4 job succeeded but returned no URL.",
                provider_name=self.name,
                error_code="NO_VIDEO",
            )
        video = await self._download(client, video_url)
        return MusicResult(
            audio_data=video,
            output_format="mp4",
            metadata={
                "provider": self.name,
                "operation": "to_mp4",
                "task_id": new_task,
                "source_task_id": task_id,
                "type": "video",
            },
        )
