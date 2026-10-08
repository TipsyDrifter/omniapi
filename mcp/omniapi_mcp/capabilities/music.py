"""Music generation capability — providers for ElevenLabs Music and Suno (kie.ai).

Modality: text/audio -> music. Two very different provider styles:

- **ElevenLabs Music** is SYNCHRONOUS: ``POST /v1/music`` returns audio bytes
  directly (same shape as the TTS provider, reuses the ElevenLabs key).
- **Suno via kie.ai** is ASYNCHRONOUS: a job endpoint returns a ``taskId``; we
  poll until the task succeeds, then download the result URL straight away.
  Wrapped so callers just await once. The HTTP side (Bearer auth, createTask /
  recordInfo, the old per-operation endpoints, download, balance) lives in
  ``providers/kie_client.py``; this module only maps Suno operations onto it.

Suno exposes a whole family of operations (generate / extend / cover / add
vocals / separate stems / lyrics / wav / mp4). Each one is routed either to
kie.ai's unified ``jobs`` endpoints or to its old per-operation endpoints —
see ``SUNO_JOBS_MODELS`` below for the table and why. On the old route they
differ in three axes:
  1. **result endpoint** — the audio family shares ``/generate/record-info``;
     lyrics, wav, mp4 and vocal-removal each have a dedicated one.
  2. **status field** — ``data.status`` (audio family + lyrics) vs
     ``data.successFlag`` (wav / mp4 / vocal-removal).
  3. **output** — a downloadable file (audio/wav/mp4), multiple stem files,
     lyrics text, or timestamped-word JSON.

Every old Suno endpoint requires ``callBackUrl`` even though we only poll — a
placeholder satisfies the check (verified live: it 422s without one).

Reuses ``ProviderConfig`` / ``ProviderError`` from ``providers.base``; uses
``httpx`` (already a dependency).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..providers.base import ProviderConfig, ProviderError
from ..providers.kie_client import DEFAULT_CREDIT_USD, KieClient, KieTask, credits_to_usd

logger = logging.getLogger(__name__)


@dataclass
class MusicResult:
    """Standardized music-generation response.

    Only the field(s) relevant to the operation are populated:
    - ``audio_data`` — a single downloadable file (audio / wav / mp4 bytes)
    - ``files`` — named extra files, e.g. separated stems {name: bytes}
    - ``text`` — lyrics text
    - ``data`` — structured JSON (e.g. timestamped words)
    - ``extra_tracks`` — the other songs of a job that returns more than one
      (a Suno generate / extend / cover ... gives two): one dict per song with
      ``audio_data``, ``audio_id``, ``title``, ``duration``, ``tags``, ``url``,
      or ``error`` instead of ``audio_data`` when that song could not be
      downloaded. ``audio_data`` and the metadata stay the first song's.
    """

    output_format: str = "mp3"
    metadata: dict[str, Any] = field(default_factory=dict)
    audio_data: bytes | None = None
    files: dict[str, bytes] | None = None
    text: str | None = None
    data: Any = None
    extra_tracks: list[dict[str, Any]] = field(default_factory=list)


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
    # ElevenLabs now lists music_v1 as deprecated (models page, 2026-10-05);
    # music_v2_5 is the highest-quality one and costs the same per minute.
    SUPPORTED_MODELS = {"music_v1", "music_v2", "music_v2_5"}
    MODEL_STATUS: dict[str, str] = {
        "music_v1": "deprecated",
        "music_v2": "current",
        "music_v2_5": "current",
    }
    DEFAULT_MODEL = "music_v2_5"
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


#: Which Suno operation goes to which kie.ai endpoint family.
#:
#: kie.ai rewrote its docs so every model is reached through the unified
#: ``/api/v1/jobs/createTask`` + ``/api/v1/jobs/recordInfo`` pair; the old
#: per-operation endpoints are documented under ``docs.kie.ai/old-model/``.
#: The request side of the new endpoints is documented per operation, but no
#: page says what ``recordInfo``'s ``resultJson`` holds for a Suno task, so an
#: operation moves to the new route only after a real task has shown it.
#: Measured with a real key (V6_MINI, 2026-10-06; credits are per job):
#:
#:   operation          new model value (createTask)          default   resultJson seen               credits
#:   generate           ai-music-api/generate                 jobs      {"data": [song, song]}        12
#:   extend             ai-music-api/extend                   jobs      {"data": [song, song]}        12
#:   cover              ai-music-api/upload-and-cover-audio   jobs      {"data": [song, song]}        12
#:   upload_extend      ai-music-api/upload-and-extend-audio  legacy    task failed upstream (400, refunded); not seen
#:   add_instrumental   ai-music-api/add-instrumental         jobs      {"data": [song, song]}        12
#:   add_vocals         ai-music-api/add-vocals               jobs      {"data": [song, song]}        12
#:   generate_lyrics    ai-music-api/generate-lyrics          jobs      {"resultObject": {"lyricsData": [..]}}  0.4
#:   to_wav             ai-music-api/convert-to-wav-format    jobs      {"resultUrls": [wav]}         0.4
#:   to_mp4             ai-music-api/create-music-video       jobs      {"resultUrls": [mp4]}         2
#:   separate_vocals    (ai-music-api/separate-vocals)        LEGACY ONLY  request schema contradicts itself
#:   get_timestamped_lyrics (ai-music-api/timeStamped-lyrics) LEGACY ONLY  sync answer vs createTask taskId
#:
#: A song is ``{id, title, duration, audio_url, stream_audio_url, image_url,
#: model_name, prompt, tags, createTime}``; the same payload is repeated in the
#: record's ``response``. ``PROVIDERS__KIE__SUNO_ROUTES`` still overrides the
#: table (``legacy`` puts everything back on the old endpoints).
SUNO_JOBS_MODELS: dict[str, str] = {
    "generate": "ai-music-api/generate",
    "extend": "ai-music-api/extend",
    "cover": "ai-music-api/upload-and-cover-audio",
    "upload_extend": "ai-music-api/upload-and-extend-audio",
    "add_instrumental": "ai-music-api/add-instrumental",
    "add_vocals": "ai-music-api/add-vocals",
    "generate_lyrics": "ai-music-api/generate-lyrics",
    "to_wav": "ai-music-api/convert-to-wav-format",
    "to_mp4": "ai-music-api/create-music-video",
}
SUNO_LEGACY_ONLY: dict[str, str] = {
    "separate_vocals": (
        "the new page's input schema lists only type/stem_name (stem_name required) while its "
        "example sends task_id/audio_id; the polled stem structure is undocumented"
    ),
    "get_timestamped_lyrics": (
        "the new page posts to createTask (which answers a taskId) but its 200 example is the "
        "synchronous lyrics payload, camelCase in the example and snake_case in the schema"
    ),
}
# Operations whose unified-endpoint answer has been checked against the real
# service (generate 2026-10-05; the other seven 2026-10-06, see the table
# above). upload_extend stays on the old endpoint: its one real task failed
# upstream, so its answer has not been seen.
SUNO_VERIFIED_JOBS: frozenset[str] = frozenset({
    "generate", "extend", "cover", "add_instrumental", "add_vocals", "generate_lyrics", "to_wav", "to_mp4",
})
SUNO_DEFAULT_ROUTES: dict[str, str] = {
    **{op: ("jobs" if op in SUNO_VERIFIED_JOBS else "legacy") for op in SUNO_JOBS_MODELS},
    **{op: "legacy" for op in SUNO_LEGACY_ONLY},
}
_ROUTE_ALIASES = {"lyrics": "generate_lyrics", "wav": "to_wav", "mp4": "to_mp4", "music_video": "to_mp4",
                  "upload_cover": "cover", "timestamped_lyrics": "get_timestamped_lyrics",
                  "separate": "separate_vocals"}


def suno_routes(spec: str | None) -> dict[str, str]:
    """Resolve ``PROVIDERS__KIE__SUNO_ROUTES`` into ``{operation: "jobs"|"legacy"}``.

    ``""``/``default`` -> the table above; ``jobs``/``new``/``all`` -> every
    operation that has a new-endpoint mapping; ``legacy``/``old``/``none`` ->
    all old; otherwise a comma list of operations to move to the new route.
    Legacy-only operations and unknown names are ignored with a warning.
    """
    routes = dict(SUNO_DEFAULT_ROUTES)
    text = (spec or "").strip().lower()
    if text in ("", "default"):
        return routes
    if text in ("legacy", "old", "none"):
        return {op: "legacy" for op in routes}
    wanted = set(SUNO_JOBS_MODELS) if text in ("jobs", "new", "all") else {
        _ROUTE_ALIASES.get(p.strip(), p.strip()) for p in text.split(",") if p.strip()
    }
    for op in sorted(wanted):
        if op in SUNO_JOBS_MODELS:
            routes[op] = "jobs"
        elif op in SUNO_LEGACY_ONLY:
            logger.warning("Suno '%s' stays on the old kie.ai endpoint: %s", op, SUNO_LEGACY_ONLY[op])
        else:
            logger.warning("Ignoring unknown Suno operation %r in the kie.ai route setting", op)
    return routes


class SunoProvider(MusicProvider):
    """Suno via the kie.ai aggregator — asynchronous, wrapped to look synchronous.

    Each operation is sent to kie.ai either on the unified ``jobs`` route
    (``createTask`` + ``recordInfo``) or the ``legacy`` per-operation route —
    see the table above ``SUNO_JOBS_MODELS``. Both go through the shared
    :class:`~omniapi_mcp.providers.kie_client.KieClient`, and the result file
    is downloaded the moment the task succeeds.

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
    # add-instrumental / add-vocals accept a narrower set. Both the old and the
    # new kie.ai pages list V4_5PLUS, V5, V5_5, V6, V6_MINI, V6_WILD for these
    # two. We allow the current V6 family and keep V4_5PLUS/V5/V5_5 as a
    # deprecated tail so existing callers do not break.
    STEM_ADD_MODELS = {"V6", "V6_MINI", "V6_WILD"}
    STEM_ADD_DEPRECATED = {"V4_5PLUS", "V5", "V5_5"}
    DEFAULT_MODEL = "V6"
    DEFAULT_ADD_MODEL = "V6"
    DEFAULT_BASE = "https://api.kie.ai"

    # The old endpoints reject requests without callBackUrl even though we only
    # poll; the new createTask documents it as optional, so it is sent there
    # only when a caller passes one.
    _CALLBACK_PLACEHOLDER = "https://example.com/omniapi-mcp/suno-callback"
    # Non-terminal statuses of the old record-info endpoints (status / successFlag).
    _PENDING = frozenset({"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"})
    # All the stem URL fields the old vocal-removal record-info can return.
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
    # Old-endpoint paths, one place.
    LEGACY_PATHS = {
        "generate": "/api/v1/generate",
        "extend": "/api/v1/generate/extend",
        "cover": "/api/v1/generate/upload-cover",
        "upload_extend": "/api/v1/generate/upload-extend",
        "add_instrumental": "/api/v1/generate/add-instrumental",
        "add_vocals": "/api/v1/generate/add-vocals",
        "audio_record": "/api/v1/generate/record-info",
        "separate_vocals": "/api/v1/vocal-removal/generate",
        "separate_record": "/api/v1/vocal-removal/record-info",
        "generate_lyrics": "/api/v1/lyrics",
        "lyrics_record": "/api/v1/lyrics/record-info",
        "get_timestamped_lyrics": "/api/v1/generate/get-timestamped-lyrics",
        "to_wav": "/api/v1/wav/generate",
        "wav_record": "/api/v1/wav/record-info",
        "to_mp4": "/api/v1/mp4/generate",
        "mp4_record": "/api/v1/mp4/record-info",
    }

    def __init__(
        self,
        config: ProviderConfig,
        *,
        poll_timeout: float | None = None,
        routes: str | dict[str, str] | None = None,
        credit_usd: float | None = None,
        all_tracks: bool = True,
    ):
        super().__init__(config)
        self.base_url = (config.base_url or self.DEFAULT_BASE).rstrip("/")
        self.routes = dict(routes) if isinstance(routes, dict) else suno_routes(routes)
        self.credit_usd = credit_usd if credit_usd is not None else DEFAULT_CREDIT_USD
        #: download every song a job returns (Suno gives two), or only the first;
        #: a call can override it with ``all_tracks=`` in its kwargs
        self.all_tracks = bool(all_tracks)
        self.kie = KieClient(
            config.api_key,
            base_url=self.base_url,
            http=self._http,
            provider_name=self.name,
            request_timeout=config.timeout,
            # before 1.4 one ``timeout`` was both; keep that when no ceiling is given
            poll_timeout=poll_timeout if poll_timeout is not None else config.timeout,
            max_retries=config.max_retries,
        )

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    def route(self, operation: str) -> str:
        return self.routes.get(operation, "legacy")

    # ---- request building ------------------------------------------------

    def _cb(self, kwargs: dict[str, Any]) -> str:
        return kwargs.get("callBackUrl") or self._CALLBACK_PLACEHOLDER

    @staticmethod
    def _common(kwargs: dict[str, Any], *, persona: bool = True) -> dict[str, Any]:
        """The optional style/weight/persona params shared by most jobs, keyed
        by their new (snake_case) names. Read from the camelCase kwargs the
        tool layer has always passed."""
        out: dict[str, Any] = {}
        vocal_gender = kwargs.get("vocalGender") or kwargs.get("vocal_gender")
        if vocal_gender:
            out["vocal_gender"] = vocal_gender
        if kwargs.get("negativeTags"):
            out["negative_tags"] = kwargs["negativeTags"]
        for camel, snake in (("styleWeight", "style_weight"), ("weirdnessConstraint", "weirdness_constraint"),
                             ("audioWeight", "audio_weight")):
            if kwargs.get(camel) is not None:
                out[snake] = kwargs[camel]
        if persona:
            for camel, snake in (("personaId", "persona_id"), ("personaModel", "persona_model")):
                if kwargs.get(camel):
                    out[snake] = kwargs[camel]
        return out

    _CAMEL = {
        "vocal_gender": "vocalGender",
        "negative_tags": "negativeTags",
        "style_weight": "styleWeight",
        "weirdness_constraint": "weirdnessConstraint",
        "audio_weight": "audioWeight",
        "persona_id": "personaId",
        "persona_model": "personaModel",
    }

    def _legacy_common(self, body: dict[str, Any], kwargs: dict[str, Any]) -> None:
        """The pre-1.4 ``_apply_common``: same keys, same order, camelCase."""
        for snake, value in self._common(kwargs).items():
            body[self._CAMEL[snake]] = value

    def _jobs_callback(self, kwargs: dict[str, Any]) -> str | None:
        return kwargs.get("callBackUrl") or None

    # ---- result handling ---------------------------------------------------

    def _cost(self, credits: float | None) -> float | None:
        # creditsConsumed (recordInfo) x USD per credit (catalog pricing.credit_usd)
        return credits_to_usd(credits, self.credit_usd)

    def _unrecognised(self, task: KieTask, what: str) -> ProviderError:
        snippet = (task.result_raw or "")[:300]
        return ProviderError(
            f"kie.ai task {task.task_id} succeeded but its resultJson has no {what} we recognise "
            f"(the task is paid for; resultJson: {snippet!r})",
            provider_name=self.name,
            error_code="UNRECOGNISED_RESULT",
        )

    @staticmethod
    def _containers(result: Any) -> list[Any]:
        """Places a Suno payload may sit in ``resultJson``. Seen for real: the
        songs at the top level under ``data`` and the lyrics under
        ``resultObject.lyricsData``; ``response`` (the old record-info's
        wrapper) is kept as a fallback in case kie moves things again."""
        out: list[Any] = [result]
        if isinstance(result, dict):
            for key in ("resultObject", "response", "data"):
                if key in result:
                    out.append(result[key])
            ro = result.get("resultObject")
            if isinstance(ro, dict):
                out.extend(ro[k] for k in ("response", "data") if k in ro)
        return out

    @classmethod
    def _find_list(cls, result: Any, keys: tuple[str, ...], need: tuple[str, ...]) -> list[dict[str, Any]]:
        """The first list of dicts that has one of ``need`` (guess, see above)."""
        for box in cls._containers(result):
            candidates = [box] if isinstance(box, list) else [box.get(k) for k in keys] if isinstance(box, dict) else []
            for cand in candidates:
                if isinstance(cand, list) and cand and all(isinstance(i, dict) for i in cand):
                    if any(any(i.get(n) for n in need) for i in cand):
                        return cand
        return []

    @classmethod
    def _find_value(cls, result: Any, keys: tuple[str, ...]) -> Any:
        for box in cls._containers(result):
            if isinstance(box, dict):
                for k in keys:
                    if box.get(k):
                        return box[k]
        return None

    @staticmethod
    def _result_urls(result: Any) -> list[str]:
        """The one documented shape: ``{"resultUrls": [...]}``."""
        urls = result.get("resultUrls") if isinstance(result, dict) else None
        return [u for u in urls if isinstance(u, str) and u] if isinstance(urls, list) else []

    @staticmethod
    def _track_url(track: dict[str, Any]) -> str | None:
        return track.get("audioUrl") or track.get("audio_url")

    async def _legacy_tracks(self, task_id: str) -> list[dict[str, Any]]:
        """Fallback for a jobs task whose resultJson has URLs but no track ids:
        the new pages say the taskId is used with "Get Music Details", which is
        the old ``/generate/record-info``. Whether that endpoint knows a jobs
        task is NOT verified; any failure just returns []."""
        try:
            status, body = await self.kie.legacy_get(self.LEGACY_PATHS["audio_record"], {"taskId": task_id})
        except Exception as e:  # noqa: BLE001 - best effort only
            self._logger.info("Old record-info lookup for jobs task %s failed: %s", task_id, e)
            return []
        if status != 200 or not isinstance(body, dict):
            return []
        data = body.get("data") or {}
        tracks = ((data.get("response") or {}).get("sunoData")) if isinstance(data, dict) else None
        return [t for t in tracks if isinstance(t, dict)] if isinstance(tracks, list) else []

    def _audio_music_result(
        self, op: str, task_id: str, songs: list[tuple[str | None, dict[str, Any]]], audio: bytes, route: str,
        cost: float | None = None,
    ) -> MusicResult:
        """The first song's metadata, plus every song's id and URL. ``songs`` is
        ``[(url, track dict), ...]`` in kie's order, the downloaded one first."""
        track = songs[0][1] if songs else {}
        meta: dict[str, Any] = {
            "provider": self.name,
            "operation": op,
            "task_id": task_id,
            "audio_id": track.get("id"),
            "audio_ids": [t.get("id") for _, t in songs if t],
            "title": track.get("title"),
            "duration": track.get("duration"),
            "tags": track.get("tags"),
            "all_tracks": [u for u, _ in songs],
            "route": route,
        }
        if cost is not None:
            meta["cost_usd"] = cost
        return MusicResult(audio_data=audio, output_format="mp3", metadata=meta)

    def _want_all(self, kwargs: dict[str, Any]) -> bool:
        flag = kwargs.get("all_tracks")
        return self.all_tracks if flag is None else bool(flag)

    async def _finish_audio(
        self, op: str, task_id: str, songs: list[tuple[str | None, dict[str, Any]]], route: str,
        kwargs: dict[str, Any], cost: float | None = None,
    ) -> MusicResult:
        """Download the first song (a failure here fails the call, as before),
        then, unless switched off, every other song. The other songs are best
        effort: the job is already paid for, so one that cannot be fetched is
        reported in ``extra_tracks`` with an ``error`` instead of failing."""
        first_url = songs[0][0]
        audio = await self.kie.download(first_url)  # type: ignore[arg-type]
        result = self._audio_music_result(op, task_id, songs, audio, route, cost=cost)
        if not self._want_all(kwargs):
            return result
        for url, track in songs[1:]:
            entry: dict[str, Any] = {
                "audio_id": track.get("id"),
                "title": track.get("title"),
                "duration": track.get("duration"),
                "tags": track.get("tags"),
                "url": url,
            }
            if not url:
                entry["error"] = "this song came back without an audio URL"
            else:
                try:
                    entry["audio_data"] = await self.kie.download(url)
                except ProviderError as e:
                    self._logger.warning("Suno %s task %s: song %s could not be downloaded: %s",
                                         op, task_id, track.get("id") or url, e)
                    entry["error"] = str(e)
            result.extra_tracks.append(entry)
        return result

    async def _run_audio(
        self, op: str, legacy_body: dict[str, Any], jobs_input: dict[str, Any], kwargs: dict[str, Any]
    ) -> MusicResult:
        """Run an audio-family job on its route and download its songs (Suno
        answers with two; see ``_finish_audio``)."""
        if self.route(op) == "jobs":
            self._logger.info("Suno %s -> createTask %s", op, SUNO_JOBS_MODELS[op])
            task = await self.kie.run(SUNO_JOBS_MODELS[op], jobs_input, callback_url=self._jobs_callback(kwargs))
            tracks = [t for t in self._find_list(task.result, ("sunoData", "data", "tracks"), ("audio_url", "audioUrl"))]
            if tracks and self._track_url(tracks[0]):
                songs: list[tuple[str | None, dict[str, Any]]] = [(self._track_url(t), t) for t in tracks]
            else:
                urls = self._result_urls(task.result)
                if not urls:
                    raise self._unrecognised(task, "audio URL")
                # bare URLs: ids / titles come from the old record-info when it
                # knows this task, matched by URL (the order is not known)
                known = await self._legacy_tracks(task.task_id)
                by_url = {self._track_url(t): t for t in known if self._track_url(t)}
                songs = [(u, by_url.get(u, {})) for u in urls]
                songs += [(u, t) for u, t in by_url.items() if u not in urls]
            return await self._finish_audio(op, task.task_id, songs, "jobs", kwargs,
                                            cost=self._cost(task.credits_consumed))

        path = self.LEGACY_PATHS[op]
        self._logger.info("Suno %s -> %s", op, path)
        task_id = await self.kie.legacy_submit(path, legacy_body)
        data = await self.kie.legacy_poll(self.LEGACY_PATHS["audio_record"], task_id, status_field="status",
                                          pending=self._PENDING)
        suno_data = [t for t in (((data.get("response") or {}).get("sunoData")) or []) if isinstance(t, dict)]
        if not suno_data:
            raise ProviderError(
                "Suno reported SUCCESS but returned no sunoData.",
                provider_name=self.name,
                error_code="NO_AUDIO",
            )
        if not self._track_url(suno_data[0]):
            raise ProviderError(
                "Suno finished but returned no audio URL.",
                provider_name=self.name,
                error_code="NO_AUDIO",
            )
        return await self._finish_audio(op, task_id, [(self._track_url(t), t) for t in suno_data], "legacy", kwargs)

    # ---- text -> music (generate) ---------------------------------------

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
        self._legacy_common(body, kwargs)

        jobs: dict[str, Any] = {
            "prompt": prompt,
            "model": model_id,
            "custom_mode": custom_mode,
            "instrumental": bool(instrumental),
        }
        if style:
            jobs["style"] = style
        if title:
            jobs["title"] = title
        jobs.update(self._common(kwargs))
        return await self._run_audio("generate", body, jobs, kwargs)

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
        # The new endpoint has no defaultParamFlag: leaving prompt/style/title/
        # continue_at out is how it inherits the source track's settings.
        jobs: dict[str, Any] = {"audio_id": audio_id, "model": model_id}
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
            jobs.update(prompt=prompt, style=style, title=title, continue_at=continue_at)
        self._legacy_common(body, kwargs)
        jobs.update(self._common(kwargs))
        return await self._run_audio("extend", body, jobs, kwargs)

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
        self._legacy_common(body, kwargs)

        # The new page has no custom_mode field (nor does the rewritten old one).
        jobs: dict[str, Any] = {
            "upload_url": upload_url,
            "prompt": prompt,
            "instrumental": bool(instrumental),
            "model": model_id,
        }
        if style:
            jobs["style"] = style
        if title:
            jobs["title"] = title
        jobs.update(self._common(kwargs))
        return await self._run_audio("cover", body, jobs, kwargs)

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
        jobs: dict[str, Any] = {"upload_url": upload_url, "instrumental": bool(instrumental), "model": model_id}
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
            jobs.update(prompt=prompt, style=style, title=title, continue_at=continue_at)
        self._legacy_common(body, kwargs)
        jobs.update(self._common(kwargs))
        return await self._run_audio("upload_extend", body, jobs, kwargs)

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
        self._legacy_common(body, kwargs)
        # the new page lists no persona fields for this operation
        jobs: dict[str, Any] = {
            "upload_url": upload_url,
            "title": title,
            "tags": tags,
            "negative_tags": negative_tags,
            "model": model_id,
        }
        jobs.update(self._common(kwargs, persona=False))
        return await self._run_audio("add_instrumental", body, jobs, kwargs)

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
        self._legacy_common(body, kwargs)
        jobs: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "negative_tags": negative_tags,
            "style": style,
            "upload_url": upload_url,
            "model": model_id,
        }
        jobs.update(self._common(kwargs, persona=False))
        return await self._run_audio("add_vocals", body, jobs, kwargs)

    # ---- separate vocals / stems (old route only) ------------------------

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
        new_task = await self.kie.legacy_submit(self.LEGACY_PATHS["separate_vocals"], body)
        data = await self.kie.legacy_poll(self.LEGACY_PATHS["separate_record"], new_task,
                                          status_field="successFlag", pending=self._PENDING)
        resp = data.get("response") or {}
        files: dict[str, bytes] = {}
        for key in self._STEM_FIELDS:
            url = resp.get(key)
            if url:
                # e.g. "vocalUrl" -> "vocal"
                name = key[:-3] if key.endswith("Url") else key
                files[name] = await self.kie.download(url)
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
                "route": "legacy",
            },
        )

    # ---- lyrics ------------------------------------------------------------

    async def generate_lyrics(self, *, prompt: str, **kwargs: Any) -> MusicResult:
        self._logger.info("Suno generate_lyrics (%s)", self.route("generate_lyrics"))
        cost: float | None = None
        if self.route("generate_lyrics") == "jobs":
            task = await self.kie.run(SUNO_JOBS_MODELS["generate_lyrics"], {"prompt": prompt},
                                      callback_url=self._jobs_callback(kwargs))
            task_id = task.task_id
            items = self._find_list(task.result, ("lyricsData", "data", "lyrics"), ("text",))  # seen: resultObject.lyricsData
            if not items:
                text = self._find_value(task.result, ("text", "lyrics"))
                if isinstance(text, str) and text:
                    items = [{"text": text, "title": self._find_value(task.result, ("title",))}]
            if not items:
                raise self._unrecognised(task, "lyrics text")
            cost = self._cost(task.credits_consumed)
        else:
            body = {"prompt": prompt, "callBackUrl": self._cb(kwargs)}
            task_id = await self.kie.legacy_submit(self.LEGACY_PATHS["generate_lyrics"], body)
            data = await self.kie.legacy_poll(self.LEGACY_PATHS["lyrics_record"], task_id,
                                              status_field="status", pending=self._PENDING)
            items = ((data.get("response") or {}).get("data")) or []
            if not items:
                raise ProviderError(
                    "Lyrics job succeeded but returned no text.",
                    provider_name=self.name,
                    error_code="NO_LYRICS",
                )
        first = items[0]
        meta: dict[str, Any] = {
            "provider": self.name,
            "operation": "generate_lyrics",
            "task_id": task_id,
            "title": first.get("title"),
            "variants": [i.get("text") for i in items],
            "route": self.route("generate_lyrics"),
        }
        if cost is not None:
            meta["cost_usd"] = cost
        return MusicResult(output_format="txt", text=first.get("text"), metadata=meta)

    # ---- timestamped lyrics (old route only; SYNCHRONOUS) ------------------

    async def get_timestamped_lyrics(
        self, *, task_id: str, audio_id: str, **kwargs: Any
    ) -> MusicResult:
        body = {"taskId": task_id, "audioId": audio_id}
        self._logger.info("Suno get_timestamped_lyrics (sync)")
        payload = await self.kie.legacy_post(self.LEGACY_PATHS["get_timestamped_lyrics"], body)
        data = (payload.get("data") if isinstance(payload, dict) else None) or {}
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
                "route": "legacy",
            },
        )

    # ---- format conversion: wav / mp4 ------------------------------------

    async def _run_file(
        self,
        op: str,
        legacy_body: dict[str, Any],
        jobs_input: dict[str, Any],
        kwargs: dict[str, Any],
        *,
        record_key: str,
        legacy_field: str,
        jobs_fields: tuple[str, ...],
        missing_code: str,
        missing_msg: str,
    ) -> tuple[str, bytes, float | None]:
        """wav / mp4: one output file. Returns ``(task_id, bytes, cost_usd)``."""
        if self.route(op) == "jobs":
            task = await self.kie.run(SUNO_JOBS_MODELS[op], jobs_input, callback_url=self._jobs_callback(kwargs))
            # seen for real: {"resultUrls": [url]}; a named field is the fallback
            urls = self._result_urls(task.result)
            url = urls[0] if urls else self._find_value(task.result, jobs_fields)
            if not isinstance(url, str):
                url = None
            if not url:
                raise self._unrecognised(task, "file URL")
            return task.task_id, await self.kie.download(url), self._cost(task.credits_consumed)
        new_task = await self.kie.legacy_submit(self.LEGACY_PATHS[op], legacy_body)
        data = await self.kie.legacy_poll(self.LEGACY_PATHS[record_key], new_task, status_field="successFlag",
                                          pending=self._PENDING)
        url = (data.get("response") or {}).get(legacy_field)
        if not url:
            raise ProviderError(missing_msg, provider_name=self.name, error_code=missing_code)
        return new_task, await self.kie.download(url), None

    async def to_wav(
        self, *, task_id: str, audio_id: str, **kwargs: Any
    ) -> MusicResult:
        body = {
            "taskId": task_id,
            "audioId": audio_id,
            "callBackUrl": self._cb(kwargs),
        }
        self._logger.info("Suno to_wav (%s)", self.route("to_wav"))
        new_task, audio, cost = await self._run_file(
            "to_wav", body, {"task_id": task_id, "audio_id": audio_id}, kwargs,
            record_key="wav_record", legacy_field="audioWavUrl",
            jobs_fields=("audioWavUrl", "audio_wav_url", "wavUrl", "wav_url"),
            missing_code="NO_AUDIO", missing_msg="WAV job succeeded but returned no URL.",
        )
        meta: dict[str, Any] = {
            "provider": self.name,
            "operation": "to_wav",
            "task_id": new_task,
            "source_task_id": task_id,
            "route": self.route("to_wav"),
        }
        if cost is not None:
            meta["cost_usd"] = cost
        return MusicResult(audio_data=audio, output_format="wav", metadata=meta)

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
        jobs: dict[str, Any] = {"task_id": task_id, "audio_id": audio_id}
        if author:
            body["author"] = author
            jobs["author"] = author
        if domain_name:
            body["domainName"] = domain_name
            jobs["domain_name"] = domain_name
        self._logger.info("Suno to_mp4 (%s)", self.route("to_mp4"))
        new_task, video, cost = await self._run_file(
            "to_mp4", body, jobs, kwargs,
            record_key="mp4_record", legacy_field="videoUrl",
            jobs_fields=("videoUrl", "video_url"),
            missing_code="NO_VIDEO", missing_msg="MP4 job succeeded but returned no URL.",
        )
        meta: dict[str, Any] = {
            "provider": self.name,
            "operation": "to_mp4",
            "task_id": new_task,
            "source_task_id": task_id,
            "type": "video",
            "route": self.route("to_mp4"),
        }
        if cost is not None:
            meta["cost_usd"] = cost
        return MusicResult(audio_data=video, output_format="mp4", metadata=meta)


class LyriaProvider(MusicProvider):
    """Google Lyria via the Gemini Developer API — one call, one song.

    Call shape (docs/research/2026-10-02-Lyria音樂生成API查證.md):
    ``client.interactions.create(model=..., input=prompt)`` — the Interactions
    API, not ``generate_content``. The reply carries ``output_audio`` (base64,
    MP3 44.1 kHz stereo by default) and ``output_text`` (lyrics / structure).

    There are no parameters beyond the prompt: lyrics go into it with
    ``[Verse]`` / ``[Chorus]`` tags, length is steered with timestamps, and
    "no vocals" is a sentence. ``instrumental`` therefore appends that sentence.
    The clip model always returns 30 seconds. Needs a paid tier: the free tier
    has no Lyria quota.

    NOT MEASURED against a live response (the owner's project has no billing
    yet): the attribute names come from the docs, so the audio is looked for
    in more than one place and a reply without any raises a clear error.
    """

    SUPPORTED_MODELS = {"lyria-3.5", "lyria-3-clip-preview"}
    MODEL_STATUS: dict[str, str] = {"lyria-3.5": "current", "lyria-3-clip-preview": "current"}
    DEFAULT_MODEL = "lyria-3.5"
    #: USD per song (Gemini API pricing page, 2026-10-02)
    PRICE_PER_SONG = {"lyria-3.5": 0.08, "lyria-3-clip-preview": 0.04}
    INSTRUMENTAL_SUFFIX = "Instrumental only, no vocals."

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        from google import genai
        from google.genai import types as genai_types

        http_options: Any = None
        if config.timeout:
            http_options = genai_types.HttpOptions(timeout=int(config.timeout * 1000))  # milliseconds
        self.client = genai.Client(api_key=config.api_key, **({"http_options": http_options} if http_options else {}))

    def get_supported_models(self) -> set[str]:
        return set(self.SUPPORTED_MODELS)

    @staticmethod
    def _audio_of(interaction: Any) -> tuple[bytes | None, str | None]:
        """``(audio bytes, mime type)`` from an interaction: the documented
        ``output_audio`` first, then any audio part among its outputs / steps."""
        import base64

        def decode(data: Any) -> bytes | None:
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
            if isinstance(data, str) and data:
                try:
                    return base64.b64decode(data)
                except Exception:
                    return None
            return None

        direct = getattr(interaction, "output_audio", None)
        if direct is not None:
            data = decode(getattr(direct, "data", direct))
            if data:
                return data, getattr(direct, "mime_type", None)
        for holder in ("outputs", "steps"):
            for item in getattr(interaction, holder, None) or []:
                for part in [item, *(getattr(item, "content", None) or [])] if not isinstance(item, (str, bytes)) else []:
                    mime = getattr(part, "mime_type", None)
                    if getattr(part, "type", None) == "audio" or (isinstance(mime, str) and mime.startswith("audio/")):
                        data = decode(getattr(part, "data", None))
                        if data:
                            return data, mime
        return None, None

    async def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        instrumental: bool = False,
        output_format: str = "mp3",
        **kwargs: Any,
    ) -> MusicResult:
        model_id = self._validate_model(model or self.DEFAULT_MODEL, self.SUPPORTED_MODELS, operation="generate")
        text = prompt.strip()
        if instrumental and "no vocals" not in text.lower():
            text = f"{text}\n\n{self.INSTRUMENTAL_SUFFIX}"
        want_wav = (output_format or "mp3").split("_")[0].lower() == "wav"
        try:
            self._logger.info("Generating music with %s (%d chars)", model_id, len(text))
            interaction = await self.client.aio.interactions.create(
                model=model_id, input=text, **({"response_format": {"type": "audio"}} if want_wav else {})
            )
        except Exception as e:
            self._logger.error("Error in Lyria generation: %s", e)
            raise ProviderError(f"Lyria generation failed: {e}", provider_name=self.name, error_code="MUSIC_FAILED")
        audio, mime = self._audio_of(interaction)
        if not audio:
            raise ProviderError("Lyria returned no audio data", provider_name=self.name, error_code="MUSIC_FAILED")
        fmt = "wav" if (mime or "").endswith(("wav", "x-wav")) or (want_wav and not mime) else "mp3"
        lyrics = getattr(interaction, "output_text", None)
        return MusicResult(
            audio_data=audio,
            output_format=fmt,
            text=lyrics if isinstance(lyrics, str) else None,
            metadata={
                "provider": "google",
                "model": model_id,
                "operation": "generate",
                "task_id": getattr(interaction, "id", None),
                "cost_usd": self.PRICE_PER_SONG.get(model_id),
                "duration": 30.0 if model_id == "lyria-3-clip-preview" else None,
            },
        )
