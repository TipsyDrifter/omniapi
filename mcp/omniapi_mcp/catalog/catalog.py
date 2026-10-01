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
_SNAPSHOT_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
STATUSES = ("current", "deprecated", "retired", "discovered")


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
    snapshot: bool = False  # dated snapshot id (gpt-5.5-2026-04-23): hidden by default

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "provider": self.provider,
            "modality": self.modality,
            "name": self.name or self.discovered_name or self.id,
            "status": self.status,
            "online": self.online,
            "harness": self.harness,
            "pricing": self.pricing,
            "capabilities": self.capabilities,
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
        for tier, target in (self._raw.get("tiers") or {}).items():
            self._alias_index[tier] = target
        self.loaded_at = time.time()

    # ------------------------------------------------------------ queries
    @property
    def tiers(self) -> dict[str, str]:
        return dict(self._raw.get("tiers") or {})

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

    async def refresh(self, settings: Any, *, force: bool = False) -> dict[str, DiscoveryResult]:
        """Run discovery for every configured provider and merge results.

        ``settings.providers.<name>`` objects need ``api_key``/``base_url``/
        ``enabled``. Providers without a key or with discovery=None are
        skipped (their entries keep ``online=None``).
        """
        jobs: dict[str, Any] = {}
        for provider, pcfg in self.providers.items():
            settings_key = (pcfg or {}).get("settings_key") or provider
            cfg = getattr(getattr(settings, "providers", None), settings_key, None)
            if cfg is None or not getattr(cfg, "enabled", False):
                continue
            fetcher = self._fetcher_for(provider, cfg)
            if fetcher:
                jobs[provider] = discover(provider, fetcher, force=force)
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
        return dict(self._discovery)

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
            self._entries[(provider, dm.id)] = ModelEntry(
                id=dm.id,
                provider=provider,
                modality=modality,
                name=dm.display_name or dm.id,
                status="discovered",
                pricing=pricing,
                context=dm.context,
                max_output=dm.max_output,
                harness=(self.providers.get(provider) or {}).get("harness"),
                online=True,
                discovered_name=dm.display_name,
                snapshot=bool(_SNAPSHOT_RE.search(dm.id)),
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
        self, model_id: str, usage: dict[str, Any] | None
    ) -> float | None:
        """USD estimate for a chat completion from token usage; ``None`` when
        the catalog has no per-token pricing for the model."""
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
        ) / 1_000_000

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
