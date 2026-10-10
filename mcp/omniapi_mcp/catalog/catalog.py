"""The model catalog: curated overlay (``catalog.json``) merged with live
discovery (``discovery.py``).

Two layers, two questions:

* **Is it online?** — discovery answers. A curated model that the provider
  no longer lists gets ``online=False``; a listed model we never curated
  shows up as ``status="discovered"`` with no pricing.
* **What is it?** — the curated layer answers: pricing, capabilities,
  status (current / deprecated / retired), shutdown date, replacement,
  which harness runs it as an agent, tier aliases.

Rule of thumb for callers: text-modality providers share one wire format,
so *any* online text model of a configured provider is callable. Image,
speech, transcription and music keep static ``SUPPORTED_MODELS`` sets in
their capability modules (each needs bespoke request code); the catalog
only decorates those with metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..modalities import CATALOG_MODALITIES
from .discovery import (
    DiscoveryResult,
    discover,
    fetch_anthropic,
    fetch_elevenlabs,
    fetch_gemini,
    fetch_openai_compatible,
)
from .popularity import popularity
from .popularity import validate as validate_popularity

logger = logging.getLogger(__name__)

MODALITIES = CATALOG_MODALITIES
#: the discovery key of OpenRouter's image roster (1.4-M2; ``openrouter_images.py``)
OR_IMAGES = "openrouter-images"
#: the discovery key of OpenRouter's video roster (1.4-M3; ``openrouter_videos.py``)
OR_VIDEOS = "openrouter-videos"
# Fixed snapshots (gpt-5.5-2026-04-23, gpt-4-0613, …-preview-10-2025) and moving aliases
# (gemini-flash-latest): callable, but an uncurated one is not worth a row in any listing.
_SNAPSHOT_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{2}-\d{4}|\d{4}|latest)$")
# Gateways name real models that way (mistralai/mistral-large-2411): only the dated form is a snapshot there.
_DATED_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
STATUSES = ("current", "deprecated", "retired", "discovered")
#: a curated entry's status (``discovered`` is only ever set by discovery)
CURATED_STATUSES = ("current", "deprecated", "retired")
#: ``pricing.unit`` values the estimate, the chat menu and the GUI know how to
#: read. A text model's pricing carries no unit: input / output per 1M tokens.
#: ``openrouter`` is the listed pricing lines of an OpenRouter image model (live, never curated);
#: ``openrouter_video`` an OpenRouter video model's listed SKUs (``openrouter_videos.parse_skus``).
PRICING_UNITS = ("per_1m_tokens", "per_image", "per_minute", "per_1m_chars", "per_1k_chars", "per_song", "credits", "openrouter",
                 "openrouter_video")
#: catalog providers whose curated video rows are called directly (1.4-M5: Gemini Omni with the Google key);
#: every other video model goes through OpenRouter
DIRECT_VIDEO_PROVIDERS = ("google",)
#: what every curated model must have
REQUIRED_MODEL_FIELDS = ("id", "provider", "modality", "name", "status")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(raw: dict[str, Any]) -> list[str]:
    """Problems in a ``catalog.json`` document, one line each; empty when it
    is sound. Checked: every model has the required fields, a known modality,
    status and pricing unit, a dated shutdown and a provider the file
    defines; the providers' and the id rules' modalities are known; ids are
    unique per provider; tiers and suggested tiers name curated models."""
    problems: list[str] = []
    providers = raw.get("providers") or {}
    known_mod = set(MODALITIES)
    for name, p in providers.items():
        for key in ("modalities", "discovery_modalities"):
            bad = sorted(set(p.get(key) or []) - known_mod)
            if bad:
                problems.append(f"providers.{name}.{key}: unknown modality {bad}")
    for name, rules in (raw.get("id_rules") or {}).items():
        if name.startswith("_"):
            continue
        for i, rule in enumerate(rules or []):
            if rule.get("modality") not in known_mod | {"ignore"}:
                problems.append(f"id_rules.{name}[{i}]: unknown modality {rule.get('modality')!r}")
    seen: set[tuple[str, str]] = set()
    ids: set[str] = set()
    for i, m in enumerate(raw.get("models") or []):
        where = f"models[{i}] ({m.get('id', '?')})"
        missing = [k for k in REQUIRED_MODEL_FIELDS if not m.get(k)]
        if missing:
            problems.append(f"{where}: missing {missing}")
        if m.get("modality") and m["modality"] not in known_mod:
            problems.append(f"{where}: unknown modality {m['modality']!r} (known: {', '.join(MODALITIES)})")
        if m.get("status") and m["status"] not in CURATED_STATUSES:
            problems.append(f"{where}: unknown status {m['status']!r} (known: {', '.join(CURATED_STATUSES)})")
        if m.get("provider") and m["provider"] not in providers:
            problems.append(f"{where}: provider {m['provider']!r} is not defined under providers")
        pricing = m.get("pricing")
        if pricing is not None and not isinstance(pricing, dict):
            problems.append(f"{where}: pricing is neither an object nor null")
        elif isinstance(pricing, dict) and "unit" in pricing and pricing["unit"] not in PRICING_UNITS:
            problems.append(f"{where}: unknown pricing.unit {pricing['unit']!r} (known: {', '.join(PRICING_UNITS)})")
        if m.get("shutdown") is not None and not _DATE_RE.match(str(m["shutdown"])):
            problems.append(f"{where}: shutdown {m['shutdown']!r} is not YYYY-MM-DD")
        key = (str(m.get("provider")), str(m.get("id")))
        if key in seen:
            problems.append(f"{where}: listed twice for {key[0]}")
        seen.add(key)
        ids.add(str(m.get("id")))
        for alias in m.get("aliases") or []:
            ids.add(str(alias))
    for tier, target in (raw.get("tiers") or {}).items():
        if target not in ids:
            problems.append(f"tiers.{tier}: {target!r} is not a curated model")
    for name, p in providers.items():
        for tier, target in (p.get("suggested_tiers") or {}).items():
            if target not in ids:
                problems.append(f"providers.{name}.suggested_tiers.{tier}: {target!r} is not a curated model")
    return problems


def peak_multiplier(pricing: dict[str, Any], at: float | None = None) -> float:
    """Price multiplier at time ``at`` for a pricing object with a ``peak`` rule
    (``{"multiplier": 2, "utc_hours": [[1, 4], [6, 10]], "weekdays": [0..4],
    "except_dates": ["2026-10-01"]}``); 1 outside the window or without a rule.
    The listed prices are the off-peak ones."""
    rule = pricing.get("peak")
    if not rule:
        return 1.0
    t = time.gmtime(time.time() if at is None else at)
    if t.tm_wday not in rule.get("weekdays", range(7)):
        return 1.0
    if time.strftime("%Y-%m-%d", t) in rule.get("except_dates", ()):
        return 1.0
    hour = t.tm_hour + t.tm_min / 60
    if any(start <= hour < end for start, end in rule.get("utc_hours", ())):
        return float(rule.get("multiplier", 1))
    return 1.0


def discovered_vision(extra: Any) -> bool | None:
    """``vision`` for a discovered model from what its listing reported, or
    ``None`` when it reported nothing about input modalities (決策記錄 1.2-M2-a).

    OpenRouter's ``GET /models`` (checked 2026-10-03, 466 models) gives every
    model ``architecture.input_modalities`` (e.g. ``["text", "image"]``) and
    ``architecture.modality`` (e.g. ``"text+image->text"``); "image" among the
    inputs means it takes images. The modality string is only the fallback."""
    arch = extra.get("architecture") if isinstance(extra, dict) else None
    if not isinstance(arch, dict):
        return None
    inputs = arch.get("input_modalities")
    if isinstance(inputs, list):
        return any(str(x).lower() == "image" for x in inputs)
    modality = arch.get("modality")
    if isinstance(modality, str) and "->" in modality:
        return "image" in modality.split("->", 1)[0].lower().split("+")
    return None


def discovered_input(extra: Any, modality: str) -> bool | None:
    """Does a discovered model take ``modality`` ("file" / "audio") as input,
    from OpenRouter's ``architecture.input_modalities`` (1.2-M5); ``None``
    when the listing said nothing."""
    arch = extra.get("architecture") if isinstance(extra, dict) else None
    inputs = arch.get("input_modalities") if isinstance(arch, dict) else None
    if not isinstance(inputs, list):
        return None
    return any(str(x).lower() == modality for x in inputs)


def discovered_tools(extra: Any) -> bool | None:
    """``tools`` (function calling) for a discovered model from its listing,
    or ``None`` when the listing said nothing (1.2-M4). OpenRouter documents
    ``supported_parameters`` on every model of ``GET /models`` ("tools —
    function calling capabilities"; docs checked 2026-10-03). A cache written
    before this field was kept has no such key: ``None``, i.e. no tools."""
    params = extra.get("supported_parameters") if isinstance(extra, dict) else None
    if not isinstance(params, list):
        return None
    return any(str(p).lower() == "tools" for p in params)


@dataclass
class ModelEntry:
    id: str
    provider: str
    modality: str
    name: str = ""
    status: str = "current"
    shutdown: str | None = None
    replacement: str | None = None
    pricing: dict[str, Any] | None = None
    context: int | None = None
    max_output: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    leaderboard: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    implemented: bool = True
    harness: str | None = None
    online: bool | None = None  # None = provider has no discovery / not run yet
    discovered_name: str | None = None
    snapshot: bool = False  # uncurated snapshot / alias id (gpt-5.5-2026-04-23, gpt-4-0613, *-latest): hidden by default
    #: an OpenRouter image model (1.4-M2): who makes it (``x-ai`` / "xAI") and the
    #: request shape its listing states (``openrouter_images.image_params``)
    vendor: str | None = None
    vendor_label: str | None = None
    image_params: dict[str, Any] | None = None
    #: an OpenRouter video model (1.4-M3): what a request may ask for
    #: (``openrouter_videos.video_params``)
    video_params: dict[str, Any] | None = None
    #: listed by a live roster but left out of every listing, with why
    #: (``not_generation``: an OpenRouter video editor / upscaler / avatar)
    unlisted: str | None = None

    @property
    def via_openrouter_image(self) -> bool:
        return self.provider == "openrouter" and self.modality == "image"

    @property
    def via_openrouter_video(self) -> bool:
        return self.provider == "openrouter" and self.modality == "video"

    @property
    def video_route(self) -> str | None:
        """Which video provider makes this model's videos: ``"openrouter"`` (its
        roster), ``"google"`` (a curated Google video row, called directly with
        the Google key: Gemini Omni, 1.4-M5), or ``None`` (not a video model, or
        one a vendor's listing merely turned up)."""
        if self.modality != "video":
            return None
        if self.via_openrouter_video:
            return "openrouter"
        if self.provider in DIRECT_VIDEO_PROVIDERS and self.status != "discovered":
            return self.provider
        return None

    @property
    def live_media(self) -> bool:
        """A row of an OpenRouter media roster (image or video): keyed by
        (provider, id, modality), since the same id may be a chat model too."""
        return self.via_openrouter_image or self.via_openrouter_video

    @property
    def vision(self) -> bool:
        """Takes images as input (決策記錄 1.2-M2-a: the flag decides, nothing else)."""
        return bool(self.capabilities.get("vision"))

    @property
    def tools(self) -> bool:
        """Can call functions (1.2-M4: only then does a chat offer it tools)."""
        return bool(self.capabilities.get("tools"))

    @property
    def pdf(self) -> bool:
        """Takes a PDF as it is in a chat (1.2-M5, 決策記錄 1.2-M5-d): Anthropic's
        ``document`` block and OpenAI's ``file`` part on their vision models,
        OpenRouter per model (``files``, from its input modalities); Gemini's
        compatible endpoint documents no PDF input, DeepSeek none."""
        if self.provider in ("anthropic", "openai"):
            return self.vision
        if self.provider == "openrouter":
            return bool(self.capabilities.get("files"))
        return False

    @property
    def audio(self) -> bool:
        """Takes audio as it is (``input_audio``) in a chat: a curated or
        discovered ``audio_input`` flag; Gemini's compatible endpoint documents
        ``input_audio``, so a multimodal (vision) Gemini model counts unless
        flagged otherwise. Anthropic none; OpenAI only its audio models, which
        the catalog does not route."""
        if "audio_input" in self.capabilities:
            return bool(self.capabilities["audio_input"])
        return self.provider == "google" and self.vision

    def to_dict(self) -> dict[str, Any]:
        caps = dict(self.capabilities)
        if self.modality == "text":
            caps["vision"] = self.vision  # always a boolean on text models: the GUI greys out attaching by it
            caps["tools"] = self.tools  # likewise: the GUI says when a model cannot generate in a chat
            caps["pdf"] = self.pdf  # 1.2-M5: a PDF goes as it is (else its text)
            caps["audio"] = self.audio  # 1.2-M5: audio goes as it is (else the owner is asked about transcribing)
        d = {
            "id": self.id,
            "provider": self.provider,
            "modality": self.modality,
            "name": self.name or self.discovered_name or self.id,
            "status": self.status,
            "online": self.online,
            "harness": self.harness,
            "pricing": self.pricing,
            "capabilities": caps,
        }
        for k in ("shutdown", "replacement", "context", "max_output", "note"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.aliases:
            d["aliases"] = self.aliases
        if self.leaderboard:
            d["leaderboard"] = self.leaderboard
        if not self.implemented:
            d["implemented"] = False
        if self.snapshot:
            d["snapshot"] = True
        if self.vendor:
            d["vendor"] = self.vendor
            d["vendor_label"] = self.vendor_label or self.vendor
        if self.image_params is not None:
            d["image_params"] = self.image_params
        if self.video_params is not None:
            d["video_params"] = self.video_params
        if self.unlisted:
            d["unlisted"] = self.unlisted
        # where it stands on the Artificial Analysis boards of its modality (popularity.py);
        # the GUI sorts by ``rank``, nothing here reorders a list
        pop = popularity.lookup(self.id, self.modality, self.aliases)
        if pop:
            d.update(pop)
        return d


class ModelCatalog:
    """Curated + discovered model catalog. One instance per process."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).with_name("catalog.json")
        self._raw: dict[str, Any] = {}
        #: (provider, id); an OpenRouter image model is (provider, id, "image") — the
        #: same id can also be an OpenRouter chat model (google/gemini-3.1-flash-image)
        self._entries: dict[tuple[str, ...], ModelEntry] = {}
        self._alias_index: dict[str, str] = {}  # alias/tier -> canonical id
        self._discovery: dict[str, DiscoveryResult] = {}
        #: catalog providers whose key is set and switched on (1.4-M2): an OpenRouter
        #: image model of such a vendor is a duplicate and is not listed
        self._direct: set[str] = set()
        #: the owner's tier choices (settings.json / env), laid over catalog.json's tiers
        self._tier_overrides: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.loaded_at: float | None = None
        #: what :func:`validate` found at the last ``load()``
        self.problems: list[str] = []
        self.load()

    # ------------------------------------------------------------ loading
    def load(self) -> None:
        """Read ``catalog.json`` and check it (:func:`validate`). A problem is
        logged as an error, not raised: a wrong row must not keep the service
        from starting. ``tests/unit/test_modalities.py`` fails on any."""
        self._raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.problems = validate(self._raw)
        curated: dict[str, set[str]] = {}
        for m in self._raw.get("models") or []:
            if m.get("modality") and m.get("id"):
                curated.setdefault(m["modality"], set()).update([str(m["id"]), *map(str, m.get("aliases") or [])])
        self.problems += validate_popularity(popularity.doc, curated)
        for problem in self.problems:
            logger.error("catalog.json: %s", problem)
        self._entries.clear()
        self._alias_index.clear()
        for m in self._raw.get("models", []):
            if not all(m.get(k) for k in ("id", "provider", "modality")):
                continue  # reported above; a row without them cannot be keyed
            entry = ModelEntry(
                id=m["id"],
                provider=m["provider"],
                modality=m["modality"],
                name=m.get("name", ""),
                status=m.get("status", "current"),
                shutdown=m.get("shutdown"),
                replacement=m.get("replacement"),
                pricing=m.get("pricing"),
                context=m.get("context"),
                max_output=m.get("max_output"),
                capabilities=dict(m.get("capabilities") or {}),
                aliases=list(m.get("aliases") or []),
                leaderboard=dict(m.get("leaderboard") or {}),
                note=m.get("note"),
                implemented=m.get("implemented", True),
                video_params=dict(m["video_params"]) if isinstance(m.get("video_params"), dict) else None,
                harness=(self._raw.get("providers", {}).get(m["provider"]) or {}).get(
                    "harness"
                ),
            )
            self._entries[(entry.provider, entry.id)] = entry
            for a in entry.aliases:
                self._alias_index[a] = entry.id
        for tier, target in self.tiers.items():
            self._alias_index[tier] = target
        self.loaded_at = time.time()

    def set_tier_overrides(self, overrides: dict[str, str] | None) -> dict[str, str]:
        """Lay the owner's tier choices over ``catalog.json`` (the file is never
        written). Only the tiers the catalog defines can be overridden; an
        empty value or a tier left out falls back to the catalog's own.
        Returns the tiers now in effect."""
        known = set((self._raw.get("tiers") or {}).keys())
        clean = {k: str(v).strip() for k, v in (overrides or {}).items() if k in known and v and str(v).strip()}
        ignored = sorted(set(overrides or {}) - known)
        if ignored:
            logger.warning("Ignoring overrides for unknown tiers: %s", ignored)
        self._tier_overrides = clean
        for tier, target in self.tiers.items():
            self._alias_index[tier] = target
        return self.tiers

    @property
    def tier_overrides(self) -> dict[str, str]:
        return dict(self._tier_overrides)

    @property
    def catalog_tiers(self) -> dict[str, str]:
        """The tiers as ``catalog.json`` ships them (no overrides)."""
        return dict(self._raw.get("tiers") or {})

    # ------------------------------------------------------------ queries
    @property
    def tiers(self) -> dict[str, str]:
        return {**(self._raw.get("tiers") or {}), **self._tier_overrides}

    @property
    def providers(self) -> dict[str, dict[str, Any]]:
        return dict(self._raw.get("providers") or {})

    def resolve(self, model: str | None) -> str | None:
        """Map a tier name (``cheap``) or alias (``deepseek-flash``) to its
        canonical id. Unknown names pass through unchanged."""
        if not model:
            return model
        return self._alias_index.get(model, model)

    def get(self, model_id: str, provider: str | None = None, modality: str | None = None) -> ModelEntry | None:
        """The entry for ``model_id``. Without ``modality`` a curated or chat
        entry wins over an OpenRouter image entry of the same id (callers that
        price or route an image ask with ``modality="image"``)."""
        model_id = self.resolve(model_id) or model_id
        if provider and modality is None:
            return self._entries.get((provider, model_id))
        found = None
        for entry in self._entries.values():
            if entry.id != model_id or (provider and entry.provider != provider):
                continue
            if modality is not None and entry.modality != modality:
                continue
            if not entry.live_media:
                return entry
            found = found or entry
        return found

    def openrouter_image(self, model_id: str) -> ModelEntry | None:
        """The listed OpenRouter image model ``model_id`` (hidden or not)."""
        return self._entries.get(("openrouter", model_id, "image"))

    def openrouter_video(self, model_id: str) -> ModelEntry | None:
        """The listed OpenRouter video model ``model_id`` (unlisted ones too)."""
        return self._entries.get(("openrouter", model_id, "video"))

    # ------------------------------------------------------------ direct vs OpenRouter (1.4-M2)
    def set_direct_providers(self, settings: Any) -> set[str]:
        """Which catalog providers we call directly right now (key set and
        switched on). Called whenever settings are (re)applied."""
        direct: set[str] = set()
        for provider, pcfg in self.providers.items():
            slot = (pcfg or {}).get("settings_key") or provider
            cfg = getattr(getattr(settings, "providers", None), slot, None)
            if cfg is not None and getattr(cfg, "enabled", False) and getattr(cfg, "api_key", ""):
                direct.add(provider)
        self._direct = direct
        return set(direct)

    def hidden_as_duplicate(self, entry: ModelEntry) -> bool:
        """An OpenRouter image or video model whose vendor we already call
        directly with a working key: listing both would show the same model
        twice. The rules live in ``openrouter_images.DIRECT_VENDORS`` (images)
        and ``openrouter_videos.DIRECT_VIDEO_TWINS`` (videos: Google's
        ``gemini-omni*`` and ``veo-*`` once the Google key is set, 1.4-M5)."""
        if entry.via_openrouter_image:
            from .openrouter_images import direct_twin

            twin = direct_twin(entry.id)
            return bool(twin and twin in self._direct)
        if entry.via_openrouter_video:
            from .openrouter_videos import direct_video_twin

            twin = direct_video_twin(entry.id)
            return bool(twin and twin in self._direct)
        return False

    @staticmethod
    def direct_video_listed(entry: ModelEntry) -> bool:
        """A video id a direct vendor's live ``/models`` listing turned up
        (OpenAI ``sora-2`` / ``sora-2-pro``, Google ``veo-3.1-*-preview``):
        not listed in this version. Sora's API is closed and video goes through
        OpenRouter (or a curated direct row) only, so these would sit on the video
        list as "not wired yet" without ever being callable. The curated video rows (Gemini Omni,
        called directly since 1.4-M5) are not discovered ones and stay."""
        return entry.modality == "video" and entry.provider != "openrouter" and entry.status == "discovered"

    def provider_of(self, model_id: str) -> str | None:
        e = self.get(model_id)
        return e.provider if e else None

    def vision(self, model_id: str, provider: str) -> bool:
        """Does ``provider``'s ``model_id`` take images? A model the catalog
        does not know is treated as text-only: its images are left out rather
        than risking a request the vendor may reject or quietly ignore."""
        e = self.get(model_id, provider)
        return bool(e and e.vision)

    def tools(self, model_id: str, provider: str) -> bool:
        """Does ``provider``'s ``model_id`` take tools? Unknown → no."""
        e = self.get(model_id, provider)
        return bool(e and e.tools)

    def models(
        self,
        *,
        modality: str | None = None,
        provider: str | None = None,
        include_retired: bool = False,
        include_snapshots: bool = False,
        online_only: bool = False,
        include_duplicates: bool = False,
        include_unlisted: bool = False,
    ) -> list[ModelEntry]:
        out = []
        for entry in self._entries.values():
            if modality and entry.modality != modality:
                continue
            if entry.unlisted and not include_unlisted:
                continue
            if provider and entry.provider != provider:
                continue
            if entry.status == "retired" and not include_retired:
                continue
            if entry.snapshot and not include_snapshots:
                continue
            if online_only and entry.online is False:
                continue
            if not include_duplicates and self.hidden_as_duplicate(entry):
                continue
            if not include_unlisted and self.direct_video_listed(entry):
                continue
            out.append(entry)
        return sorted(out, key=lambda e: (e.modality, e.provider, e.id))

    def ids(self, **kw: Any) -> set[str]:
        return {e.id for e in self.models(**kw)}

    def classify(self, provider: str, model_id: str) -> str | None:
        """Modality for a discovered id via ``id_rules``; ``None`` = ignore."""
        rules = (self._raw.get("id_rules") or {}).get(provider) or []
        for rule in rules:
            if re.search(rule["match"], model_id):
                mod = rule.get("modality")
                return None if mod == "ignore" else mod
        return None

    # ------------------------------------------------------------ discovery
    def _fetcher_for(self, provider: str, cfg: Any):
        kind = (self.providers.get(provider) or {}).get("discovery")
        key = getattr(cfg, "api_key", "") or ""
        base = getattr(cfg, "base_url", None)
        if not kind or not key:
            return None
        if kind == "openai_compatible":
            default_base = {
                "openai": "https://api.openai.com/v1",
                "deepseek": "https://api.deepseek.com",
                "openrouter": "https://openrouter.ai/api/v1",
            }.get(provider, "")
            return lambda: fetch_openai_compatible(base or default_base, key)
        if kind == "anthropic":
            return lambda: fetch_anthropic(key)
        if kind == "gemini":
            return lambda: fetch_gemini(key)
        if kind == "elevenlabs":
            return lambda: fetch_elevenlabs(key)
        return None

    async def refresh(self, settings: Any, *, force: bool = False, only: Iterable[str] | None = None) -> dict[str, DiscoveryResult]:
        """Run discovery for every configured provider and merge results.

        ``settings.providers.<name>`` objects need ``api_key``/``base_url``/
        ``enabled``. Providers without a key or with discovery=None are
        skipped (their entries keep ``online=None``). ``only`` limits the run
        to those catalog providers (a key just changed in the settings).

        An offline sandbox lists nothing, whoever asks (startup, a forced
        refresh from the API or the MCP tool, a settings change): no request
        leaves, and no key is filed as working by a listing that never happened.
        """
        from ..devmode import dev_enabled, offline
        from . import health

        self.set_direct_providers(settings)
        if offline():
            if dev_enabled():
                # the development sandbox shows stand-in OpenRouter image and video
                # rosters (saved copies, read from disk: nothing is fetched)
                from .openrouter_images import sandbox_listing
                from .openrouter_videos import sandbox_listing as sandbox_videos

                async with self._lock:
                    self._merge_images(DiscoveryResult(provider=OR_IMAGES, models=sandbox_listing(), fetched_at=time.time()))
                    self._merge_videos(DiscoveryResult(provider=OR_VIDEOS, models=sandbox_videos(), fetched_at=time.time()))
            return {}
        jobs: dict[str, Any] = {}
        keys: dict[str, str] = {}
        wanted = set(only) if only is not None else None
        for provider, pcfg in self.providers.items():
            if wanted is not None and provider not in wanted:
                continue
            settings_key = (pcfg or {}).get("settings_key") or provider
            cfg = getattr(getattr(settings, "providers", None), settings_key, None)
            if cfg is None or not getattr(cfg, "enabled", False):
                continue
            fetcher = self._fetcher_for(provider, cfg)
            if fetcher:
                keys[provider] = getattr(cfg, "api_key", "") or ""
                jobs[provider] = discover(provider, fetcher, force=force, key_fp=health.fingerprint(keys[provider]))
            images = self._image_roster_fetcher(provider, cfg)
            if images:
                # 1.4-M2: the image roster is public (no key is sent), but it is only
                # listed for someone who can call it: the provider's key is set
                jobs[OR_IMAGES] = discover(OR_IMAGES, images, force=force)
            videos = self._video_roster_fetcher(provider, cfg)
            if videos:
                # 1.4-M3: likewise public, likewise only listed for someone with the key
                jobs[OR_VIDEOS] = discover(OR_VIDEOS, videos, force=force)
        if not jobs:
            return {}
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        async with self._lock:
            for provider, res in zip(jobs, results):
                if isinstance(res, Exception):
                    logger.warning("Discovery task for %s crashed: %s", provider, res)
                    continue
                self._discovery[provider] = res
                if provider == OR_IMAGES:
                    self._merge_images(res)
                elif provider == OR_VIDEOS:
                    self._merge_videos(res)
                else:
                    self._merge(provider, res)
        for provider, res in zip(jobs, results):
            if not isinstance(res, Exception) and provider in keys:
                await asyncio.to_thread(self._record_health, provider, keys[provider], res)
        return dict(self._discovery)

    def _image_roster_fetcher(self, provider: str, cfg: Any):
        """The fetcher of ``provider``'s image roster (``image_discovery`` in
        catalog.json), when its key is set."""
        kind = (self.providers.get(provider) or {}).get("image_discovery")
        if kind != "openrouter_images" or not (getattr(cfg, "api_key", "") or ""):
            return None
        from .discovery import load_cached
        from .openrouter_images import fetch_openrouter_images

        base = getattr(cfg, "base_url", None) or "https://openrouter.ai/api/v1"

        async def fetch():
            cached = load_cached(OR_IMAGES)
            previous = {m.id: m.extra for m in cached.models} if cached else None
            return await fetch_openrouter_images(base, previous=previous)

        return fetch

    def _merge_images(self, res: DiscoveryResult) -> None:
        """Lay OpenRouter's image roster over the catalog: one ``image`` row per
        listed model (``openrouter_images.entry_fields``). A failed listing with
        nothing cached changes nothing; a model that dropped off the roster
        goes offline."""
        from .openrouter_images import bare_id, direct_twin, entry_fields

        if res.error and not res.models:
            return
        listed = {dm.id for dm in res.models}
        for key, entry in self._entries.items():
            if len(key) == 3 and key[2] == "image" and entry.id not in listed:
                entry.online = False
        for dm in res.models:
            twin_provider = direct_twin(dm.id)
            twin = self._entries.get((twin_provider, bare_id(dm.id))) if twin_provider else None
            fields = entry_fields(dm, twin)
            self._entries[("openrouter", dm.id, "image")] = ModelEntry(
                id=dm.id,
                provider="openrouter",
                modality="image",
                harness=None,
                online=True,
                discovered_name=dm.display_name,
                **fields,
            )

    def _video_roster_fetcher(self, provider: str, cfg: Any):
        """The fetcher of ``provider``'s video roster (``video_discovery`` in
        catalog.json), when its key is set (the request itself sends none)."""
        kind = (self.providers.get(provider) or {}).get("video_discovery")
        if kind != "openrouter_videos" or not (getattr(cfg, "api_key", "") or ""):
            return None
        from .openrouter_videos import fetch_openrouter_videos

        base = getattr(cfg, "base_url", None) or "https://openrouter.ai/api/v1"
        return lambda: fetch_openrouter_videos(base)

    def _merge_videos(self, res: DiscoveryResult) -> None:
        """Lay OpenRouter's video roster over the catalog: one ``video`` row per
        listed model (``openrouter_videos.entry_fields``); those that are not
        generation models are kept but unlisted. A failed listing with nothing
        cached changes nothing; a model that dropped off the roster goes offline."""
        from .openrouter_images import bare_id
        from .openrouter_videos import entry_fields, vendor_of

        if res.error and not res.models:
            return
        listed = {dm.id for dm in res.models}
        for key, entry in self._entries.items():
            if len(key) == 3 and key[2] == "video" and entry.id not in listed:
                entry.online = False
        for dm in res.models:
            vendor = {"openai": "openai", "google": "google"}.get(vendor_of(dm.id))
            twin = self._entries.get((vendor, bare_id(dm.id))) if vendor else None
            fields = entry_fields(dm, twin)
            self._entries[("openrouter", dm.id, "video")] = ModelEntry(
                id=dm.id,
                provider="openrouter",
                modality="video",
                harness=None,
                online=True,
                discovered_name=dm.display_name,
                **fields,
            )

    @staticmethod
    def _record_health(provider: str, key: str, res: DiscoveryResult) -> None:
        """File what this listing said about the key (1.3-M4, ``health.py``).
        A cache hit counts only when that cache was made with this very key."""
        from . import health

        if res.error:
            info = res.error_info or {"reason": "error", "message": res.error}
            health.record(provider, key, ok=False, source="discovery", **info)
        elif not res.from_cache:
            health.record(provider, key, ok=True, source="discovery", at=res.fetched_at, listed=len(res.models))
        elif res.key_fp and res.key_fp == health.fingerprint(key):
            health.record(provider, key, ok=True, source="discovery", at=res.fetched_at, listed=len(res.models))

    def online_count(self, provider: str) -> int | None:
        """How many of ``provider``'s listed models (curated or discovered,
        snapshots and retired ones aside) the last listing saw online; ``None``
        when it has not been listed in this process."""
        if provider not in self._discovery:
            return None
        entries = self.models(provider=provider)
        if not any(e.online is not None for e in entries):
            return None
        return sum(1 for e in entries if e.online)

    def _merge(self, provider: str, res: DiscoveryResult) -> None:
        if res.error and not res.models:
            # nothing learned; leave online=None so callers do not hide models
            return
        online = res.ids
        pcfg = self.providers.get(provider) or {}
        covered = set(pcfg.get("discovery_modalities") or pcfg.get("modalities") or MODALITIES)
        seen: set[str] = set()
        for key, entry in self._entries.items():
            if entry.provider != provider or len(key) != 2:
                continue  # another provider's, or an OpenRouter image model (listed by its own roster)
            mid = entry.id
            seen.add(mid)
            if entry.modality not in covered:
                continue  # listing endpoint does not cover this modality
            entry.online = mid in online
        for dm in res.models:
            if dm.id in seen:
                e = self._entries[(provider, dm.id)]
                e.discovered_name = dm.display_name
                e.context = e.context or dm.context
                e.max_output = e.max_output or dm.max_output
                if "vision" not in e.capabilities and (v := discovered_vision(dm.extra)) is not None:
                    e.capabilities["vision"] = v  # the curated flag wins; the listing only fills a gap
                if "tools" not in e.capabilities and (t := discovered_tools(dm.extra)) is not None:
                    e.capabilities["tools"] = t
                for cap, mod in (("files", "file"), ("audio_input", "audio")):
                    if cap not in e.capabilities and (v := discovered_input(dm.extra, mod)) is not None:
                        e.capabilities[cap] = v
                continue
            modality = self.classify(provider, dm.id)
            if not modality:
                continue
            pricing = None
            pr = dm.extra.get("pricing") if isinstance(dm.extra, dict) else None
            if isinstance(pr, dict):
                # OpenRouter-style per-token strings → per-1M floats
                try:
                    pricing = {
                        "input": float(pr.get("prompt", 0)) * 1_000_000,
                        "output": float(pr.get("completion", 0)) * 1_000_000,
                        "source": "openrouter",
                    }
                except (TypeError, ValueError):
                    pricing = None
            caps: dict[str, Any] = {}
            vision = discovered_vision(dm.extra)
            if vision is not None:
                caps["vision"] = vision
            tools = discovered_tools(dm.extra)
            if tools is not None:
                caps["tools"] = tools
            for cap, mod in (("files", "file"), ("audio_input", "audio")):
                if (v := discovered_input(dm.extra, mod)) is not None:
                    caps[cap] = v
            self._entries[(provider, dm.id)] = ModelEntry(
                id=dm.id,
                provider=provider,
                modality=modality,
                name=dm.display_name or dm.id,
                status="discovered",
                pricing=pricing,
                capabilities=caps,
                context=dm.context,
                max_output=dm.max_output,
                harness=(self.providers.get(provider) or {}).get("harness"),
                online=True,
                discovered_name=dm.display_name,
                snapshot=bool((_DATED_RE if "/" in dm.id else _SNAPSHOT_RE).search(dm.id)),
            )

    def discovery_status(self) -> dict[str, dict[str, Any]]:
        out = {}
        for provider, res in self._discovery.items():
            out[provider] = {
                "models": len(res.models),
                "fetched_at": res.fetched_at,
                "from_cache": res.from_cache,
                "error": res.error,
            }
        return out

    # ------------------------------------------------------------ pricing
    def estimate_text_cost(
        self, model_id: str, usage: dict[str, Any] | None, *, at: float | None = None
    ) -> float | None:
        """USD estimate for a chat completion from token usage; ``None`` when
        the catalog has no per-token pricing for the model. ``at`` is when the
        call happened (epoch seconds, default now) — it only matters for models
        with a peak-hours surcharge."""
        entry = self.get(model_id)
        if not entry or not usage or not entry.pricing:
            return None
        p = entry.pricing
        if "input" not in p or "output" not in p:
            return None
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
        cached = usage.get("prompt_cache_hit_tokens") or usage.get("cached_tokens") or 0
        cached_rate = p.get("cached_input", p["input"])
        return (
            (prompt - cached) * p["input"] + cached * cached_rate + completion * p["output"]
        ) / 1_000_000 * peak_multiplier(p, at)

    def speech_price_per_1k_chars(self, model_id: str) -> float | None:
        """Catalog price of a flat, per-character speech model in USD per 1,000
        characters (``per_1k_chars`` or ``per_1m_chars``); ``None`` for a token-priced
        or unpriced one, where characters say nothing reliable about the bill."""
        entry = self.get(model_id, modality="speech") or self.get(model_id)
        p = entry.pricing if entry and isinstance(entry.pricing, dict) else None
        text = p.get("text") if p else None
        if not isinstance(text, (int, float)) or isinstance(text, bool):
            return None
        unit = p.get("unit")
        if unit == "per_1k_chars":
            return float(text)
        if unit == "per_1m_chars":
            return float(text) / 1000
        return None

    def estimate_speech_cost(self, model_id: str, chars: int) -> float | None:
        """USD estimate for speaking ``chars`` characters with a flat per-character
        model, ``None`` when the model is not priced that way (see above)."""
        per_1k = self.speech_price_per_1k_chars(model_id)
        return None if per_1k is None else round(per_1k * max(0, int(chars)) / 1000, 6)

    def estimate_per_minute_cost(self, model_id: str, seconds: float | None, *, modality: str) -> float | None:
        """USD estimate for ``seconds`` of audio (transcribed, or composed) with a per-minute
        model; ``None`` when the length is unknown or the model is not priced per minute."""
        entry = self.get(model_id, modality=modality) or self.get(model_id)
        p = entry.pricing if entry and isinstance(entry.pricing, dict) else None
        price = p.get("audio") if p and p.get("unit") == "per_minute" else None
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            return None
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
            return None
        return round(float(price) * float(seconds) / 60, 6)

    # ------------------------------------------------------------ export
    def snapshot(
        self, *, include_retired: bool = False, modality: str | None = None
    ) -> dict[str, Any]:
        """Serializable view for ``list_available_models`` / the GUI."""
        entries = self.models(include_retired=include_retired, modality=modality)
        by_modality: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            by_modality.setdefault(e.modality, []).append(e.to_dict())
        return {
            "updated": self._raw.get("updated"),
            "tiers": self.tiers,
            "providers": self.providers,
            "discovery": self.discovery_status(),
            "models": by_modality,
            "counts": {k: len(v) for k, v in by_modality.items()},
        }


# Process-wide singleton (tests may construct their own ModelCatalog).
catalog = ModelCatalog()
