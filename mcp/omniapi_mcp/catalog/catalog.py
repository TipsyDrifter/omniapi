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

from .discovery import (
    DiscoveryResult,
    discover,
    fetch_anthropic,
    fetch_elevenlabs,
    fetch_gemini,
    fetch_openai_compatible,
)

logger = logging.getLogger(__name__)

MODALITIES = ("text", "image", "transcription", "speech", "music")
# Fixed snapshots (gpt-5.5-2026-04-23, gpt-4-0613, …-preview-10-2025) and moving aliases
# (gemini-flash-latest): callable, but an uncurated one is not worth a row in any listing.
_SNAPSHOT_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{2}-\d{4}|\d{4}|latest)$")
# Gateways name real models that way (mistralai/mistral-large-2411): only the dated form is a snapshot there.
_DATED_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
STATUSES = ("current", "deprecated", "retired", "discovered")


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
        return d


class ModelCatalog:
    """Curated + discovered model catalog. One instance per process."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).with_name("catalog.json")
        self._raw: dict[str, Any] = {}
        self._entries: dict[tuple[str, str], ModelEntry] = {}  # (provider, id)
        self._alias_index: dict[str, str] = {}  # alias/tier -> canonical id
        self._discovery: dict[str, DiscoveryResult] = {}
        #: the owner's tier choices (settings.json / env), laid over catalog.json's tiers
        self._tier_overrides: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.loaded_at: float | None = None
        self.load()

    # ------------------------------------------------------------ loading
    def load(self) -> None:
        self._raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._entries.clear()
        self._alias_index.clear()
        for m in self._raw.get("models", []):
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

    def get(self, model_id: str, provider: str | None = None) -> ModelEntry | None:
        model_id = self.resolve(model_id) or model_id
        if provider:
            return self._entries.get((provider, model_id))
        for (prov, mid), entry in self._entries.items():
            if mid == model_id:
                return entry
        return None

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
    ) -> list[ModelEntry]:
        out = []
        for entry in self._entries.values():
            if modality and entry.modality != modality:
                continue
            if provider and entry.provider != provider:
                continue
            if entry.status == "retired" and not include_retired:
                continue
            if entry.snapshot and not include_snapshots:
                continue
            if online_only and entry.online is False:
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
        from ..devmode import offline
        from . import health

        if offline():
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
        if not jobs:
            return {}
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        async with self._lock:
            for provider, res in zip(jobs, results):
                if isinstance(res, Exception):
                    logger.warning("Discovery task for %s crashed: %s", provider, res)
                    continue
                self._discovery[provider] = res
                self._merge(provider, res)
        for provider, res in zip(jobs, results):
            if not isinstance(res, Exception):
                await asyncio.to_thread(self._record_health, provider, keys[provider], res)
        return dict(self._discovery)

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
        for (prov, mid), entry in self._entries.items():
            if prov != provider:
                continue
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
